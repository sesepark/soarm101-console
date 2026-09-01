from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass

from ..owner_lock import DeviceLockError, DeviceLockSet
from .authority import AuthorityManager, Lease
from .backend import FollowerBackend, HardwareError, make_backend
from .safety import (
    CommandValidator,
    Reject,
    RejectError,
    Trip,
    TRIP_KOREAN,
    TripDetector,
    VLeaderSettings,
)
from .spec import JointSpec

logger = logging.getLogger(__name__)


class State:
    """SAFETY.md의 상태 모델을 이 구현이 쓰는 이름으로.

    문서의 `SAFE -> READY -> ACTIVE`와 `HOLD`/`FAULT`를 그대로 따르고, 물러나는 동안을
    `RETREATING`으로 하나 더 두었다. 상태를 늘리는 것 자체가 안전은 아니지만, 물러나는
    중과 다 물러나고 선 상태는 화면이 다른 말을 해야 하는 서로 다른 상황이다.
    """

    STOPPED = "STOPPED"
    SAFE = "SAFE"
    READY = "READY"
    ACTIVE = "ACTIVE"
    RETREATING = "RETREATING"
    HOLD = "HOLD"
    FAULT = "FAULT"


STATE_KOREAN = {
    State.STOPPED: "꺼짐",
    State.SAFE: "관찰 전용",
    State.READY: "대기",
    State.ACTIVE: "조작 중",
    State.RETREATING: "물러나는 중",
    State.HOLD: "자세 유지",
    State.FAULT: "고장",
}

#: 관측 하나가 명령의 근거로 쓰일 수 있는 나이. 30Hz에서 0.5초다.
STALE_OBSERVATION_FRAMES = 15


@dataclass
class Fault:
    code: str
    joint: str | None
    message: str
    at: float

    def as_dict(self) -> dict[str, object]:
        return {"code": self.code, "joint": self.joint, "message": self.message, "at": self.at}


