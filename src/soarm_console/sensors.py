"""수집이 남기는 서보 판독값 — 위치 말고 나머지 전부.

왜 있는가. `observation.state`는 관절 여섯의 **위치**만 담는다. 그런데 서보는 매 틱마다
자기가 얼마나 힘을 쓰고 있는지(`Present_Load`), 얼마나 빨리 도는지, 몇 도인지, 전압이
얼마인지, 과부하·과열 비트가 섰는지를 함께 들고 있다. 그 값들은 읽지 않으면 사라진다 —
시연은 다시 찍을 수 없으므로, 나중에 "집게가 물체를 물었을 때 부하가 어떻게 변했나"를
묻고 싶다면 지금 적어 두는 수밖에 없다. 목적은 모터 부하로 간접 촉각을 추정하는 연구다.

두 가지 원칙이 이 파일의 모양을 정한다.

**하나. 새 값은 `observation.state`에 섞지 않는다.** 별도 열로 넣는다. `observation.state`가
여섯에서 마흔둘로 늘어나면 지금까지 찍은 데이터로 배운 정책도, 사전학습 정규화 통계도
모두 모양이 어긋난다. 정책은 자기가 모르는 열을 그냥 지나치므로(ACT의 `robot_state_feature`는
이름이 `observation.state`인 열 **하나만** 집는다), 별도 열은 기존 학습에 아무 일도 하지
않는다.

**둘. 서보 읽기는 틱당 블록 하나다.** STS3215의 주소 56~70에 위치·속도·부하·전압·온도·
상태·이동·전류가 이어져 있다. 레지스터마다 `sync_read`를 부르면 한 틱에 버스 왕복이
일곱 번이고, 30Hz(33ms)에서 그것은 데이터의 시간축을 늘린다 — 늘어난 시간축은 파케이에
남지도 않는다(`timestamp`는 `frame_index / fps`로 합성된 값이다). 그래서 한 번에 15바이트를
읽어 우리가 쪼갠다.

부호는 우리가 직접 풀지 않고 `MotorsBus._decode_sign`에 맡긴다. 가상 리더 백엔드가
`sync_read`로 부하를 읽을 때 지나는 바로 그 코드다. 두 곳이 다르게 풀면 같은 힘이 다른
숫자가 되고, 어느 쪽이 맞는지는 데이터만 봐서는 알 수 없다.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Any


#: 이 팔의 모터 순서. `SOFollower`가 버스를 세우는 순서 그대로이고(`so_follower.py`),
#: 따라서 `observation.state`의 이름 순서와도 같다. 열마다 이름이 다르게 늘어서면 6번째
#: 값이 어느 관절의 것인지 열마다 달라진다.
MOTORS = ("shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll", "gripper")

#: 카메라 역할. `observation.camera_fresh`의 열 순서다.
CAMERA_KEYS = ("scene", "wrist")

#: 이 열들의 뜻이 바뀌면 올린다. `soarm_provenance.json`에 함께 적히므로, 나중에 읽는
#: 쪽이 "이 데이터의 load는 어떤 단위였나"를 파일 하나로 답할 수 있다.
EXTRAS_SCHEMA = 1

#: 한 번에 읽는 구간. 56(Present_Position)에서 70(Present_Current의 둘째 바이트)까지다.
#: 위치도 구간 안에 들지만 쓰지는 않는다 — `observation.state`는 LeRobot이 자기 읽기로
#: 채우고, 같은 값을 두 곳에서 서로 다른 시각에 읽어 두면 둘이 어긋났을 때 설명할 길이 없다.
BLOCK_ADDRESS = 56
BLOCK_LENGTH = 15


@dataclass(frozen=True)
class Register:
    """관절 여섯 개짜리 열 하나와, 그것이 서보 어디에서 오는지."""

    #: 데이터셋 열 이름.
    column: str
    #: 관절 이름 뒤에 붙는 마디(`shoulder_pan.load`의 `load`).
    suffix: str
    #: `_decode_sign`이 부호 표를 찾는 이름. 반드시 LeRobot의 control table 이름이어야 한다.
    data_name: str
    address: int
    length: int
    #: 저장하기 전에 곱하는 값. 전압만 1/10이다(서보는 0.1V 단위로 답한다).
    scale: float = 1.0
    unit: str = ""
    note: str = ""


#: 관절마다 여섯 개씩 나오는 열들. 순서가 곧 `info.json`의 열 순서다.
REGISTERS = (
    Register(
        "observation.load", "load", "Present_Load", 60, 2,
        unit="-1000..1000", note="부호 포함. 서보가 지금 쓰고 있는 힘.",
    ),
    Register(
        "observation.velocity", "vel", "Present_Velocity", 58, 2,
        unit="ticks/s", note="부호 포함. 서보 눈금 단위 그대로.",
    ),
    Register(
        "observation.temperature", "temp", "Present_Temperature", 63, 1,
        unit="°C", note="STS3215는 과열로 스스로 토크를 끊는다.",
    ),
    Register(
        "observation.voltage", "volt", "Present_Voltage", 62, 1, scale=0.1,
        unit="V", note="서보는 0.1V 단위로 답한다.",
    ),
    Register(
        "observation.servo_status", "status", "Status", 65, 1,
        unit="bitmask", note="과부하·과열 비트를 담은 상태 바이트 그대로.",
    ),
    Register(
        "observation.servo_moving", "moving", "Moving", 66, 1,
        unit="0/1", note="서보가 스스로 '움직이는 중'이라고 말하는가.",
    ),
    Register(
        "observation.current", "current", "Present_Current", 69, 2,
        unit="raw",
        note="이 펌웨어(3.9)에서는 0이나 1로만 읽힌다(2026-09-05 확인). 그래도 남긴다 — "
             "값이 없다는 사실 자체가 기록이다.",
    ),
)

#: 관절에 매이지 않은 두 열.
WALL_TIME_COLUMN = "observation.wall_time"
WALL_TIME_NAME = "since_start"
CAMERA_FRESH_COLUMN = "observation.camera_fresh"

#: `observation.camera_fresh`의 값이 관측 dict에 실려 오는 자리.
#:
#: 나머지 여덟 열은 `build_dataset_frame`이 `names`를 키로 관측 dict에서 값을 모아 가므로
#: 그냥 `obs["shoulder_pan.load"] = ...`처럼 넣으면 된다. 이 열만 그럴 수 없다: 이름이
#: `scene`·`wrist`인데 그 두 키에는 이미 카메라가 준 **그림**이 들어 있다(`SOFollower`가
#: `obs_dict[cam_key] = cam.read_latest()`로 넣는다). 이름을 바꾸면 열이 무엇을 가리키는지
#: 흐려지고, 그림을 덮으면 영상이 사라진다. 그래서 이 열만 열 이름 자체를 키로 써서 옆으로
#: 실어 나르고, `recording._build_dataset_frame_with_sensors`가 그 자리에서 꺼낸다.
CAMERA_FRESH_KEY = CAMERA_FRESH_COLUMN

#: 이름으로 모을 수 없어 따로 채우는 열들.
SIDE_CHANNEL_COLUMNS = (CAMERA_FRESH_COLUMN,)


def extra_features() -> dict[str, dict[str, Any]]:
    """데이터셋 features에 더할 아홉 열.

    `dtype`이 `float32`이고 `shape`이 1차원이면 `build_dataset_frame`이 `names`를 키로
    값을 모아 채운다. 그래서 여기 적힌 이름이 곧 관측 dict에 넣어야 할 키다.
    """
    features: dict[str, dict[str, Any]] = {}
    for register in REGISTERS:
        features[register.column] = {
            "dtype": "float32",
            "shape": (len(MOTORS),),
            "names": [f"{motor}.{register.suffix}" for motor in MOTORS],
        }
    features[WALL_TIME_COLUMN] = {
        "dtype": "float32",
        "shape": (1,),
        "names": [WALL_TIME_NAME],
    }
    features[CAMERA_FRESH_COLUMN] = {
        "dtype": "float32",
        "shape": (len(CAMERA_KEYS),),
        "names": list(CAMERA_KEYS),
    }
    return features


def extra_columns() -> tuple[str, ...]:
    return tuple(extra_features())


def extra_suffixes() -> tuple[str, ...]:
    """열 이름의 마지막 마디. `/api/datasets`의 `extras`가 내보내는 것과 같은 목록."""
    return tuple(column.split(".")[-1] for column in extra_features())


class SensorReader:
    """틱마다 서보 블록을 한 번 읽어 관측 dict에 실을 값을 만든다.

    **읽기가 실패해도 수집은 멈추지 않는다.** `validate_frame`은 모든 열이 프레임에 있기를
    요구하므로 값을 빼놓을 수는 없다. 그래서 마지막으로 성공한 값을 그대로 한 번 더 쓰고
    실패를 센다 — 실패 수는 상태에 실려 사람이 볼 수 있다. 버스 패킷 하나가 깨졌다고 30초
    짜리 시연을 잃는 것이 훨씬 나쁘다.

    재시도는 하지 않는다(`num_retry=0`). 재시도 한 번은 타임아웃 한 번이고, 33ms 예산 안에서
    그것은 루프를 늦춘다. 늦은 루프는 파케이에 남지 않지만 데이터의 시간축은 실제로 늘어난다.
    """

    #: 읽기 시간 표본을 담아 두는 창. 상태에 평균을 싣기 위한 것이고, 루프 속도 창과 맞춘다.
    _SAMPLE_LIMIT = 128

    def __init__(self, bus: Any, started_at: float, motors: tuple[str, ...] = MOTORS) -> None:
        self._bus = bus
        self._started_at = float(started_at)
        self._motors = motors
        self._ids: list[int] = []
        if bus is not None:
            try:
                self._ids = [bus.motors[name].id for name in motors]
            except (AttributeError, KeyError, TypeError):
                self._ids = []
        #: 마지막으로 읽어 낸 값. 첫 읽기가 성공하기 전에는 0이다 — 그 0도 기록이며,
        #: `read_failures`가 몇 프레임이 그랬는지 말한다.
        self._values: dict[str, list[float]] = {
            register.column: [0.0] * len(motors) for register in REGISTERS
        }
        self.read_failures = 0
        self.reads = 0
        self._durations: deque[float] = deque(maxlen=self._SAMPLE_LIMIT)

    @property
    def enabled(self) -> bool:
        """블록을 실제로 읽을 수 있는가. 아니면 마지막 값(처음에는 0)만 실린다."""
        return self._bus is not None and bool(self._ids)

    def read_ms(self) -> float:
        """블록 읽기에 든 시간의 평균(ms). 표본이 없으면 0."""
        samples = list(self._durations)
        if not samples:
            return 0.0
        return 1000.0 * sum(samples) / len(samples)

    def observation_extras(
        self, position_read_at: float, camera_fresh: dict[str, bool]
    ) -> dict[str, Any]:
        """이번 틱의 관측 dict에 더할 값들.

        Args:
            position_read_at: 이 프레임의 `Present_Position`을 읽은 epoch 초.
            camera_fresh: 카메라 키마다 "이번 틱이 새 프레임인가".
        """
        self._refresh()
        extras: dict[str, Any] = {}
        for register in REGISTERS:
            for motor, value in zip(self._motors, self._values[register.column], strict=True):
                extras[f"{motor}.{register.suffix}"] = value
        # epoch을 그대로 넣지 않는다. float32는 유효숫자가 일곱 자리 남짓이라 1.75e9초는
        # 100초 단위까지밖에 담기지 못한다 — 30Hz로 찍은 프레임이 전부 같은 시각이 된다.
        # 기준 epoch은 `soarm_provenance.json`의 `started_at`에 적는다.
        extras[WALL_TIME_NAME] = float(position_read_at - self._started_at)
        extras[CAMERA_FRESH_KEY] = [
            1.0 if _camera_is_fresh(camera_fresh, key) else 0.0 for key in CAMERA_KEYS
        ]
        return extras

    def _refresh(self) -> None:
        if not self.enabled:
            return
        started = time.perf_counter()
        try:
            ok = self._read_block()
        except Exception:  # noqa: BLE001 - 서보 읽기가 수집을 죽이는 일은 없다
            ok = False
        self._durations.append(time.perf_counter() - started)
        self.reads += 1
        if not ok:
            self.read_failures += 1

    def _read_block(self) -> bool:
        bus = self._bus
        _, comm = bus._sync_read(
            BLOCK_ADDRESS, BLOCK_LENGTH, self._ids, num_retry=0, raise_on_error=False
        )
        if not bus._is_comm_success(comm):
            return False
        # `_sync_read`가 돌려주는 값은 15바이트를 통째로 정수 하나로 만들려다 실패한 0이다
        # (`GroupSyncRead.getData`는 1·2·4바이트만 안다). 쓸모 있는 것은 방금 채워진
        # 수신 버퍼이고, 그것을 레지스터 폭으로 다시 물어본다. 다음 `sync_read`가
        # `clearParam()`으로 이 버퍼를 비우므로 **여기서 곧바로** 꺼내야 한다.
        reader = bus.sync_reader
        for register in REGISTERS:
            raw = {
                id_: reader.getData(id_, register.address, register.length) for id_ in self._ids
            }
            decoded = bus._decode_sign(register.data_name, raw)
            self._values[register.column] = [
                float(decoded[id_]) * register.scale for id_ in self._ids
            ]
        return True


def _camera_is_fresh(camera_fresh: dict[str, bool], key: str) -> bool:
    """`scene`으로도 `observation.images.scene`으로도 올 수 있다. 뒤 마디로 맞춘다."""
    for observed, fresh in camera_fresh.items():
        if str(observed).rsplit(".", 1)[-1] == key:
            return bool(fresh)
    return False
