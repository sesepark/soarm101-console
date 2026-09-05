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


def apply_recording_controls(path: str) -> dict[str, object]:
    """Apply and read back collection controls without ever blocking recording.

    A camera model may omit any of these controls. Those failures are reported
    by name while every remaining control is still attempted. ``values`` only
    contains values returned by ``VIDIOC_G_CTRL``; desired values are never
    presented as camera state.
    """
    resolved = os.path.realpath(path)
    failures: list[str] = []
    values: dict[str, int] = {}
    flags = os.O_RDWR | os.O_NONBLOCK
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        fd = os.open(resolved, flags)
    except OSError as exc:
        for control in RECORDING_CONTROLS:
            failures.append(control.name)
            logger.warning(
                "V4L2 recording control %s failed on %s: %s",
                control.name,
                resolved,
                exc,
            )
        return {"values": values, "failures": failures}

    try:
        for control in RECORDING_CONTROLS:
            failed = False
            buffer = bytearray(_CONTROL.pack(control.identifier, control.value))
            try:
                fcntl.ioctl(fd, _SET_CONTROL, buffer)
            except OSError as exc:
                failed = True
                logger.warning(
                    "V4L2 recording control %s failed on %s: %s",
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
                        "V4L2 recording control %s readback failed on %s: %s",
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
