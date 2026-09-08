from __future__ import annotations

import fcntl
import hashlib
import json
import os
import socket
import stat
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path


LOCK_SCHEMA = 1
LOCK_DIR_ENV = "SOARM_OWNER_LOCK_DIR"


class DeviceLockError(RuntimeError):
    """다른 hardware owner가 같은 장치를 이미 예약했다."""

    def __init__(self, device: str, lock_path: Path, holder: dict[str, object] | None):
        self.device = device
        self.lock_path = lock_path
        self.holder = holder
        description = "unknown owner"
        if holder:
            description = f"{holder.get('owner', 'unknown')} (pid {holder.get('pid', '?')})"
        super().__init__(f"Device is owned by {description}: {device}")


def canonical_device(device: str | Path) -> str:
    """서로 다른 stable symlink가 같은 장치를 가리키면 같은 이름을 돌려준다."""
    return str(Path(device).expanduser().resolve(strict=False))


def _configured_lock_root() -> Path:
    configured = os.getenv(LOCK_DIR_ENV)
    if configured:
        return Path(configured).expanduser()
    if runtime := os.getenv("XDG_RUNTIME_DIR"):
        return Path(runtime) / "soarm-console/owner-locks"
    return Path("/tmp") / f"soarm-console-{os.getuid()}/owner-locks"


def lock_root() -> Path:
    root = _configured_lock_root()
    existed = root.exists()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    details = root.stat()
    if not stat.S_ISDIR(details.st_mode) or details.st_uid != os.getuid():
        raise PermissionError(f"Owner lock directory is not a directory owned by this user: {root}")
    if existed and details.st_mode & 0o077:
        # 사용자가 지정한 기존 디렉터리의 권한을 여기서 조용히 바꾸지 않는다.
        raise PermissionError(f"Owner lock directory must not be accessible by group/other: {root}")
    if not existed:
        # 새 leaf는 umask가 느슨하더라도 고정한다.
        root.chmod(0o700)
    return root


def _kernel_flocks(path: Path = Path("/proc/locks")) -> dict[tuple[int, int], int | None]:
    """현재 커널이 보유 중인 flock을 ``(st_dev, st_ino)``로 돌려준다.

    락 파일의 JSON은 마지막 소유자가 놓은 뒤에도 남는다. 현재 소유 여부를 알려면 파일
    내용이 아니라 커널 원장과 inode를 대조해야 한다. 여기서는 조회 중 잠깐이라도 장치 락을
    얻지 않도록 ``LOCK_NB`` probe 대신 Linux의 ``/proc/locks``를 읽는다.
    """
    held: dict[tuple[int, int], int | None] = {}
    # 읽을 수 없을 때 빈 원장으로 가장하면 실제 소유자를 "없음"으로 보고한다. 조회 실패를
    # 그대로 올려 API가 fail closed 하게 둔다.
    lines = path.read_text(encoding="utf-8").splitlines()
    for line in lines:
        fields = line.split()
        # 대기 중인 행에는 lock id 뒤에 ``->``가 붙는다. 보유 중인 FLOCK만 센다.
        if len(fields) < 6 or fields[1] != "FLOCK":
            continue
        try:
            major, minor, inode = fields[5].split(":", 2)
            device = os.makedev(int(major, 16), int(minor, 16))
            pid = int(fields[4])
            held[(device, int(inode))] = pid if pid >= 0 else None
        except (ValueError, OSError, OverflowError):
            continue
    return held


def read_lock_ledger(
    *, proc_locks_path: Path = Path("/proc/locks")
) -> list[dict[str, object]]:
    """락 디렉터리를 바꾸지 않고 현재 장치 소유 원장을 읽는다.

    잠기지 않은 파일의 metadata는 과거 기록일 뿐이다. 그런 장치도 알려 주되 현재 owner와
    pid는 ``None``으로 답해 stale 기록을 살아 있는 소유자로 오인하지 않게 한다.
    """
    root = _configured_lock_root()
    try:
        details = root.stat()
    except OSError:
        return []
    if (
        not stat.S_ISDIR(details.st_mode)
        or details.st_uid != os.getuid()
        or details.st_mode & 0o077
    ):
        raise PermissionError(f"Owner lock directory is not a private directory: {root}")

    held = _kernel_flocks(proc_locks_path)
    devices: list[dict[str, object]] = []
    for path in sorted(root.glob("*.lock")):
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            file_descriptor = os.open(path, flags)
            try:
                file_details = os.fstat(file_descriptor)
                metadata = _read_metadata(file_descriptor)
            finally:
                os.close(file_descriptor)
        except OSError:
            continue
        if not metadata or not isinstance(metadata.get("device"), str):
            continue
        kernel_pid = held.get((file_details.st_dev, file_details.st_ino))
        locked = (file_details.st_dev, file_details.st_ino) in held
        devices.append(
            {
                "device": metadata["device"],
                "locked": locked,
                "owner": metadata.get("owner") if locked else None,
                "pid": kernel_pid if locked else None,
                "acquired_at": metadata.get("acquired_at") if locked else None,
            }
        )
    return sorted(devices, key=lambda item: str(item["device"]))


