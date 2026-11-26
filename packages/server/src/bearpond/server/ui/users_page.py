from collections.abc import Callable

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel
from quantgebra.auth import auth_services
from quantgebra.auth.core import auth_config
from quantgebra.auth.core import auth_error
from quantgebra.auth.core import auth_service
from quantgebra.auth.core import group_record
from quantgebra.auth.http import auth_gate
from quantgebra.auth.local import local_authentication
from starlette.status import HTTP_303_SEE_OTHER

from ..api import dependencies
from . import page_renderer
from . import page_resource

_USERS_PATH: str = "/admin/users"


# ======================================================================================================================
class GroupOption(BaseModel):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    group_name: str
    is_selected: bool


# ======================================================================================================================
class UserRow(BaseModel):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    user_id: str
    username: str
    display_name: str
    initials: str
    # this server, API keys for a service account, or the name of the provider that the person logs in through
    issuer_text: str
    is_local: bool
    is_service_account: bool
    # invited, but the link to choose a password has not been used yet
    is_waiting_for_password: bool
    is_disabled: bool
    # the administrator looking at the page, who cannot turn their own account off
    is_self: bool
    group_names: list[str]
    group_options: list[GroupOption]
    last_login_at: str | None


# ======================================================================================================================
class LinkBox(BaseModel):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    url: str
    expires_at: str
    # who the link is for, as the page says it
    recipient_text: str


# ======================================================================================================================
class UsersModel(BaseModel):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    rows: list[UserRow]
    group_options: list[GroupOption]
    csrf_token: str
    # a link that was just made, shown once
    link: LinkBox | None
    error_message: str | None


