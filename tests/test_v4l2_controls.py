import logging
import os

from soarm_console import v4l2_controls
from soarm_console import record_manager as record_manager_module
from soarm_console.config import Settings
from soarm_console.record_manager import RecordManager
from soarm_console.v4l2_controls import RECORDING_CONTROLS, apply_recording_controls


def test_missing_camera_returns_every_control_as_a_failure(tmp_path, caplog):
    missing = tmp_path / "missing-camera"

    with caplog.at_level(logging.WARNING):
        result = apply_recording_controls(str(missing))

    names = [control.name for control in RECORDING_CONTROLS]
    assert result == {"values": {}, "failures": names}
    for name in names:
        assert name in caplog.text


def test_controls_are_applied_to_resolved_device_and_values_are_read_back(
    tmp_path, monkeypatch
):
    device = tmp_path / "video0"
    device.touch()
    stable_path = tmp_path / "by-path-camera"
    stable_path.symlink_to(device)
    opened: list[tuple[str, int]] = []
    calls: list[tuple[int, int]] = []
    set_values: dict[int, int] = {}

    def fake_open(path, flags):
        opened.append((path, flags))
        return 17

    def fake_ioctl(fd, request, buffer):
        assert fd == 17
        identifier, value = v4l2_controls._CONTROL.unpack(buffer)
        calls.append((request, identifier))
        if request == v4l2_controls._SET_CONTROL:
            set_values[identifier] = value
        else:
            # 장치가 요청값과 다른 값을 돌려줘도 그 값을 그대로 보고해야 한다.
            buffer[:] = v4l2_controls._CONTROL.pack(identifier, set_values[identifier] + 1)

    monkeypatch.setattr(v4l2_controls.os, "open", fake_open)
    monkeypatch.setattr(v4l2_controls.os, "close", lambda fd: None)
    monkeypatch.setattr(v4l2_controls.fcntl, "ioctl", fake_ioctl)

    result = apply_recording_controls(str(stable_path))

    assert opened[0][0] == os.path.realpath(stable_path)
    assert result == {
        "values": {control.key: control.value + 1 for control in RECORDING_CONTROLS},
        "failures": [],
    }
    assert calls == [
        (request, control.identifier)
        for control in RECORDING_CONTROLS
        for request in (v4l2_controls._SET_CONTROL, v4l2_controls._GET_CONTROL)
    ]


def test_one_rejected_control_does_not_stop_the_rest(monkeypatch):
    rejected = RECORDING_CONTROLS[1]
    seen: list[int] = []

    monkeypatch.setattr(v4l2_controls.os.path, "realpath", lambda path: "/dev/video9")
    monkeypatch.setattr(v4l2_controls.os, "open", lambda path, flags: 19)
    monkeypatch.setattr(v4l2_controls.os, "close", lambda fd: None)

    def fake_ioctl(fd, request, buffer):
        identifier, value = v4l2_controls._CONTROL.unpack(buffer)
        if request == v4l2_controls._SET_CONTROL:
            seen.append(identifier)
            if identifier == rejected.identifier:
                raise OSError("not supported")
        else:
            buffer[:] = v4l2_controls._CONTROL.pack(identifier, value)

    monkeypatch.setattr(v4l2_controls.fcntl, "ioctl", fake_ioctl)

    result = apply_recording_controls("/dev/v4l/by-path/camera")

    assert seen == [control.identifier for control in RECORDING_CONTROLS]
    assert result["failures"] == [rejected.name]


def test_record_manager_applies_both_cameras_before_starting_lerobot(
    tmp_path, monkeypatch
):
    manager = RecordManager(
        Settings(scene_camera="/stable/scene", wrist_camera="/stable/wrist")
    )
    manager.runtime_dir = tmp_path
    events: list[str] = []

    class FakeLocks:
        inherited_spec = "{}"
        file_descriptors = ()

        def release(self):
            pass

    class FakeProcess:
        pid = 123
        stdout = None

        def poll(self):
            return None

    class FakeThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

    def fake_apply(path):
        events.append(path)
        return {"values": {"power_line_frequency": 2}, "failures": []}

    def fake_popen(*args, **kwargs):
        events.append("popen")
        return FakeProcess()

    monkeypatch.setattr(manager, "preflight", lambda teleop_source: [])
    monkeypatch.setattr(
        record_manager_module.DeviceLockSet, "acquire", lambda devices, owner: FakeLocks()
    )
    monkeypatch.setattr(record_manager_module, "apply_recording_controls", fake_apply)
    monkeypatch.setattr(record_manager_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(record_manager_module.threading, "Thread", FakeThread)

    manager.start("pick", 1, 5)

    assert events == ["/stable/scene", "/stable/wrist", "popen"]
