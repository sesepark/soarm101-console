from __future__ import annotations

import asyncio
import logging
import os
import secrets
import time
from dataclasses import replace
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from ..calibration import validate_calibration
from ..config import Settings
from ..diagnostics import inspect_arm
from .authority import AuthorityManager, LeaseConflict
from .backend import HardwareError
from .owner import State, VirtualLeaderOwner
from .safety import (
    KOREAN,
    PROFILES,
    OPERATOR_GONE,
    PROFILE_KOREAN,
    Reject,
    RejectError,
    Trip,
    VLeaderSettings,
    load_settings,
    profile_of,
)
from .spec import SpecError, load_joint_specs

logger = logging.getLogger(__name__)

#: 토크를 거는 순간의 확인 문구. 앱이 대신 채워 주지 않는다 — 손으로 옮겨 적는 그 순간이
#: 게이트의 전부이고, 미리 채워 넣으면 게이트가 아니라 버튼 하나가 된다.
ARM_CONFIRMATION = "MOVE SOARM101"
#: 토크를 푸는 순간의 확인 문구. **팔이 떨어질 수 있다.**
RELEASE_CONFIRMATION = "RELEASE TORQUE SOARM101"

MOTION_TOKEN_ENV = "SOARM_MOTION_TOKEN"


class ArmRequest(BaseModel):
    confirmation: str


class PolicyRequest(BaseModel):
    """바꿀 값들.

    `profile` 하나만 보내는 것이 보통의 길이다. `values`는 그 위에 겹쳐지고, 둘 다
    보내면 프로필을 먼저 깐 뒤 개별 값이 덮어쓴다 — 화면의 `고급`이 그렇게 동작한다.
    """

    profile: str | None = None
    values: dict[str, float] = {}


class LeaseRequest(BaseModel):
    #: 조작 권한을 받는 순간의 확인 문구. 토크를 거는 자리에도 같은 것을 요구하지만,
    #: 그것만으로는 모자란다 — 토크가 이미 걸려 있으면 그 게이트를 지나치게 되고,
    #: 그러면 먼저 켜 둔 사람이 있는 팔에 아무나 문구 없이 붙을 수 있다.
    #: **팔이 움직일 수 있게 되는 순간**은 리스를 받는 순간이므로 게이트는 여기에도 있다.
    confirmation: str = ""
    holder: str = "unknown"
    session_id: str = ""


#: 앱에서 조절할 수 있는 값과 그 범위.
#:
#: 전부를 열지 않는다. 온도 문턱처럼 하드웨어를 지키는 값은 화면에서 만질 것이 아니고,
#: 리스 만료처럼 프로토콜이 정하는 값은 양쪽이 같아야 한다.
#:
#: 그리고 **여기 있는 값들도 화면의 첫 번째 선택지가 아니다.** `lead_deg`가 12여야
#: 하는지 15여야 하는지는 이 팔을 만든 사람도 재 보기 전에는 모르고, 쓰는 사람에게
#: 물을 일은 더더욱 아니다. 화면에는 `PROFILES`의 세 가지를 먼저 두고, 이 표는 그
#: 아래 `고급`에 접어 둔다. 여기 있는 이유는 값을 하나씩 재 볼 수 있어야 하기 때문이지
#: 사람이 매번 고르라고 있는 것이 아니다.
#:
#: 범위를 두는 이유는 오타 하나로 팔이 최고 속도로 출발하지 않게 하기 위해서다.
TUNABLES: dict[str, tuple[float, float, type, str]] = {
    "max_deg_per_s": (10.0, 150.0, float, "SOARM_VL_MAX_DEG_PER_S"),
    "max_percent_per_s": (10.0, 118.0, float, "SOARM_VL_MAX_PERCENT_PER_S"),
    "lead_deg": (3.0, 25.0, float, "SOARM_VL_LEAD_DEG"),
    "lead_percent": (3.0, 25.0, float, "SOARM_VL_LEAD_PERCENT"),
    "sync_tolerance_deg": (2.0, 30.0, float, "SOARM_VL_SYNC_TOLERANCE_DEG"),
    "following_error_deg": (2.0, 24.0, float, "SOARM_VL_FOLLOW_ERROR_DEG"),
    "following_error_ms": (200, 3000, int, "SOARM_VL_FOLLOW_ERROR_MS"),
    "stall_load": (60, 800, int, "SOARM_VL_STALL_LOAD"),
    "stall_load_ms": (200, 5000, int, "SOARM_VL_STALL_LOAD_MS"),
    "load_trip": (200, 900, int, "SOARM_VL_LOAD_TRIP"),
    "retreat_deg": (1.0, 15.0, float, "SOARM_VL_RETREAT_DEG"),
    "command_hold_ms": (500, 8000, int, "SOARM_VL_COMMAND_HOLD_MS"),
}


