#!/usr/bin/env python3
"""Exercise Spark remote inference with synthetic observations and no robot connection.

The script intentionally never imports or constructs RobotClient: RobotClient connects the follower
in its constructor and its control loop sends every received action to the arm. This diagnostic
speaks the same gRPC protocol directly, receives action chunks, and discards them in memory.
"""

from __future__ import annotations

import json
import os
import pickle  # nosec: both endpoints are the trusted SSH-tunnelled LeRobot installation
import re
import signal
import statistics
import subprocess
import sys
import time
from pathlib import Path

import grpc
import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from lerobot.async_inference.helpers import RemotePolicyConfig, TimedObservation
from lerobot.transport import services_pb2, services_pb2_grpc
from lerobot.transport.utils import grpc_channel_options, send_bytes_in_chunks
from lerobot.utils.feature_utils import hw_to_dataset_features

from soarm_console.config import Settings
from soarm_console.spark import (
    SSH_OPTIONS,
    _queue_request,
    describe_remote_model,
    ensure_policy_side,
    policy_tunnel_command,
    stop_policy_side,
)


RUN = "soarm101_cube104_dn_strat__pi05__b67e"
STEP = "002000"
TASK = "Pick up the orange cube and place it in the yellow square area."
LOAD_LINE = re.compile(r"Time taken to put policy on cuda: ([0-9.]+) seconds")


def remote_snapshot(settings: Settings) -> str:
    target = f"{settings.spark_user}@{settings.spark_host}"
    command = (
        'printf "UTC="; date -u +%Y-%m-%dT%H:%M:%SZ; '
        'printf "GPU="; nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader; '
        'printf "QUEUE="; python3 ~/sparkq/sparkq.py ls | head -1; '
        'printf "TRAINING_PROCESSES\\n"; '
        'ps -eo pid=,stat=,args= | grep -E "[l]erobot-train|[r]sl_rl/train.py"'
    )
    result = subprocess.run(
        ["ssh", *SSH_OPTIONS, target, command], capture_output=True, text=True, timeout=30
    )
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip())
    return result.stdout.strip()


def side_log(settings: Settings, side_id: str) -> list[str]:
    response = _queue_request(settings, "GET", f"/api/queue/{side_id}/log")
    if isinstance(response, dict):
        value = response.get("log_tail") or response.get("lines") or response.get("log") or []
    else:
        value = response
    if isinstance(value, str):
        return value.splitlines()
    return [str(line) for line in value]


def synthetic_features(
    rename_map: dict[str, str], *, policy_keyed: bool
) -> tuple[dict[str, dict], dict[str, object]]:
    motors = [
        "shoulder_pan.pos",
        "shoulder_lift.pos",
        "elbow_flex.pos",
        "wrist_flex.pos",
        "wrist_roll.pos",
        "gripper.pos",
    ]
    hardware = {name: float for name in motors}
    camera_names = ["scene", "wrist"]
    if policy_keyed:
        camera_names = [
            rename_map[f"observation.images.{name}"].removeprefix("observation.images.")
            for name in camera_names
        ]
    hardware.update({name: (480, 640, 3) for name in camera_names})
    features = hw_to_dataset_features(hardware, "observation", use_video=False)
    black = np.zeros((480, 640, 3), dtype=np.uint8)
    observation: dict[str, object] = {name: 0.0 for name in motors}
    observation.update({name: black for name in camera_names})
    observation["task"] = TASK
    return features, observation


