#!/usr/bin/env python3
"""정책이 큐브의 자리를 보고 계획을 바꾸는가, 아니면 외운 동작을 되풀이하는가.

**팔은 한 마디도 움직이지 않는다.** RobotClient를 만들지 않고 gRPC로 관측만 보내며,
돌아온 액션 청크는 메모리에서 버린다.

무엇을 재는가: 회차마다 **첫 프레임**을 보낸다. 그 순간 팔은 어느 회차에서나 같은 안전
자세에 있고 다른 것은 큐브가 놓인 자리뿐이므로, 계획이 달라진다면 그 차이는 카메라에서
온 것이다. 예측한 청크의 마지막 `shoulder_pan`(1.67초 뒤 팔이 향할 곳)이 그 회차에서
사람이 실제로 잡은 자리와 함께 움직이면 정책은 큐브를 보고 있는 것이고, 큐브가 어디 있든
같은 값이 나오면 외운 것이다.
"""

from __future__ import annotations

import os
import pickle  # nosec: 양쪽 다 SSH 터널 안의 신뢰된 LeRobot 설치다
import sys
import time
from pathlib import Path

import grpc
import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from lerobot.async_inference.helpers import RemotePolicyConfig, TimedObservation
from lerobot.transport import services_pb2, services_pb2_grpc
from lerobot.transport.utils import grpc_channel_options

from soarm_console.config import Settings
from soarm_console.policying import build_remote_client_config
from soarm_console.spark import (
    describe_remote_model,
    ensure_policy_side,
    stop_policy_side,
)

from verify_remote_policy_pipeline import (  # noqa: E402 — 위의 경로 조정 뒤에 와야 한다
    RUN,
    STEP,
    TASK,
    synthetic_observation,
)

SAMPLES = Path(os.environ.get("SOARM_VISION_PROBE", "/tmp/vision_probe.npz"))