def _persist_tunables(applied: dict[str, float], profile: str | None = None) -> str | None:
    """바꾼 값을 `config/soarm.env`에 남긴다. 다음 재시작에도 살아 있어야 한다.

    파일에는 조작 토큰도 들어 있으므로 통째로 다시 쓰지 않고, 해당 줄만 갈아 끼우거나
    없으면 끝에 붙인다. 쓰지 못해도 지금 적용된 값은 살아 있다 — 그 사실을 호출한 쪽에
    돌려준다.
    """
    # 시험이 진짜 설정 파일에 쓰지 않도록 자리를 바꿀 수 있게 둔다. 한 번 그렇게 쓰였고,
    # 그 뒤로 서버의 step_deg가 시험이 정한 값으로 남아 있었다.
    override = os.getenv("SOARM_ENV_FILE")
    path = Path(override) if override else Path(__file__).resolve().parents[3] / "config" / "soarm.env"
    try:
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        for name, value in applied.items():
            if name not in TUNABLES:
                # 프로필이 함께 옮기는 값 가운데 화면에서 만질 수 없는 것이 있다.
                # 적용은 되지만 파일에는 남기지 않는다 — 그러면 다음 재시작에서 프로필의
                # 기본값이 다시 깔린다. 프로필 자체는 아래에서 이름으로 남는다.
                continue
            variable = TUNABLES[name][3]
            rendered = f"{variable}={value}"
            for index, line in enumerate(lines):
                if line.strip().startswith(f"{variable}="):
                    lines[index] = rendered
                    break
            else:
                lines.append(rendered)
        if profile is not None:
            rendered = f"SOARM_VL_PROFILE={profile}"
            for index, line in enumerate(lines):
                if line.strip().startswith("SOARM_VL_PROFILE="):
                    lines[index] = rendered
                    break
            else:
                lines.append(rendered)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return None
    except OSError as exc:  # noqa: BLE001 - 적용은 됐고 저장만 실패했다
        return str(exc)


