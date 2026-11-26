from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel
from quantgebra.auth import auth_services
from quantgebra.auth.apikey import api_key_authentication
from quantgebra.auth.core import auth_config
from quantgebra.auth.core import auth_error
from quantgebra.auth.core import auth_service
from quantgebra.auth.core import group_record
from quantgebra.auth.core import user_record
from quantgebra.auth.http import auth_gate
from starlette.status import HTTP_303_SEE_OTHER

from ..api import dependencies
from . import page_renderer
from . import page_resource
from . import users_page

_KEYS_PATH: str = "/account/keys"


# ======================================================================================================================
class KeyRow(BaseModel):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    key_id: str
    # the first characters of the secret, which tell keys apart; the secret itself is never shown again
    prefix: str
    key_name: str
    owner_display_name: str
    owner_username: str
    is_owner_service_account: bool
    created_at: str
    expires_at: str | None
    last_used_at: str | None
    is_expired: bool


# ======================================================================================================================
class OwnerOption(BaseModel):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    user_id: str
    label: str
    is_selected: bool


# ======================================================================================================================
class NewKeyBox(BaseModel):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    key_name: str
    token: str


# ======================================================================================================================
class ApiKeysModel(BaseModel):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    # nobody logs in, so there is nobody for a key to stand for
    is_unavailable: bool
    is_administrator: bool
    own_rows: list[KeyRow]
    all_rows: list[KeyRow]
    owner_options: list[OwnerOption]
    group_options: list[users_page.GroupOption]
    # a key that was just made, whose secret is shown once
    new_key: NewKeyBox | None
    base_url: str
    csrf_token: str
    notice_message: str | None
    error_message: str | None


