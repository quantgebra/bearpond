import logging
from collections.abc import Iterator
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from starlette.status import (
    HTTP_204_NO_CONTENT,
    HTTP_303_SEE_OTHER,
    HTTP_400_BAD_REQUEST,
    HTTP_401_UNAUTHORIZED,
    HTTP_403_FORBIDDEN,
    HTTP_404_NOT_FOUND,
    HTTP_429_TOO_MANY_REQUESTS,
    HTTP_500_INTERNAL_SERVER_ERROR,
)

from .. import auth_services
from ..core import auth_config
from ..core import auth_error
from ..core import auth_service
from ..local import local_authentication
from ..oidc import external_identity
from ..oidc import oidc_provider
from ..oidc import oidc_provider_record
from ..oidc import pending_logins
from . import auth_gate
from . import cookies

logger: logging.Logger = logging.getLogger(__name__)

_LOGIN_FAILED_MESSAGE: str = "Logging in failed. Try again, or ask an administrator."

_STATUS_BY_KIND: dict[str, int] = {
    "invalid_request": HTTP_400_BAD_REQUEST,
    "invalid_credentials": HTTP_401_UNAUTHORIZED,
    "invalid_invite": HTTP_400_BAD_REQUEST,
    "weak_password": HTTP_400_BAD_REQUEST,
    "throttled": HTTP_429_TOO_MANY_REQUESTS,
    "disabled": HTTP_403_FORBIDDEN,
    "forbidden": HTTP_403_FORBIDDEN,
    "unauthenticated": HTTP_401_UNAUTHORIZED,
}


# ----------------------------------------------------------------------------------------------------------------------
def to_http_exception(error: auth_error.AuthError) -> HTTPException:
    # what an error of authentication is to the person who made the request
    return HTTPException(_STATUS_BY_KIND.get(error.kind, HTTP_400_BAD_REQUEST), error.message)


# ----------------------------------------------------------------------------------------------------------------------
def make_safe_next_path(next_path: str) -> str:
    # where to send a person after they log in, which can only be a path of this service, never another site
    result: str = "/"
    if next_path.startswith("/") and not next_path.startswith("//") and "\\" not in next_path:
        result = next_path
    return result


# ======================================================================================================================
class LoginBody(BaseModel):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    username: str
    password: str


# ======================================================================================================================
class AcceptInviteBody(BaseModel):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    password: str


# ======================================================================================================================
class ChangePasswordBody(BaseModel):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    current_password: str
    new_password: str


# ======================================================================================================================
class ProviderOption(BaseModel):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    provider_name: str
    label: str


# ======================================================================================================================
class AuthOptions(BaseModel):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    mode: str
    is_local_enabled: bool
    providers: list[ProviderOption]


# ======================================================================================================================
class MeResponse(BaseModel):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    user_id: str
    username: str
    display_name: str
    issuer: str
    group_names: list[str]
    csrf_token: str


# ======================================================================================================================
class LogoutResponse(BaseModel):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    redirect_url: str | None