class VirtualLeader:
    """가상 리더 하나. 앱 모듈이 이것 하나만 들고 있으면 된다."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.policy = load_settings()
        self.authority = AuthorityManager(self.policy)
        self._owner: VirtualLeaderOwner | None = None
        self._specs = None
        self._spec_error: str | None = None
        #: 마지막으로 실제로 읽은 관절 자세. 수집으로 넘어갈 때의 출발점이다.
        self.last_known_position: dict[str, float] = {}
        self.last_known_at = 0.0
        #: 텔레메트리를 듣는 쪽. **여기**가 들고 있어야 한다. 제어 루프가 들고 있으면,
        #: 루프가 뜨기 전에 붙은 연결은 영영 아무것도 받지 못한다 — 화면을 먼저 열고
        #: 조작 권한을 나중에 받는 것이 정상적인 순서인데도 그렇다.
        self._listeners: list = []

    def retune(self, values: dict[str, float]) -> None:
        """조작감 값들을 갈아 끼운다.

        설정은 얼려 둔 값이다. 그 자리에서 고치는 대신 새 것을 만들어 **들고 있는 곳마다**
        건네준다 — 권한 관리자와 제어 루프(그리고 루프 안의 검증기·감지기)가 각각 참조를
        쥐고 있어서, 한 곳만 바꾸면 나머지가 옛 값으로 계속 판단한다.
        """
        self.policy = replace(self.policy, **values)
        self.authority.settings = self.policy
        if self._owner is not None:
            self._owner.adopt_settings(self.policy)

    # MARK: 계약

    def specs(self):
        """관절 계약. calibration이 바뀌면 다시 읽는다."""
        if self._specs is None:
            try:
                self._specs = load_joint_specs(self.settings.follower_calibration)
                self._spec_error = None
            except SpecError as exc:
                self._spec_error = str(exc)
                raise
        return self._specs

    def invalidate_specs(self) -> None:
        self._specs = None

    @property
    def owner(self) -> VirtualLeaderOwner | None:
        return self._owner

    @property
    def running(self) -> bool:
        return self._owner is not None and self._owner.running

    def preflight(self) -> list[str]:
        """가상 리더를 시작하기 전에 걸리는 것들.

        물리 리더 팔은 요구하지 않는다 — 그것이 이 모드의 존재 이유다. 리더 쪽 포트와
        calibration이 없어도 여기서는 문제가 아니다.
        """
        problems: list[str] = []
        if not self.settings.motion_enabled:
            problems.append("SOARM_ENABLE_MOTION=1 is not set")
        if not Path(self.settings.follower_port).exists():
            problems.append(f"Missing follower port: {self.settings.follower_port}")
        error = validate_calibration(self.settings.follower_calibration)
        if error:
            problems.append(error)
        if not motion_token():
            problems.append(f"{MOTION_TOKEN_ENV} is not set on the server")
        return problems

    def status(self) -> dict[str, object]:
        try:
            contract = [spec.as_dict() for spec in self.specs()]
        except SpecError:
            contract = []
        snapshot = (
            self._owner.snapshot()
            if self._owner is not None
            else {
                "running": False,
                "state": State.STOPPED,
                "state_korean": "꺼짐",
                "torque_enabled": False,
                "torque_known": False,
                "joints": [],
                "fault": None,
                "warnings": [],
                "lease": None,
                "error": None,
            }
        )
        return {
            "available": self._spec_error is None,
            "spec_error": self._spec_error,
            "preflight": self.preflight(),
            # 관절 **계약**이다. 지금 값이 아니라 이름·단위·절대 한계·URDF 대응이다.
            # 지금 값은 `joints`에 있고, 그것은 아래 `**snapshot`이 넣는다 — 두 가지를
            # 같은 키에 담았더니 계약이 조용히 덮여 사라졌다.
            "spec": contract,
            "policy": self.policy.as_dict(),
            "arm_confirmation_length": len(ARM_CONFIRMATION),
            "viewer_url": "/viewer/",
            "lease_history": self.authority.history,
            **snapshot,
        }

    # MARK: 수명

    def start(self) -> dict[str, object]:
        if self.running:
            raise HardwareError("Virtual leader is already running")
        # 루프가 죽은 채로 남아 있는 소유자를 먼저 치운다.
        #
        # 죽은 루프는 참조만 남기고 사라지는데, 그 참조가 있는 한 상태 화면은 마지막
        # 상태(대개 `SAFE`)를 그대로 말하고 `require_owner`는 409로 거절한다. 그러면
        # 화면에는 "관찰 전용"이라고 적혀 있는데 아무 버튼도 듣지 않는다. 다시 시작하는
        # 것이 사람이 그 상태에서 빠져나올 유일한 길이므로, 여기서 막히지 않아야 한다.
        if self._owner is not None:
            logger.warning("clearing a virtual leader whose loop is gone before restarting")
            try:
                self._owner.stop(force=True)
            except Exception:  # noqa: BLE001 - 치우는 길은 어떤 이유로도 막히지 않는다
                logger.debug("could not stop the dead owner cleanly", exc_info=True)
            self._owner = None
        problems = self.preflight()
        if problems:
            raise HardwareError("; ".join(problems))
        specs = self.specs()
        # 팔로워만 읽는 진단이다. 물리 리더는 이 모드에 필요 없으므로 없다고 막지 않는다.
        # serial을 여는 것은 이 진단이 끝난 **뒤**여야 한다 — 소유자는 한 번에 하나다.
        #
        # 흉내 백엔드에서는 건너뛴다. 열 버스가 없는데도 진단을 돌리면 **진짜** 버스를
        # 열게 되고, 그 순간 소유자가 둘이 된다. 시험 중에 그 실수를 실제로 한 번 했고,
        # 두 프로세스가 같은 포트에 말을 걸자 status packet이 깨져 나왔다.
        if os.getenv("SOARM_VL_BACKEND", "real").strip().lower() != "simulated":
            report = inspect_arm("follower", self.settings.follower_port)
            if not report.healthy:
                raise HardwareError(report.error or "Follower bus did not read back healthy")
            # **토크가 걸려 있다고 시작을 막지 않는다.**
            #
            # 처음에는 여기서 막았다. 그랬더니 막다른 골목이 생겼다 — 토크를 걸어 둔 채
            # 루프를 내리면(서비스 재시작이면 충분하다) 팔은 뻣뻣한 채로 남는데, 다시
            # 시작하려면 토크가 꺼져 있어야 하고, 토크를 끄려면 루프가 돌아야 한다.
            # 앱으로는 그 팔을 아무것도 할 수 없게 된다. 실제로 그 상태를 한 번 만들었다.
            #
            # 여기서 시작하는 것은 **읽기**다. 움직임의 게이트는 `arm`에 있고 거기에는
            # 확인과 토큰이 붙어 있다. 지금 어떤 상태인지는 화면이 그대로 말해 준다.
        owner = VirtualLeaderOwner(
            specs=specs,
            settings=self.policy,
            port=self.settings.follower_port,
            robot_id=self.settings.follower_id,
            authority=self.authority,
        )
        owner.start()
        self._attach(owner)
        return owner.snapshot()

    def _attach(self, owner: VirtualLeaderOwner) -> None:
        self._owner = owner
        for listener in self._listeners:
            owner.add_listener(listener)

    def add_listener(self, callback) -> None:
        self._listeners.append(callback)
        if self._owner is not None:
            self._owner.add_listener(callback)

    def remove_listener(self, callback) -> None:
        if callback in self._listeners:
            self._listeners.remove(callback)
        if self._owner is not None:
            self._owner.remove_listener(callback)

    def stop(self, *, force: bool = False) -> None:
        owner = self._owner
        if owner is not None:
            # 마지막으로 읽은 자세를 남긴다. 데이터 수집으로 넘어갈 때 중계 모드가
            # 여기서 출발해야 하고, 그러지 않으면 첫 목표가 어디인지 아무도 모른다.
            snapshot = owner.snapshot()
            positions = {
                joint["name"]: joint["present"] for joint in snapshot.get("joints", [])
            }
            if positions and not snapshot.get("relay"):
                self.last_known_position = positions
                self.last_known_at = time.time()
        # **먼저 내리고 그다음에 놓는다.**
        #
        # 반대로 하면 `owner.stop()`이 거절할 때(토크가 걸려 있는데 `force`가 없을 때가
        # 그렇다) 참조는 이미 사라진 뒤다. 루프는 계속 돌면서 serial과 lock을 쥐고 팔을
        # 잡고 있는데 서비스는 \"꺼짐\"이라고 말하고, 그 루프에 닿을 방법이 다시는 없다.
        # 토크를 풀 수도, 다시 시작할 수도 없다 — 서비스를 재시작하는 것 말고는.
        # 실제로 그 상태를 만들었다: 다음 `start`가 \"Device is owned by virtual-leader
        # (pid ...)\"로 거절당했고, 그 pid는 콘솔 자신이었다.
        if owner is not None:
            owner.stop(force=force)
        self._owner = None

    def start_relay(self) -> dict[str, object]:
        """데이터 수집으로 넘기기 전, 장치를 놓고 목표만 중계하는 모드로 바꾼다.

        `lerobot-record`가 팔로워 serial의 소유자가 되어야 하므로 우리는 장치를 놓는다.
        놓기 직전의 자세에서 출발해야 팔이 튀지 않는다 — 그래서 가상 리더가 한 번도 돌지
        않았거나 마지막으로 읽은 자세가 오래되었으면 거절한다.
        """
        seed = dict(self.last_known_position)
        if self.running and self._owner is not None and not self._owner.snapshot().get("relay"):
            snapshot = self._owner.snapshot()
            seed = {joint["name"]: joint["present"] for joint in snapshot.get("joints", [])}
            self.stop(force=True)
        if not seed:
            raise HardwareError(
                "The virtual leader has not read the arm yet; start it once so the relay knows where the arm is"
            )
        if time.time() - self.last_known_at > 120:
            raise HardwareError(
                "The last known arm position is more than two minutes old; start the virtual leader again"
            )
        owner = VirtualLeaderOwner(
            specs=self.specs(),
            settings=self.policy,
            port=self.settings.follower_port,
            robot_id=self.settings.follower_id,
            authority=self.authority,
        )
        owner.start(relay_from=seed)
        self._attach(owner)
        return owner.snapshot()

    def goal(self) -> dict[str, object]:
        """`lerobot-record` 안의 가상 리더 teleoperator가 매 틱 가져가는 목표."""
        owner = self._owner
        if owner is None:
            return {"joints": {}, "stale": True, "state": State.STOPPED}
        snapshot = owner.snapshot()
        age = snapshot.get("command_age_ms")
        stale = snapshot["state"] != State.ACTIVE or age is None or age > self.policy.command_timeout_ms
        return {
            "joints": {joint["name"]: joint["goal"] for joint in snapshot["joints"]},
            "stale": bool(stale),
            "state": snapshot["state"],
            "observation": snapshot.get("observation"),
        }

    def require_owner(self) -> VirtualLeaderOwner:
        if self._owner is None:
            raise HTTPException(
                status_code=409, detail="관찰이 꺼져 있습니다. 먼저 관찰을 시작하세요."
            )
        if not self._owner.running:
            # 참조는 남았는데 루프가 없다. 사람이 할 수 있는 일은 다시 시작하는 것뿐이고,
            # 화면이 그렇게 말해 주어야 한다 — 영어로 "not running"이라고만 하면 방금
            # "관찰 전용"이라고 적혀 있던 화면과 앞뒤가 맞지 않는다.
            detail = self._owner.snapshot().get("error") or "제어 루프가 멈췄습니다"
            raise HTTPException(
                status_code=409,
                detail=f"{detail} — `관찰 시작`을 다시 누르면 이어서 쓸 수 있습니다.",
            )
        return self._owner


def motion_token() -> str:
    return os.getenv(MOTION_TOKEN_ENV, "").strip()


def _authorise_motion(request_token: str | None) -> None:
    """관찰과 조작의 권한을 가른다.

    관찰(상태, 카메라, 텔레메트리 구독)에는 아무것도 요구하지 않는다. Tailscale의 tailnet
    안에 있다는 것으로 충분하다. 조작은 그 위에 토큰을 하나 더 요구한다 — 폰을 잃어버렸을
    때 토큰만 갈아 끼우면 조작 권한만 끊긴다.
    """
    expected = motion_token()
    if not expected:
        raise HTTPException(
            status_code=409,
            detail=f"{MOTION_TOKEN_ENV} is not configured on the server; motion is refused",
        )
    if not request_token or not secrets.compare_digest(request_token, expected):
        raise HTTPException(status_code=401, detail="Motion token is missing or wrong")


def _token_from(request: Request) -> str | None:
    return request.headers.get("x-soarm-motion-token") or request.query_params.get("token")


def build_router(vleader: VirtualLeader) -> APIRouter:
    router = APIRouter(prefix="/api/vleader", tags=["virtual-leader"])

    @router.get("")
    def describe() -> dict[str, object]:
        return vleader.status()

    @router.get("/motion-auth")
    def verify_motion_auth(request: Request) -> dict[str, bool]:
        """동작 없이 Tailscale 경로의 application token만 확인한다.

        확인 문구나 그 길이는 내보내지 않는다. 이 endpoint의 200은 장치 준비나 동작 허가가
        아니라, 요청이 현재 token을 알고 있다는 한 가지만 뜻한다.
        """
        _authorise_motion(_token_from(request))
        return {"authorized": True}

    @router.post("/start")
    def start() -> dict[str, object]:
        """팔로워 serial을 잡고 관찰을 시작한다. 토크는 아직 걸지 않는다.

        확인 문구를 요구하지 않는 이유: 여기서는 아무것도 움직이지 않는다. 게이트는 토크를
        거는 자리에 있고, 게이트를 여러 개 늘어놓으면 하나하나가 가벼워진다.
        """
        try:
            return vleader.start()
        except (HardwareError, SpecError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - 이유 없는 500만은 내보내지 않는다
            # 500은 화면에서 \"서버가 요청을 끝내지 못했습니다\"로만 보인다. 무엇이
            # 잘못됐는지가 사라지면 사람은 다음에 무엇을 할지 알 수 없다. 실제로
            # status packet 하나가 깨졌을 때 이 자리에서 500이 나갔다.
            raise HTTPException(
                status_code=409, detail=f"Could not start the virtual leader: {exc}"
            ) from exc

    @router.post("/stop")
    def stop(request: Request, force: bool = False) -> dict[str, object]:
        if force:
            _authorise_motion(_token_from(request))
        try:
            vleader.stop(force=force)
        except HardwareError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return vleader.status()

    @router.post("/arm")
    def arm(request: Request, body: ArmRequest) -> dict[str, object]:
        """토크를 건다. 여기서부터 팔은 스스로 자세를 버티고, 명령을 받을 수 있다."""
        _authorise_motion(_token_from(request))
        if body.confirmation != ARM_CONFIRMATION:
            raise HTTPException(status_code=400, detail="Confirmation phrase does not match")
        owner = vleader.require_owner()
        try:
            owner.arm()
        except HardwareError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return owner.snapshot()

    @router.post("/torque/release")
    def release_torque(request: Request, body: ArmRequest) -> dict[str, object]:
        """토크를 푼다. 받치지 않으면 팔이 떨어진다."""
        _authorise_motion(_token_from(request))
        if body.confirmation != RELEASE_CONFIRMATION:
            raise HTTPException(status_code=400, detail="Confirmation phrase does not match")
        owner = vleader.require_owner()
        try:
            owner.release_torque()
        except HardwareError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return owner.snapshot()

    @router.post("/lease")
    def grant(request: Request, body: LeaseRequest) -> dict[str, object]:
        _authorise_motion(_token_from(request))
        if body.confirmation != ARM_CONFIRMATION:
            raise HTTPException(status_code=400, detail="Confirmation phrase does not match")
        owner = vleader.require_owner()
        if owner.state in (State.STOPPED, State.SAFE):
            raise HTTPException(
                status_code=409,
                detail="Enable torque first: the arm cannot follow a goal while torque is off",
            )
        try:
            lease = vleader.authority.grant(body.holder or "unknown", body.session_id)
        except LeaseConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        # **조작하던 사람이 사라져서** 선 것이라면 새 사람이 이어받는 것으로 풀린다.
        # 반납했든, 리스가 만료됐든, 스트림이 끊겼든 — 팔에 일어난 일이 아니라 조작면에
        # 일어난 일이고, 읽을 현장이 없다. 지금 확인 체크를 한 사람이 바로 여기 있다.
        #
        # 처음에는 반납만 이렇게 두었다. 그랬더니 앞 사람의 창이 그냥 닫힌 경우(그쪽이
        # 훨씬 흔하다) 새 사람은 권한을 받고도 움직일 수 없었다 — 화면에는 "1524ms 동안
        # 통과한 명령이 없습니다"라고만 적혀 있었고, 그것은 다음 사람이 확인할 만한
        # 내용이 아니다.
        #
        # 그 밖의 이유로 선 것은 풀지 않는다. 누가 정지를 눌렀거나 무언가에 닿았거나 모터가
        # 뜨거웠던 것이고, 그 이유는 다음 사람이 **읽고** 확인해야 한다. 권한을 새로 받는
        # 것으로 조용히 지워지면, 멈춘 이유를 아무도 보지 않은 채 다시 움직이게 된다.
        owner = vleader.owner
        if owner is not None and owner.state == State.HOLD:
            fault = owner.snapshot().get("fault") or {}
            if fault.get("code") in OPERATOR_GONE:
                owner.resume()
        return lease.as_dict()

    @router.post("/lease/{lease_id}/heartbeat")
    def heartbeat(request: Request, lease_id: str) -> dict[str, object]:
        _authorise_motion(_token_from(request))
        try:
            return vleader.authority.renew(lease_id).as_dict()
        except RejectError as exc:
            raise HTTPException(status_code=409, detail=exc.detail) from exc

    @router.delete("/lease/{lease_id}")
    def release(request: Request, lease_id: str) -> dict[str, object]:
        _authorise_motion(_token_from(request))
        released = vleader.authority.release(lease_id)
        owner = vleader.owner
        if released and owner is not None:
            # 반납했다고 팔을 떨어뜨리지 않는다. 지금 자세에서 선다.
            owner.hold(Trip.LEASE_RELEASED)
        return {"released": released}

    @router.get("/policy")
    def read_policy() -> dict[str, object]:
        return {
            "policy": vleader.policy.as_dict(),
            # 지금 값이 어느 프로필인가. 손으로 하나만 바꿔 두었으면 `null`이고, 화면은
            # 그때 "직접 정한 값"이라고 말해야 한다 — 세 칸 중 하나를 켜 두면 실제와
            # 다른 말을 하게 된다.
            "profile": profile_of(vleader.policy),
            "profiles": [
                {
                    "name": name,
                    "title": PROFILE_KOREAN[name][0],
                    "detail": PROFILE_KOREAN[name][1],
                    "values": values,
                }
                for name, values in PROFILES.items()
            ],
            "tunable": {
                name: {"min": low, "max": high, "integer": cast is int, "env": variable}
                for name, (low, high, cast, variable) in TUNABLES.items()
            },
        }

    @router.post("/policy")
    def write_policy(request: Request, body: PolicyRequest) -> dict[str, object]:
        """조작감과 민감도를 바꾼다.

        **팔이 움직이는 중에는 받지 않는다.** 틱당 상한이 커지는 순간 목표가 한 번에
        멀어지고, 그러면 팔이 튀어 나간다. 세워 두고 바꾸는 것이 맞다.
        """
        _authorise_motion(_token_from(request))
        owner = vleader.owner
        if owner is not None and owner.state == State.ACTIVE:
            raise HTTPException(
                status_code=409,
                detail="팔이 움직이는 중에는 바꿀 수 없습니다. 먼저 정지하세요.",
            )
        applied: dict[str, float] = {}
        if body.profile is not None:
            if body.profile not in PROFILES:
                raise HTTPException(
                    status_code=400,
                    detail=f"모르는 조작감입니다: {body.profile} (있는 것: {', '.join(PROFILES)})",
                )
            applied.update(PROFILES[body.profile])
        for name, raw in (body.values or {}).items():
            if name not in TUNABLES:
                raise HTTPException(status_code=400, detail=f"바꿀 수 없는 값입니다: {name}")
            low, high, cast, _ = TUNABLES[name]
            try:
                value = cast(raw)
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=400, detail=f"{name}: 숫자가 아닙니다") from exc
            if not low <= value <= high:
                raise HTTPException(
                    status_code=400,
                    detail=f"{name}은(는) {low}에서 {high} 사이여야 합니다 (받은 값 {value})",
                )
            applied[name] = value
        if applied:
            vleader.retune(applied)
        problem = _persist_tunables(applied, profile_of(vleader.policy))
        return {
            "policy": vleader.policy.as_dict(),
            "profile": profile_of(vleader.policy),
            "applied": applied,
            "save_error": problem,
        }

    @router.post("/hold")
    def hold() -> dict[str, object]:
        """리스가 없어도 누구나 부를 수 있는 정지.

        토큰도 요구하지 않는다. 폰이 맥을 멈출 수 있어야 하고, 멈추는 것은 권한을 빼앗는
        것이 아니다. 이것으로 토크가 풀리지는 않는다 — 자세를 유지한 채 선다.
        """
        owner = vleader.require_owner()
        owner.hold(Trip.OPERATOR_HOLD)
        return owner.snapshot()

    @router.post("/resume")
    def resume(request: Request) -> dict[str, object]:
        """멈춘 이유를 사람이 확인했다. 다음 명령부터 다시 받는다."""
        _authorise_motion(_token_from(request))
        owner = vleader.require_owner()
        owner.resume()
        return owner.snapshot()

    @router.get("/goal")
    def goal() -> dict[str, object]:
        """수집 중 `lerobot-record`가 가져가는 목표. 읽기 전용이라 토큰을 묻지 않는다."""
        return vleader.goal()

    @router.websocket("/stream")
    async def stream(socket: WebSocket) -> None:
        """관찰과 조작이 함께 흐르는 하나의 연결.

        붙는 데는 아무 권한도 필요 없다 — 관찰은 배타적이지 않다. 조작은 이 연결로 오는
        `command` 메시지에 유효한 `lease_id`가 실려 있을 때만 통한다. 즉 권한 검사는
        연결이 아니라 명령 하나하나에 붙어 있다.
        """
        await socket.accept()
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue(maxsize=1)

        def publish(snapshot: dict[str, object]) -> None:
            # 제어 루프 스레드에서 불린다. 큐가 차 있으면 오래된 프레임을 버린다 —
            # 밀린 텔레메트리를 순서대로 다 보내면 화면이 과거를 그리게 된다.
            def put() -> None:
                if queue.full():
                    try:
                        queue.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                queue.put_nowait(snapshot)

            try:
                loop.call_soon_threadsafe(put)
            except RuntimeError:
                pass

        vleader.add_listener(publish)
        await socket.send_json({"type": "hello", **vleader.status()})

        async def pump() -> None:
            while True:
                snapshot = await queue.get()
                await socket.send_json({"type": "telemetry", **snapshot})

        async def idle() -> None:
            # 제어 루프가 돌지 않는 동안에도 화면은 상태를 알아야 한다. 루프가 있으면
            # 그쪽이 30Hz로 밀어 주므로 이 느린 맥박은 조용히 남는다.
            while True:
                await asyncio.sleep(0.5)
                if vleader.owner is None or not vleader.owner.running:
                    await socket.send_json({"type": "telemetry", **vleader.status()})

        pumping = asyncio.gather(pump(), idle())
        try:
            while True:
                message = await socket.receive_json()
                kind = message.get("type")
                if kind == "command":
                    live = vleader.owner
                    if live is None:
                        await socket.send_json(
                            {
                                "type": "reject",
                                "sequence": message.get("sequence"),
                                "code": Reject.HARDWARE_NOT_READY,
                                "message": KOREAN[Reject.HARDWARE_NOT_READY],
                            }
                        )
                        continue
                    try:
                        result = await asyncio.to_thread(
                            live.submit,
                            payload=message.get("joints"),
                            lease_id=message.get("lease_id"),
                            sequence=message.get("sequence"),
                            valid_for_ms=message.get("valid_for_ms"),
                            observation=message.get("observation"),
                        )
                        await socket.send_json({"type": "ack", **result})
                    except RejectError as exc:
                        await socket.send_json(
                            {"type": "reject", "sequence": message.get("sequence"), **exc.as_dict()}
                        )
                    except HardwareError as exc:
                        await socket.send_json(
                            {
                                "type": "reject",
                                "sequence": message.get("sequence"),
                                "code": Reject.HARDWARE_NOT_READY,
                                "message": str(exc),
                            }
                        )
                elif kind == "heartbeat":
                    try:
                        lease = vleader.authority.renew(message.get("lease_id", ""))
                        await socket.send_json({"type": "lease", **lease.as_dict()})
                    except RejectError as exc:
                        await socket.send_json({"type": "reject", **exc.as_dict()})
                elif kind == "hold":
                    live = vleader.owner
                    if live is not None:
                        live.hold(Trip.OPERATOR_HOLD)
                elif kind == "ping":
                    await socket.send_json({"type": "pong"})
        except WebSocketDisconnect:
            pass
        except Exception:  # noqa: BLE001
            logger.debug("virtual leader socket ended", exc_info=True)
        finally:
            pumping.cancel()
            vleader.remove_listener(publish)
            # 연결이 사라졌다고 리스를 자동으로 회수하지는 않는다 — 잠깐 끊겼다가 돌아오는
            # 경우가 있고, 그 사이에도 팔은 워치독 때문에 이미 HOLD다. 회수는 만료가 한다.

    return router
