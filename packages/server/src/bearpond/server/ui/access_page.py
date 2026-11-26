from collections.abc import Callable
from typing import Literal
from urllib.parse import quote

import pydantic
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel
from quantgebra.auth import auth_services
from quantgebra.auth.core import auth_config
from quantgebra.auth.core import auth_error
from quantgebra.auth.core import auth_settings_service
from quantgebra.auth.core import grant
from quantgebra.auth.core import grant_rule
from quantgebra.auth.core import group_record
from quantgebra.auth.core import permission_definition
from quantgebra.auth.http import auth_gate
from quantgebra.auth.oidc import oidc_provider_settings
from quantgebra.auth.oidc import oidc_settings
from starlette.status import HTTP_303_SEE_OTHER

from .. import server as server_module
from .. import server_auth
from ..api import dependencies
from . import page_renderer
from . import page_resource

_ACCESS_PATH: str = "/admin/access"
_LOGOUT_LABELS: dict[str, str] = {
    "none": "No",
    "standard": "Yes, the standard way",
    "auth0": "Yes, the way of Auth0",
}


# ======================================================================================================================
class ProviderRow(BaseModel):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    provider_name: str
    label: str
    issuer: str
    client_id: str
    is_secret_present: bool
    edit_href: str
    # where the forms that act on the provider post to
    action_path: str


# ======================================================================================================================
class RuleRow(BaseModel):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    group_name: str
    permission_label: str
    permission_name: str
    # the repository that the grant is limited to, None for every one
    repo_name: str | None
    rule_index: int
    grant_index: int


# ======================================================================================================================
class PermissionOption(BaseModel):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    name: str
    label: str
    description: str
    # whether it can be limited to one repository
    is_per_repository: bool


# ======================================================================================================================
class AccessModel(BaseModel):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    mode: str
    base_url: str
    session_duration_minutes: int
    is_local_enabled: bool
    provider_rows: list[ProviderRow]
    rule_rows: list[RuleRow]
    # the problems of rules that the permissions of this version no longer agree with
    dangling_texts: list[str]
    group_names: list[str]
    permission_options: list[PermissionOption]
    repo_names: list[str]
    csrf_token: str
    notice_message: str | None
    error_message: str | None


# ======================================================================================================================
class LogoutOption(BaseModel):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    value: str
    label: str
    is_selected: bool


# ======================================================================================================================
class ProviderFormModel(BaseModel):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    is_new: bool
    action_path: str
    callback_url: str
    provider_name: str
    label: str
    issuer: str
    client_id: str
    # the scopes separated by spaces, and the parameters one name=value on each line, as they are typed
    scopes_text: str
    authorize_parameters_text: str
    groups_claim: str
    username_claim: str
    name_claim: str
    email_claim: str
    logout: str
    # the choices of the logout, filled in when the form is shown
    logout_options: list[LogoutOption] = []
    is_insecure_http_allowed: bool
    is_secret_present: bool
    csrf_token: str
    error_message: str | None


