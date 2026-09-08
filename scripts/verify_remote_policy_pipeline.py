#!/usr/bin/env python3
"""Exercise Spark remote inference with synthetic observations and no robot connection.

The script intentionally never imports or constructs RobotClient: RobotClient connects the follower
in its constructor and its control loop sends every received action to the arm. This diagnostic
speaks the same gRPC protocol directly, receives action chunks, and discards them in memory.
"""

from __future__ import annotations

import json
import os
import pickle  # nosec: both endpoints are the same trusted LeRobot installation on the tailnet
import re
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
from lerobot.async_inference.helpers import map_robot_keys_to_lerobot_features
from lerobot.robots import make_robot_from_config
from lerobot.transport import services_pb2, services_pb2_grpc
from lerobot.transport.utils import grpc_channel_options, send_bytes_in_chunks

from soarm_console.config import Settings
from soarm_console.policying import build_remote_client_config
from soarm_console.spark import (
    SSH_OPTIONS,
    _queue_request,
    describe_remote_model,
    ensure_policy_side,
    stop_policy_side,
)


RUN = os.environ.get("SOARM_PROBE_RUN", "soarm101_cube104_dn_strat__pi05__b67e")
STEP = os.environ.get("SOARM_PROBE_STEP", "002000")
TASK = "Pick up the orange cube and place it in the yellow square area."
LOAD_LINE = re.compile(r"Time taken to put policy on cuda: ([0-9.]+) seconds")
SERVER_TOTAL_LINE = re.compile(r"Observation \d+ \| Total time: ([0-9.]+)ms")


