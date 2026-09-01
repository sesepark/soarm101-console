from __future__ import annotations

import os
import random
import threading
import time
from typing import Protocol

from .spec import JointSpec


class FollowerBackend(Protocol):
    """가상 리더가 팔로워를 만지는 유일한 통로.

    실물과 흉내를 같은 모양으로 둔 이유는, 리스 경쟁·워치독·검증기·재연결처럼 팔을
    움직이지 않고도 확인할 수 있는 것을 **실제로 걸어 보기** 위해서다. 이 프로젝트에서
    팔이 실제로 움직이는 시험은 사람이 현장에 있을 때만 한다.
    """

    def connect(self) -> None: ...
    def disconnect(self) -> None: ...
    def apply_speed_limit(self, settings) -> None: ...
    def read(self, include_health: bool) -> dict[str, dict[str, float]]: ...
    def write(self, goal: dict[str, float]) -> dict[str, float]: ...
    def set_torque(self, enabled: bool) -> None: ...
    @property
    def torque_enabled(self) -> bool: ...


class HardwareError(RuntimeError):
    pass


class RealFollower:
    """LeRobot의 `SOFollower`를 in-process로 쥔다.

    `lerobot-teleoperate`를 서브프로세스로 띄우던 기존 텔레옵과 결정적으로 다른 점이다.
    서브프로세스는 물리 리더 팔에서 읽은 값을 그대로 흘려보낼 뿐이라 바깥에서 목표를
    넣을 자리가 없다. 여기서는 우리가 루프를 돌리므로 목표를 검증한 뒤 넣을 수 있다.

    장치 소유자는 여전히 하나다 — 이 백엔드가 살아 있는 동안 `lerobot-teleoperate`와
    `lerobot-record`는 서버가 409로 막는다(ADR 0001).
    """

    def __init__(self, port: str, robot_id: str, max_relative_target: float, specs: list[JointSpec]):
        self.port = port
        self.robot_id = robot_id
        self.max_relative_target = max_relative_target
        self.specs = {spec.name: spec for spec in specs}
        self._robot = None
        self._torque = False
        #: 서보에서 되읽은 속도 상한(눈금/초). 화면이 "정말로 걸렸는가"를 말할 수 있게 한다.
        self.speed_ticks: dict[str, int] = {}

    def connect(self) -> None:
        """팔로워에 붙는다. **붙었다는 이유로 토크가 바뀌지 않게** 한다.

        `SOFollower.connect()`를 그대로 부르면 안 된다. 그 안의 `configure()`가
        `with bus.torque_disabled():`를 쓰는데, 이 컨텍스트 관리자는 빠져나오면서 토크를
        **켠다.** 그래서 관찰만 하려고 시작해도 팔이 뻣뻣해졌고, 그러면 확인을 요구하는
        `arm` 게이트를 지나지 않고도 팔이 명령을 받을 수 있는 상태가 된다 — 게이트를
        우회하는 길을 열어 둔 셈이다. 더 나쁜 것은 그 순간 서보가 지난번 `Goal_Position`을
        향해 스스로 달린다는 점이다.

        그래서 `connect()` 대신 그 안에서 하는 일을 직접 하고, 그 사이에 우리 손을 넣는다:
        붙기 전의 토크 상태를 기억했다가 원래대로 돌려놓고, 토크가 켜지기 전에 지금 자세를
        목표로 먼저 써 넣는다. 이미 토크가 걸려 있던 팔은 그대로 둔다 — 들려 있던 팔을
        여기서 놓으면 떨어진다.
        """
        # 상태 패킷 하나가 깨졌다고 시작이 통째로 실패하지 않게 한 번 더 해 본다. 실제로
        # `Failed to write 'Lock' on id_=5 ... There is no status packet!` 한 번에
        # 관찰 시작이 막혔고, 화면에는 이유 없는 500만 떴다. 판독값이 튀듯 쓰기도 튄다.
        failure: Exception | None = None
        for _ in range(2):
            try:
                self._connect_once()
                return
            except HardwareError:
                raise
            except ConnectionError as exc:
                failure = exc
        raise HardwareError(f"Follower bus did not answer while connecting: {failure}")

    def _connect_once(self) -> None:
        # import를 함수 안에 두는 이유: 콘솔 웹 서버는 LeRobot을 쓰지 않고도 떠야 한다.
        # 모듈을 읽는 것만으로 torch까지 끌려 들어오면 상태 화면이 그만큼 늦게 뜬다.
        from lerobot.robots.so_follower import SOFollower, SOFollowerRobotConfig

        config = SOFollowerRobotConfig(
            port=self.port,
            id=self.robot_id,
            max_relative_target=self.max_relative_target,
            # 도(degree) 단위. calibration에서 계산한 절대 한계와 같은 단위여야 한다.
            use_degrees=True,
            # 연결을 끊는다고 토크를 끄지 않는다. 끄면 팔이 떨어진다 — SAFETY.md가
            # 토크 해제를 모든 고장의 기본 동작으로 삼지 말라고 적어 둔 이유다.
            disable_torque_on_disconnect=False,
            cameras={},
        )
        robot = SOFollower(config)
        robot.bus.connect()
        try:
            # calibration을 여기서 맞춘다. `SOFollower.connect(calibrate=True)`는 모터에
            # 적힌 값이 파일과 다를 때 `input()`으로 사람을 기다리는데, 웹 서버 안에서
            # 그 자리는 곧 무한 대기다.
            if not robot.bus.is_calibrated:
                if not robot.calibration:
                    raise HardwareError(
                        "Follower calibration file is missing; run scripts/calibrate_follower.sh"
                    )
                robot.bus.write_calibration(robot.calibration)

            # 붙기 전의 상태를 먼저 읽는다. 이 값을 놓치면 `configure()`가 켠 토크와
            # 사람이 걸어 둔 토크를 구별할 수 없다.
            try:
                torque = robot.bus.sync_read("Torque_Enable", normalize=False, num_retry=2)
                was_enabled = any(int(value) != 0 for value in torque.values())
            except ConnectionError as exc:  # 읽지 못하면 걸려 있다고 보는 쪽이 안전하다
                was_enabled = True

            # 토크가 켜지기 전에 지금 자세를 목표로 박아 둔다. 이러지 않으면 켜지는 순간
            # 서보가 지난번 목표까지 스스로 달린다.
            present = robot.bus.sync_read("Present_Position", normalize=False, num_retry=2)
            robot.bus.sync_write("Goal_Position", present, normalize=False, num_retry=2)

            robot.configure()  # 여기서 토크가 켜진다 — LeRobot의 동작이다

            if not was_enabled:
                # 원래 꺼져 있던 팔이다. 관찰을 시작했다는 이유로 켜 두지 않는다.
                # 목표를 지금 자세로 맞춰 두었으므로 끄는 동안 팔은 그 자리에 있다.
                robot.bus.disable_torque()
            self._torque = was_enabled
        except BaseException:
            try:
                robot.bus.disconnect()
            except Exception:  # noqa: BLE001 - 정리하는 길은 어떤 이유로도 막히지 않는다
                pass
            raise
        self._robot = robot

    def disconnect(self) -> None:
        robot, self._robot = self._robot, None
        if robot is None:
            return
        try:
            robot.disconnect()
        except Exception:  # noqa: BLE001 - 내려가는 길은 어떤 이유로도 막히지 않아야 한다
            pass

    @property
    def torque_enabled(self) -> bool:
        return self._torque

    def set_torque(self, enabled: bool) -> None:
        """토크를 걸거나 푼다.

        거는 쪽에는 순서가 있다. **지금 자세를 목표로 먼저 써 넣고 그다음에 토크를 건다.**
        서보의 `Goal_Position`에는 지난번에 쓴 값이 그대로 남아 있고, 토크가 걸리는 순간
        서보는 그 값을 향해 스스로 달린다 — 우리 제어 루프가 첫 목표를 쓰기까지의 한 틱이
        그 틈이다. 33ms지만 전속력이다.

        토크가 꺼진 상태에서도 `Goal_Position`은 쓸 수 있다. 써 두면 토크를 거는 순간
        서보가 겨냥하는 곳이 지금 있는 자리가 되어, 팔은 제자리에 선 채로 뻣뻣해진다.
        """
        if self._robot is None:
            raise HardwareError("Follower is not connected")
        bus = self._robot.bus
        if enabled:
            try:
                present = bus.sync_read("Present_Position", num_retry=2)
                bus.sync_write("Goal_Position", present)
            except Exception as exc:  # noqa: BLE001 - 겨냥할 곳을 모르면 토크를 걸지 않는다
                raise HardwareError(f"Could not aim the servos at their present position: {exc}") from exc

        # **한 번 더 해 본다.** Feetech 버스는 상태 패킷 하나가 깨지는 일이 드물지 않고,
        # 그때 LeRobot은 `ConnectionError`를 올린다. 붙을 때(`connect`)는 이미 다시
        # 시도하고 있었는데 여기는 그러지 않았다 — 2026-09-02에 `Failed to write 'Lock'
        # on id_=1 ... There is no status packet!` 한 번으로 토크 걸기가 실패했다.
        #
        # 더 나쁜 것은 그 예외의 **종류**였다. `ConnectionError`는 제어 루프의
        # `except HardwareError`에 걸리지 않아 스레드를 통째로 죽였고, 죽은 루프는
        # serial과 owner lock을 쥔 채 남았다. 화면은 `Virtual leader is not running`만
        # 말했고 다시 시작할 길이 없었다. 그래서 여기서 우리 예외로 바꿔 올린다.
        failure: Exception | None = None
        for _ in range(3):
            try:
                if enabled:
                    bus.enable_torque()
                else:
                    bus.disable_torque()
                self._torque = enabled
                return
            except Exception as exc:  # noqa: BLE001 - 버스 오류는 종류를 가리지 않는다
                failure = exc
        raise HardwareError(
            f"Could not {'enable' if enabled else 'disable'} torque: {failure}"
        ) from failure

    def apply_speed_limit(self, settings) -> None:
        """속도 상한을 **서보 안에** 세운다.

        STS3215의 `Goal_Velocity`(주소 46)는 위치 모드에서 "목표까지 이 속도로 간다"는
        뜻이고, 0이면 제한 없음이다(공장 기본값). 여기에 값을 써 넣으면 서보 안의 궤적
        생성기가 스스로 속도를 지키므로, 우리는 목표를 **절대 자세 그대로** 던져 놓고
        속도 걱정을 놓을 수 있다.

        이것이 왜 중요한가. 예전에는 우리가 목표를 조금씩 앞세우는 방식으로 속도를
        만들었고, 그 "조금"이 동시에 서보가 보는 위치 오차 — 즉 힘 — 이었다. 그래서
        느리게 하면 팔이 약해졌다(2°에서 어깨가 팔을 들지 못했다). 속도를 서보에게
        맡기고 나면 두 값이 서로를 붙잡지 않는다.

        눈금은 실측했다(2026-09-02, 집게). `Goal_Velocity` 한 칸이 초당 위치 눈금 하나이고,
        위치 눈금 하나는 360/4096도다. 200 → 21°/s, 500 → 47°/s, 1000 → 93°/s,
        2000 → 150°/s에서 이 팔의 천장에 닿았다.

        `Acceleration`(주소 41)도 함께 만져 보았으나 이 펌웨어(3.9)에서는 값을 바꿔도
        도달 시간이 달라지지 않았다. 그래서 건드리지 않고 둔다 — 효과가 없는 쓰기를
        루프에 남겨 두면 나중에 그것이 무언가를 지켜 준다고 믿게 된다.
        """
        if self._robot is None:
            raise HardwareError("Follower is not connected")
        bus = self._robot.bus
        values = {spec.name: settings.ticks_per_second(spec) for spec in self.specs.values()}
        try:
            bus.sync_write("Goal_Velocity", values, normalize=False, num_retry=2)
            # **되읽어 확인한다.** 이 한 번의 쓰기가 지금 이 팔의 속도 상한 전부이고,
            # 실패하면 서보는 "제한 없음"(0)으로 남는다. 그 차이는 화면에서 보이지
            # 않으므로 — 팔은 여전히 목표를 따라가고, 다만 최고 속도로 간다 — 여기서
            # 확인하지 않으면 아무도 알아채지 못한다.
            written = bus.sync_read("Goal_Velocity", normalize=False, num_retry=2)
            self.speed_ticks = {name: int(value) for name, value in written.items()}
        except Exception as exc:  # noqa: BLE001
            raise HardwareError(str(exc)) from exc
        wrong = [name for name, want in values.items() if self.speed_ticks.get(name) != want]
        if wrong:
            raise HardwareError(
                "속도 상한이 서보에 들어가지 않았습니다: " + ", ".join(sorted(wrong))
            )
        self.max_relative_target = settings.lead_deg
        # LeRobot의 `send_action`이 쓰는 상한도 함께 옮긴다. 두 겹으로 자르는 것이
        # 이 사다리의 요점인데, 한쪽만 바뀌면 겹이 아니라 어긋남이 된다.
        self._robot.config.max_relative_target = settings.lead_deg

    def read(self, include_health: bool) -> dict[str, dict[str, float]]:
        """관절 상태.

        `include_health`가 거짓이면 위치만 읽는다. Feetech 버스에서 `sync_read` 한 번은
        6개 모터 기준 몇 밀리초가 걸리고, 30Hz에서 위치·부하·전류·온도를 매 틱마다 다
        읽으면 예산을 넘긴다. 부하와 온도는 우리가 보는 창(300~400ms 연속 초과)보다 훨씬
        빠르게 변하지 않으므로 10Hz로 충분하다.
        """
        if self._robot is None:
            raise HardwareError("Follower is not connected")
        bus = self._robot.bus
        try:
            frame = {"position": dict(bus.sync_read("Present_Position", num_retry=2))}
            if include_health:
                frame["load"] = dict(bus.sync_read("Present_Load", normalize=False, num_retry=2))
                frame["current"] = dict(bus.sync_read("Present_Current", normalize=False, num_retry=2))
                frame["temperature"] = dict(
                    bus.sync_read("Present_Temperature", normalize=False, num_retry=2)
                )
        except Exception as exc:  # noqa: BLE001 - 버스 오류는 종류를 가리지 않고 정지 사유다
            raise HardwareError(str(exc)) from exc
        return frame

    def write(self, goal: dict[str, float]) -> dict[str, float]:
        if self._robot is None:
            raise HardwareError("Follower is not connected")
        if not self._torque:
            raise HardwareError("Torque is disabled; the arm cannot follow a goal")
        try:
            # LeRobot의 `send_action`을 그대로 쓴다. 우리 쪽에서 이미 틱당 변화량을 잘랐지만
            # 여기서 `max_relative_target`이 한 번 더 자른다. 두 겹인 것이 맞다 — 한쪽을
            # 잘못 고쳐도 다른 쪽이 남는 것이 이 사다리의 요점이다.
            sent = self._robot.send_action({f"{name}.pos": value for name, value in goal.items()})
        except Exception as exc:  # noqa: BLE001
            raise HardwareError(str(exc)) from exc
        return {key.removesuffix(".pos"): float(value) for key, value in sent.items()}