def probe(settings: Settings, model: dict[str, object]) -> None:
    data = np.load(SAMPLES)
    scene, wrist = data["scene"], data["wrist"]
    states, grasp_pan, episodes = data["state"], data["grasp_pan"], data["episode"]
    # 같은 시점에 시연자 자신은 어디에 있었나. 이것이 기준선이다.
    human_ahead = data["ahead_pan"] if "ahead_pan" in data else states[:, 0]

    address = f"{settings.effective_remote_policy_host}:{settings.remote_policy_port}"
    channel = grpc.insecure_channel(address, grpc_channel_options(initial_backoff="0.0333s"))
    stub = services_pb2_grpc.AsyncInferenceStub(channel)
    rename_map = dict(model["rename_map"])
    client_config = build_remote_client_config(
        settings, str(model["policy"]), str(model["source"]), TASK, 30, rename_map
    )
    features, raw_observation = synthetic_observation(client_config.robot)
    cameras = [key for key in raw_observation if key.endswith("_rgb")]
    print("CAMERA_KEYS=" + ",".join(cameras))

    policy = RemotePolicyConfig(
        policy_type=client_config.policy_type,
        pretrained_name_or_path=client_config.pretrained_name_or_path,
        lerobot_features=features,
        actions_per_chunk=client_config.actions_per_chunk,
        device=client_config.policy_device,
        rename_map=dict(client_config.checkpoint_rename_map),
    )
    stub.Ready(services_pb2.Empty(), timeout=10)
    stub.SendPolicyInstructions(services_pb2.PolicySetup(data=pickle.dumps(policy)), timeout=600)

    joints = [key for key in raw_observation if key.endswith(".pos")]
    predicted = []
    for index in range(len(episodes)):
        observation = dict(raw_observation)
        # 카메라는 이름 순서가 곧 역할이다. 첫째가 작업공간(scene), 둘째가 측면(wrist).
        for position, key in enumerate(sorted(cameras)):
            frame = scene[index] if "base" in key else wrist[index]
            observation[key] = np.ascontiguousarray(frame.transpose(1, 2, 0))
        for position, key in enumerate(joints):
            observation[key] = float(states[index][position])
        observation["task"] = TASK

        # 회차 사이에 timestep을 멀리 띄운다. 붙여 두면 서버가 앞 계획에 이어 붙여서
        # (RTC) 이 시험이 재려는 "이 그림만 보고 무엇을 계획하는가"가 흐려진다.
        timed = TimedObservation(
            timestamp=time.time(), timestep=index * 10_000, observation=observation, must_go=True
        )
        from lerobot.transport.utils import send_bytes_in_chunks

        stub.SendObservations(
            send_bytes_in_chunks(
                pickle.dumps(timed), services_pb2.Observation, log_prefix="[PROBE]", silent=True
            ),
            timeout=60,
        )
        response = stub.GetActions(services_pb2.Empty(), timeout=120)
        actions = pickle.loads(response.data) if response.data else []  # nosec
        if not actions:
            print(f"  회차 {episodes[index]}: 액션을 받지 못했습니다")
            continue
        last = np.asarray(actions[-1].get_action(), dtype=np.float32)
        predicted.append(float(last[0]))
        print(
            f"  회차 {int(episodes[index]):3d} | 사람이 잡은 자리 {grasp_pan[index]:7.1f}° "
            f"| 정책이 향한 곳 {last[0]:7.1f}°"
        )

    channel.close()
    if len(predicted) < 3:
        print("표본이 모자라 판단하지 않습니다")
        return
    truth = np.asarray(grasp_pan[: len(predicted)], dtype=np.float64)
    guess = np.asarray(predicted, dtype=np.float64)
    baseline = np.asarray(human_ahead[: len(predicted)], dtype=np.float64)

    def fit(values):
        """큐브 자리에 대한 상관과 기울기. 기울기가 곧 "얼마나 따라가는가"다."""
        if values.std() < 1e-6 or truth.std() < 1e-6:
            return 0.0, 0.0
        centred = truth - truth.mean()
        slope = float((centred * (values - values.mean())).sum() / (centred**2).sum())
        return float(np.corrcoef(truth, values)[0, 1]), slope

    # **시연자 자신을 기준선으로 둔다.** 청크 하나는 1.67초뿐이고, 그 시점에는 사람도 아직
    # 큐브 쪽으로 방향을 정하지 않았다. 정책의 계획을 최종 잡는 자리와 곧바로 견주면 사람도
    # 통과하지 못하는 시험이 되고, 멀쩡한 정책이 "외웠다"로 읽힌다 — 2026-09-07에 실제로
    # 그렇게 잘못 읽었다. 같은 시점의 사람과 견주는 것이 옳은 잣대다.
    human_r, human_slope = fit(baseline)
    policy_r, policy_slope = fit(guess)
    print(f"\n큐브가 놓인 폭: {truth.max() - truth.min():.1f}°")
    print(f"  같은 시점의 사람  상관 {human_r:+.2f} | 기울기 {human_slope:+.3f}")
    print(f"  정책의 계획       상관 {policy_r:+.2f} | 기울기 {policy_slope:+.3f}")
    if policy_slope <= max(0.0, human_slope) + 0.02:
        print("판정: 같은 시점의 시연자보다 큐브를 더 읽지 못합니다 — 카메라를 쓴다고 볼 수 없습니다")
    elif policy_slope < 0.5:
        print(
            "판정: 큐브 쪽을 맞게 가리키지만 폭이 모자랍니다. 큐브가 10도 옆으로 가면 계획은 "
            f"{policy_slope * 10:.1f}도만 따라갑니다. 카메라는 보고 있고, 그 신호를 동작의 "
            "크기로 옮기는 것이 덜 배워진 상태입니다 — 더 학습하면 커지는 쪽입니다."
        )
    else:
        print("판정: 큐브 자리를 제대로 따라갑니다")


def main() -> int:
    settings = Settings()
    model = describe_remote_model(settings, RUN, STEP)
    print("CHECKPOINT=" + str(model["source"]))
    print("ROBOT_CONNECTION=DISABLED (합성 gRPC 관측; 돌려받은 액션은 버린다)")
    side_started = False
    try:
        side = ensure_policy_side(settings)
        side_started = True
        print(f"SIDE_ID={side['id']}")
        # 터널은 없다. tailnet 주소로 바로 붙는다 (docs/원격_추론_끊김_진단_2026-09-08.md §5-5).
        print(f"DIRECT={settings.effective_remote_policy_host}:{settings.remote_policy_port}")
        probe(settings, model)
        return 0
    finally:
        if side_started:
            stop_policy_side(settings)


if __name__ == "__main__":
    raise SystemExit(main())