def remote_snapshot(settings: Settings) -> str:
    target = f"{settings.spark_user}@{settings.spark_host}"
    command = (
        'printf "UTC="; date -u +%Y-%m-%dT%H:%M:%SZ; '
        'printf "GPU="; nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader; '
        'printf "QUEUE="; python3 ~/sparkq/sparkq.py ls | head -1; '
        'printf "TRAINING_PROCESSES\\n"; '
        'ps -eo pid=,stat=,args= | grep -E "[l]erobot-train|[r]sl_rl/train.py" || true'
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


def synthetic_observation(robot_config) -> tuple[dict[str, dict], dict[str, object]]:
    # This constructs the same robot object as RobotClient but deliberately never calls connect().
    # Camera and bus constructors only describe devices; no serial/video handle is opened.
    robot = make_robot_from_config(robot_config)
    features = map_robot_keys_to_lerobot_features(robot)
    observation: dict[str, object] = {}
    for name, feature_type in robot.observation_features.items():
        observation[name] = (
            np.zeros(feature_type, dtype=np.uint8) if isinstance(feature_type, tuple) else 0.0
        )
    observation["task"] = TASK
    return features, observation


def run_grpc_probe(settings: Settings, model: dict[str, object], side_id: str) -> list[float]:
    address = f"{settings.effective_remote_policy_host}:{settings.remote_policy_port}"
    channel = grpc.insecure_channel(address, grpc_channel_options(initial_backoff="0.0333s"))
    stub = services_pb2_grpc.AsyncInferenceStub(channel)
    rename_map = dict(model["rename_map"])
    client_config = build_remote_client_config(
        settings,
        str(model["policy"]),
        str(model["source"]),
        TASK,
        30,
        rename_map,
    )
    features, raw_observation = synthetic_observation(client_config.robot)
    print("PROBE_INPUT=CONSOLE_REMOTE_ROBOT_CONFIG (synthetic images; no device connection)")
    print("ROBOT_CONFIG_CAMERA_KEYS=" + ",".join(client_config.robot.cameras))
    policy = RemotePolicyConfig(
        policy_type=client_config.policy_type,
        pretrained_name_or_path=client_config.pretrained_name_or_path,
        lerobot_features=features,
        actions_per_chunk=client_config.actions_per_chunk,
        device=client_config.policy_device,
        # Match FailSafeRobotClient: the inputs already have policy keys, so this map is a no-op.
        rename_map=dict(client_config.checkpoint_rename_map),
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
        sends: list[float] = []
        # 관측 사이의 간격(프레임). 기본 50은 앞 청크를 남김없이 소진한 상태여서, 서버가
        # 이어 붙일 꼬리가 없다 — RTC가 걸리지 않는 조건이다. 실제 클라이언트는 큐가 절반이
        # 되면 요청하므로 24프레임쯤이고, 그때 25프레임쯤이 꼬리로 남는다. 그 조건을 재려면
        # SOARM_PROBE_STRIDE=24 로 준다.
        stride = int(os.environ.get("SOARM_PROBE_STRIDE", "50"))
        for index in range(10):
            timed = TimedObservation(
                timestamp=time.time(),
                timestep=index * stride,
                observation=raw_observation,
                must_go=True,
            )
            payload = pickle.dumps(timed)
            # **보내기와 받기를 따로 잰다.** 제어 루프를 실제로 붙잡는 것은 `SendObservations`
            # 하나뿐이다 — 행동은 별도 스레드(`receive_actions`)가 받는다. 둘을 묶어서 재면
            # 틱을 막는 값이 추론 시간에 묻혀, 무엇을 줄여야 하는지 알 수 없다.
            started = time.perf_counter()
            iterator = send_bytes_in_chunks(
                payload, services_pb2.Observation, log_prefix="[PROBE]", silent=True
            )
            stub.SendObservations(iterator, timeout=30)
            send_ms = (time.perf_counter() - started) * 1000
            response = stub.GetActions(services_pb2.Empty(), timeout=60)
            elapsed_ms = (time.perf_counter() - started) * 1000
            sends.append(send_ms)
            if not response.data:
                raise RuntimeError(f"Inference {index + 1} returned an empty response")
            actions = pickle.loads(response.data)  # nosec
            if not actions:
                raise RuntimeError(f"Inference {index + 1} returned no actions")
            latencies.append(elapsed_ms)
            print(
                f"INFERENCE_{index + 1:02d}_MS={elapsed_ms:.2f} "
                f"SEND_MS={send_ms:.2f} ACTIONS={len(actions)}"
            )
        print(f"INFERENCE_MEDIAN_MS={statistics.median(latencies):.2f}")
        print(f"OBSERVATION_BYTES={len(payload)}")
        print(f"SEND_MEDIAN_MS={statistics.median(sends):.2f}")
        print(f"SEND_MAX_MS={max(sends):.2f}")
        # 제어 루프의 예산은 33.3ms다. 보내기가 그 안에 들어가지 못하면 주기가 처진다.
        print(f"SEND_BUDGET_33MS={'OK' if statistics.median(sends) < 33.3 else 'OVER'}")

        logs = side_log(settings, side_id)
        server_latencies = [
            float(match.group(1))
            for line in logs
            if (match := SERVER_TOTAL_LINE.search(line)) is not None
        ][-10:]
        if len(server_latencies) == 10:
            print(f"SERVER_INFERENCE_MEDIAN_MS={statistics.median(server_latencies):.2f}")
        else:
            print(f"SERVER_INFERENCE_SAMPLES={len(server_latencies)}")

        if os.environ.get("SOARM_PROBE_RECONNECT", "1") != "0":
            # A new RobotClient calls Ready and SendPolicyInstructions again. Repeat those two RPCs
            # on a fresh channel to determine whether the server caches the loaded checkpoint.
            channel.close()
            channel = grpc.insecure_channel(address, grpc_channel_options(initial_backoff="0.0333s"))
            stub = services_pb2_grpc.AsyncInferenceStub(channel)
            stub.Ready(services_pb2.Empty(), timeout=10)
            started = time.perf_counter()
            stub.SendPolicyInstructions(
                services_pb2.PolicySetup(data=pickle.dumps(policy)), timeout=300
            )
            reconnect_ms = (time.perf_counter() - started) * 1000
            reconnect_logs = side_log(settings, side_id)
            reconnect_loads = [line for line in reconnect_logs if LOAD_LINE.search(line)]
            print(f"RECONNECT_SETUP_WALL_MS={reconnect_ms:.2f}")
            print(
                "SERVER_RECONNECT_POLICY_LOAD="
                + (reconnect_loads[-1] if reconnect_loads else "NOT_FOUND")
            )
        return latencies
    finally:
        channel.close()




def main() -> int:
    settings = Settings()
    model = describe_remote_model(settings, RUN, STEP)
    print("CHECKPOINT=" + str(model["source"]))
    print("ROBOT_CONNECTION=DISABLED (synthetic gRPC observations; returned actions discarded)")
    print("\n=== BEFORE SIDE ===")
    print(remote_snapshot(settings))

    side_started = False
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
            if "PolicyServer started on " in line
        ]
        print("SERVER_READY=" + (ready_lines[-1] if ready_lines else "NOT_FOUND"))

        # 터널은 없다. tailnet 주소로 바로 붙는다 — 터널은 크기와 무관하게 왕복마다 ~47ms를
        # 더해 30Hz 제어를 못 맞춘다 (docs/원격_추론_끊김_진단_2026-09-08.md §5-5).
        print(f"DIRECT={settings.effective_remote_policy_host}:{settings.remote_policy_port}")
        run_grpc_probe(settings, model, side_id)
        return 0
    finally:
        if side_started:
            stop_policy_side(settings)
        print("\n=== AFTER SIDE STOP ===")
        # Give SIGCONT and the GPU sampler a moment, then leave the actual resumed state in output.
        time.sleep(3)
        print(remote_snapshot(settings))


if __name__ == "__main__":
    raise SystemExit(main())