# ======================================================================================================================
class UsersPage(page_resource.PageResource):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def make_group_options(groups: list[group_record.GroupRecord], selected_names: list[str]) -> list[GroupOption]:
        options: list[GroupOption] = []
        for group in groups:
            option: GroupOption = GroupOption(
                group_name=group.group_name, is_selected=group.group_name in selected_names
            )
            options.append(option)
        return options

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def make_link_box(link: local_authentication.InviteLink, recipient_text: str) -> LinkBox:
        return LinkBox(url=link.url, expires_at=link.expires_datetime.isoformat(), recipient_text=recipient_text)

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================

    # ------------------------------------------------------------------------------------------------------------------
    def add_routes(self, router: APIRouter) -> None:
        router.add_api_route(_USERS_PATH, self.show_users, methods=["GET"], response_class=HTMLResponse)
        router.add_api_route(f"{_USERS_PATH}/invite", self.invite_user, methods=["POST"], response_class=HTMLResponse)
        router.add_api_route(
            f"{_USERS_PATH}/{{user_id}}/groups", self.set_user_groups, methods=["POST"], response_class=HTMLResponse
        )
        router.add_api_route(
            f"{_USERS_PATH}/{{user_id}}/disable", self.disable_user, methods=["POST"], response_class=HTMLResponse
        )
        router.add_api_route(
            f"{_USERS_PATH}/{{user_id}}/enable", self.enable_user, methods=["POST"], response_class=HTMLResponse
        )
        router.add_api_route(
            f"{_USERS_PATH}/{{user_id}}/reset", self.make_reset_link, methods=["POST"], response_class=HTMLResponse
        )

    # ------------------------------------------------------------------------------------------------------------------
    def show_users(
        self, request: Request, services: auth_services.AuthServices = Depends(dependencies.get_auth_services)
    ) -> Response:
        found: auth_gate.RequestAuth = self._require_administrator(request, services)
        return self._send_users(services, found, None, None, 200)

    # ------------------------------------------------------------------------------------------------------------------
    def invite_user(
        self,
        request: Request,
        csrf_token: str = Form("", alias="_csrf"),
        username: str = Form(""),
        display_name: str = Form(""),
        group_names: list[str] = Form([], alias="groups"),
        services: auth_services.AuthServices = Depends(dependencies.get_auth_services),
    ) -> Response:
        # makes an account of this server for a person, and the link that they choose their password with, which is
        # shown once and has to be given to them
        found: auth_gate.RequestAuth = self._require_form_administrator(request, services, csrf_token)
        response: Response
        try:
            invite_request: local_authentication.InviteRequest = local_authentication.InviteRequest(
                username=username, display_name=display_name, group_names=group_names
            )
            link: local_authentication.InviteLink = services.local.invite(found.actor, invite_request)
            link_box: LinkBox = UsersPage.make_link_box(link, username.strip().lower())
            response = self._send_users(services, found, link_box, None, 200)
        except auth_error.AuthError as error:
            services.session.rollback()
            response = self._send_users(services, found, None, error.message, page_resource.get_status_code(error))
        return response

    # ------------------------------------------------------------------------------------------------------------------
    def set_user_groups(
        self,
        request: Request,
        user_id: str,
        csrf_token: str = Form("", alias="_csrf"),
        group_names: list[str] = Form([], alias="groups"),
        services: auth_services.AuthServices = Depends(dependencies.get_auth_services),
    ) -> Response:
        # puts the person in exactly the groups that were ticked, besides the ones their provider sends
        found: auth_gate.RequestAuth = self._require_form_administrator(request, services, csrf_token)
        return self._run_then_show_users(
            services, found, lambda: services.auth.set_user_groups(found.actor, user_id, group_names)
        )

    # ------------------------------------------------------------------------------------------------------------------
    def disable_user(
        self,
        request: Request,
        user_id: str,
        csrf_token: str = Form("", alias="_csrf"),
        services: auth_services.AuthServices = Depends(dependencies.get_auth_services),
    ) -> Response:
        # stops the person from logging in, and ends the logins that they have
        found: auth_gate.RequestAuth = self._require_form_administrator(request, services, csrf_token)
        return self._run_then_show_users(
            services, found, lambda: services.auth.set_user_disabled(found.actor, user_id, True)
        )

    # ------------------------------------------------------------------------------------------------------------------
    def enable_user(
        self,
        request: Request,
        user_id: str,
        csrf_token: str = Form("", alias="_csrf"),
        services: auth_services.AuthServices = Depends(dependencies.get_auth_services),
    ) -> Response:
        found: auth_gate.RequestAuth = self._require_form_administrator(request, services, csrf_token)
        return self._run_then_show_users(
            services, found, lambda: services.auth.set_user_disabled(found.actor, user_id, False)
        )

    # ------------------------------------------------------------------------------------------------------------------
    def make_reset_link(
        self,
        request: Request,
        user_id: str,
        csrf_token: str = Form("", alias="_csrf"),
        services: auth_services.AuthServices = Depends(dependencies.get_auth_services),
    ) -> Response:
        # a link for the owner of an account of this server to choose a new password with, shown once
        found: auth_gate.RequestAuth = self._require_form_administrator(request, services, csrf_token)
        response: Response
        try:
            link: local_authentication.InviteLink = services.local.reset_link(found.actor, user_id)
            response = self._send_users(services, found, UsersPage.make_link_box(link, "the person"), None, 200)
        except auth_error.AuthError as error:
            services.session.rollback()
            response = self._send_users(services, found, None, error.message, page_resource.get_status_code(error))
        return response

    # ------------------------------------------------------------------------------------------------------------------
    def _send_users(
        self,
        services: auth_services.AuthServices,
        found: auth_gate.RequestAuth,
        link: LinkBox | None,
        error_message: str | None,
        status_code: int,
    ) -> HTMLResponse:
        # everybody who has logged in or been invited, with their groups, and what can be done about each
        users: list[auth_service.UserSummary] = services.auth.list_users(found.actor)
        user_ids: list[str] = []
        for user in users:
            user_ids.append(user.user_id)
        user_ids_with_passwords: set[str] = services.local.find_user_ids_with_passwords(user_ids)
        groups: list[group_record.GroupRecord] = services.groups.list_groups(found.actor)

        rows: list[UserRow] = []
        issuer_texts: dict[str, str] = {
            auth_config.LOCAL_NAME: "this server",
            auth_config.SERVICE_NAME: "API keys only",
        }
        for user in users:
            is_local: bool = user.issuer == auth_config.LOCAL_NAME
            row: UserRow = UserRow(
                user_id=user.user_id,
                username=user.username,
                display_name=user.display_name,
                initials=page_resource.PageResource.compute_initials(user.display_name),
                issuer_text=issuer_texts.get(user.issuer, user.issuer),
                is_local=is_local,
                is_service_account=user.issuer == auth_config.SERVICE_NAME,
                is_waiting_for_password=is_local and user.user_id not in user_ids_with_passwords,
                is_disabled=user.is_disabled,
                is_self=user.user_id == found.actor.user_id,
                group_names=user.group_names,
                group_options=UsersPage.make_group_options(groups, user.group_names),
                last_login_at=user.last_login_datetime.isoformat() if user.last_login_datetime is not None else None,
            )
            rows.append(row)

        head: page_renderer.PageHead = self._make_head(
            services,
            found,
            "Users",
            "admin-users",
            [page_renderer.Crumb(label="Admin"), page_renderer.Crumb(label="Users")],
        )
        model: UsersModel = UsersModel(
            rows=rows,
            group_options=UsersPage.make_group_options(groups, []),
            csrf_token=head.user.csrf_token if head.user is not None else "",
            link=link,
            error_message=error_message,
        )
        return self._send("users", head, model, status_code)

    # ------------------------------------------------------------------------------------------------------------------
    def _run_then_show_users(
        self, services: auth_services.AuthServices, found: auth_gate.RequestAuth, action: Callable[[], None]
    ) -> Response:
        # a change that worked goes back to the page, so that reloading it does not make the change again; one that was
        # refused shows the page with the reason
        response: Response
        try:
            action()
            response = RedirectResponse(_USERS_PATH, status_code=HTTP_303_SEE_OTHER)
        except auth_error.AuthError as error:
            services.session.rollback()
            response = self._send_users(services, found, None, error.message, page_resource.get_status_code(error))
        return response