# ======================================================================================================================
class ApiKeysPage(page_resource.PageResource):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def make_key_rows(keys: list[api_key_authentication.ApiKeyView]) -> list[KeyRow]:
        rows: list[KeyRow] = []
        for key in keys:
            row: KeyRow = KeyRow(
                key_id=key.key_id,
                prefix=key.prefix,
                key_name=key.key_name,
                owner_display_name=key.owner_display_name,
                owner_username=key.owner_username,
                is_owner_service_account=key.is_owner_service_account,
                created_at=key.created_datetime.isoformat(),
                expires_at=key.expires_datetime.isoformat() if key.expires_datetime is not None else None,
                last_used_at=key.last_used_datetime.isoformat() if key.last_used_datetime is not None else None,
                is_expired=key.is_expired,
            )
            rows.append(row)
        return rows

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def parse_days(expires_in_days_text: str) -> int | None:
        # the days in the field of the form, None when it is left empty
        stripped_text: str = expires_in_days_text.strip()
        days: int | None = None
        if stripped_text:
            if stripped_text.isdigit():
                days = int(stripped_text)
            else:
                raise auth_error.AuthError("invalid_request", "The days until the key stops working are a number.")
        return days

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================

    # ------------------------------------------------------------------------------------------------------------------
    def add_routes(self, router: APIRouter) -> None:
        router.add_api_route(_KEYS_PATH, self.show_keys, methods=["GET"], response_class=HTMLResponse)
        router.add_api_route(_KEYS_PATH, self.create_key, methods=["POST"], response_class=HTMLResponse)
        router.add_api_route(
            f"{_KEYS_PATH}/{{key_id}}/revoke", self.revoke_key, methods=["POST"], response_class=HTMLResponse
        )
        router.add_api_route(
            f"{_KEYS_PATH}/service-accounts",
            self.create_service_account,
            methods=["POST"],
            response_class=HTMLResponse,
        )

    # ------------------------------------------------------------------------------------------------------------------
    def show_keys(
        self, request: Request, services: auth_services.AuthServices = Depends(dependencies.get_auth_services)
    ) -> Response:
        found: auth_gate.RequestAuth = self._require_viewer(request, services)
        return self._send_keys(services, found, None, None, None, None, 200)

    # ------------------------------------------------------------------------------------------------------------------
    def create_key(
        self,
        request: Request,
        csrf_token: str = Form("", alias="_csrf"),
        key_name: str = Form(""),
        expires_in_days_text: str = Form("", alias="expires_in_days"),
        user_id: str = Form(""),
        services: auth_services.AuthServices = Depends(dependencies.get_auth_services),
    ) -> Response:
        # a key for the person themselves, unless an administrator chose another user; its secret is shown once
        found: auth_gate.RequestAuth = self._require_form_viewer(request, services, csrf_token)
        response: Response
        try:
            key_request: api_key_authentication.ApiKeyRequest = api_key_authentication.ApiKeyRequest(
                user_id=user_id or found.actor.user_id,
                key_name=key_name,
                expires_in_days=ApiKeysPage.parse_days(expires_in_days_text),
            )
            new_key: api_key_authentication.NewApiKey = services.api_keys.create_key(found.actor, key_request)
            new_key_box: NewKeyBox = NewKeyBox(key_name=new_key.key.key_name, token=new_key.token)
            response = self._send_keys(services, found, new_key_box, None, None, None, 200)
        except auth_error.AuthError as error:
            services.session.rollback()
            status_code: int = page_resource.get_status_code(error)
            response = self._send_keys(services, found, None, None, error.message, None, status_code)
        return response

    # ------------------------------------------------------------------------------------------------------------------
    def revoke_key(
        self,
        request: Request,
        key_id: str,
        csrf_token: str = Form("", alias="_csrf"),
        services: auth_services.AuthServices = Depends(dependencies.get_auth_services),
    ) -> Response:
        # the key stops working at once
        found: auth_gate.RequestAuth = self._require_form_viewer(request, services, csrf_token)
        response: Response
        try:
            services.api_keys.revoke_key(found.actor, key_id)
            response = RedirectResponse(_KEYS_PATH, status_code=HTTP_303_SEE_OTHER)
        except auth_error.AuthError as error:
            services.session.rollback()
            status_code: int = page_resource.get_status_code(error)
            response = self._send_keys(services, found, None, None, error.message, None, status_code)
        return response

    # ------------------------------------------------------------------------------------------------------------------
    def create_service_account(
        self,
        request: Request,
        csrf_token: str = Form("", alias="_csrf"),
        username: str = Form(""),
        display_name: str = Form(""),
        group_names: list[str] = Form([], alias="groups"),
        services: auth_services.AuthServices = Depends(dependencies.get_auth_services),
    ) -> Response:
        # a user for a program, which the form for a key then has chosen, since a key is what it needs next
        found: auth_gate.RequestAuth = self._require_form_administrator(request, services, csrf_token)
        response: Response
        try:
            account_request: api_key_authentication.ServiceAccountRequest = (
                api_key_authentication.ServiceAccountRequest(
                    username=username, display_name=display_name, group_names=group_names
                )
            )
            account: user_record.UserRecord = services.api_keys.create_service_account(found.actor, account_request)
            notice_message: str = f"The service account {account.username} was made. Now make a key for it."
            response = self._send_keys(services, found, None, notice_message, None, account.user_id, 200)
        except auth_error.AuthError as error:
            services.session.rollback()
            status_code: int = page_resource.get_status_code(error)
            response = self._send_keys(services, found, None, None, error.message, None, status_code)
        return response

    # ------------------------------------------------------------------------------------------------------------------
    def _send_keys(
        self,
        services: auth_services.AuthServices,
        found: auth_gate.RequestAuth,
        new_key: NewKeyBox | None,
        notice_message: str | None,
        error_message: str | None,
        selected_user_id: str | None,
        status_code: int,
    ) -> HTMLResponse:
        # the person's keys and the form to make one; an administrator also sees every key, picks who a key acts as,
        # and makes service accounts; with nobody logging in there is nobody for a key to stand for
        is_unavailable: bool = services.auth.config.mode == "none"
        is_administrator: bool = found.actor.can(services.auth.config.catalog.admin_name) and not is_unavailable
        own_rows: list[KeyRow] = []
        all_rows: list[KeyRow] = []
        owner_options: list[OwnerOption] = []
        group_options: list[users_page.GroupOption] = []
        if not is_unavailable:
            own_rows = ApiKeysPage.make_key_rows(services.api_keys.list_own_keys(found.actor))
        if is_administrator:
            all_rows = ApiKeysPage.make_key_rows(services.api_keys.list_all_keys(found.actor))
            owner_options = self._make_owner_options(services, found, selected_user_id or found.actor.user_id)
            groups: list[group_record.GroupRecord] = services.groups.list_groups(found.actor)
            group_options = users_page.UsersPage.make_group_options(groups, [])

        head: page_renderer.PageHead = self._make_head(
            services,
            found,
            "API keys",
            "account-keys",
            [page_renderer.Crumb(label="Account", href="/account"), page_renderer.Crumb(label="API keys")],
        )
        model: ApiKeysModel = ApiKeysModel(
            is_unavailable=is_unavailable,
            is_administrator=is_administrator,
            own_rows=own_rows,
            all_rows=all_rows,
            owner_options=owner_options,
            group_options=group_options,
            new_key=new_key,
            base_url=services.auth.config.base_url.rstrip("/"),
            csrf_token=head.user.csrf_token if head.user is not None else "",
            notice_message=notice_message,
            error_message=error_message,
        )
        return self._send("api_keys", head, model, status_code)

    # ------------------------------------------------------------------------------------------------------------------
    def _make_owner_options(
        self, services: auth_services.AuthServices, found: auth_gate.RequestAuth, selected_user_id: str
    ) -> list[OwnerOption]:
        # the users that an administrator can make a key for, which are the ones that are not disabled
        options: list[OwnerOption] = []
        for user in services.auth.list_users(found.actor):
            if not user.is_disabled:
                option: OwnerOption = OwnerOption(
                    user_id=user.user_id,
                    label=self._make_owner_label(user),
                    is_selected=user.user_id == selected_user_id,
                )
                options.append(option)
        return options

    # ------------------------------------------------------------------------------------------------------------------
    def _make_owner_label(self, user: auth_service.UserSummary) -> str:
        suffix: str = ", service account" if user.issuer == auth_config.SERVICE_NAME else ""
        return f"{user.display_name} ({user.username}){suffix}"
