"""Recording camera controls, issued directly through V4L2 ioctls."""

from __future__ import annotations

import fcntl
import logging
import os
import struct
from dataclasses import dataclass


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Control:
    key: str
    name: str
    identifier: int
    value: int


# `auto_exposure=3`으로 노출은 자동 그대로 두고 노출 시간은 일부러 고정하지 않는다.
# 이 카메라는 gain 컨트롤이 없어서 자동 노출이 고른 하한 50(5 ms)을 수동으로 얼리면
# 내부 gain까지 사라져 영상만 절반가량 어두워지고, 이미 노출 하한에 붙어 있으므로
# 모션 블러를 더 줄이는 이득도 없다.
RECORDING_CONTROLS = (
    Control("power_line_frequency", "Power Line Frequency", 0x00980918, 2),
    Control(
        "exposure_dynamic_framerate", "Exposure, Dynamic Framerate", 0x009A0903, 0
    ),
    Control("white_balance_automatic", "White Balance, Automatic", 0x0098090C, 0),
    Control("white_balance_temperature", "White Balance Temperature", 0x0098091A, 4600),
    Control("auto_exposure", "Auto Exposure", 0x009A0901, 3),
)


# 카메라 위치 추정 전용. **`RECORDING_CONTROLS`와 섞지 않는다.**
#
# 수집 경로에 노출 고정을 넣을지는 사용자가 보류한 결정이다(`조명_차이_조사_2026-09-06.md`
# §5.3 각주). 그러나 perception에는 고정이 필요하다 — HSV 문턱은 색이 흔들리면 무너지고,
# 자동 노출은 장면이 바뀔 때마다 밝기를, 자동 화이트밸런스는 색상(Hue) 자체를 민다.
#
# 노출 167은 지어낸 값이 아니라 이 방에서 잰 값이다. 60Hz 전원의 조명은 120Hz로 깜빡이므로
# (주기 8.33ms) 노출이 그 정수배가 아니면 프레임마다 담는 깜빡임 조각이 달라져 밝기가
# 흔들린다. 실측: 160(1.92배) 흔들림 0.008 · **167(2.00배) 0.004** · 200(2.40배) 0.034.
# 쓸 수 있는 값은 83·167·250·333이고, 그 방의 그때 빛에서 다시 재서 포화 없이 가장 밝은
# 것을 고른다. 16.7ms의 모션 블러는 문제가 없다 — 관절이 가장 빠를 때도 한 프레임에 1.8°다.
PERCEPTION_CONTROLS = (
    Control("power_line_frequency", "Power Line Frequency", 0x00980918, 2),
    Control("exposure_dynamic_framerate", "Exposure, Dynamic Framerate", 0x009A0903, 0),
    Control("white_balance_automatic", "White Balance, Automatic", 0x0098090C, 0),
    Control("white_balance_temperature", "White Balance Temperature", 0x0098091A, 4600),
    Control("auto_exposure", "Auto Exposure", 0x009A0901, 1),  # 1 = 수동
    Control("exposure_time_absolute", "Exposure Time, Absolute", 0x009A0902, 167),
)


# linux/videodev2.h: struct v4l2_control { __u32 id; __s32 value; }.
_CONTROL = struct.Struct("=Ii")


def _iowr(number: int, size: int) -> int:
    return (3 << 30) | (size << 16) | (ord("V") << 8) | number


_GET_CONTROL = _iowr(27, _CONTROL.size)
_SET_CONTROL = _iowr(28, _CONTROL.size)


def _read_control(fd: int, control: Control) -> int:
    buffer = bytearray(_CONTROL.pack(control.identifier, 0))
    fcntl.ioctl(fd, _GET_CONTROL, buffer)
    _, value = _CONTROL.unpack(buffer)
    return value


def apply_perception_controls(path: str) -> dict[str, object]:
    """위치 추정·캘리브레이션이 쓰는 값. 수집이 쓰는 값과 **다른 묶음**이다."""
    return _apply(path, PERCEPTION_CONTROLS, "perception")


def apply_recording_controls(path: str) -> dict[str, object]:
    """Apply and read back collection controls without ever blocking recording.

    A camera model may omit any of these controls. Those failures are reported
    by name while every remaining control is still attempted. ``values`` only
    contains values returned by ``VIDIOC_G_CTRL``; desired values are never
    presented as camera state.
    """
    return _apply(path, RECORDING_CONTROLS, "recording")


def _apply(path: str, controls: tuple[Control, ...], label: str) -> dict[str, object]:
    resolved = os.path.realpath(path)
    failures: list[str] = []
    values: dict[str, int] = {}
    flags = os.O_RDWR | os.O_NONBLOCK
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        fd = os.open(resolved, flags)
    except OSError as exc:
        for control in controls:
            failures.append(control.name)
            logger.warning(
                "V4L2 %s control %s failed on %s: %s",
                label,
                control.name,
                resolved,
                exc,
            )
        return {"values": values, "failures": failures}

    try:
        for control in controls:
            failed = False
            buffer = bytearray(_CONTROL.pack(control.identifier, control.value))
            try:
                fcntl.ioctl(fd, _SET_CONTROL, buffer)
            except OSError as exc:
                failed = True
                logger.warning(
                    "V4L2 %s control %s failed on %s: %s",
                    label,
                    control.name,
                    resolved,
                    exc,
                )

            # SET이 성공했다는 사실이 아니라 장치가 실제로 돌려준 값을 상태에 싣는다.
            try:
                values[control.key] = _read_control(fd, control)
            except OSError as exc:
                if not failed:
                    logger.warning(
                        "V4L2 %s control %s readback failed on %s: %s",
                        label,
                        control.name,
                        resolved,
                        exc,
                    )
                failed = True
            if failed:
                failures.append(control.name)
    finally:
        os.close(fd)

    return {"values": values, "failures": failures}
