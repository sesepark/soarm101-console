"""새 엔드포인트의 **거절 경로**. 아무것도 시작되지 않는지가 요점이다.

팔이 혼자 움직이는 경로라 거절이 정확해야 한다. 틀린 문구로 눌렀는데 이미 팔이 한 걸음
움직인 뒤라면 게이트는 아무것도 지키지 못한 것이다.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from soarm_console.app import (
    CalibrationRequest,
    IntrinsicsRequest,
    calibrator,
    perception,
    perception_status,
    start_extrinsics,
    start_intrinsics,
    status,
)
from soarm_console.perception_manager import CONFIRMATION


class _Request:
    """모션 토큰을 실은 가짜 요청. `_token_from`은 헤더와 쿼리를 둘 다 본다."""

    def __init__(self, token: str = "") -> None:
        self.headers = {"x-soarm-motion-token": token} if token else {}
        self.query_params: dict[str, str] = {}


def test_perception_answers_even_before_anything_is_calibrated():
    payload = perception_status()
    # 카메라가 꺼져 있어도 `rig`는 읽을 수 있어야 한다. 사람이 이 화면에서 가장 먼저
    # 묻는 것이 "무엇이 되어 있고 무엇이 안 되어 있나"이기 때문이다.
    assert payload["rig"]["calibrated"] is False
    assert payload["rig"]["problems"]
    assert payload["object"]["position"] is None
    assert payload["object"]["reason"]


def test_status_carries_both_calibration_stages():
    calibration = status()["calibration"]
    # 두 단계는 성질이 다르다. 하나는 팔이 가만히 있고 하나는 혼자 움직인다. 화면이 그
    # 둘을 따로 그릴 수 있도록 상태도 따로 싣는다.
    assert calibration["intrinsics"]["running"] is False
    assert calibration["extrinsics"]["running"] is False
    assert calibration["intrinsics"]["coverage"] == [[0, 0, 0], [0, 0, 0], [0, 0, 0]]


def test_unknown_camera_is_a_404_not_a_silent_no_op():
    with pytest.raises(HTTPException) as excinfo:
        start_intrinsics(IntrinsicsRequest(camera="overhead"))
    assert excinfo.value.status_code == 404
    assert perception.calibration_status()["running"] is False


def test_extrinsics_without_a_motion_token_never_starts(monkeypatch):
    monkeypatch.setattr("soarm_console.vleader.api.motion_token", lambda: "token")
    with pytest.raises(HTTPException) as excinfo:
        start_extrinsics(_Request(), CalibrationRequest(confirmation=CONFIRMATION))
    assert excinfo.value.status_code == 401
    assert calibrator.running is False


def test_extrinsics_with_a_wrong_phrase_never_starts(monkeypatch):
    monkeypatch.setattr("soarm_console.vleader.api.motion_token", lambda: "token")
    with pytest.raises(HTTPException) as excinfo:
        start_extrinsics(_Request("token"), CalibrationRequest(confirmation="CALIBRATE"))
    # 401(토큰)이든 400(문구)이든 아무것도 시작되지 않는 것이 이 시험이 지키는 것이다.
    assert excinfo.value.status_code == 400
    assert calibrator.running is False


def test_the_motion_gate_comes_before_everything_else(monkeypatch):
    monkeypatch.setattr("soarm_console.vleader.api.motion_token", lambda: "token")
    with pytest.raises(HTTPException) as excinfo:
        start_extrinsics(_Request("token"), CalibrationRequest(confirmation=CONFIRMATION))
    # 서버가 팔의 움직임을 아예 꺼 두었으면 그것이 가장 먼저다. 뒤의 검사가 무엇을
    # 말하든 팔은 움직이지 않아야 한다.
    assert excinfo.value.status_code == 400
    assert "SOARM_ENABLE_MOTION" in str(excinfo.value.detail)
    assert calibrator.running is False


def test_extrinsics_refuses_before_the_lens_is_known(monkeypatch):
    import dataclasses

    import soarm_console.app as app_module

    monkeypatch.setattr("soarm_console.vleader.api.motion_token", lambda: "token")
    monkeypatch.setattr(
        app_module, "settings", dataclasses.replace(app_module.settings, motion_enabled=True)
    )
    with pytest.raises(HTTPException) as excinfo:
        start_extrinsics(_Request("token"), CalibrationRequest(confirmation=CONFIRMATION))
    # 내부 파라미터 없이 자리를 구하는 것은 성립하지 않는다. 픽셀에서 광선을 못 만드니까.
    assert excinfo.value.status_code == 400
    assert "lens" in str(excinfo.value.detail)
    assert calibrator.running is False


def test_the_pose_count_is_bounded():
    # 10 아래는 회전이 축퇴하고, 40 위는 사람이 지켜보기에 너무 길다.
    with pytest.raises(Exception):
        calibrator.start(4)
    with pytest.raises(Exception):
        calibrator.start(80)
    assert calibrator.running is False