def lock_path_for(device: str | Path) -> Path:
    canonical = canonical_device(device)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:20]
    return lock_root() / f"{digest}.lock"


def _process_start_ticks(pid: int) -> int | None:
    try:
        # comm에는 공백과 괄호가 들어갈 수 있으므로 마지막 ')' 뒤에서 필드를 센다.
        fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()
        return int(fields[19])  # proc(5)의 22번째 필드 starttime
    except (FileNotFoundError, OSError, ValueError, IndexError):
        return None


def _boot_id() -> str | None:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _read_metadata(file_descriptor: int) -> dict[str, object] | None:
    try:
        os.lseek(file_descriptor, 0, os.SEEK_SET)
        raw = os.read(file_descriptor, 64 * 1024).decode("utf-8")
        value = json.loads(raw)
        return value if isinstance(value, dict) else None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


@dataclass
class DeviceLock:
    device: str
    owner: str
    path: Path
    metadata: dict[str, object]
    _file_descriptor: int
    _released: bool = False

    @classmethod
    def acquire(cls, device: str | Path, owner: str) -> DeviceLock:
        canonical = canonical_device(device)
        path = lock_path_for(canonical)
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        file_descriptor = os.open(path, flags, 0o600)
        os.fchmod(file_descriptor, 0o600)
        try:
            fcntl.flock(file_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            holder = _read_metadata(file_descriptor)
            os.close(file_descriptor)
            raise DeviceLockError(canonical, path, holder) from exc

        # flock를 얻었다는 사실이 stale 판정이다. 이전 PID가 남아 있어도 커널 lock이
        # 없다면 그 owner는 죽었거나 정상 반납한 것이므로 다음 정상 시작이 덮어쓴다.
        now = time.time()
        metadata: dict[str, object] = {
            "schema": LOCK_SCHEMA,
            "device": canonical,
            "owner": owner,
            "pid": os.getpid(),
            "process_start_ticks": _process_start_ticks(os.getpid()),
            "boot_id": _boot_id(),
            "hostname": socket.gethostname(),
            "command": sys.argv,
            "acquired_at": now,
        }
        encoded = (json.dumps(metadata, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        os.ftruncate(file_descriptor, 0)
        os.lseek(file_descriptor, 0, os.SEEK_SET)
        os.write(file_descriptor, encoded)
        os.fsync(file_descriptor)
        return cls(canonical, owner, path, metadata, file_descriptor)

    @property
    def file_descriptor(self) -> int:
        return self._file_descriptor

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        try:
            fcntl.flock(self._file_descriptor, fcntl.LOCK_UN)
        finally:
            os.close(self._file_descriptor)

    def __enter__(self) -> DeviceLock:
        return self

    def __exit__(self, *_: object) -> None:
        self.release()


class DeviceLockSet:
    """교착을 피하도록 장치 이름 순서대로 한 owner의 lock을 잡는다."""

    def __init__(self, locks: list[DeviceLock]):
        self.locks = locks
        self._release_lock = threading.Lock()
        self._released = False

    @classmethod
    def acquire(cls, devices: list[str | Path], owner: str) -> DeviceLockSet:
        unique = sorted({canonical_device(device) for device in devices})
        locks: list[DeviceLock] = []
        try:
            for device in unique:
                locks.append(DeviceLock.acquire(device, owner))
        except BaseException:
            for lock in reversed(locks):
                lock.release()
            raise
        return cls(locks)

    @property
    def file_descriptors(self) -> tuple[int, ...]:
        return tuple(lock.file_descriptor for lock in self.locks)

    @property
    def inherited_spec(self) -> str:
        return json.dumps(
            {lock.device: {"fd": lock.file_descriptor, "path": str(lock.path)} for lock in self.locks}
        )

    @property
    def devices(self) -> list[str]:
        return [lock.device for lock in self.locks]

    def release(self) -> None:
        with self._release_lock:
            if self._released:
                return
            self._released = True
            for lock in reversed(self.locks):
                lock.release()

    def __enter__(self) -> DeviceLockSet:
        return self

    def __exit__(self, *_: object) -> None:
        self.release()


def inherited_locks_cover(devices: list[str | Path]) -> bool:
    """record child가 parent의 열린 flock descriptor를 실제로 물려받았는지 확인한다."""
    raw = os.getenv("SOARM_OWNER_LOCK_FDS", "")
    if not raw:
        return False
    try:
        inherited = json.loads(raw)
        expected = {canonical_device(device) for device in devices}
        if not isinstance(inherited, dict) or set(inherited) != expected:
            return False
        for device, descriptor in inherited.items():
            file_descriptor = int(descriptor["fd"])
            path = Path(descriptor["path"])
            if path != lock_path_for(device):
                return False
            if os.fstat(file_descriptor).st_ino != path.stat().st_ino:
                return False
        return True
    except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError):
        return False