class SimulatedFollower:
    """모터 없이 도는 팔로워.

    검증용이다. `SOARM_VL_BACKEND=simulated`일 때만 쓰이고, 실제 운용에서는 켜지지
    않는다. 목표를 향해 틱당 한계만큼 따라가고, 설정한 각도를 넘어서면 "책상에 닿은 것"처럼
    더 가지 않으면서 부하만 올린다 — 접촉 트립과 물러남을 팔 없이 걸어 보기 위한 것이다.
    """

    def __init__(self, specs: list[JointSpec], step: float = 2.0):
        self.specs = {spec.name: spec for spec in specs}
        self.step = step
        #: 틱당 갈 수 있는 거리. `apply_speed_limit`이 채운다.
        self.speed: dict[str, float] = {}
        self.speed_ticks: dict[str, int] = {}
        self._lock = threading.Lock()
        self._position = {name: 0.0 for name in self.specs}
        self._goal = dict(self._position)
        self._torque = False
        self._connected = False
        self._temperature = {name: 32.0 for name in self.specs}
        #: 이 관절이 이 값을 넘어가려 하면 막힌 것으로 흉내 낸다. 비어 있으면 막힘 없음.
        self.obstacle: dict[str, float] = {}
        for raw in os.getenv("SOARM_VL_SIM_OBSTACLE", "").split(","):
            if ":" in raw:
                name, _, value = raw.partition(":")
                try:
                    self.obstacle[name.strip()] = float(value)
                except ValueError:
                    continue

    def connect(self) -> None:
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    @property
    def torque_enabled(self) -> bool:
        return self._torque

    def set_torque(self, enabled: bool) -> None:
        if not self._connected:
            raise HardwareError("Simulated follower is not connected")
        if enabled:
            # 실물과 같은 성질: 토크를 거는 순간 서보가 겨냥하는 곳은 지금 있는 자리다.
            with self._lock:
                self._goal = dict(self._position)
        self._torque = enabled

    def apply_speed_limit(self, settings) -> None:
        """흉내 백엔드에서도 속도는 서보가 지키는 것처럼 행동한다.

        틱마다 갈 수 있는 거리를 속도에서 계산한다. 그러지 않으면 시험 안의 팔만
        순간이동하고, 정작 확인하려는 것(목표가 절대값이어도 팔은 천천히 간다)이
        시험에서 사라진다.
        """
        self.speed = {
            name: settings.speed(spec) / max(1, settings.hz)
            for name, spec in self.specs.items()
        }
        self.speed_ticks = {
            name: settings.ticks_per_second(spec) for name, spec in self.specs.items()
        }

    def read(self, include_health: bool) -> dict[str, dict[str, float]]:
        if not self._connected:
            raise HardwareError("Simulated follower is not connected")
        with self._lock:
            frame: dict[str, dict[str, float]] = {"position": dict(self._position)}
            if include_health:
                load = {}
                current = {}
                for name in self.specs:
                    blocked = self._blocked(name)
                    load[name] = 520.0 if blocked else random.uniform(20.0, 90.0)
                    current[name] = 140.0 if blocked else random.uniform(5.0, 25.0)
                frame["load"] = load
                frame["current"] = current
                frame["temperature"] = dict(self._temperature)
            return frame

    def write(self, goal: dict[str, float]) -> dict[str, float]:
        if not self._connected:
            raise HardwareError("Simulated follower is not connected")
        if not self._torque:
            raise HardwareError("Torque is disabled; the arm cannot follow a goal")
        with self._lock:
            sent = {}
            for name, value in goal.items():
                spec = self.specs[name]
                current = self._position[name]
                cap = self.speed.get(name, self.step)
                delta = max(-cap, min(cap, value - current))
                target = spec.clamp(current + delta)
                limit = self.obstacle.get(name)
                if limit is not None and target > limit:
                    target = limit  # 책상에 닿았다. 목표는 계속 멀어지지만 위치는 여기서 선다.
                self._position[name] = target
                self._goal[name] = value
                sent[name] = value
            return sent

    def _blocked(self, name: str) -> bool:
        limit = self.obstacle.get(name)
        return limit is not None and self._goal.get(name, 0.0) > limit + 0.5

    # 시험에서 손으로 팔을 움직인 것처럼 만들 때 쓴다.
    def nudge(self, name: str, value: float) -> None:
        with self._lock:
            self._position[name] = self.specs[name].clamp(value)

    def heat(self, name: str, celsius: float) -> None:
        with self._lock:
            self._temperature[name] = celsius


def make_backend(
    *, port: str, robot_id: str, max_relative_target: float, specs: list[JointSpec]
) -> FollowerBackend:
    if os.getenv("SOARM_VL_BACKEND", "real").strip().lower() == "simulated":
        return SimulatedFollower(specs, step=max_relative_target)
    return RealFollower(port, robot_id, max_relative_target, specs)
