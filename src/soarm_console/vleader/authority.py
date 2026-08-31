from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field

from .safety import Reject, RejectError, VLeaderSettings


class LeaseConflict(RuntimeError):
    """이미 다른 쪽이 조작 권한을 쥐고 있다."""

    def __init__(self, holder: str, seconds_left: float):
        super().__init__(f"{holder} holds the follower motion lease for another {seconds_left:.1f}s")
        self.holder = holder
        self.seconds_left = seconds_left


@dataclass
class Lease:
    """Follower motion 한 벌.

    PROTOCOL.md의 lease는 하드웨어 소유권이 아니라 **명령 권한**을 빌려주는 것이다.
    소유자는 언제나 서버의 가상 리더 프로세스 하나이고, 이 표는 그 프로세스에게 목표를
    제안할 수 있는 쪽이 지금 누구인지만 가리킨다.
    """

    lease_id: str
    holder: str
    session_id: str
    granted_at: float
    expires_at: float
    #: 리스를 잡은 뒤 아직 자세 동기화된 첫 명령을 받지 못했다. 이 값이 참인 동안에는
    #: 현재 자세에서 먼 목표를 거절한다.
    needs_sync: bool = True
    last_sequence: int = -1
    scope: str = "follower_motion"

    def as_dict(self, now: float | None = None) -> dict[str, object]:
        now = time.monotonic() if now is None else now
        return {
            "lease_id": self.lease_id,
            "holder": self.holder,
            "session_id": self.session_id,
            "scope": self.scope,
            "needs_sync": self.needs_sync,
            "expires_in_ms": max(0, int((self.expires_at - now) * 1000)),
        }


class AuthorityManager:
    """Follower motion 리스를 하나만 발급한다.

    관찰은 리스가 필요 없다. WebSocket에 붙기만 하면 관절값과 카메라를 계속 볼 수 있고,
    조작하는 쪽이 바뀌어도 보던 화면이 끊기지 않는다 — ADR 0001이 읽기 권한은 배타적이지
    않다고 적어 둔 그대로다.

    빼앗기는 없다. 만료와 명시적 반납만 있다. 폰이 맥의 리스를 강제로 가져갈 수 있게
    만들면, 맥 앞에 앉은 사람은 자기가 아직 조종 중이라고 믿는 채로 팔이 다른 명령을
    따르는 순간이 생긴다. 대신 리스 없이 누구나 부를 수 있는 강제 HOLD를 따로 둔다 —
    멈추는 것은 빼앗는 것이 아니다.
    """

    def __init__(self, settings: VLeaderSettings):
        self.settings = settings
        self._lock = threading.RLock()
        self._lease: Lease | None = None
        self._history: list[dict[str, object]] = []

    # MARK: 조회

    def active(self, now: float | None = None) -> Lease | None:
        now = time.monotonic() if now is None else now
        with self._lock:
            lease = self._lease
            if lease is None:
                return None
            if lease.expires_at <= now:
                self._expire(lease, "expired", now)
                return None
            return lease

    @property
    def history(self) -> list[dict[str, object]]:
        with self._lock:
            return list(self._history[-20:])

    # MARK: 발급과 반납

    def grant(self, holder: str, session_id: str, now: float | None = None) -> Lease:
        now = time.monotonic() if now is None else now
        with self._lock:
            existing = self.active(now)
            if existing is not None:
                raise LeaseConflict(existing.holder, existing.expires_at - now)
            lease = Lease(
                lease_id=secrets.token_hex(8),
                holder=holder,
                session_id=session_id,
                granted_at=now,
                expires_at=now + self.settings.lease_ttl_ms / 1000.0,
            )
            self._lease = lease
            self._record("granted", lease, now)
            return lease

    def renew(self, lease_id: str, now: float | None = None) -> Lease:
        now = time.monotonic() if now is None else now
        with self._lock:
            lease = self.active(now)
            if lease is None:
                raise RejectError(Reject.NO_ACTIVE_LEASE)
            if lease.lease_id != lease_id:
                raise RejectError(Reject.WRONG_AUTHORITY, f"{lease.holder}이(가) 쥐고 있습니다")
            lease.expires_at = now + self.settings.lease_ttl_ms / 1000.0
            return lease

    def release(self, lease_id: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        with self._lock:
            lease = self._lease
            if lease is None or lease.lease_id != lease_id:
                return False
            self._expire(lease, "released", now)
            return True

    def release_all(self, reason: str = "released", now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        with self._lock:
            if self._lease is not None:
                self._expire(self._lease, reason, now)

    # MARK: 명령 검사

    def authorise(self, lease_id: object, sequence: object, now: float | None = None) -> Lease:
        """이 명령을 낸 쪽이 지금 권한을 쥔 쪽인가, 그리고 처음 보는 순번인가."""
        now = time.monotonic() if now is None else now
        with self._lock:
            lease = self.active(now)
            if lease is None:
                raise RejectError(Reject.NO_ACTIVE_LEASE)
            if not isinstance(lease_id, str) or lease_id != lease.lease_id:
                raise RejectError(Reject.WRONG_AUTHORITY, f"{lease.holder}이(가) 쥐고 있습니다")
            if isinstance(sequence, bool) or not isinstance(sequence, int):
                raise RejectError(Reject.INVALID_SHAPE, "sequence가 정수가 아닙니다")
            # 늦게 도착한 명령과 다시 보낸 명령을 같은 자리에서 막는다. 순번은 리스마다
            # 처음부터 세므로, 리스가 바뀌면 이전 리스의 순번은 의미가 없다.
            if sequence <= lease.last_sequence:
                raise RejectError(
                    Reject.DUPLICATE_SEQUENCE,
                    f"순번 {sequence}는 이미 {lease.last_sequence}까지 처리했습니다",
                )
            lease.last_sequence = sequence
            lease.expires_at = now + self.settings.lease_ttl_ms / 1000.0
            return lease

    def mark_synced(self, lease_id: str) -> None:
        with self._lock:
            if self._lease is not None and self._lease.lease_id == lease_id:
                self._lease.needs_sync = False

    def require_resync(self) -> None:
        """고장이나 HOLD 뒤에 다시 자세를 맞추게 한다.

        SAFETY.md 불변조건 7 — 고장이나 소유자 변경 뒤에 이전 동작을 자동으로 재개하지
        않는다. 리스를 빼앗지는 않지만, 다음 명령은 다시 현재 자세 근처에서 시작해야 한다.
        """
        with self._lock:
            if self._lease is not None:
                self._lease.needs_sync = True

    # MARK: 내부

    def _expire(self, lease: Lease, reason: str, now: float) -> None:
        self._lease = None
        self._record(reason, lease, now)

    def _record(self, event: str, lease: Lease, now: float) -> None:
        self._history.append(
            {
                "event": event,
                "lease_id": lease.lease_id,
                "holder": lease.holder,
                "at": time.time(),
                "held_seconds": round(now - lease.granted_at, 1),
            }
        )
        del self._history[:-40]
