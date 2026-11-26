from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel
from quantgebra.auth import auth_services
from quantgebra.auth.core import auth_error
from quantgebra.auth.core import auth_service
from quantgebra.auth.http import cookies
from quantgebra.auth.local import local_authentication
from starlette.status import HTTP_303_SEE_OTHER

from ..api import dependencies
from . import page_renderer
from . import page_resource


# ======================================================================================================================
class InviteModel(BaseModel):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    token: str
    username: str
    display_name: str
    # a reset link chooses a new password for an account that has one already
    is_reset: bool
    error_message: str | None


# ======================================================================================================================
class InvitePage(page_resource.PageResource):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================

    # ------------------------------------------------------------------------------------------------------------------
    def add_routes(self, router: APIRouter) -> None:
        router.add_api_route("/invite/{token}", self.show_invite, methods=["GET"], response_class=HTMLResponse)
        router.add_api_route("/invite/{token}", self.accept_invite, methods=["POST"], response_class=HTMLResponse)

    # ------------------------------------------------------------------------------------------------------------------
    def show_invite(
        self, token: str, services: auth_services.AuthServices = Depends(dependencies.get_auth_services)
    ) -> Response:
        # the link of an invitation, or of a reset, where the person chooses their password
        info: local_authentication.InviteInfo = self._require_invite_info(services, token)
        return self._send_invite(token, info, None, 200)

    # ------------------------------------------------------------------------------------------------------------------
    def accept_invite(
        self,
        request: Request,
        token: str,
        password: str = Form(""),
        password_again: str = Form(""),
        services: auth_services.AuthServices = Depends(dependencies.get_auth_services),
    ) -> Response:
        # a password that is typed the same twice and is strong enough uses up the link and logs the person in
        dependencies.get_auth_gate().check_origin(request, services)
        info: local_authentication.InviteInfo = self._require_invite_info(services, token)
        response: Response
        if password == password_again:
            try:
                login: auth_service.Login = services.local.accept_invite(token, password)
                response = RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)
                cookie_name: str = dependencies.get_auth_gate().cookie_name
                cookies.set_session_cookie(response, services.auth.config, cookie_name, login.token)
            except auth_error.AuthError as error:
                response = self._send_invite(token, info, error.message, 400)
        else:
            response = self._send_invite(token, info, "The two passwords are not the same.", 400)
        return response

    # ------------------------------------------------------------------------------------------------------------------
    def _require_invite_info(self, services: auth_services.AuthServices, token: str) -> local_authentication.InviteInfo:
        info: local_authentication.InviteInfo | None = services.local.get_invite_info(token)
        if info is None:
            raise page_resource.PageRefusedError(
                404, "This link has been used, has expired, or is not right. Ask an administrator for a new one."
            )
        return info

    # ------------------------------------------------------------------------------------------------------------------
    def _send_invite(
        self, token: str, info: local_authentication.InviteInfo, error_message: str | None, status_code: int
    ) -> HTMLResponse:
        model: InviteModel = InviteModel(
            token=token,
            username=info.username,
            display_name=info.display_name,
            is_reset=info.purpose == "reset",
            error_message=error_message,
        )
        title: str = "Choose a new password" if model.is_reset else "Welcome"
        head: page_renderer.PageHead = page_renderer.PageHead(title=title, section="", is_bare=True)
        return self._send("invite", head, model, status_code)
