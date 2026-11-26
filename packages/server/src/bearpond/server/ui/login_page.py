from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel
from quantgebra.auth import auth_services
from quantgebra.auth.core import auth_error
from quantgebra.auth.core import auth_service
from quantgebra.auth.http import auth_gate
from quantgebra.auth.http import auth_router
from quantgebra.auth.http import cookies
from quantgebra.auth.oidc import oidc_provider
from quantgebra.auth.oidc import oidc_provider_record
from starlette.status import HTTP_303_SEE_OTHER

from ..api import dependencies
from . import page_renderer
from . import page_resource

_STATUS_BY_ERROR_KIND: dict[str, int] = {
    "invalid_credentials": 401,
    "throttled": 429,
    "disabled": 403,
}


# ======================================================================================================================
class ProviderButton(BaseModel):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    label: str
    href: str


# ======================================================================================================================
class LoginModel(BaseModel):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    next_path: str
    is_local_enabled: bool
    providers: list[ProviderButton]
    # what was typed, kept when the login failed
    username: str
    error_message: str | None


# ======================================================================================================================
class LoginPage(page_resource.PageResource):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================

    # ------------------------------------------------------------------------------------------------------------------
    def add_routes(self, router: APIRouter) -> None:
        router.add_api_route("/login", self.show_login, methods=["GET"], response_class=HTMLResponse)
        router.add_api_route("/login", self.log_in, methods=["POST"], response_class=HTMLResponse)
        router.add_api_route("/logout", self.log_out, methods=["POST"], response_class=HTMLResponse)

    # ------------------------------------------------------------------------------------------------------------------
    def show_login(
        self,
        next_path: str = Query("/", alias="next"),
        services: auth_services.AuthServices = Depends(dependencies.get_auth_services),
    ) -> Response:
        # when nobody logs in there is nothing to do here, and the person goes where they were going
        safe_next_path: str = auth_router.make_safe_next_path(next_path)
        response: Response
        if services.auth.config.mode == "enforced":
            response = self._send_login(services, safe_next_path, "", None, 200)
        else:
            response = RedirectResponse(safe_next_path, status_code=HTTP_303_SEE_OTHER)
        return response

    # ------------------------------------------------------------------------------------------------------------------
    def log_in(
        self,
        request: Request,
        username: str = Form(""),
        password: str = Form(""),
        next_path: str = Form("/", alias="next"),
        services: auth_services.AuthServices = Depends(dependencies.get_auth_services),
    ) -> Response:
        # a login that works ends on the page the person was going to, with the cookie of the login; one that does not
        # shows the form again with what went wrong, keeping the user name
        dependencies.get_auth_gate().check_origin(request, services)
        safe_next_path: str = auth_router.make_safe_next_path(next_path)
        address: str = request.client.host if request.client is not None else "unknown"
        response: Response
        try:
            login: auth_service.Login = services.local.login(username, password, address)
            response = RedirectResponse(safe_next_path, status_code=HTTP_303_SEE_OTHER)
            cookie_name: str = dependencies.get_auth_gate().cookie_name
            cookies.set_session_cookie(response, services.auth.config, cookie_name, login.token)
        except auth_error.AuthError as error:
            status_code: int = _STATUS_BY_ERROR_KIND.get(error.kind, 400)
            response = self._send_login(services, safe_next_path, username, error.message, status_code)
        return response

    # ------------------------------------------------------------------------------------------------------------------
    def log_out(
        self,
        request: Request,
        csrf_token: str = Form("", alias="_csrf"),
        services: auth_services.AuthServices = Depends(dependencies.get_auth_services),
    ) -> Response:
        # ends the login, and the one at the provider too when the person logged in through one that can end it
        found: auth_gate.RequestAuth = self._require_form_viewer(request, services, csrf_token)
        token: str | None = cookies.get_cookie(request, dependencies.get_auth_gate().cookie_name)
        redirect_path: str = "/login"
        if found.session is not None:
            provider: oidc_provider.OidcProvider | None = services.providers.find_provider(
                services.session, found.session.provider_name
            )
            if provider is not None:
                redirect_path = provider.get_logout_url(services.auth.config.base_url) or redirect_path
        if token is not None:
            services.auth.logout(token)

        response: RedirectResponse = RedirectResponse(redirect_path, status_code=HTTP_303_SEE_OTHER)
        cookies.clear_session_cookie(response, services.auth.config, dependencies.get_auth_gate().cookie_name)
        return response

    # ------------------------------------------------------------------------------------------------------------------
    def _send_login(
        self,
        services: auth_services.AuthServices,
        next_path: str,
        username: str,
        error_message: str | None,
        status_code: int,
    ) -> HTMLResponse:
        # the form for an account of this server, when there are any, and a button for each provider
        providers: list[ProviderButton] = []
        for settings in oidc_provider_record.OidcProviderRecord.get_all_settings(services.session):
            href: str = f"/auth/oidc/{quote(settings.provider_name)}/login?next={quote(next_path, safe='/')}"
            providers.append(ProviderButton(label=settings.label, href=href))
        model: LoginModel = LoginModel(
            next_path=next_path,
            is_local_enabled=services.auth.config.is_local_enabled,
            providers=providers,
            username=username,
            error_message=error_message,
        )
        head: page_renderer.PageHead = page_renderer.PageHead(title="Log in", section="", is_bare=True)
        return self._send("login", head, model, status_code)
