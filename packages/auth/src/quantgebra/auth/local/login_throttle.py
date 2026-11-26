import threading
import time
from pydantic import BaseModel

_ALLOWED_FAILURES: int = 5
_ALLOWED_FAILURES_PER_ADDRESS: int = 30
_LOCKED_SECONDS: int = 15 * 60


# ======================================================================================================================
class _Failures(BaseModel):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    failure_count: int = 0
    locked_until_seconds: float = 0.0


# ======================================================================================================================
class LoginThrottle:

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================

    # ------------------------------------------------------------------------------------------------------------------
    def __init__(
        self,
        allowed_failures: int = _ALLOWED_FAILURES,
        allowed_failures_per_address: int = _ALLOWED_FAILURES_PER_ADDRESS,
    ) -> None:
        # slows down guessing of passwords; it is kept in memory, so a restart forgets it, which is fine for a defence
        # against guessing
        self._lock: threading.Lock = threading.Lock()
        self._failures_by_key: dict[str, _Failures] = {}
        self._allowed_failures: int = allowed_failures
        self._allowed_failures_per_address: int = allowed_failures_per_address

    # ------------------------------------------------------------------------------------------------------------------
    def get_seconds_locked(self, key: str, now_seconds: float | None = None) -> int:
        # how many seconds the key is refused for, 0 when attempts are allowed
        current_seconds: float = time.time() if now_seconds is None else now_seconds
        result: int = 0
        with self._lock:
            failures: _Failures | None = self._failures_by_key.get(key)
            if failures is not None and failures.locked_until_seconds > current_seconds:
                result = int(failures.locked_until_seconds - current_seconds) + 1
        return result

    # ------------------------------------------------------------------------------------------------------------------
    def record_failure(self, key: str, now_seconds: float | None = None) -> None:
        # notes a wrong password under the key, which locks the key once it has had too many; the keys of addresses
        # start with "address:" and are allowed more, since many people can be behind one
        current_seconds: float = time.time() if now_seconds is None else now_seconds
        with self._lock:
            failures: _Failures = self._failures_by_key.setdefault(key, _Failures())
            failures.failure_count += 1

            allowed_failures: int = self._allowed_failures
            if key.startswith("address:"):
                allowed_failures = self._allowed_failures_per_address

            if failures.failure_count >= allowed_failures:
                failures.locked_until_seconds = current_seconds + _LOCKED_SECONDS
                failures.failure_count = 0

    # ------------------------------------------------------------------------------------------------------------------
    def record_success(self, key: str) -> None:
        # forgets the failures under the key, after a right password
        with self._lock:
            self._failures_by_key.pop(key, None)
