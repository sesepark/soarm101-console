"""서보가 내주는 나머지 판독값이 데이터셋까지 가는 길.

팔 없이 도는 시험만 여기에 둔다. 버스는 바이트 열로 흉내 내고, 데이터셋은 LeRobot이
실제로 쓰는 길로 한 회차를 만들어 확인한다. 실물 한 회는 사람이 옆에 있을 때 한다.
"""

from __future__ import annotations

import numpy as np
import pytest

from soarm_console import recording, sensors


MOTOR_IDS = (1, 2, 3, 4, 5, 6)


class _FakeSyncReader:
    """`scservo_sdk.GroupSyncRead`가 하는 일 가운데 우리가 쓰는 것만.

    실제 SDK와 같은 규칙을 지킨다: `getData`는 요청한 폭이 시작 주소부터의 구간 안에
    들어야 값을 주고, 2바이트는 리틀엔디언으로 합친다. 구간 밖이면 0이다.
    """

    #: `rows`가 담고 있는 첫 주소. 서보는 요청한 시작 주소부터 답하므로, 흉내도 주소로
    #: 찾아야 한다 — 리스트의 앞에서부터 세면 `sync_read("Present_Load")`처럼 60에서
    #: 시작하는 읽기가 위치 바이트를 부하로 읽는다.
    BASE_ADDRESS = 56

    def __init__(self, rows: dict[int, list[int]], comm: int = 0) -> None:
        self.rows = rows
        self.comm = comm
        self.start_address = 0
        self.data_length = 0
        self.ids: list[int] = []
        self.transactions = 0

    def clearParam(self) -> None:  # noqa: N802 - SDK의 이름 그대로여야 한다
        self.ids = []

    def addParam(self, scs_id: int) -> bool:  # noqa: N802
        self.ids.append(scs_id)
        return True

    def txRxPacket(self) -> int:  # noqa: N802
        self.transactions += 1
        return self.comm

    def getData(self, scs_id: int, address: int, data_length: int) -> int:  # noqa: N802
        row = self.rows.get(scs_id)
        if row is None:
            return 0
        if address < self.start_address:
            return 0
        if self.start_address + self.data_length - data_length < address:
            return 0
        offset = address - self.BASE_ADDRESS
        if data_length == 1:
            return row[offset]
        if data_length == 2:
            return row[offset] | (row[offset + 1] << 8)
        return 0


def _block(
    *,
    position: int = 2048,
    velocity: int = 0,
    load: int = 0,
    voltage: int = 121,
    temperature: int = 34,
    status: int = 0,
    moving: int = 0,
    current: int = 0,
) -> list[int]:
    """주소 56~70의 15바이트. 서보가 실제로 돌려주는 배치 그대로.

    부호는 **서보가 보내는 모양**으로 넣는다: 부호-크기(sign-magnitude)이고, 부하는 비트
    10이, 속도는 비트 15가 부호다. 2의 보수가 아니다.
    """

    def word(value: int, sign_bit: int) -> tuple[int, int]:
        raw = abs(value) | ((1 << sign_bit) if value < 0 else 0)
        return raw & 0xFF, (raw >> 8) & 0xFF

    row = [0] * 15
    row[0], row[1] = word(position, 15)  # 56 Present_Position
    row[2], row[3] = word(velocity, 15)  # 58 Present_Velocity
    row[4], row[5] = word(load, 10)  # 60 Present_Load
    row[6] = voltage  # 62 Present_Voltage
    row[7] = temperature  # 63 Present_Temperature
    row[8] = 0  # 64 (미사용)
    row[9] = status  # 65 Status
    row[10] = moving  # 66 Moving
    row[11] = row[12] = 0  # 67~68 (미사용)
    row[13], row[14] = word(current, 15)  # 69 Present_Current
    return row


def _bus(rows: dict[int, list[int]], comm: int = 0):
    """포트를 열지 않은 진짜 `FeetechMotorsBus`. 수신 버퍼만 흉내로 갈아 끼운다.

    가짜 버스를 새로 쓰지 않는 이유가 있다. 이 시험이 확인하려는 것 가운데 하나가
    "부호를 가상 리더와 **같게** 푸는가"인데, 부호를 푸는 코드는 이 클래스 안에 있다.
    흉내 낸 버스로 시험하면 그 코드는 시험에 한 번도 나오지 않는다.
    """
    from lerobot.motors import Motor, MotorNormMode
    from lerobot.motors.feetech import FeetechMotorsBus

    bus = FeetechMotorsBus(
        port="/dev/null",
        motors={
            name: Motor(index + 1, "sts3215", MotorNormMode.DEGREES)
            for index, name in enumerate(sensors.MOTORS)
        },
    )
    bus.sync_reader = _FakeSyncReader(rows, comm=comm)
    # `sync_read`는 열린 포트를 요구한다. 이 시험은 바이트를 다루는 코드만 보므로 열렸다고
    # 말해 준다 — 실제로 무엇을 열거나 보내지 않는다.
    bus.port_handler.is_open = True
    return bus


