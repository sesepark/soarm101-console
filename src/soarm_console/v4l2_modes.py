"""What a camera can actually give, asked of the driver rather than assumed.

The console used to open every camera at a fixed 640×480 and there was no way to
ask for anything else. Once the resolution became a choice, the choice had to be
the camera's own list — offering a mode the device cannot do would let the
console show a setting that silently never took effect, which is the one thing
this console is not allowed to do.

`v4l2-ctl` is not installed on this machine, so the two enumeration ioctls are
issued directly. Both are read-only and do not disturb a capture already in
progress: opening the node for `ioctl` is not the same as opening it for frames.
"""

from __future__ import annotations

import fcntl
import os
import struct

# linux/videodev2.h. The union in each structure is six `__u32` wide, and the
# request code carries the structure size, so these lengths have to be exact.
_FRAME_SIZE = struct.Struct("11I")   # index, pixel_format, type, union[6], reserved[2]
_FRAME_INTERVAL = struct.Struct("13I")  # index, pixel_format, width, height, type, union[6], reserved[2]
_DISCRETE = 1
_MJPG = int.from_bytes(b"MJPG", "little")


def _iowr(number: int, size: int) -> int:
    return (3 << 30) | (size << 16) | (ord("V") << 8) | number


_ENUM_FRAMESIZES = _iowr(74, _FRAME_SIZE.size)
_ENUM_FRAMEINTERVALS = _iowr(75, _FRAME_INTERVAL.size)


def _frame_sizes(fd: int) -> list[tuple[int, int]]:
    sizes: list[tuple[int, int]] = []
    for index in range(64):
        buffer = bytearray(_FRAME_SIZE.pack(index, _MJPG, *([0] * 9)))
        try:
            fcntl.ioctl(fd, _ENUM_FRAMESIZES, buffer)
        except OSError:
            break
        fields = _FRAME_SIZE.unpack(buffer)
        if fields[2] != _DISCRETE:
            break
        sizes.append((fields[3], fields[4]))
    return sizes


def _frame_rates(fd: int, width: int, height: int) -> list[int]:
    rates: list[int] = []
    for index in range(64):
        buffer = bytearray(_FRAME_INTERVAL.pack(index, _MJPG, width, height, *([0] * 9)))
        try:
            fcntl.ioctl(fd, _ENUM_FRAMEINTERVALS, buffer)
        except OSError:
            break
        fields = _FRAME_INTERVAL.unpack(buffer)
        if fields[4] != _DISCRETE:
            break
        numerator, denominator = fields[5], fields[6]
        if numerator:
            rates.append(round(denominator / numerator))
    return sorted(set(rates), reverse=True)


def discrete_modes(path: str) -> list[dict[str, object]]:
    """Every MJPG mode the device reports, largest first.

    Returns an empty list rather than raising when the device is gone or the
    kernel refuses the ioctl; the caller then falls back to what it already has,
    which is the behaviour the console had before this existed.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return []
    try:
        modes = [
            {"width": width, "height": height, "fps": _frame_rates(fd, width, height)}
            for width, height in _frame_sizes(fd)
        ]
    except OSError:
        return []
    finally:
        os.close(fd)
    return sorted(modes, key=lambda mode: (mode["width"] * mode["height"]), reverse=True)
