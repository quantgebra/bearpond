from typing import NamedTuple
from urllib.parse import ParseResult, urlparse

from fastapi import HTTPException, Request
from starlette.status import HTTP_401_UNAUTHORIZED, HTTP_403_FORBIDDEN

from .. import auth_services
from ..core import actor
from ..core import auth_service
from ..core import session_record
from ..core import token_secrets
from . import cookies

_SAFE_METHODS: list[str] = ["GET", "HEAD", "OPTIONS"]
_CSRF_HEADER: str = "x-csrf-token"


# ======================================================================================================================
class RequestAuth(NamedTuple):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    actor: actor.Actor
    session: session_record.SessionRecord | None


# ======================================================================================================================
class AuthGate:

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================

    # ------------------------------------------------------------------------------------------------------------------
    def __init__(self, cookie_name: str = "auth_session") -> None:
        # stands in front of what the service answers: it finds out who made a request, refuses the ones that nobody
        # made, and protects everything that changes something against other sites making the browser do it (an Origin
        # that is not this service's is refused, and so is a request without the secret of the login); it keeps nothing
        # of a request, so the services of the request are what it is given each time
        self.cookie_name: str = cookie_name

    # ------------------------------------------------------------------------------------------------------------------
    def recognize(self, request: Request, services: auth_services.AuthServices) -> RequestAuth | None:
        # finds out who made the request, from the cookie of a login; in development mode nobody logs in and everybody
        # is the development user
        result: RequestAuth | None = None
        if services.auth.config.mode == "none":
            result = RequestAuth(services.auth.get_development_actor(), None)
        else:
            token: str | None = cookies.get_cookie(request, self.cookie_name)
            if token is not None:
                authenticated: auth_service.AuthenticatedRequest | None = services.auth.authenticate(token)
                if authenticated is not None:
                    result = RequestAuth(authenticated.actor, authenticated.session)
        return result

    # ------------------------------------------------------------------------------------------------------------------
    def check_origin(self, request: Request, services: auth_services.AuthServices) -> None:
        # a request that changes something has to come from this service's own pages: its Origin, when it has one, is
        # this service's
        origin: str | None = request.headers.get("origin")
        service_origin: str = self._get_service_origin(services)
        if request.method not in _SAFE_METHODS and origin is not None and origin != service_origin:
            raise HTTPException(HTTP_403_FORBIDDEN, "forbidden")

    # ------------------------------------------------------------------------------------------------------------------
    def require_login(self, request: Request, services: auth_services.AuthServices) -> RequestAuth:
        # for what only people who are logged in may use, whatever they have been given: it refuses the others, and a
        # request that changes something has to carry the secret of the login besides
        self.check_origin(request, services)
        found: RequestAuth | None = self.recognize(request, services)
        if found is None:
            raise HTTPException(HTTP_401_UNAUTHORIZED, "unauthenticated")

        if request.method not in _SAFE_METHODS and found.session is not None:
            sent_token: str = request.headers.get(_CSRF_HEADER, "")
            if not token_secrets.is_same_token(sent_token, found.session.csrf_token):
                raise HTTPException(HTTP_403_FORBIDDEN, "forbidden")
        return found

    # ------------------------------------------------------------------------------------------------------------------
    def require_actor(self, request: Request, services: auth_services.AuthServices) -> RequestAuth:
        # for what only people who may see the service may use: it refuses everyone else, such as a person who has
        # logged in and been given nothing yet
        found: RequestAuth = self.require_login(request, services)
        baseline_name: str | None = services.auth.config.catalog.baseline_name
        if baseline_name is not None and not found.actor.can(baseline_name):
            raise HTTPException(HTTP_403_FORBIDDEN, "no_access")
        return found

    # ------------------------------------------------------------------------------------------------------------------
    def check_permission(
        self,
        request: Request,
        services: auth_services.AuthServices,
        permission_name: str,
        subject_parameter: str | None = None,
    ) -> RequestAuth:
        # refuses a request that does not come from somebody who may see the service and who has the permission, for
        # the subject that the named path parameter holds when there is one, such as the repository that the request
        # is about
        found: RequestAuth = self.require_actor(request, services)
        subject_ids: list[str] = []
        if subject_parameter is not None:
            subject_ids.append(str(request.path_params.get(subject_parameter, "")))
        if not found.actor.can(permission_name, *subject_ids):
            raise HTTPException(HTTP_403_FORBIDDEN, "forbidden")
        return found

    # ------------------------------------------------------------------------------------------------------------------
    def _get_service_origin(self, services: auth_services.AuthServices) -> str:
        parsed_url: ParseResult = urlparse(services.auth.config.base_url)
        return f"{parsed_url.scheme}://{parsed_url.netloc}"