# MARK: 열의 모양


def test_the_nine_columns_are_named_and_shaped_exactly_as_the_contract_says():
    features = sensors.extra_features()

    assert list(features) == [
        "observation.load",
        "observation.velocity",
        "observation.temperature",
        "observation.voltage",
        "observation.servo_status",
        "observation.servo_moving",
        "observation.current",
        "observation.wall_time",
        "observation.camera_fresh",
    ]
    assert features["observation.load"] == {
        "dtype": "float32",
        "shape": (6,),
        "names": [
            "shoulder_pan.load",
            "shoulder_lift.load",
            "elbow_flex.load",
            "wrist_flex.load",
            "wrist_roll.load",
            "gripper.load",
        ],
    }
    assert features["observation.wall_time"]["names"] == ["since_start"]
    assert features["observation.camera_fresh"]["shape"] == (2,)
    assert features["observation.camera_fresh"]["names"] == ["scene", "wrist"]
    assert all(feature["dtype"] == "float32" for feature in features.values())
    assert all(
        len(feature["shape"]) == 1 and feature["shape"][0] == len(feature["names"])
        for feature in features.values()
    )


def test_the_state_vector_is_left_alone():
    """새 열이 `observation.state`를 건드리지 않는다는 것이 이 작업 전체의 전제다."""
    assert "observation.state" not in sensors.extra_features()
    assert "action" not in sensors.extra_features()


# MARK: 블록 읽기


def test_one_block_read_per_tick_splits_into_every_register():
    rows = {
        motor_id: _block(
            velocity=100 * motor_id,
            load=10 * motor_id,
            voltage=120 + motor_id,
            temperature=30 + motor_id,
            status=motor_id,
            moving=motor_id % 2,
            current=motor_id,
        )
        for motor_id in MOTOR_IDS
    }
    bus = _bus(rows)
    reader = sensors.SensorReader(bus, started_at=1000.0)

    extras = reader.observation_extras(1002.5, {"scene": True, "wrist": False})

    # 버스 왕복은 **한 번**이다. 레지스터마다 따로 읽으면 30Hz가 흔들린다.
    assert bus.sync_reader.transactions == 1
    assert bus.sync_reader.start_address == sensors.BLOCK_ADDRESS
    assert bus.sync_reader.data_length == sensors.BLOCK_LENGTH
    assert reader.read_failures == 0

    assert extras["shoulder_pan.vel"] == 100.0
    assert extras["gripper.vel"] == 600.0
    assert extras["shoulder_pan.load"] == 10.0
    assert extras["gripper.load"] == 60.0
    assert extras["shoulder_pan.temp"] == 31.0
    assert extras["gripper.temp"] == 36.0
    # 전압만 눈금이 다르다. 서보는 0.1V 단위로 답한다.
    assert extras["shoulder_pan.volt"] == pytest.approx(12.1)
    assert extras["gripper.volt"] == pytest.approx(12.6)
    assert extras["shoulder_pan.status"] == 1.0
    assert extras["shoulder_pan.moving"] == 1.0
    assert extras["shoulder_lift.moving"] == 0.0
    assert extras["gripper.current"] == 6.0


def test_the_sign_comes_out_the_same_way_the_virtual_leader_reads_it():
    """같은 힘이 두 곳에서 다른 숫자가 되면 어느 쪽이 맞는지 알 길이 없다.

    가상 리더 백엔드는 `bus.sync_read("Present_Load", normalize=False)`로 부하를 읽는다.
    블록 읽기가 그 값과 글자까지 같아야 한다.
    """
    rows = {
        motor_id: _block(load=-(100 * motor_id), velocity=-(7 * motor_id))
        for motor_id in MOTOR_IDS
    }
    bus = _bus(rows)
    reader = sensors.SensorReader(bus, started_at=0.0)

    extras = reader.observation_extras(0.0, {})

    # 부호-크기를 정말로 풀었는가. 비트 10이 서면 -100이지 1124가 아니다.
    assert extras["shoulder_pan.load"] == -100.0
    assert extras["gripper.load"] == -600.0
    assert extras["shoulder_pan.vel"] == -7.0

    for name, motor_id in zip(sensors.MOTORS, MOTOR_IDS, strict=True):
        by_sync_read = bus.sync_read("Present_Load", normalize=False)
        assert extras[f"{name}.load"] == float(by_sync_read[name])
        assert extras[f"{name}.vel"] == float(
            bus.sync_read("Present_Velocity", normalize=False)[name]
        )


