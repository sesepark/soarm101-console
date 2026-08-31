from __future__ import annotations

import http.client
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

from lerobot.teleoperators.config import TeleoperatorConfig
from lerobot.teleoperators.teleoperator import Teleoperator

logger = logging.getLogger(__name__)

#: `lerobot-record`의 이벤트 표. `recording.py`가 수집을 시작할 때 채운다. LeRobot은
#: teleoperator에게 이 표를 넘겨주지 않지만, 조작이 끊겼을 때 에피소드를 끝내려면
#: 여기에 닿아야 한다.
RECORDING_EVENTS: dict[str, bool] | None = None


@TeleoperatorConfig.register_subclass("soarm_virtual_leader")
@dataclass
class SOArmVirtualLeaderConfig(TeleoperatorConfig):
    """가상 리더를 LeRobot의 teleoperator로 쓰기 위한 설정.

    포트도 calibration도 없다. 이 teleoperator에는 만질 하드웨어가 없기 때문이다 —
    관절 목표는 콘솔이 이미 검증해서 들고 있는 것을 가져온다.
    """

    host: str = field(default_factory=lambda: os.getenv("SOARM_WEB_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(os.getenv("SOARM_WEB_PORT", "8088")))
    #: 목표가 이만큼 갱신되지 않으면 마지막 목표를 그대로 유지한다(=팔은 선다).
    stale_ms: int = 400
    #: 이만큼 지나면 에피소드를 끝낸다. 조작하는 사람이 사라진 채로 계속 찍지 않는다.
    abort_ms: int = 5000


class SOArmVirtualLeader(Teleoperator):
    """물리 리더 팔이 없어도 `lerobot-record`가 그대로 도는 teleoperator.

    `SO101Leader`는 리더 팔의 serial을 열고 관절값을 읽어 돌려준다. 여기서는 그 자리에
    콘솔의 중계 목표가 들어간다 — 3D 뷰어를 만진 손가락이 만든 값이고, 콘솔이 절대 한계와
    틱당 변화량, 리스, 워치독을 이미 통과시킨 값이다.

    이 프로세스가 팔로워 serial의 소유자다. 그래서 콘솔의 가상 리더 루프는 이 사이에
    중계 모드로 내려가 있다 — 소유자는 한 시점에 하나(ADR 0001).
    """

    config_class = SOArmVirtualLeaderConfig
    name = "soarm_virtual_leader"

    def __init__(self, config: SOArmVirtualLeaderConfig):
        super().__init__(config)
        self.config = config
        self._connection: http.client.HTTPConnection | None = None
        self._joints: list[str] = []
        self._last_goal: dict[str, float] = {}
        self._last_fresh = 0.0
        self._aborted = False


    # MARK: LeRobot 계약

    @property
    def action_features(self) -> dict[str, type]:
        return {f"{name}.pos": float for name in self._joints}

    @property
    def feedback_features(self) -> dict[str, type]:
        return {}

    @property
    def is_connected(self) -> bool:
        return self._connection is not None

    @property
    def is_calibrated(self) -> bool:
        # calibration은 팔로워 쪽에 있고, 목표는 이미 그 단위로 온다.
        return True

    def calibrate(self) -> None:
        return None

    def configure(self) -> None:
        return None

    def connect(self, calibrate: bool = True) -> None:
        self._connection = http.client.HTTPConnection(
            self.config.host, self.config.port, timeout=1.0
        )
        payload = self._get("/api/vleader/goal")
        joints = payload.get("joints") or {}
        if not joints:
            raise RuntimeError(
                "The console is not relaying a virtual-leader goal. Start the virtual leader "
                "and take a motion lease before recording."
            )
        self._joints = sorted(joints)
        self._last_goal = {name: float(value) for name, value in joints.items()}
        self._last_fresh = time.monotonic()
        logger.info("Virtual leader teleoperator connected: %s", ", ".join(self._joints))

    def disconnect(self) -> None:
        connection, self._connection = self._connection, None
        if connection is not None:
            connection.close()

    def get_action(self) -> dict[str, float]:
        """이번 틱에 팔로워로 보낼 목표.

        콘솔에 닿지 못하거나 목표가 오래되면 **마지막 목표를 그대로** 돌려준다. 목표는
        절대 위치이므로 같은 값을 다시 쓰는 것은 그 자리에 서 있으라는 뜻이고, 그것이
        이 하드웨어에서 HOLD의 물리 동작이다. 움직이라는 명령을 반복하는 것과 다르다.

        그 상태가 `abort_ms`를 넘기면 에피소드를 끝낸다. 조종하는 사람이 사라진 채로
        계속 기록되는 데이터는 쓸모도 없고, 그동안 팔은 아무도 보지 않는 채로 서 있다.
        """
        now = time.monotonic()
        try:
            payload = self._get("/api/vleader/goal")
            joints = payload.get("joints") or {}
            if joints and not payload.get("stale", False):
                self._last_goal = {name: float(value) for name, value in joints.items()}
                self._last_fresh = now
        except Exception as exc:  # noqa: BLE001 - 콘솔이 죽어도 수집 루프는 서지 않는다
            logger.debug("virtual leader goal unavailable: %s", exc)

        silent_ms = (now - self._last_fresh) * 1000.0
        if silent_ms > self.config.abort_ms and not self._aborted:
            self._aborted = True
            logger.warning(
                "No virtual-leader goal for %.0fms; ending the episode and holding position",
                silent_ms,
            )
            if RECORDING_EVENTS is not None:
                RECORDING_EVENTS["stop_recording"] = True
                RECORDING_EVENTS["exit_early"] = True
        return {f"{name}.pos": value for name, value in self._last_goal.items()}

    def send_feedback(self, feedback: dict[str, Any]) -> None:
        return None

    # MARK: 전송

    def _get(self, path: str) -> dict[str, Any]:
        if self._connection is None:
            raise RuntimeError("Not connected")
        try:
            self._connection.request("GET", path)
            response = self._connection.getresponse()
            body = response.read()
        except Exception:
            # keep-alive 연결이 끊기면 다음 틱에 새로 연다. 한 번의 실패로 수집이
            # 끝나지 않아야 한다.
            self._connection.close()
            self._connection = http.client.HTTPConnection(
                self.config.host, self.config.port, timeout=1.0
            )
            raise
        if response.status != 200:
            raise RuntimeError(f"HTTP {response.status}")
        return json.loads(body)