class VirtualLeaderOwner:
    """팔로워 serial을 in-process로 쥐고 도는 제어 루프.

    이름이 "가상 리더"인 이유는 이것이 물리 리더 팔이 있던 자리를 대신하기 때문이다.
    물리 리더는 사람이 손으로 움직이는 관절값을 내놓았고, 여기서는 3D 뷰어를 만지는
    손가락이 그 값을 내놓는다. 팔로워 쪽에서 보면 달라진 것이 없어야 한다.

    루프는 스레드 하나다. serial 버스는 한 번에 하나만 말할 수 있으므로 그편이 맞고,
    WebSocket 쪽(비동기)에서 들어온 명령은 검증만 그 자리에서 하고 목표 값만 넘긴다 —
    검증에 필요한 것은 캐시된 현재 위치뿐이라 버스를 만지지 않는다.
    """

    def __init__(
        self,
        *,
        specs: list[JointSpec],
        settings: VLeaderSettings,
        port: str,
        robot_id: str,
        authority: AuthorityManager,
    ):
        self.specs = specs
        self.settings = settings
        self.port = port
        self.robot_id = robot_id
        self.authority = authority
        self.validator = CommandValidator(specs, settings)
        self.detector = TripDetector(specs, settings)

        self._lock = threading.RLock()
        self._backend: FollowerBackend | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._state = State.STOPPED
        self._fault: Fault | None = None
        self._error: str | None = None

        self._present: dict[str, float] = {}
        self._load: dict[str, float] = {}
        self._current: dict[str, float] = {}
        self._temperature: dict[str, float] = {}
        self._goal: dict[str, float] = {}
        #: 사람이 **요청한** 값. 틱당 상한으로 자르기 전의 것이다.
        #:
        #: 자른 목표는 매 틱 실제 위치에 다시 붙으므로 절대 벌어지지 않는다. 무언가에
        #: 닿았다는 것을 알아보려면 "계속 요청하는데 팔이 안 움직인다"를 봐야 하고,
        #: 그 "요청"이 이 값이다.
        self._requested: dict[str, float] = {}
        self._hold_goal: dict[str, float] = {}
        self._limited: list[str] = []
        self._observation = 0
        self._last_command_at = 0.0
        self._goal_expires_at = 0.0
        self._loop_ms = 0.0
        #: 최근 경로. 접촉으로 걸렸을 때 어디에서 왔는지 알아야 물러날 방향이 정해진다.
        self._trail: deque[dict[str, float]] = deque(maxlen=30)
        self._listeners: list = []
        #: 중계 모드인가. 장치를 열지 않고 목표만 들고 있는 상태.
        self._relay = False
        #: 제어 루프에게 시킬 토크 조작. `True`면 걸고 `False`면 푼다.
        #:
        #: 여기 두는 이유는 **serial bus를 만지는 스레드가 하나여야 하기 때문**이다.
        #: 토크를 HTTP 스레드에서 직접 걸었더니, 30Hz로 읽고 있는 제어 루프와 같은 포트를
        #: 동시에 쓰게 되어 `[TxRxResult] Port is in use!`로 루프가 통째로 FAULT에 빠졌다.
        #: 버스에 말을 거는 일은 전부 루프의 몫이고, 바깥에서는 부탁만 남긴다.
        self._pending_torque: bool | None = None
        #: 마지막으로 거절한 명령. 워치독이 걸렸을 때 "명령이 오지 않았다"와 "명령이
        #: 오긴 했는데 전부 거절당했다"를 구별해 주기 위한 것이다. 화면에 앞의 말만 뜨면
        #: 조작하던 사람은 자기 쪽 연결을 의심하게 되는데, 사실은 서버가 이유를 갖고
        #: 되돌려보내고 있었다.
        self._last_reject: tuple[str, str, float] | None = None
        #: 명령이 잠깐 끊겨 목표를 지금 자리에 붙여 둔 상태인가. HOLD가 아니라 잠깐
        #: 선 것이고, 화면은 이 둘을 다르게 말해야 한다.
        self._stalled_by_silence = False
        #: 서보에 속도 상한을 다시 써 넣어야 하는가.
        self._pending_speed = False
        #: 그 쓰기가 실패했다면 그 이유. 읽기가 성공했다고 지워지지 않는다.
        self._speed_error: str | None = None
        #: 자기 끝에 닿아 선 관절과, 그때 서 있던 자리.
        #:
        #: 자리를 함께 들고 있는 이유는 떨림 때문이다. 미는 것을 그만두면 위치 오차가
        #: 0이 되어 부하가 빠지고, 그러면 관절이 조금 되돌아온다. 그 순간 "움직였다"가
        #: 되어 다시 밀고, 다시 서고, 다시 밀게 된다. 한 번 끝이라고 본 자리를 붙잡고
        #: 있으면 그 왕복이 생기지 않는다.
        self._end_stop: dict[str, float] = {}
        self._owner_locks: DeviceLockSet | None = None

    # MARK: 수명

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def state(self) -> str:
        return self._state

    def start(
        self,
        *,
        relay_from: dict[str, float] | None = None,
        owner_locks: DeviceLockSet | None = None,
    ) -> None:
        """루프를 띄운다.

        `relay_from`이 주어지면 **중계 모드**다. 팔로워 serial을 열지 않고, 검증을 통과한
        목표만 들고 있다가 `lerobot-record`의 가상 리더 teleoperator가 가져가게 한다.
        데이터 수집 중에는 장치 소유자가 record 프로세스이기 때문이다 — 소유자는 한 번에
        하나이고(ADR 0001), 그 규칙을 지키면서 같은 조작면을 쓰는 방법이 이것이다.

        중계 모드에서는 부하·전류·온도·추종오차 사다리가 없다. 그 값들은 버스를 쥔 쪽만
        읽을 수 있다. 남는 것은 절대 한계, 틱당 변화량, 리스, 워치독, 그리고 record
        프로세스 안에서 도는 LeRobot의 `max_relative_target`이다. 이 차이는 문서에 적어 둔다.
        """
        with self._lock:
            if self.running:
                raise HardwareError("Virtual leader is already running")
            if relay_from is None:
                backend = make_backend(
                    port=self.port,
                    robot_id=self.robot_id,
                    max_relative_target=self.settings.lead_deg,
                    specs=self.specs,
                )
                # 흉내 백엔드는 실제 장치를 예약하지 않는다. 실물 백엔드는 connect보다
                # 먼저 lock을 잡아, 다른 정상 진입점과 serial open이 경합할 틈을 없앤다.
                if os.getenv("SOARM_VL_BACKEND", "real").strip().lower() != "simulated":
                    try:
                        owner_locks = owner_locks or DeviceLockSet.acquire(
                            [self.port], "virtual-leader"
                        )
                    except DeviceLockError as exc:
                        raise HardwareError(str(exc)) from exc
                try:
                    backend.connect()
                except BaseException:
                    if owner_locks is not None:
                        owner_locks.release()
                    raise
                self._owner_locks = owner_locks
                self._backend = backend
            else:
                self._backend = None
                self._owner_locks = None
                self._present = dict(relay_from)
                self._goal = dict(relay_from)
                self._hold_goal = dict(relay_from)
            self._stop.clear()
            self._fault = None
            self._error = None
            self._goal = {}
            self._hold_goal = {}
            self._trail.clear()
            self.detector.reset()
            self._relay = relay_from is not None
            # 중계 모드에는 토크 게이트가 없다. 토크를 쥔 쪽은 record 프로세스다.
            #
            # 실물에서는 붙었을 때 이미 토크가 걸려 있을 수 있다 — 앞서 누가 걸어 두고
            # 루프만 내린 경우다. 그때 `SAFE`라고 말하면 화면이 "관찰 전용"이라고 하는데
            # 팔은 뻣뻣하게 힘을 주고 있다. 있는 그대로 `READY`로 이어받는다.
            armed = self._backend is not None and self._backend.torque_enabled
            self._state = State.READY if (self._relay or armed) else State.SAFE
            thread = threading.Thread(target=self._run, name="soarm-vleader", daemon=True)
            self._thread = thread
            thread.start()

    def stop(self, *, force: bool = False) -> None:
        """루프를 내린다.

        토크가 걸려 있으면 기본적으로 거절한다. 여기서 조용히 내려가면 팔은 토크가 걸린
        채 아무도 보지 않는 상태로 남고, 중력을 버티느라 계속 뜨거워진다. 사람이 팔을
        받치고 토크를 푸는 것이 먼저다 — 그래서 `force`는 명시적인 확인이 있을 때만 온다.
        """
        with self._lock:
            backend = self._backend
            if backend is not None and backend.torque_enabled and not force:
                raise HardwareError(
                    "Torque is still enabled. Release it explicitly (the arm will drop) or hold the arm first."
                )
            self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5)
        with self._lock:
            self.authority.release_all("owner_stopped")
            if self._backend is not None:
                self._backend.disconnect()
            self._backend = None
            if self._owner_locks is not None:
                self._owner_locks.release()
            self._owner_locks = None
            self._thread = None
            self._state = State.STOPPED

    # MARK: 토크

    def arm(self) -> None:
        """토크를 건다. 여기서부터 팔은 자기 자세를 스스로 버틴다.

        실제로 거는 것은 제어 루프다. 여기서는 부탁만 남기고 결과를 기다린다 — 버스에
        말을 거는 스레드는 하나여야 한다.
        """
        with self._lock:
            if self._relay:
                raise HardwareError("Torque belongs to the recording process while relaying")
            self._require_backend()
            if self._state in (State.FAULT,):
                raise HardwareError("Clear the fault before arming")
            self._pending_torque = True
        self._await_torque(True)

    def release_torque(self) -> None:
        """토크를 푼다. **팔이 떨어질 수 있다.**

        어떤 고장에서도 자동으로 불리지 않는다. 사람이 팔을 받치고 명시적으로 부를 때만
        온다 — SAFETY.md가 torque-off를 모든 fault의 기본값으로 삼지 말라고 적어 둔 그대로다.
        """
        with self._lock:
            if self._relay:
                raise HardwareError("Torque belongs to the recording process while relaying")
            self._require_backend()
            self.authority.release_all("torque_released")
            self._pending_torque = False
        self._await_torque(False)

    # MARK: 명령

    def submit(
        self,
        *,
        payload: object,
        lease_id: object,
        sequence: object,
        valid_for_ms: object,
        observation: object,
    ) -> dict[str, object]:
        """WebSocket에서 들어온 목표 하나. 통과하면 다음 틱에 실린다.

        검사 순서가 곧 사다리다. 권한 → 유효기간 → 상태 → 형식·유한성·절대 한계·자세
        동기화 → 틱당 변화량. 앞의 세 개를 먼저 보는 이유는 그것이 *이 명령을 볼 이유가
        있는가*에 답하기 때문이고, 형식 검사보다 싸기 때문이다.
        """
        now = time.monotonic()
        try:
            return self._submit(
                payload=payload, lease_id=lease_id, sequence=sequence,
                valid_for_ms=valid_for_ms, observation=observation, now=now,
            )
        except RejectError as reject:
            with self._lock:
                self._last_reject = (reject.code, reject.detail, now)
            raise

    def _submit(self, *, payload, lease_id, sequence, valid_for_ms, observation, now):
        with self._lock:
            lease = self.authority.authorise(lease_id, sequence, now)
            self._check_command_freshness(observation)
            if self._state in (State.HOLD, State.RETREATING, State.FAULT):
                raise RejectError(
                    Reject.NOT_ACCEPTING_MOTION,
                    self._fault.message if self._fault else "지금은 새 동작을 받지 않습니다",
                )
            if not self._relay:
                backend = self._require_backend()
                if not backend.torque_enabled:
                    raise RejectError(Reject.HARDWARE_NOT_READY, "토크가 걸려 있지 않습니다")
            if not self._present:
                raise RejectError(Reject.HARDWARE_NOT_READY, "현재 관절값을 아직 읽지 못했습니다")

            targets = self.validator.validate(
                payload, present=self._present, needs_sync=lease.needs_sync
            )
            # 보내지 않은 관절은 지금 자리를 유지한다. 화면이 관절 하나만 끌 때 나머지가
            # 마지막 목표로 되돌아가는 일이 없어야 한다.
            merged = dict(self._goal or self._present)
            merged.update(targets)

            # **목표는 사람이 말한 절대 자세 그대로 남는다.**
            #
            # 예전에는 여기서 틱당 상한으로 잘라 `present + step`을 목표로 삼았다. 그
            # 순간 절대 목표가 사라진다 — 서버가 들고 있는 것은 "지금 자리에서 한 걸음"
            # 이라는 증분이고, 다음 명령이 오지 않으면 팔은 목표에 닿지 못한 채 선다.
            # 그것은 이 구조가 하려던 것과 반대다. 리더가 절대 자세를 말하고 팔로워가
            # 거기로 수렴한다는 것이 요점이고, 그래야 한 프레임이 밀리거나 늦어도 오차가
            # 쌓이지 않는다.
            #
            # 부작용도 함께 사라졌다. 잘린 목표는 매 틱 실제 위치에 다시 붙으므로
            # `goal - present`가 정의상 벌어지지 않았고, 그래서 "막혔다"를 알아보려면
            # 사람이 요청한 값을 따로 들고 다녀야 했다(`_requested`). 이제 목표가 그
            # 값이다.
            self._goal = merged
            self._requested = merged
            _, limited = self.validator.clamp_lead(merged, self._present)
            self._limited = limited
            self._last_command_at = now
            self._stalled_by_silence = False
            valid = self._validity_seconds(valid_for_ms)
            self._goal_expires_at = now + valid
            self.authority.mark_synced(lease.lease_id)
            self._state = State.ACTIVE
            return {
                "sequence": sequence,
                "goal": {name: round(value, 3) for name, value in merged.items()},
                "rate_limited": limited,
                "valid_for_ms": int(valid * 1000),
            }

    def _validity_seconds(self, valid_for_ms: object) -> float:
        cap = self.settings.command_valid_ms
        try:
            asked = int(valid_for_ms)
        except (TypeError, ValueError):
            asked = cap
        return max(50, min(cap, asked)) / 1000.0

    def _check_command_freshness(self, observation: object) -> None:
        if observation is None:
            return
        try:
            seen = int(observation)
        except (TypeError, ValueError):
            raise RejectError(Reject.INVALID_SHAPE, "observation이 정수가 아닙니다") from None
        if self._observation - seen > STALE_OBSERVATION_FRAMES:
            raise RejectError(
                Reject.STALE_OBSERVATION,
                f"{self._observation - seen} 프레임 전의 관측을 근거로 하고 있습니다",
            )

    def hold(self, reason: str = Trip.OPERATOR_HOLD, joint: str | None = None, message: str = "") -> None:
        """지금 자세에서 선다. 토크는 그대로 둔다.

        리스가 없어도 누구나 부를 수 있어야 한다 — 폰이 맥을 멈출 수 있어야 하고, 멈추는
        것은 권한을 빼앗는 것이 아니다.
        """
        with self._lock:
            if self._state in (State.STOPPED,):
                return
            # 이미 서 있으면 이유를 덮어쓰지 않는다.
            #
            # 팔을 세운 것은 **처음 이유**다. 그 뒤에 리스가 반납되거나 누가 정지를 한 번
            # 더 눌렀다고 그것으로 바뀌면, 화면에는 마지막으로 일어난 일이 뜨고 정작 팔을
            # 세운 이유는 사라진다. 접촉으로 멈춘 팔에 "조작 권한을 반납했습니다"라고
            # 적혀 있는 것을 시험에서 보았다.
            if self._state in (State.HOLD, State.FAULT) and self._fault is not None:
                return
            self._enter_hold(reason, joint, message or TRIP_KOREAN.get(reason, reason))

    def resume(self) -> None:
        """HOLD를 사람이 확인했다. 다음 명령부터 다시 받는다.

        이전 목표를 이어서 실행하지 않는다. 다시 시작하는 쪽은 현재 자세에서 출발해야
        하고(자세 동기화가 다시 요구된다), 그것이 SAFETY.md 불변조건 7이다.
        """
        with self._lock:
            if self._state not in (State.HOLD, State.FAULT):
                return
            backend = self._backend
            self._fault = None
            self._stalled_by_silence = False
            self.detector.reset()
            self._goal = dict(self._present)
            self._hold_goal = dict(self._present)
            self.authority.require_resync()
            self._state = State.READY if (self._relay or (backend and backend.torque_enabled)) else State.SAFE

    def _await_torque(self, expected: bool, seconds: float = 3.0) -> None:
        """루프가 부탁을 처리할 때까지 기다린다. 못 하면 그 이유를 그대로 올린다."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            with self._lock:
                pending = self._pending_torque
                backend = self._backend
                error = self._error
                state = self._state
            if pending is None:
                if backend is not None and backend.torque_enabled == expected:
                    return
                if state == State.FAULT:
                    raise HardwareError(error or "토크를 바꾸지 못했습니다")
            time.sleep(0.02)
        raise HardwareError("제어 루프가 토크 요청에 답하지 않았습니다")

    # MARK: 상태

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            backend = self._backend
            lease = self.authority.active()
            return {
                "running": self.running,
                "relay": self._relay,
                "state": self._state,
                "state_korean": STATE_KOREAN.get(self._state, self._state),
                "torque_enabled": bool(backend.torque_enabled) if backend else False,
                # 루프가 돌지 않으면 토크가 걸려 있는지 **알 수 없다**. 그때 거짓을
                # 돌려주면 화면은 힘을 주고 서 있는 팔을 두고 "토크 없음"이라고 말한다.
                # 실제로 그 화면을 봤다 — 모터 여섯 개가 전부 켜져 있는데도.
                "torque_known": backend is not None,
                "observation": self._observation,
                "loop_ms": round(self._loop_ms, 2),
                "joints": [
                    {
                        "name": spec.name,
                        "present": round(self._present.get(spec.name, 0.0), 3),
                        "goal": round(self._goal.get(spec.name, self._present.get(spec.name, 0.0)), 3),
                        "load": round(self._load.get(spec.name, 0.0), 1),
                        "current": round(self._current.get(spec.name, 0.0), 1),
                        "temperature": round(self._temperature.get(spec.name, 0.0), 1),
                        "rate_limited": spec.name in self._limited,
                        # 자기 끝에 닿아 선 관절. 고장이 아니므로 배너가 아니라 이 표로만
                        # 말한다 — 화면은 "더 갈 곳이 없다"고 적으면 된다.
                        "at_limit": spec.name in self._end_stop,
                    }
                    for spec in self.specs
                ],
                # 서보가 실제로 들고 있는 속도 상한(눈금/초). 우리가 부탁한 값이 아니라
                # 되읽은 값이다 — 화면이 "걸렸다"고 말하려면 그 근거가 팔에서 와야 한다.
                "speed_ticks": dict(getattr(backend, "speed_ticks", {}) or {}) if backend else {},
                # 명령이 잠깐 끊겨 선 것인지. HOLD와 달리 확인을 요구하지 않는다.
                "command_stalled": self._stalled_by_silence and self._state == State.ACTIVE,
                "fault": self._fault.as_dict() if self._fault else None,
                "warnings": self._warnings(),
                "lease": lease.as_dict() if lease else None,
                "error": self._error,
                "command_age_ms": (
                    int((time.monotonic() - self._last_command_at) * 1000)
                    if self._last_command_at
                    else None
                ),
            }

    def _warnings(self) -> list[dict[str, object]]:
        lines = self.detector.warnings(self._temperature)
        if self._speed_error:
            lines.insert(0, {"joint": None, "code": "SPEED_LIMIT", "message": self._speed_error})
        return lines

    def add_listener(self, callback) -> None:
        with self._lock:
            self._listeners.append(callback)

    def remove_listener(self, callback) -> None:
        with self._lock:
            if callback in self._listeners:
                self._listeners.remove(callback)

    # MARK: 루프

    def _require_backend(self) -> FollowerBackend:
        if self._backend is None:
            raise HardwareError("Virtual leader is not running")
        return self._backend

    def _silenceReason(self, silent_ms: float, now: float) -> str:
        """왜 명령이 끊겼는지. 오지 않은 것과 거절당한 것은 다른 일이다."""
        base = f"{int(silent_ms)}ms 동안 통과한 명령이 없습니다"
        reject = self._last_reject
        if reject is not None and (now - reject[2]) * 1000 <= silent_ms + self.settings.command_timeout_ms:
            return f"{base} — 마지막 명령은 {reject[0]}로 거절되었습니다: {reject[1]}"
        return f"{base} (조작하는 쪽에서 아무것도 오지 않았습니다)"

    def _enter_hold(self, code: str, joint: str | None, message: str) -> None:
        self._stalled_by_silence = False
        self._end_stop.clear()
        self._hold_goal = dict(self._present)
        self._goal = dict(self._present)
        # 요청도 지운다. 남겨 두면 사람이 `확인하고 계속`을 누르는 순간 아직 살아 있는
        # 옛 요청으로 곧바로 다시 걸린다.
        self._requested = {}
        self._limited = []
        self._fault = Fault(code=code, joint=joint, message=message, at=time.time())
        self.authority.require_resync()
        self._state = State.HOLD

    def adopt_settings(self, settings) -> None:
        """새 설정을 받아 든다.

        설정은 얼려 둔(frozen) 값이라 그 자리에서 고치지 않고 통째로 갈아 끼운다. 들고
        있는 곳이 넷이므로 한 곳이라도 빠뜨리면 어떤 검사만 옛 문턱으로 남는다 — 그런
        어긋남은 화면에서 보이지 않는다.
        """
        with self._lock:
            self.settings = settings
            self.validator.settings = settings
            self.detector.settings = settings
            # 속도 상한은 서보 안에 있다. 여기서 부탁만 남기고, 실제로 써 넣는 것은
            # 제어 루프다 — serial 버스에 말을 거는 스레드는 하나여야 한다.
            self._pending_speed = True

    def _movement(self) -> dict[str, float]:
        """최근 창 동안 관절이 실제로 얼마나 움직였는가.

        `_trail`은 30틱(1초)치를 들고 있다. 그보다 짧게 보면 천천히 미는 동안에도 "서 있다"로
        읽히고, 길게 보면 이미 닿은 뒤에도 한참 알아채지 못한다.
        """
        if len(self._trail) < 2:
            return {}
        oldest = self._trail[0]
        return {
            name: abs(value - oldest.get(name, value)) for name, value in self._present.items()
        }

    def _relax_at_end_stops(self, goal: dict[str, float], state: str) -> dict[str, float]:
        """자기 끝에 닿아 선 관절은 그만 민다.

        끝에 닿는 것은 고장이 아니라 기하학이다. 그래서 팔을 세우지도, 사람에게 확인을
        요구하지도 않는다 — 집게를 끝까지 닫을 때마다 `확인하고 계속`을 눌러야 한다면
        그것은 보호가 아니라 방해다. 대신 그 관절의 목표를 서 있는 자리에 붙여 두어
        서보가 힘을 쓰지 않게 한다. 나머지 관절은 그대로 조작된다.

        실측(2026-09-02): 집게가 1.25%에서 부하 84로 6초 내내 밀고 있었고 사다리의 어느
        칸에도 걸리지 않았다. 걸리지 않는 것이 맞다 — 84는 아무것도 부수지 않는다.
        다만 그동안 모터는 계속 뜨거워지고, 그것을 끊는 것은 온도 문턱뿐이었다.
        """
        if state != State.ACTIVE:
            self._end_stop.clear()
            return goal
        moved = self._movement()
        result = dict(goal)
        for spec in self.specs:
            name = spec.name
            if name not in result or name not in self._present:
                continue
            here = self._present[name]
            wanted = self._goal.get(name, here)
            latched = self._end_stop.get(name)
            if latched is not None:
                # 붙잡아 둔 자리에서 풀리는 조건은 하나다 — 사람이 목표를 끝에서
                # 되돌렸을 때. 위치가 조금 흔들렸다고 다시 밀기 시작하면 그 왕복이
                # 곧 이 장치가 없애려던 것이다.
                if abs(wanted - latched) > self.settings.limit_epsilon * 2 and (
                    (latched - spec.minimum <= self.settings.limit_epsilon and wanted > latched)
                    or (spec.maximum - latched <= self.settings.limit_epsilon and wanted < latched)
                ):
                    self._end_stop.pop(name, None)
                    continue
                result[name] = latched
                continue
            if self.validator.at_end_stop(name, wanted, here, moved.get(name)):
                self._end_stop[name] = here
                result[name] = here
        return result

    def _retreat_goal(self, joint: str) -> dict[str, float]:
        """걸린 관절만 밀던 방향의 반대로 물러난다.

        최근 경로를 쓰는 이유는 방향을 알아야 하기 때문이다. 목표와 현재의 차이가 곧 밀던
        방향이므로 그 반대로 설정값만큼 뺀다. 나머지 관절은 지금 자리를 지킨다 — 걸린 곳을
        풀자고 팔 전체를 움직이면 무엇이 어디에 닿았는지 알 수 없게 된다.
        """
        spec = next((s for s in self.specs if s.name == joint), None)
        target = dict(self._present)
        if spec is None:
            return target
        pushing = self._requested.get(
            joint, self._goal.get(joint, self._present.get(joint, 0.0))
        ) - self._present.get(joint, 0.0)
        if abs(pushing) < 1e-6 and len(self._trail) > 1:
            pushing = self._present.get(joint, 0.0) - self._trail[0].get(joint, 0.0)
        direction = 1.0 if pushing >= 0 else -1.0
        back = self.settings.retreat_deg
        target[joint] = spec.clamp(self._present.get(joint, 0.0) - direction * back)
        return target

    def _run(self) -> None:
        """제어 루프. **어떤 이유로도 조용히 죽지 않는다.**

        죽은 루프는 이 시스템에서 가장 나쁜 상태다. serial과 owner lock을 쥔 채로 남고,
        토크는 걸려 있을 수 있는데 아무도 그것을 내리지 못한다. 화면은 `Virtual leader
        is not running`이라고만 말하고, 다시 시작하려 하면 자기 자신이 쥔 lock에 막힌다.
        서비스를 재시작하는 것 말고는 빠져나올 길이 없다.

        실제로 그 상태를 만들었다(2026-09-02). 토크를 거는 중에 Feetech 상태 패킷 하나가
        깨져 `ConnectionError`가 올라왔는데, 그 종류는 아래의 `except HardwareError`에
        걸리지 않았다. 예외 하나가 팔을 통째로 잠근 셈이다.

        이제 두 겹이다. 안쪽에서는 각 단계가 어떤 예외든 잡아 `FAULT`로 적고 계속 돌고,
        바깥에서는 그래도 빠져나오는 것이 있으면 여기서 잡아 **정리까지 하고** 끝낸다.
        정리를 해 두면 다음 `start`가 통한다 — 그것이 사람이 앱에서 빠져나올 길이다.
        """
        try:
            self._loop()
        except BaseException as exc:  # noqa: BLE001 - 여기서 새는 것은 없어야 한다
            logger.exception("virtual leader loop died")
            with self._lock:
                self._error = f"제어 루프가 멈췄습니다: {exc}"
                self._fault = Fault(Trip.HARDWARE_ERROR, None, self._error, time.time())
                self._state = State.FAULT
                self.authority.release_all("loop_died")
            raise
        finally:
            # 루프가 끝나면 장치도 놓는다. 잡은 채로 사라지면 다음 시작이 자기 자신이
            # 쥔 lock에 막힌다.
            self._release_device_if_orphaned()

    def _release_device_if_orphaned(self) -> None:
        """루프가 스스로 끝난 뒤의 뒷정리.

        `stop()`을 거쳐 내려온 경우에는 이미 정리되어 있으므로 아무 일도 하지 않는다.
        여기서 정리하는 것은 **예상치 못하게** 끝난 경우다.
        """
        with self._lock:
            if not self._stop.is_set() and self._backend is not None:
                backend, self._backend = self._backend, None
                locks, self._owner_locks = self._owner_locks, None
            else:
                return
        try:
            backend.disconnect()
        except Exception:  # noqa: BLE001 - 정리하는 길은 어떤 이유로도 막히지 않는다
            logger.debug("disconnect after loop death failed", exc_info=True)
        if locks is not None:
            locks.release()

    def _loop(self) -> None:
        period = 1.0 / max(1, self.settings.hz)
        # 부하·전류·온도는 매 틱 읽지 않는다. 30Hz에서 sync_read 네 번은 예산을 넘기고,
        # 우리가 보는 창(300~400ms 연속 초과)에 비하면 10Hz로도 촘촘하다.
        health_every = max(1, self.settings.hz // 10)
        tick = 0
        # 붙자마자 한 번 써 넣는다. `Goal_Velocity`는 SRAM이라 전원을 내리면 0(제한
        # 없음)으로 돌아간다. 시작할 때마다 다시 세우지 않으면 어느 날은 상한이 있고
        # 어느 날은 없는 팔이 된다.
        with self._lock:
            self._pending_speed = True
        retreat_target: dict[str, float] | None = None
        retreat_reason: tuple[str, str | None, str] | None = None
        retreat_started_at = 0.0

        while not self._stop.is_set():
            started = time.perf_counter()
            tick += 1

            # 바깥에서 남긴 토크 부탁을 여기서 처리한다. 버스를 만지는 것은 이 스레드뿐이다.
            with self._lock:
                pending, self._pending_torque = self._pending_torque, None
            with self._lock:
                speed_wanted, self._pending_speed = self._pending_speed, False
            if speed_wanted and self._backend is not None:
                try:
                    self._backend.apply_speed_limit(self.settings)
                    with self._lock:
                        self._speed_error = None
                except Exception as exc:  # noqa: BLE001
                    # 속도 상한을 못 썼다고 팔을 세우지는 않는다 — 못 쓰면 서보는 예전
                    # 값(대개 제한 없음)으로 도는데, 그때 조용히 넘어가면 화면은 상한이
                    # 걸린 줄 안다.
                    #
                    # `_error`에 적지 않는 이유: 그 칸은 바로 다음 줄의 읽기가 성공하면
                    # 지워진다. 실패를 적자마자 지우는 자리에 적는 것은 적지 않는 것과
                    # 같다. 이것은 사람이 볼 때까지 남아 있어야 하는 경고다.
                    with self._lock:
                        self._speed_error = f"속도 상한을 서보에 쓰지 못했습니다: {exc}"

            if pending is not None and self._backend is not None:
                try:
                    self._backend.set_torque(pending)
                    with self._lock:
                        if pending:
                            # 지금 자세를 그대로 목표로 잡는다. 이것이 없으면 첫 write가
                            # 어디로 갈지 모터의 마지막 Goal_Position에 달린다.
                            self._hold_goal = dict(self._present)
                            self._goal = dict(self._present)
                            self._state = State.READY
                            self._fault = None
                            self._error = None
                            self.detector.reset()
                        else:
                            self._goal = {}
                            self._hold_goal = {}
                            self._state = State.SAFE
                except Exception as exc:  # noqa: BLE001 - 루프를 죽이는 예외는 없다
                    with self._lock:
                        self._error = str(exc)
                        self._fault = Fault(Trip.HARDWARE_ERROR, None, str(exc), time.time())
                        self._state = State.FAULT
                        self.authority.require_resync()

            if self._relay:
                # 중계 모드에는 읽을 버스가 없다. 마지막으로 통과한 목표가 곧 우리가 아는
                # 전부이고, 뷰어에는 그것이 "명령한 자세"로 표시된다.
                frame = {"position": dict(self._goal or self._present)}
            else:
                try:
                    frame = self._backend.read(include_health=tick % health_every == 0)
                except Exception as exc:  # noqa: BLE001
                    with self._lock:
                        self._error = str(exc)
                        self._fault = Fault(Trip.HARDWARE_ERROR, None, str(exc), time.time())
                        # 토크는 건드리지 않는다. 통신이 끊겼다고 팔을 떨어뜨리지 않는다.
                        self._state = State.FAULT
                        self.authority.require_resync()
                    self._publish()
                    time.sleep(period)
                    continue

            now = time.monotonic()
            with self._lock:
                self._error = None
                self._present = {k: float(v) for k, v in frame["position"].items()}
                if "load" in frame:
                    self._load = {k: float(v) for k, v in frame["load"].items()}
                    self._current = {k: float(v) for k, v in frame["current"].items()}
                    self._temperature = {k: float(v) for k, v in frame["temperature"].items()}
                self._observation += 1
                self._trail.append(dict(self._present))

                state = self._state
                backend = self._backend

                # 1. 권한이 사라졌는가. 만료도 반납도 결과는 같다 — 자세를 유지하고 선다.
                if state in (State.ACTIVE,) and self.authority.active(now) is None:
                    self._enter_hold(Trip.LEASE_EXPIRED, None, TRIP_KOREAN[Trip.LEASE_EXPIRED])
                    state = self._state

                # 2. 명령이 끊겼는가. 마지막 명령을 무한히 반복하지 않는다.
                #
                # 두 단계로 나눈다. **서는 것과 사람에게 확인을 요구하는 것은 다른 일**
                # 이기 때문이다. 예전에는 300ms 침묵이 곧바로 HOLD였고, HOLD는 `확인하고
                # 계속`을 눌러야 풀린다. 30Hz에서 300ms는 무선 구간이 한 번 흔들리기에
                # 충분한 시간이라, 폰으로 조작하면 아무 잘못 없이 멈추고 그때마다 버튼을
                # 눌러야 했다 — 사용자가 "왜 안 되지" 하던 자리 가운데 하나가 이것이다.
                #
                # 이제 짧은 침묵에는 목표를 지금 자리에 붙이기만 한다. 팔은 곧바로
                # 서지만 상태는 `ACTIVE`로 남고, 명령이 다시 오면 그대로 이어진다.
                # 침묵이 `command_hold_ms`까지 이어지면 그때 HOLD다 — 그쯤이면 잠깐
                # 흔들린 것이 아니라 조작하던 쪽이 사라진 것이다.
                if state == State.ACTIVE:
                    silent_ms = (now - self._last_command_at) * 1000.0
                    expired = now > self._goal_expires_at
                    if silent_ms > self.settings.command_hold_ms:
                        self._enter_hold(
                            Trip.COMMAND_TIMEOUT, None, self._silenceReason(silent_ms, now)
                        )
                        state = self._state
                    elif silent_ms > self.settings.command_timeout_ms or expired:
                        # 선다. 그러나 이유를 세우지는 않는다.
                        self._goal = dict(self._present)
                        self._requested = {}
                        self._limited = []
                        self._stalled_by_silence = True
                    else:
                        self._stalled_by_silence = False

                # 3. 관측이 거는 정지. 토크가 걸려 있을 때만 볼 이유가 있다.
                if backend is not None and backend.torque_enabled and state in (State.ACTIVE, State.READY):
                    trip = self.detector.inspect(
                        now=now,
                        present=self._present,
                        goal=self._goal,
                        load=self._load,
                        current=self._current,
                        temperature=self._temperature,
                        requested=self._requested,
                        moved=self._movement(),
                    )
                    if trip is not None:
                        code, joint, message = trip
                        if code in (Trip.OVERLOAD, Trip.OVERCURRENT, Trip.FOLLOWING_ERROR, Trip.STALLED) and joint:
                            retreat_target = self._retreat_goal(joint)
                            retreat_reason = (code, joint, message)
                            retreat_started_at = now
                            self._state = State.RETREATING
                            self._fault = Fault(code, joint, message, time.time())
                            self.authority.require_resync()
                            state = self._state
                        else:
                            self._enter_hold(code, joint, message)
                            state = self._state

                # 4. 쓰기. 무엇을 쓰는지는 상태가 정한다.
                write: dict[str, float] | None = None
                if backend is not None and backend.torque_enabled:
                    if state == State.ACTIVE:
                        write = self._goal
                    elif state == State.RETREATING and retreat_target is not None:
                        write = retreat_target
                        done = all(
                            abs(retreat_target.get(name, value) - value) < 0.5
                            for name, value in self._present.items()
                        )
                        # 물러남에는 끝이 있어야 한다. 물러날 곳이 없는 자리(걸린 방향의
                        # 반대편에도 무언가가 있는 경우)에서는 목표에 영영 닿지 못하고,
                        # 그 자리에서 계속 밀게 된다. 물러나는 동안에는 관측 정지도 보지
                        # 않으므로 아무것도 그것을 끊지 못한다. 시간이 다 되면 지금 자리에
                        # 그대로 세운다 — 세우는 것은 언제나 할 수 있다.
                        stuck = (now - retreat_started_at) * 1000.0 > self.settings.retreat_ms
                        if done or stuck:
                            code, joint, message = retreat_reason or (Trip.OPERATOR_HOLD, None, "")
                            if stuck and not done:
                                message = (
                                    f"{message} 물러나려 했지만 "
                                    f"{self.settings.retreat_ms}ms 안에 빠져나오지 못해 "
                                    "그 자리에서 세웁니다"
                                )
                            self._enter_hold(code, joint, message)
                            retreat_target = None
                            retreat_reason = None
                    elif state in (State.HOLD, State.READY, State.FAULT):
                        # 자세 유지. HOLD의 물리 동작이 무엇인지 SAFETY.md가 하드웨어마다
                        # 정하라고 했고, 이 하드웨어에서는 지금 자리를 목표로 계속 쓰는 것이다.
                        write = self._hold_goal or dict(self._present)

            if write and not self._relay:
                try:
                    # 절대 목표를 그대로 쓰지 않고 `lead`만큼만 앞세워 쓴다. 이것이
                    # 막혔을 때 서보가 내는 힘의 상한이다. 속도는 서보의 `Goal_Velocity`가
                    # 지키므로 이 자르기가 팔을 느리게 만들지 않는다 — 자유롭게 움직이는
                    # 동안 목표와 실제의 거리는 서보의 추종오차만큼밖에 되지 않는다.
                    clamped, limited = self.validator.clamp_lead(write, self._present)
                    clamped = self._relax_at_end_stops(clamped, state)
                    if state == State.ACTIVE:
                        self._limited = limited
                    self._backend.write(clamped)
                except Exception as exc:  # noqa: BLE001
                    with self._lock:
                        self._error = str(exc)
                        self._fault = Fault(Trip.HARDWARE_ERROR, None, str(exc), time.time())
                        self._state = State.FAULT
                        self.authority.require_resync()

            with self._lock:
                self._loop_ms = (time.perf_counter() - started) * 1000.0
            self._publish()
            time.sleep(max(0.0, period - (time.perf_counter() - started)))

    def _publish(self) -> None:
        snapshot = self.snapshot()
        for listener in list(self._listeners):
            try:
                listener(snapshot)
            except Exception:  # noqa: BLE001 - 듣는 쪽의 사고가 제어 루프를 세우지 않는다
                logger.debug("telemetry listener failed", exc_info=True)
