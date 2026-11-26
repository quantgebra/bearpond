from fastapi import Request
from quantgebra.auth import auth_services
from quantgebra.auth.core import actor
from quantgebra.auth.core import grant
from quantgebra.auth.core import token_secrets
from quantgebra.auth.http import auth_gate


# ======================================================================================================================
class TokenAuthGate(auth_gate.AuthGate):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================

    # ------------------------------------------------------------------------------------------------------------------
    def __init__(self, static_token: str | None, cookie_name: str = "bearpond_session") -> None:
        # the gate of the library with one more way of being recognized: the shared bearer token that scripts and the
        # command line send until they have tokens of their own; whoever sends it is an administrator, as before there
        # were users
        super().__init__(cookie_name)
        self._static_token: str | None = static_token

    # ------------------------------------------------------------------------------------------------------------------
    def recognize(self, request: Request, services: auth_services.AuthServices) -> auth_gate.RequestAuth | None:
        # a token that is right makes an administrator; when there is a token and nobody logs in, a request without it
        # is refused, which is how the server has always been protected; otherwise logins are what recognize people
        result: auth_gate.RequestAuth | None = None
        sent_token: str = self._read_bearer_token(request)

        if self._static_token is not None and token_secrets.is_same_token(sent_token, self._static_token):
            result = auth_gate.RequestAuth(self._make_token_actor(services), None)
        elif self._static_token is None or services.auth.config.mode == "enforced":
            result = super().recognize(request, services)
        return result

    # ------------------------------------------------------------------------------------------------------------------
    def _read_bearer_token(self, request: Request) -> str:
        header: str = request.headers.get("authorization", "")
        return header.removeprefix("Bearer ") if header.startswith("Bearer ") else ""

    # ------------------------------------------------------------------------------------------------------------------
    def _make_token_actor(self, services: auth_services.AuthServices) -> actor.Actor:
        admin_grant: grant.Grant = grant.Grant(permission=services.auth.config.catalog.admin_name)
        result: actor.Actor = actor.Actor(
            "token",
            "token",
            "Shared token",
            "token",
            [services.auth.config.administrators_group_name],
            [admin_grant],
            services.auth.config.catalog,
        )
        return result
