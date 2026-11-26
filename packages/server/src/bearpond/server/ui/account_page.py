from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel
from quantgebra.auth import auth_services
from quantgebra.auth.core import auth_config
from quantgebra.auth.core import auth_error
from quantgebra.auth.http import auth_gate

from ..api import dependencies
from . import page_renderer
from . import page_resource


# ======================================================================================================================
class AccountModel(BaseModel):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    username: str
    display_name: str
    # where the account comes from: this server, or the provider that the person logs in through
    issuer: str
    is_local_account: bool
    group_names: list[str]
    # only an account of this server has a password here; a provider keeps its own
    can_change_password: bool
    csrf_token: str
    error_message: str | None
    notice_message: str | None


# ======================================================================================================================
class AccountPage(page_resource.PageResource):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================

    # ------------------------------------------------------------------------------------------------------------------
    def add_routes(self, router: APIRouter) -> None:
        router.add_api_route("/account", self.show_account, methods=["GET"], response_class=HTMLResponse)
        router.add_api_route("/account/password", self.change_password, methods=["POST"], response_class=HTMLResponse)

    # ------------------------------------------------------------------------------------------------------------------
    def show_account(
        self, request: Request, services: auth_services.AuthServices = Depends(dependencies.get_auth_services)
    ) -> Response:
        found: auth_gate.RequestAuth = self._require_viewer(request, services)
        return self._send_account(services, found, None, None, 200)

    # ------------------------------------------------------------------------------------------------------------------
    def change_password(
        self,
        request: Request,
        csrf_token: str = Form("", alias="_csrf"),
        current_password: str = Form(""),
        new_password: str = Form(""),
        new_password_again: str = Form(""),
        services: auth_services.AuthServices = Depends(dependencies.get_auth_services),
    ) -> Response:
        # the present password is needed to choose a new one, which is typed twice; the other logins of the account end
        found: auth_gate.RequestAuth = self._require_form_viewer(request, services, csrf_token)
        if found.session is None:
            raise page_resource.PageRefusedError(400, "There is no login to change the password of.")

        response: Response
        if new_password == new_password_again:
            try:
                services.local.change_password(found.actor, found.session, current_password, new_password)
                response = self._send_account(services, found, None, "Your password has been changed.", 200)
            except auth_error.AuthError as error:
                response = self._send_account(services, found, error.message, None, 400)
        else:
            response = self._send_account(services, found, "The two new passwords are not the same.", None, 400)
        return response

    # ------------------------------------------------------------------------------------------------------------------
    def _send_account(
        self,
        services: auth_services.AuthServices,
        found: auth_gate.RequestAuth,
        error_message: str | None,
        notice_message: str | None,
        status_code: int,
    ) -> HTMLResponse:
        head: page_renderer.PageHead = self._make_head(
            services, found, "Account", "account", [page_renderer.Crumb(label="Account")]
        )
        model: AccountModel = AccountModel(
            username=found.actor.username,
            display_name=found.actor.display_name,
            issuer=found.actor.issuer,
            is_local_account=found.actor.issuer == auth_config.LOCAL_NAME,
            group_names=found.actor.group_names,
            can_change_password=found.actor.issuer == auth_config.LOCAL_NAME and found.session is not None,
            csrf_token=head.user.csrf_token if head.user is not None else "",
            error_message=error_message,
            notice_message=notice_message,
        )
        return self._send("account", head, model, status_code)
