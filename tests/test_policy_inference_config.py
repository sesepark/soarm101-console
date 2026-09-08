"""RTC 유도 구간이 실측 지연을 덮는가, 그리고 정책 종류에 맞는 엔진을 고르는가.

이 시험이 있는 이유. 2026-09-06 13:14의 rollout에서 팔이 **0.73초 주기로 툭툭 끊겼다.**
원인은 lerobot의 기본값 두 개가 이 기계의 실측값과 어긋난 것이었다 — 큐는 새 청크의 앞
`real_delay`(실측 19프레임)개를 버리는데 유도(`execution_horizon`)는 앞 10개에만 걸려서,
실제로 실행되는 부분에는 연속성 제약이 하나도 남지 않았다. 숫자 하나가 조용히 되돌아가면
증상도 조용히 돌아오므로, 그 부등식을 시험으로 못 박는다.
"""

from __future__ import annotations

import pytest

from soarm_console import policying


#: 이 기계에서 실측한 추론 지연(프레임). 2026-09-06 13:14 rollout, 병합 52회의 중앙값.
MEASURED_REAL_DELAY = 19


def test_the_guidance_window_covers_the_measured_inference_delay():
    # 유도가 버려지는 구간보다 짧으면 실행되는 부분에 제약이 남지 않는다.
    assert policying.RTC_EXECUTION_HORIZON > MEASURED_REAL_DELAY


def test_the_guidance_window_fits_inside_the_queue_threshold():
    # 추론은 큐가 threshold 밑으로 내려갈 때 시작하고 그때 남은 것을 앞 계획으로 넘긴다.
    # 그것이 유도 구간보다 짧으면 0으로 채워지고, 정규화 공간의 0은 평균 자세다.
    from lerobot.rollout.inference import RTCInferenceConfig

    assert policying.RTC_EXECUTION_HORIZON <= RTCInferenceConfig().queue_threshold


def test_smolvla_gets_rtc_with_our_horizon():
    from lerobot.rollout.inference import RTCInferenceConfig

    config = policying._inference_config("smolvla")
    assert isinstance(config, RTCInferenceConfig)
    assert config.rtc.enabled
    assert config.rtc.execution_horizon == policying.RTC_EXECUTION_HORIZON


def test_act_falls_back_to_sync_instead_of_dying():
    # ACT의 predict_action_chunk는 RTC 인자를 받지 않는다. 그대로 RTC를 걸면 lerobot이
    # rollout을 시작하기도 전에 ValueError를 낸다 — 팔에 ACT를 올리는 길이 막혀 있었다.
    from lerobot.rollout.inference import SyncInferenceConfig

    assert isinstance(policying._inference_config("act"), SyncInferenceConfig)
    assert policying.inference_kind("act") == "sync"


def test_an_unknown_policy_type_falls_back_to_sync():
    from lerobot.rollout.inference import SyncInferenceConfig

    assert isinstance(policying._inference_config("no_such_policy"), SyncInferenceConfig)


def test_smolvla_reports_the_rtc_engine_the_rollout_uses():
    assert policying.inference_kind("smolvla") == "rtc"


def test_the_horizon_fits_in_the_chunk_the_policy_actually_emits():
    # 유도 구간이 청크보다 길 수는 없다. 배포된 SmolVLA의 chunk_size는 50이다.
    assert policying.RTC_EXECUTION_HORIZON < 50


@pytest.mark.parametrize("policy_type", ["smolvla", "act"])
def test_choosing_an_engine_never_touches_the_arm(policy_type):
    # 이 함수는 설정만 만든다. 하드웨어가 없는 이 시험이 도는 것 자체가 그 증거다.
    assert policying._inference_config(policy_type) is not None