# ======================================================================================================================
class AccessPage(page_resource.PageResource):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def make_provider_form(
        settings: oidc_provider_settings.OidcProviderSettings | None,
        is_secret_present: bool,
        csrf_token: str,
        error_message: str | None,
    ) -> ProviderFormModel:
        # the form of a provider as it is stored, or, without settings, the form of a new one with the usual claims
        shown: oidc_provider_settings.OidcProviderSettings = settings or oidc_provider_settings.OidcProviderSettings(
            provider_name="new", label="", issuer="https://", client_id=""
        )
        parameter_lines: list[str] = []
        for parameter_name, parameter_value in shown.authorize_parameters.items():
            parameter_lines.append(f"{parameter_name}={parameter_value}")
        return ProviderFormModel(
            is_new=settings is None,
            action_path=f"{_ACCESS_PATH}/providers" if settings is None else make_provider_path(shown.provider_name),
            callback_url="",
            provider_name="" if settings is None else shown.provider_name,
            label=shown.label,
            issuer="" if settings is None else shown.issuer,
            client_id=shown.client_id,
            scopes_text=" ".join(shown.scopes),
            authorize_parameters_text="\n".join(parameter_lines),
            groups_claim=shown.groups_claim,
            username_claim=shown.username_claim,
            name_claim=shown.name_claim,
            email_claim=shown.email_claim,
            logout=shown.logout,
            is_insecure_http_allowed=shown.is_insecure_http_allowed,
            is_secret_present=is_secret_present,
            csrf_token=csrf_token,
            error_message=error_message,
        )

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def parse_provider_form(form: ProviderFormModel) -> oidc_provider_settings.OidcProviderSettings:
        # the settings that a submitted form stands for; what cannot be settings is refused with what is wrong
        authorize_parameters: dict[str, str] = {}
        for line in form.authorize_parameters_text.splitlines():
            if line.strip():
                if "=" in line:
                    parameter_name: str = line.split("=", 1)[0].strip()
                    authorize_parameters[parameter_name] = line.split("=", 1)[1].strip()
                else:
                    raise auth_error.AuthError("invalid_request", f'"{line.strip()}" is not name=value.')
        # a logout that is not one of the choices is the default, which only a form made by hand can send
        logout: Literal["none", "standard", "auth0"] = "none"
        if form.logout == "standard":
            logout = "standard"
        elif form.logout == "auth0":
            logout = "auth0"

        try:
            settings: oidc_provider_settings.OidcProviderSettings = oidc_provider_settings.OidcProviderSettings(
                provider_name=form.provider_name.strip(),
                label=form.label.strip(),
                issuer=form.issuer.strip(),
                client_id=form.client_id.strip(),
                scopes=form.scopes_text.split(),
                authorize_parameters=authorize_parameters,
                groups_claim=form.groups_claim.strip(),
                username_claim=form.username_claim.strip(),
                name_claim=form.name_claim.strip(),
                email_claim=form.email_claim.strip(),
                logout=logout,
                is_insecure_http_allowed=form.is_insecure_http_allowed,
            )
        except pydantic.ValidationError as error:
            messages: list[str] = []
            for problem in error.errors():
                messages.append(str(problem["msg"]).removeprefix("Value error, "))
            raise auth_error.AuthError("invalid_request", "; ".join(messages).capitalize() + ".") from error
        return settings

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================

    # ------------------------------------------------------------------------------------------------------------------
    def add_routes(self, router: APIRouter) -> None:
        router.add_api_route(_ACCESS_PATH, self.show_access, methods=["GET"], response_class=HTMLResponse)
        router.add_api_route(
            f"{_ACCESS_PATH}/settings", self.update_settings, methods=["POST"], response_class=HTMLResponse
        )
        router.add_api_route(
            f"{_ACCESS_PATH}/providers/new", self.show_new_provider, methods=["GET"], response_class=HTMLResponse
        )
        router.add_api_route(
            f"{_ACCESS_PATH}/providers", self.add_provider, methods=["POST"], response_class=HTMLResponse
        )
        router.add_api_route(
            f"{_ACCESS_PATH}/providers/{{provider_name}}",
            self.show_provider,
            methods=["GET"],
            response_class=HTMLResponse,
        )
        router.add_api_route(
            f"{_ACCESS_PATH}/providers/{{provider_name}}",
            self.update_provider,
            methods=["POST"],
            response_class=HTMLResponse,
        )
        router.add_api_route(
            f"{_ACCESS_PATH}/providers/{{provider_name}}/check",
            self.check_provider,
            methods=["POST"],
            response_class=HTMLResponse,
        )
        router.add_api_route(
            f"{_ACCESS_PATH}/providers/{{provider_name}}/delete",
            self.remove_provider,
            methods=["POST"],
            response_class=HTMLResponse,
        )
        router.add_api_route(f"{_ACCESS_PATH}/rules", self.add_grant, methods=["POST"], response_class=HTMLResponse)
        router.add_api_route(
            f"{_ACCESS_PATH}/rules/delete", self.remove_grant, methods=["POST"], response_class=HTMLResponse
        )

    # ------------------------------------------------------------------------------------------------------------------
    def show_access(
        self, request: Request, services: auth_services.AuthServices = Depends(dependencies.get_auth_services)
    ) -> Response:
        found: auth_gate.RequestAuth = self._require_administrator(request, services)
        return self._send_access(services, found, None, None, 200)

    # ------------------------------------------------------------------------------------------------------------------
    def update_settings(
        self,
        request: Request,
        csrf_token: str = Form("", alias="_csrf"),
        session_duration_minutes: int = Form(480),
        is_local_enabled: bool = Form(False),
        services: auth_services.AuthServices = Depends(dependencies.get_auth_services),
    ) -> Response:
        # how long a login lasts, and whether people log in with accounts of this server; settings that would leave
        # nobody able to administer are refused
        found: auth_gate.RequestAuth = self._require_form_administrator(request, services, csrf_token)
        settings: auth_config.StoredAuthSettings = auth_config.StoredAuthSettings(
            session_duration_minutes=session_duration_minutes, is_local_enabled=is_local_enabled
        )
        return self._run_then_show_access(
            services, found, lambda: services.settings.update_settings(found.actor, settings)
        )

    # ------------------------------------------------------------------------------------------------------------------
    def show_new_provider(
        self, request: Request, services: auth_services.AuthServices = Depends(dependencies.get_auth_services)
    ) -> Response:
        found: auth_gate.RequestAuth = self._require_administrator(request, services)
        form: ProviderFormModel = AccessPage.make_provider_form(None, False, self._get_csrf_token(found), None)
        return self._send_provider_form(services, found, form, 200)

    # ------------------------------------------------------------------------------------------------------------------
    def add_provider(
        self,
        request: Request,
        csrf_token: str = Form("", alias="_csrf"),
        provider_name: str = Form(""),
        label: str = Form(""),
        issuer: str = Form(""),
        client_id: str = Form(""),
        secret: str = Form(""),
        scopes_text: str = Form(""),
        authorize_parameters_text: str = Form(""),
        groups_claim: str = Form(""),
        username_claim: str = Form(""),
        name_claim: str = Form(""),
        email_claim: str = Form(""),
        logout: str = Form("none"),
        is_insecure_http_allowed: bool = Form(False),
        services: auth_services.AuthServices = Depends(dependencies.get_auth_services),
    ) -> Response:
        # a provider that people can log in through, with the secret that it gave this server
        found: auth_gate.RequestAuth = self._require_form_administrator(request, services, csrf_token)
        form: ProviderFormModel = ProviderFormModel(
            is_new=True,
            action_path=f"{_ACCESS_PATH}/providers",
            callback_url="",
            provider_name=provider_name,
            label=label,
            issuer=issuer,
            client_id=client_id,
            scopes_text=scopes_text,
            authorize_parameters_text=authorize_parameters_text,
            groups_claim=groups_claim,
            username_claim=username_claim,
            name_claim=name_claim,
            email_claim=email_claim,
            logout=logout,
            is_insecure_http_allowed=is_insecure_http_allowed,
            is_secret_present=False,
            csrf_token=csrf_token,
            error_message=None,
        )
        return self._save_provider(services, found, form, secret)

    # ------------------------------------------------------------------------------------------------------------------
    def show_provider(
        self,
        request: Request,
        provider_name: str,
        services: auth_services.AuthServices = Depends(dependencies.get_auth_services),
    ) -> Response:
        found: auth_gate.RequestAuth = self._require_administrator(request, services)
        view: oidc_settings.ProviderView = self._require_provider_view(services, found, provider_name)
        form: ProviderFormModel = AccessPage.make_provider_form(
            view.settings, view.is_secret_present, self._get_csrf_token(found), None
        )
        return self._send_provider_form(services, found, form, 200)

    # ------------------------------------------------------------------------------------------------------------------
    def update_provider(
        self,
        request: Request,
        provider_name: str,
        csrf_token: str = Form("", alias="_csrf"),
        label: str = Form(""),
        issuer: str = Form(""),
        client_id: str = Form(""),
        secret: str = Form(""),
        scopes_text: str = Form(""),
        authorize_parameters_text: str = Form(""),
        groups_claim: str = Form(""),
        username_claim: str = Form(""),
        name_claim: str = Form(""),
        email_claim: str = Form(""),
        logout: str = Form("none"),
        is_insecure_http_allowed: bool = Form(False),
        services: auth_services.AuthServices = Depends(dependencies.get_auth_services),
    ) -> Response:
        # changes a provider; its secret is only changed when a new one is typed
        found: auth_gate.RequestAuth = self._require_form_administrator(request, services, csrf_token)
        view: oidc_settings.ProviderView = self._require_provider_view(services, found, provider_name)
        form: ProviderFormModel = ProviderFormModel(
            is_new=False,
            action_path=make_provider_path(provider_name),
            callback_url="",
            provider_name=provider_name,
            label=label,
            issuer=issuer,
            client_id=client_id,
            scopes_text=scopes_text,
            authorize_parameters_text=authorize_parameters_text,
            groups_claim=groups_claim,
            username_claim=username_claim,
            name_claim=name_claim,
            email_claim=email_claim,
            logout=logout,
            is_insecure_http_allowed=is_insecure_http_allowed,
            is_secret_present=view.is_secret_present,
            csrf_token=csrf_token,
            error_message=None,
        )
        return self._save_provider(services, found, form, secret)

    # ------------------------------------------------------------------------------------------------------------------
    def check_provider(
        self,
        request: Request,
        provider_name: str,
        csrf_token: str = Form("", alias="_csrf"),
        services: auth_services.AuthServices = Depends(dependencies.get_auth_services),
    ) -> Response:
        # tries the provider with what is entered for it, and says whether it can be used or what went wrong
        found: auth_gate.RequestAuth = self._require_form_administrator(request, services, csrf_token)
        response: Response
        try:
            check: oidc_settings.ProviderCheck = services.oidc_settings.check_provider(found.actor, provider_name)
            if check.has_worked:
                response = self._send_access(services, found, f"{provider_name}: {check.message}", None, 200)
            else:
                error_message: str = f"{provider_name} could not be used: {check.message}"
                response = self._send_access(services, found, None, error_message, 400)
        except auth_error.AuthError as error:
            services.session.rollback()
            response = self._send_access(services, found, None, error.message, page_resource.get_status_code(error))
        return response

    # ------------------------------------------------------------------------------------------------------------------
    def remove_provider(
        self,
        request: Request,
        provider_name: str,
        csrf_token: str = Form("", alias="_csrf"),
        services: auth_services.AuthServices = Depends(dependencies.get_auth_services),
    ) -> Response:
        # removes the provider with its secret, its mappings and the groups that it sent
        found: auth_gate.RequestAuth = self._require_form_administrator(request, services, csrf_token)
        return self._run_then_show_access(
            services, found, lambda: services.oidc_settings.remove_provider(found.actor, provider_name)
        )

    # ------------------------------------------------------------------------------------------------------------------
    def add_grant(
        self,
        request: Request,
        csrf_token: str = Form("", alias="_csrf"),
        group_name: str = Form(""),
        permission_name: str = Form(""),
        repo_name: str = Form(""),
        services: auth_services.AuthServices = Depends(dependencies.get_auth_services),
    ) -> Response:
        # gives a group a permission, for every repository or for one; the grant joins the group's rule, or starts it
        found: auth_gate.RequestAuth = self._require_form_administrator(request, services, csrf_token)
        new_grant: grant.Grant = grant.Grant(permission=permission_name)
        if repo_name:
            new_grant = grant.Grant(
                permission=permission_name, subject=repo_name, subject_kind=server_auth.SUBJECT_KIND_REPOSITORY
            )

        def add() -> None:
            rules: list[grant_rule.GrantRule] = []
            is_added: bool = False
            for rule in services.auth.config.rules:
                grants: list[grant.Grant] = list(rule.grants)
                if rule.group_name == group_name:
                    if new_grant in grants:
                        raise auth_error.AuthError("invalid_request", f"The group {group_name} has that already.")
                    else:
                        grants.append(new_grant)
                        is_added = True
                rules.append(grant_rule.GrantRule(group_name=rule.group_name, grants=grants))
            if not is_added:
                rules.append(grant_rule.GrantRule(group_name=group_name, grants=[new_grant]))
            services.settings.replace_rules(found.actor, rules)

        return self._run_then_show_access(services, found, add)

    # ------------------------------------------------------------------------------------------------------------------
    def remove_grant(
        self,
        request: Request,
        csrf_token: str = Form("", alias="_csrf"),
        rule_index: int = Form(-1),
        grant_index: int = Form(-1),
        services: auth_services.AuthServices = Depends(dependencies.get_auth_services),
    ) -> Response:
        # takes one grant away from a group; a rule that is left with none goes; refused when nobody could administer
        found: auth_gate.RequestAuth = self._require_form_administrator(request, services, csrf_token)

        def remove() -> None:
            rules: list[grant_rule.GrantRule] = []
            for each_rule_index, rule in enumerate(services.auth.config.rules):
                grants: list[grant.Grant] = []
                for each_grant_index, each_grant in enumerate(rule.grants):
                    if each_rule_index != rule_index or each_grant_index != grant_index:
                        grants.append(each_grant)
                if grants:
                    rules.append(grant_rule.GrantRule(group_name=rule.group_name, grants=grants))
            services.settings.replace_rules(found.actor, rules)

        return self._run_then_show_access(services, found, remove)

    # ------------------------------------------------------------------------------------------------------------------
    def _send_access(
        self,
        services: auth_services.AuthServices,
        found: auth_gate.RequestAuth,
        notice_message: str | None,
        error_message: str | None,
        status_code: int,
    ) -> HTMLResponse:
        # the settings of logging in, the providers, and the rules that say which group may do what
        config: auth_config.AuthConfig = services.auth.config
        providers: list[oidc_settings.ProviderView] = services.oidc_settings.list_providers(found.actor)
        groups: list[group_record.GroupRecord] = services.groups.list_groups(found.actor)

        # the providers, each with where its forms go
        provider_rows: list[ProviderRow] = []
        for provider in providers:
            provider_row: ProviderRow = ProviderRow(
                provider_name=provider.settings.provider_name,
                label=provider.settings.label,
                issuer=provider.settings.issuer,
                client_id=provider.settings.client_id,
                is_secret_present=provider.is_secret_present,
                edit_href=make_provider_path(provider.settings.provider_name),
                action_path=make_provider_path(provider.settings.provider_name),
            )
            provider_rows.append(provider_row)

        # a row for every grant of every rule, which is what an administrator adds and removes
        rule_rows: list[RuleRow] = []
        for rule_index, rule in enumerate(config.rules):
            for grant_index, each_grant in enumerate(rule.grants):
                definition: permission_definition.PermissionDefinition | None = config.catalog.find_definition(
                    each_grant.permission
                )
                rule_row: RuleRow = RuleRow(
                    group_name=rule.group_name,
                    permission_label=definition.label if definition is not None else each_grant.permission,
                    permission_name=each_grant.permission,
                    repo_name=each_grant.subject,
                    rule_index=rule_index,
                    grant_index=grant_index,
                )
                rule_rows.append(rule_row)
        dangling_texts: list[str] = []
        for dangling in services.settings.find_dangling_grants():
            dangling_texts.append(f"{dangling.group_name}: {dangling.problem}")

        # what the form that adds a grant offers
        group_names: list[str] = []
        for group in groups:
            group_names.append(group.group_name)
        permission_options: list[PermissionOption] = []
        for definition in config.catalog.definitions:
            if definition.name != config.catalog.baseline_name:
                permission_option: PermissionOption = PermissionOption(
                    name=definition.name,
                    label=definition.label,
                    description=definition.description,
                    is_per_repository=server_auth.SUBJECT_KIND_REPOSITORY in definition.subject_kinds,
                )
                permission_options.append(permission_option)

        head: page_renderer.PageHead = self._make_head(
            services,
            found,
            "Access",
            "admin-access",
            [page_renderer.Crumb(label="Admin"), page_renderer.Crumb(label="Access")],
        )
        model: AccessModel = AccessModel(
            mode=config.mode,
            base_url=config.base_url,
            session_duration_minutes=config.session_duration_minutes,
            is_local_enabled=config.is_local_enabled,
            provider_rows=provider_rows,
            rule_rows=rule_rows,
            dangling_texts=dangling_texts,
            group_names=group_names,
            permission_options=permission_options,
            repo_names=server_module.get_server().list_repositories(services.session),
            csrf_token=self._get_csrf_token(found),
            notice_message=notice_message,
            error_message=error_message,
        )
        return self._send("access", head, model, status_code)

    # ------------------------------------------------------------------------------------------------------------------
    def _get_csrf_token(self, found: auth_gate.RequestAuth) -> str:
        return found.session.csrf_token if found.session is not None else ""

    # ------------------------------------------------------------------------------------------------------------------
    def _run_then_show_access(
        self, services: auth_services.AuthServices, found: auth_gate.RequestAuth, action: Callable[[], None]
    ) -> Response:
        response: Response
        try:
            action()
            response = RedirectResponse(_ACCESS_PATH, status_code=HTTP_303_SEE_OTHER)
        except auth_error.AuthError as error:
            services.session.rollback()
            response = self._send_access(services, found, None, error.message, page_resource.get_status_code(error))
        return response

    # ------------------------------------------------------------------------------------------------------------------
    def _send_provider_form(
        self,
        services: auth_services.AuthServices,
        found: auth_gate.RequestAuth,
        form: ProviderFormModel,
        status_code: int,
    ) -> HTMLResponse:
        # the form of one provider, with the address that has to be registered with it
        base_url: str = services.auth.config.base_url.rstrip("/")
        shown_name: str = form.provider_name if form.provider_name else "<name>"
        logout_options: list[LogoutOption] = []
        for logout_value, logout_label in _LOGOUT_LABELS.items():
            option: LogoutOption = LogoutOption(
                value=logout_value, label=logout_label, is_selected=logout_value == form.logout
            )
            logout_options.append(option)
        shown_form: ProviderFormModel = form.model_copy(
            update={"callback_url": f"{base_url}/auth/oidc/{shown_name}/callback", "logout_options": logout_options}
        )
        title: str = "Add a provider" if form.is_new else form.provider_name
        crumbs: list[page_renderer.Crumb] = [
            page_renderer.Crumb(label="Admin"),
            page_renderer.Crumb(label="Access", href=_ACCESS_PATH),
            page_renderer.Crumb(label=title),
        ]
        head: page_renderer.PageHead = self._make_head(services, found, title, "admin-access", crumbs)
        return self._send("provider", head, shown_form, status_code)

    # ------------------------------------------------------------------------------------------------------------------
    def _require_provider_view(
        self, services: auth_services.AuthServices, found: auth_gate.RequestAuth, provider_name: str
    ) -> oidc_settings.ProviderView:
        result: oidc_settings.ProviderView | None = None
        for view in services.oidc_settings.list_providers(found.actor):
            if view.settings.provider_name == provider_name:
                result = view
        if result is None:
            raise page_resource.PageRefusedError(404, f"There is no provider named {provider_name}.")
        return result

    # ------------------------------------------------------------------------------------------------------------------
    def _save_provider(
        self,
        services: auth_services.AuthServices,
        found: auth_gate.RequestAuth,
        form: ProviderFormModel,
        secret: str,
    ) -> Response:
        # a provider that was saved goes back to the page of them all; one that was refused shows its form again, with
        # what was typed in it, except the secret, and why
        response: Response
        try:
            settings: oidc_provider_settings.OidcProviderSettings = AccessPage.parse_provider_form(form)
            if form.is_new:
                services.oidc_settings.add_provider(found.actor, settings, secret)
            else:
                services.oidc_settings.update_provider(found.actor, settings, secret)
            response = RedirectResponse(_ACCESS_PATH, status_code=HTTP_303_SEE_OTHER)
        except auth_error.AuthError as error:
            services.session.rollback()
            refused_form: ProviderFormModel = form.model_copy(update={"error_message": error.message})
            response = self._send_provider_form(services, found, refused_form, page_resource.get_status_code(error))
        return response


# ----------------------------------------------------------------------------------------------------------------------
def make_provider_path(provider_name: str) -> str:
    return f"{_ACCESS_PATH}/providers/{quote(provider_name)}"
