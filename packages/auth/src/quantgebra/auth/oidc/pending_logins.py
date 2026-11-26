import threading
import time

from pydantic import BaseModel

from . import oidc_provider

_LOGIN_SECONDS: int = 10 * 60
_MAXIMUM_PENDING: int = 1000


# ======================================================================================================================
class StartedLogin(BaseModel):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    provider_name: str
    pending: oidc_provider.PendingLogin
    next_path: str
    expires_seconds: float


# ======================================================================================================================
class PendingLogins:

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================

    # ------------------------------------------------------------------------------------------------------------------
    def __init__(self) -> None:
        # the logins that were started at a provider and not finished, by their state, which is what ties the answer to
        # the login that was started; in memory, since a person has a few minutes to come back
        self._lock: threading.Lock = threading.Lock()
        self._started_by_state: dict[str, StartedLogin] = {}

    # ------------------------------------------------------------------------------------------------------------------
    def add_started(self, provider_name: str, pending: oidc_provider.PendingLogin, next_path: str) -> None:
        now_seconds: float = time.time()
        started: StartedLogin = StartedLogin(
            provider_name=provider_name,
            pending=pending,
            next_path=next_path,
            expires_seconds=now_seconds + _LOGIN_SECONDS,
        )
        with self._lock:
            self._forget_old(now_seconds)
            self._started_by_state[pending.state] = started

    # ------------------------------------------------------------------------------------------------------------------
    def take_started(self, state: str) -> StartedLogin | None:
        # the login that was started with the state, which is forgotten as it is taken; None for one that was not
        # started here, was taken before, or took too long
        with self._lock:
            started: StartedLogin | None = self._started_by_state.pop(state, None)
        result: StartedLogin | None = None
        if started is not None and started.expires_seconds >= time.time():
            result = started
        return result

    # ------------------------------------------------------------------------------------------------------------------
    def _forget_old(self, now_seconds: float) -> None:
        # forgets the logins that were not finished in time, and the oldest ones when there are too many, so that nobody
        # can fill the memory by starting logins; the caller holds the lock
        for state in list(self._started_by_state):
            if self._started_by_state[state].expires_seconds < now_seconds:
                del self._started_by_state[state]

        while len(self._started_by_state) >= _MAXIMUM_PENDING:
            oldest_state: str = next(iter(self._started_by_state))
            del self._started_by_state[oldest_state]