def run_grpc_probe(settings: Settings, model: dict[str, object], side_id: str) -> list[float]:
    address = f"127.0.0.1:{settings.remote_policy_port}"
    channel = grpc.insecure_channel(address, grpc_channel_options(initial_backoff="0.0333s"))
    stub = services_pb2_grpc.AsyncInferenceStub(channel)
    policy_keyed = os.environ.get("SOARM_PROBE_POLICY_KEYS") == "1"
    rename_map = dict(model["rename_map"])
    features, raw_observation = synthetic_features(rename_map, policy_keyed=policy_keyed)
    print(
        "PROBE_INPUT="
        + (
            "POLICY_KEYS_DIAGNOSTIC_ONLY (checkpoint rename bypassed)"
            if policy_keyed
            else "CONSOLE_KEYS (scene/wrist; production path)"
        )
    )
    policy = RemotePolicyConfig(
        policy_type=str(model["policy"]),
        pretrained_name_or_path=str(model["source"]),
        lerobot_features=features,
        actions_per_chunk=50,
        device="cuda",
        # LeRobot 0.6.1 otherwise replaces the checkpoint's saved map with an empty override.
        rename_map={} if policy_keyed else rename_map,
    )
    try:
        stub.Ready(services_pb2.Empty(), timeout=10)
        started = time.perf_counter()
        stub.SendPolicyInstructions(
            services_pb2.PolicySetup(data=pickle.dumps(policy)), timeout=300
        )
        setup_ms = (time.perf_counter() - started) * 1000
        logs = side_log(settings, side_id)
        load_lines = [line for line in logs if LOAD_LINE.search(line)]
        print(f"CLIENT_SETUP_WALL_MS={setup_ms:.2f}")
        print("SERVER_POLICY_LOAD=" + (load_lines[-1] if load_lines else "NOT_FOUND"))

        latencies = []
        for index in range(10):
            timed = TimedObservation(
                timestamp=time.time(),
                timestep=index * 50,
                observation=raw_observation,
                must_go=True,
            )
            started = time.perf_counter()
            iterator = send_bytes_in_chunks(
                pickle.dumps(timed), services_pb2.Observation, log_prefix="[PROBE]", silent=True
            )
            stub.SendObservations(iterator, timeout=30)
            response = stub.GetActions(services_pb2.Empty(), timeout=60)
            elapsed_ms = (time.perf_counter() - started) * 1000
            if not response.data:
                raise RuntimeError(f"Inference {index + 1} returned an empty response")
            actions = pickle.loads(response.data)  # nosec
            if not actions:
                raise RuntimeError(f"Inference {index + 1} returned no actions")
            latencies.append(elapsed_ms)
            print(f"INFERENCE_{index + 1:02d}_MS={elapsed_ms:.2f} ACTIONS={len(actions)}")
        print(f"INFERENCE_MEDIAN_MS={statistics.median(latencies):.2f}")
        return latencies
    finally:
        channel.close()


def stop_tunnel(tunnel: subprocess.Popen[str] | None) -> None:
    if tunnel is None or tunnel.poll() is not None:
        return
    os.killpg(tunnel.pid, signal.SIGTERM)
    try:
        tunnel.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(tunnel.pid, signal.SIGKILL)
        tunnel.wait(timeout=5)


def main() -> int:
    settings = Settings()
    model = describe_remote_model(settings, RUN, STEP)
    print("CHECKPOINT=" + str(model["source"]))
    print("ROBOT_CONNECTION=DISABLED (synthetic gRPC observations; returned actions discarded)")
    print("\n=== BEFORE SIDE ===")
    print(remote_snapshot(settings))

    side_started = False
    tunnel: subprocess.Popen[str] | None = None
    try:
        side = ensure_policy_side(settings)
        side_started = True
        side_id = str(side["id"])
        print("\n=== AFTER SIDE READY ===")
        print(f"SIDE_ID={side_id} STREAM_READY={side.get('stream_ready')}")
        print(remote_snapshot(settings))
        ready_lines = [
            line
            for line in side_log(settings, side_id)
            if "PolicyServer started on 127.0.0.1:8091" in line
        ]
        print("SERVER_READY=" + (ready_lines[-1] if ready_lines else "NOT_FOUND"))

        tunnel = subprocess.Popen(
            policy_tunnel_command(settings),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        time.sleep(0.5)
        if tunnel.poll() is not None:
            detail = tunnel.stderr.read().strip() if tunnel.stderr else ""
            raise RuntimeError(f"Tunnel failed: {detail or tunnel.returncode}")
        print(f"TUNNEL=OPEN 127.0.0.1:{settings.remote_policy_port}")
        run_grpc_probe(settings, model, side_id)
        return 0
    finally:
        stop_tunnel(tunnel)
        if side_started:
            stop_policy_side(settings)
        print("\n=== AFTER SIDE STOP ===")
        # Give SIGCONT and the GPU sampler a moment, then leave the actual resumed state in output.
        time.sleep(3)
        print(remote_snapshot(settings))


if __name__ == "__main__":
    raise SystemExit(main())
