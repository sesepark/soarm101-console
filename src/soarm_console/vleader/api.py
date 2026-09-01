from __future__ import annotations

import asyncio
import logging
import os
import secrets
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from ..calibration import validate_calibration
from ..config import Settings
from ..diagnostics import inspect_arm
from .authority import AuthorityManager, LeaseConflict
from .backend import HardwareError
from .owner import State, VirtualLeaderOwner
from .safety import KOREAN, Reject, RejectError, Trip, VLeaderSettings
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


class LeaseRequest(BaseModel):
    #: 조작 권한을 받는 순간의 확인 문구. 토크를 거는 자리에도 같은 것을 요구하지만,
    #: 그것만으로는 모자란다 — 토크가 이미 걸려 있으면 그 게이트를 지나치게 되고,
    #: 그러면 먼저 켜 둔 사람이 있는 팔에 아무나 문구 없이 붙을 수 있다.
    #: **팔이 움직일 수 있게 되는 순간**은 리스를 받는 순간이므로 게이트는 여기에도 있다.
    confirmation: str = ""
    holder: str = "unknown"
    session_id: str = ""


class VirtualLeader:
    """가상 리더 하나. 앱 모듈이 이것 하나만 들고 있으면 된다."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.policy = VLeaderSettings()
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
            if not report.safe_for_motion_start:
                raise HardwareError(
                    "Follower motors still have torque enabled; the read-only doctor will not start motion from there"
                )
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
        self._owner = None
        if owner is not None:
            owner.stop(force=force)

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
        if self._owner is None or not self._owner.running:
            raise HTTPException(status_code=409, detail="Virtual leader is not running")
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
        # 앞 사람이 반납해서 선 것이라면 새 사람이 이어받는 것으로 풀린다. 그것은 고장이
        # 아니라 정상적인 교대이고, 지금 문구를 손으로 옮겨 적은 사람이 바로 여기 있다.
        #
        # 그 밖의 이유로 선 것은 풀지 않는다. 누가 정지를 눌렀거나 무언가에 닿았거나 모터가
        # 뜨거웠던 것이고, 그 이유는 다음 사람이 **읽고** 확인해야 한다. 권한을 새로 받는
        # 것으로 조용히 지워지면, 멈춘 이유를 아무도 보지 않은 채 다시 움직이게 된다.
        owner = vleader.owner
        if owner is not None and owner.state == State.HOLD:
            fault = owner.snapshot().get("fault") or {}
            if fault.get("code") == Trip.LEASE_RELEASED:
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