def test_a_dropped_packet_repeats_the_last_values_instead_of_stopping_the_episode():
    """버스 패킷 하나가 깨졌다고 30초짜리 시연을 잃지 않는다.

    `validate_frame`은 열이 하나라도 비면 회차를 막으므로, 값을 빼는 선택지는 없다.
    마지막으로 읽은 값을 한 번 더 쓰고 실패를 센다 — 그 수는 상태에 실려 사람이 본다.
    """
    from scservo_sdk import COMM_RX_FAIL

    rows = {motor_id: _block(load=42) for motor_id in MOTOR_IDS}
    bus = _bus(rows)
    reader = sensors.SensorReader(bus, started_at=0.0)

    good = reader.observation_extras(0.0, {})
    bus.sync_reader.comm = COMM_RX_FAIL
    repeated = reader.observation_extras(1.0, {})

    assert good["gripper.load"] == 42.0
    assert repeated["gripper.load"] == 42.0
    assert reader.read_failures == 1
    assert reader.reads == 2
    # 시각은 판독값이 아니다. 읽기가 실패해도 이 프레임이 언제였는지는 안다.
    assert repeated["since_start"] == 1.0


def test_a_bus_that_raises_never_reaches_the_record_loop():
    class _AngryBus:
        motors = {name: type("M", (), {"id": index + 1})() for index, name in enumerate(sensors.MOTORS)}

        def _sync_read(self, *args, **kwargs):
            raise ConnectionError("There is no status packet!")

    reader = sensors.SensorReader(_AngryBus(), started_at=0.0)
    extras = reader.observation_extras(0.5, {})

    assert reader.read_failures == 1
    assert extras["gripper.load"] == 0.0
    assert extras["since_start"] == 0.5


def test_without_a_bus_the_columns_are_still_complete():
    """버스가 없어도 프레임은 온전해야 한다. 열 하나가 비면 회차 전체가 막힌다."""
    reader = sensors.SensorReader(None, started_at=10.0)

    extras = reader.observation_extras(12.0, {})

    assert reader.enabled is False
    for feature in sensors.extra_features().values():
        for name in feature["names"]:
            if name in {"scene", "wrist"}:
                continue
            assert name in extras
    assert extras["since_start"] == 2.0


# MARK: 시각과 카메라


def test_wall_time_counts_from_the_start_of_this_process_not_from_the_epoch():
    """float32에 epoch을 그대로 넣으면 30Hz 프레임이 전부 같은 시각이 된다."""
    started = 1_757_000_000.0
    reader = sensors.SensorReader(None, started_at=started)

    first = reader.observation_extras(started + 12.345, {})
    second = reader.observation_extras(started + 12.378, {})

    assert first["since_start"] == pytest.approx(12.345)
    # 두 프레임이 float32로 좁혀져도 서로 다른 시각으로 남는다.
    assert np.float32(first["since_start"]) != np.float32(second["since_start"])


@pytest.mark.parametrize(
    "fresh",
    [
        {"scene": True, "wrist": False},
        {"observation.images.scene": True, "observation.images.wrist": False},
    ],
)
def test_camera_freshness_is_read_under_either_key_shape(fresh):
    reader = sensors.SensorReader(None, started_at=0.0)

    extras = reader.observation_extras(0.0, fresh)

    assert extras[sensors.CAMERA_FRESH_KEY] == [1.0, 0.0]


def test_a_camera_that_said_nothing_this_tick_is_not_fresh():
    reader = sensors.SensorReader(None, started_at=0.0)
    assert reader.observation_extras(0.0, {})[sensors.CAMERA_FRESH_KEY] == [0.0, 0.0]


# MARK: features와 프레임을 잇는 자리


def test_the_wrapper_adds_the_columns_after_lerobot_has_grouped_the_state():
    """`hw_to_dataset_features`가 float 항목을 `observation.state`로 묶은 **뒤에** 더한다."""
    from lerobot.utils.feature_utils import hw_to_dataset_features

    observation = {f"{motor}.pos": float for motor in sensors.MOTORS}
    grouped = hw_to_dataset_features(observation, "observation", use_video=True)

    features = recording._combine_feature_dicts_with_sensors(grouped)

    assert features["observation.state"]["shape"] == (6,)
    assert features["observation.state"]["names"] == [f"{m}.pos" for m in sensors.MOTORS]
    assert set(sensors.extra_columns()) <= set(features)