# ======================================================================================================================
class AuthRouter:

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================

    # ------------------------------------------------------------------------------------------------------------------
    def __init__(
        self,
        services_dependency: Callable[..., Iterator[auth_services.AuthServices] | auth_services.AuthServices],
        gate_provider: Callable[[], auth_gate.AuthGate],
        prefix: str = "/auth",
    ) -> None:
        # the endpoints that people log in and out with; what a page for logging in or choosing a password looks like
        # is the application's, which calls these; the services of a request are made by a dependency of the
        # application's, over the session that it owns for the request, and are put where the endpoints find them; the
        # gate is asked for on every request, so that an application that is configured when it first has something
        # to do can make this before it is
        self._get_gate: Callable[[], auth_gate.AuthGate] = gate_provider
        self._prefix: str = prefix

        def attach_services(
            request: Request, services: auth_services.AuthServices = Depends(services_dependency)
        ) -> None:
            request.state.auth_services = services

        self.router: APIRouter = APIRouter(prefix=prefix, tags=["auth"], dependencies=[Depends(attach_services)])
        self.router.add_api_route("/options", self.get_options, methods=["GET"])
        self.router.add_api_route("/login", self.log_in, methods=["POST"])
        self.router.add_api_route("/logout", self.log_out, methods=["POST"])
        self.router.add_api_route("/me", self.get_me, methods=["GET"])
        self.router.add_api_route("/password", self.change_password, methods=["POST"], status_code=HTTP_204_NO_CONTENT)
        self.router.add_api_route("/invite/{token}", self.get_invite, methods=["GET"])
        self.router.add_api_route("/invite/{token}", self.accept_invite, methods=["POST"])
        self.router.add_api_route("/oidc/{provider_name}/login", self.begin_oidc_login, methods=["GET"])
        self.router.add_api_route("/oidc/{provider_name}/callback", self.finish_oidc_login, methods=["GET"])

    # ------------------------------------------------------------------------------------------------------------------
    def get_options(self, request: Request) -> AuthOptions:
        # how people can log in, for the page that asks them to
        services: auth_services.AuthServices = self._get_services(request)
        provider_options: list[ProviderOption] = []
        for provider_settings in oidc_provider_record.OidcProviderRecord.get_all_settings(services.session):
            option: ProviderOption = ProviderOption(
                provider_name=provider_settings.provider_name, label=provider_settings.label
            )
            provider_options.append(option)
        config: auth_config.AuthConfig = services.auth.config
        return AuthOptions(mode=config.mode, is_local_enabled=config.is_local_enabled, providers=provider_options)

    # ------------------------------------------------------------------------------------------------------------------
    def log_in(self, body: LoginBody, request: Request, response: Response) -> MeResponse:
        services: auth_services.AuthServices = self._get_services(request)
        self._get_gate().check_origin(request, services)
        address: str = request.client.host if request.client is not None else "unknown"
        try:
            login: auth_service.Login = services.local.login(body.username, body.password, address)
        except auth_error.AuthError as error:
            raise to_http_exception(error) from error

        cookies.set_session_cookie(response, services.auth.config, self._get_gate().cookie_name, login.token)
        return self._make_me_response(services, login)

    # ------------------------------------------------------------------------------------------------------------------
    def log_out(self, request: Request, response: Response) -> LogoutResponse:
        # ends the login that the cookie stands for, and says where to send the person to end their login at the
        # provider too, when there is one
        services: auth_services.AuthServices = self._get_services(request)
        self._get_gate().check_origin(request, services)
        token: str | None = cookies.get_cookie(request, self._get_gate().cookie_name)
        redirect_url: str | None = None
        if token is not None:
            authenticated: auth_service.AuthenticatedRequest | None = services.auth.authenticate(token)
            if authenticated is not None:
                provider: oidc_provider.OidcProvider | None = services.providers.find_provider(
                    services.session, authenticated.session.provider_name
                )
                if provider is not None:
                    redirect_url = provider.get_logout_url(services.auth.config.base_url)
            services.auth.logout(token)

        cookies.clear_session_cookie(response, services.auth.config, self._get_gate().cookie_name)
        return LogoutResponse(redirect_url=redirect_url)

    # ------------------------------------------------------------------------------------------------------------------
    def get_me(self, request: Request) -> MeResponse:
        found: auth_gate.RequestAuth = self._get_gate().require_login(request, self._get_services(request))
        csrf_token: str = "" if found.session is None else found.session.csrf_token
        return MeResponse(
            user_id=found.actor.user_id,
            username=found.actor.username,
            display_name=found.actor.display_name,
            issuer=found.actor.issuer,
            group_names=found.actor.group_names,
            csrf_token=csrf_token,
        )

    # ------------------------------------------------------------------------------------------------------------------
    def change_password(self, body: ChangePasswordBody, request: Request) -> None:
        services: auth_services.AuthServices = self._get_services(request)
        found: auth_gate.RequestAuth = self._get_gate().require_login(request, services)
        if found.session is None:
            raise HTTPException(HTTP_400_BAD_REQUEST, "There is no login to change the password of.")
        try:
            services.local.change_password(found.actor, found.session, body.current_password, body.new_password)
        except auth_error.AuthError as error:
            raise to_http_exception(error) from error

    # ------------------------------------------------------------------------------------------------------------------
    def get_invite(self, token: str, request: Request) -> local_authentication.InviteInfo:
        info: local_authentication.InviteInfo | None = self._get_services(request).local.get_invite_info(token)
        if info is None:
            raise HTTPException(HTTP_404_NOT_FOUND, "This link has been used, has expired, or is not right.")
        return info

    # ------------------------------------------------------------------------------------------------------------------
    def accept_invite(self, token: str, body: AcceptInviteBody, request: Request, response: Response) -> MeResponse:
        services: auth_services.AuthServices = self._get_services(request)
        self._get_gate().check_origin(request, services)
        try:
            login: auth_service.Login = services.local.accept_invite(token, body.password)
        except auth_error.AuthError as error:
            raise to_http_exception(error) from error

        cookies.set_session_cookie(response, services.auth.config, self._get_gate().cookie_name, login.token)
        return self._make_me_response(services, login)

    # ------------------------------------------------------------------------------------------------------------------
    def begin_oidc_login(
        self, provider_name: str, request: Request, next_path: str = Query("/", alias="next")
    ) -> RedirectResponse:
        # sends the person to the provider, and remembers what ties their coming back to this login
        services: auth_services.AuthServices = self._get_services(request)
        provider: oidc_provider.OidcProvider | None = services.providers.find_provider(services.session, provider_name)
        if provider is None:
            raise HTTPException(HTTP_404_NOT_FOUND, "There is no such login method.")

        try:
            start: oidc_provider.LoginStart = provider.begin(self._get_callback_address(services, provider_name))
        except (oidc_provider.OidcLoginError, auth_error.AuthError) as error:
            logger.error(f"could not start a login through the provider {provider_name}: {error}")
            raise HTTPException(HTTP_400_BAD_REQUEST, _LOGIN_FAILED_MESSAGE) from error

        services.pending_logins.add_started(provider_name, start.pending, make_safe_next_path(next_path))
        return RedirectResponse(start.url, status_code=HTTP_303_SEE_OTHER)

    # ------------------------------------------------------------------------------------------------------------------
    def finish_oidc_login(self, provider_name: str, request: Request) -> RedirectResponse:
        # where the provider sends the person back to; the login must be one that this service started, for this
        # provider, and the provider's proof must check out; what the provider said is only logged, not shown
        services: auth_services.AuthServices = self._get_services(request)
        provider: oidc_provider.OidcProvider | None = services.providers.find_provider(services.session, provider_name)
        started: pending_logins.StartedLogin | None = services.pending_logins.take_started(
            request.query_params.get("state", "")
        )
        if provider is None or started is None or started.provider_name != provider_name:
            raise HTTPException(HTTP_400_BAD_REQUEST, "The login was not started here, or took too long. Try again.")

        try:
            callback_url: str = f"{self._get_callback_address(services, provider_name)}?{request.url.query}"
            identity: external_identity.ExternalIdentity = provider.finish(callback_url, started.pending)
            login: auth_service.Login = services.oidc.login(provider.settings, identity)
        except auth_error.AuthError as error:
            raise to_http_exception(error) from error
        except oidc_provider.OidcLoginError as error:
            logger.error(f"logging in through the provider {provider_name} failed: {error}")
            raise HTTPException(HTTP_400_BAD_REQUEST, _LOGIN_FAILED_MESSAGE) from error

        response: RedirectResponse = RedirectResponse(started.next_path, status_code=HTTP_303_SEE_OTHER)
        cookies.set_session_cookie(response, services.auth.config, self._get_gate().cookie_name, login.token)
        return response

    # ------------------------------------------------------------------------------------------------------------------
    def _get_services(self, request: Request) -> auth_services.AuthServices:
        # the services that the dependency of the router made for this request
        services: auth_services.AuthServices = request.state.auth_services
        return services

    # ------------------------------------------------------------------------------------------------------------------
    def _get_callback_address(self, services: auth_services.AuthServices, provider_name: str) -> str:
        # the address that the provider sends people back to, which is the one that is registered with it
        base_url: str = services.auth.config.base_url.rstrip("/")
        return f"{base_url}{self._prefix}/oidc/{provider_name}/callback"

    # ------------------------------------------------------------------------------------------------------------------
    def _make_me_response(self, services: auth_services.AuthServices, login: auth_service.Login) -> MeResponse:
        authenticated: auth_service.AuthenticatedRequest | None = services.auth.authenticate(login.token)
        if authenticated is None:
            raise HTTPException(HTTP_500_INTERNAL_SERVER_ERROR, "The login that was just made cannot be used.")
        return MeResponse(
            user_id=authenticated.actor.user_id,
            username=authenticated.actor.username,
            display_name=authenticated.actor.display_name,
            issuer=authenticated.actor.issuer,
            group_names=authenticated.actor.group_names,
            csrf_token=login.session.csrf_token,
        )