def test_build_dataset_frame_fills_every_new_column_from_the_observation():
    features = recording._combine_feature_dicts_with_sensors(
        {
            "observation.state": {
                "dtype": "float32",
                "shape": (6,),
                "names": [f"{m}.pos" for m in sensors.MOTORS],
            },
            "observation.images.scene": {
                "dtype": "video",
                "shape": (4, 4, 3),
                "names": ["height", "width", "channels"],
            },
        }
    )
    rows = {motor_id: _block(load=motor_id) for motor_id in MOTOR_IDS}
    reader = sensors.SensorReader(_bus(rows), started_at=100.0)
    observation = {f"{motor}.pos": 1.0 for motor in sensors.MOTORS}
    observation["scene"] = np.zeros((4, 4, 3), dtype=np.uint8)
    observation.update(reader.observation_extras(101.5, {"scene": True, "wrist": False}))

    frame = recording._build_dataset_frame_with_sensors(features, observation, "observation")

    assert set(sensors.extra_columns()) <= set(frame)
    assert frame["observation.load"].tolist() == [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    assert frame["observation.wall_time"].tolist() == [1.5]
    assert frame["observation.camera_fresh"].tolist() == [1.0, 0.0]
    assert all(
        frame[column].dtype == np.float32 for column in sensors.extra_columns()
    )
    # 그리고 카메라 그림은 그대로 지나간다 — `camera_fresh`의 이름이 카메라 이름과
    # 같다는 이유로 영상이 사라지면 안 된다.
    assert frame["observation.images.scene"].shape == (4, 4, 3)


def test_lerobot_still_calls_the_two_functions_we_replace():
    """갈아 끼우는 자리가 사라지면 시험이 먼저 말해야 한다.

    LeRobot이 `record()` 안에서 `combine_feature_dicts`를 부르고 `record_loop` 안에서
    `build_dataset_frame`을 부르는 것이 이 기능 전체가 서 있는 자리다. 업스트림이 그
    호출을 옮기면 새 열은 **조용히** 사라진다 — 수집은 그대로 돌고, 파케이에 열만 없다.
    """
    import inspect

    from lerobot.scripts import lerobot_record

    # 우리가 갈아 끼우는 이름이 이 모듈에 있어야 한다. `from ... import`로 들여온
    # 이름이므로, 모듈의 그 자리를 바꾸면 호출도 바뀐다.
    assert lerobot_record.combine_feature_dicts is recording._ORIGINAL_COMBINE_FEATURE_DICTS
    assert lerobot_record.build_dataset_frame is recording._ORIGINAL_BUILD_DATASET_FRAME
    source = inspect.getsource(lerobot_record)
    assert "dataset_features = combine_feature_dicts(" in source
    assert "build_dataset_frame(dataset.features" in source


def test_a_dataset_without_the_columns_still_builds_its_frames():
    """새 열이 없는 features로 부르면 원본과 똑같이 동작한다."""
    from lerobot.utils.feature_utils import build_dataset_frame

    features = {
        "observation.state": {
            "dtype": "float32",
            "shape": (6,),
            "names": [f"{m}.pos" for m in sensors.MOTORS],
        }
    }
    values = {f"{motor}.pos": 2.0 for motor in sensors.MOTORS}

    assert recording._build_dataset_frame_with_sensors(
        features, values, "observation"
    ).keys() == build_dataset_frame(features, values, "observation").keys()


# MARK: 데이터셋까지 가는 길


@pytest.fixture
def data_root(tmp_path, monkeypatch):
    from soarm_console import datasets

    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setattr(datasets, "data_root", lambda: root)
    return root


def _record_synthetic_episode(root, name: str, frames: int = 4):
    """LeRobot이 실제로 쓰는 길로 한 회차를 만든다. 팔도 카메라도 없이.

    파케이를 손으로 짜 넣지 않는 이유가 있다. 확인하려는 것은 우리가 적은 모양이 아니라
    `LeRobotDataset.create` → `add_frame` → `save_episode`가 새 열을 실제로 끝까지
    나르는가이다. 손으로 지은 파일은 그 길을 한 번도 걷지 않는다.

    영상은 넣지 않는다 — 이 기계에는 `libavdevice`가 없어 인코딩이 돌지 않고(`RUNBOOK`),
    새 열은 카메라 없이도 그대로 시험된다.
    """
    from lerobot.datasets import LeRobotDataset

    features = recording._combine_feature_dicts_with_sensors(
        {
            "action": {
                "dtype": "float32",
                "shape": (6,),
                "names": [f"{m}.pos" for m in sensors.MOTORS],
            },
            "observation.state": {
                "dtype": "float32",
                "shape": (6,),
                "names": [f"{m}.pos" for m in sensors.MOTORS],
            },
        }
    )
    dataset = LeRobotDataset.create(
        f"local/{name}", 30, root=root / name, robot_type="so101_follower",
        features=features, use_videos=True,
    )
    reader = sensors.SensorReader(
        _bus({motor_id: _block(load=10 * motor_id, velocity=-motor_id) for motor_id in MOTOR_IDS}),
        started_at=1000.0,
    )
    for index in range(frames):
        observation = {f"{m}.pos": float(index) for m in sensors.MOTORS}
        observation.update(
            reader.observation_extras(
                1000.0 + index / 30.0, {"scene": True, "wrist": index % 2 == 0}
            )
        )
        frame = recording._build_dataset_frame_with_sensors(
            dataset.features, observation, "observation"
        )
        frame["action"] = np.zeros(6, dtype=np.float32)
        frame["task"] = "Pick and place"
        dataset.add_frame(frame)
    dataset.save_episode()
    dataset.finalize()
    return dataset


def test_the_columns_reach_the_parquet_with_their_values(data_root):
    from soarm_console import datasets

    _record_synthetic_episode(data_root, "soarm101_pick")

    assert datasets.summarize("soarm101_pick")["extras"] == list(sensors.extra_suffixes())

    trajectory = datasets.trajectory("soarm101_pick", 0)

    assert trajectory["frames"] == 4
    # 새 열이 늘어도 상태 벡터는 여섯 그대로다.
    assert trajectory["joints"] == [f"{m}.pos" for m in sensors.MOTORS]
    assert len(trajectory["state"][0]) == 6
    # 그리고 새 열은 프레임 수와 같은 길이로 온다.
    assert len(trajectory["load"]) == 4
    assert len(trajectory["velocity"]) == 4
    assert len(trajectory["camera_fresh"]) == 4
    assert len(trajectory["wall_time"]) == 4
    assert trajectory["load"][0] == pytest.approx([10.0, 20.0, 30.0, 40.0, 50.0, 60.0])
    assert trajectory["velocity"][0] == pytest.approx([-1.0, -2.0, -3.0, -4.0, -5.0, -6.0])
    assert trajectory["camera_keys"] == ["scene", "wrist"]
    assert trajectory["camera_fresh"][0] == [1.0, 1.0]
    assert trajectory["camera_fresh"][1] == [1.0, 0.0]
    assert trajectory["wall_time"][0] == pytest.approx([0.0])
    assert trajectory["wall_time"][3] == pytest.approx([3 / 30.0], abs=1e-6)


def test_a_dataset_without_the_columns_still_answers_with_a_trajectory(data_root):
    """새 열이 생기기 전에 찍은 데이터셋도 그대로 열려야 한다.

    `pq.read_table`은 없는 열 이름 하나에 통째로 실패한다. 있는 열만 물어보지 않으면
    지금 디스크에 있는 두 데이터셋의 궤적이 여기서 막힌다.
    """
    from soarm_console import datasets
    from lerobot.datasets import LeRobotDataset

    features = {
        "action": {
            "dtype": "float32", "shape": (6,), "names": [f"{m}.pos" for m in sensors.MOTORS],
        },
        "observation.state": {
            "dtype": "float32", "shape": (6,), "names": [f"{m}.pos" for m in sensors.MOTORS],
        },
    }
    dataset = LeRobotDataset.create(
        "local/soarm101_old", 30, root=data_root / "soarm101_old",
        robot_type="so101_follower", features=features, use_videos=True,
    )
    for _ in range(3):
        dataset.add_frame({
            "action": np.zeros(6, dtype=np.float32),
            "observation.state": np.zeros(6, dtype=np.float32),
            "task": "Pick and place",
        })
    dataset.save_episode()
    dataset.finalize()

    trajectory = datasets.trajectory("soarm101_old", 0)

    assert trajectory["frames"] == 3
    assert datasets.summarize("soarm101_old")["extras"] == []
    for absent in ("load", "velocity", "camera_fresh", "wall_time", "camera_keys"):
        assert absent not in trajectory
