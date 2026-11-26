from collections.abc import Callable

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel
from quantgebra.auth import auth_services
from quantgebra.auth.core import auth_error
from quantgebra.auth.core import group_mapping_record
from quantgebra.auth.core import group_record
from quantgebra.auth.core import provider_group_record
from quantgebra.auth.core import user_group_record
from quantgebra.auth.http import auth_gate
from quantgebra.auth.oidc import oidc_settings
from starlette.status import HTTP_303_SEE_OTHER

from ..api import dependencies
from . import page_renderer
from . import page_resource

_GROUPS_PATH: str = "/admin/groups"


# ======================================================================================================================
class MappingRow(BaseModel):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    provider_name: str
    provider_group_name: str
    group_name: str


# ======================================================================================================================
class GroupRow(BaseModel):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    group_name: str
    description: str
    member_count: int
    rule_count: int
    mapped_from: list[MappingRow]
    # nothing depends on it, so it can go; the group of administrators never can
    is_removable: bool


# ======================================================================================================================
class ProviderGroupRow(BaseModel):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    provider_name: str
    provider_group_name: str
    seen_at: str
    # the groups of this server that it is mapped to, joined for showing; empty when it is mapped to none
    mapped_to_text: str


# ======================================================================================================================
class GroupsModel(BaseModel):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    rows: list[GroupRow]
    provider_group_rows: list[ProviderGroupRow]
    provider_names: list[str]
    group_names: list[str]
    csrf_token: str
    error_message: str | None


# ======================================================================================================================
class GroupsPage(page_resource.PageResource):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================

    # ------------------------------------------------------------------------------------------------------------------
    def add_routes(self, router: APIRouter) -> None:
        router.add_api_route(_GROUPS_PATH, self.show_groups, methods=["GET"], response_class=HTMLResponse)
        router.add_api_route(_GROUPS_PATH, self.create_group, methods=["POST"], response_class=HTMLResponse)
        router.add_api_route(
            f"{_GROUPS_PATH}/{{group_name}}/describe",
            self.describe_group,
            methods=["POST"],
            response_class=HTMLResponse,
        )
        router.add_api_route(
            f"{_GROUPS_PATH}/{{group_name}}/delete", self.delete_group, methods=["POST"], response_class=HTMLResponse
        )
        router.add_api_route(f"{_GROUPS_PATH}/map", self.add_mapping, methods=["POST"], response_class=HTMLResponse)
        router.add_api_route(
            f"{_GROUPS_PATH}/unmap", self.remove_mapping, methods=["POST"], response_class=HTMLResponse
        )

    # ------------------------------------------------------------------------------------------------------------------
    def show_groups(
        self, request: Request, services: auth_services.AuthServices = Depends(dependencies.get_auth_services)
    ) -> Response:
        found: auth_gate.RequestAuth = self._require_administrator(request, services)
        return self._send_groups(services, found, None, 200)

    # ------------------------------------------------------------------------------------------------------------------
    def create_group(
        self,
        request: Request,
        csrf_token: str = Form("", alias="_csrf"),
        group_name: str = Form(""),
        description: str = Form(""),
        services: auth_services.AuthServices = Depends(dependencies.get_auth_services),
    ) -> Response:
        found: auth_gate.RequestAuth = self._require_form_administrator(request, services, csrf_token)
        return self._run_then_show_groups(
            services, found, lambda: services.groups.create_group(found.actor, group_name.strip(), description)
        )

    # ------------------------------------------------------------------------------------------------------------------
    def describe_group(
        self,
        request: Request,
        group_name: str,
        csrf_token: str = Form("", alias="_csrf"),
        description: str = Form(""),
        services: auth_services.AuthServices = Depends(dependencies.get_auth_services),
    ) -> Response:
        # says what a group is for, which the pages that pick a group show beside it
        found: auth_gate.RequestAuth = self._require_form_administrator(request, services, csrf_token)
        return self._run_then_show_groups(
            services, found, lambda: services.groups.describe_group(found.actor, group_name, description)
        )

    # ------------------------------------------------------------------------------------------------------------------
    def delete_group(
        self,
        request: Request,
        group_name: str,
        csrf_token: str = Form("", alias="_csrf"),
        services: auth_services.AuthServices = Depends(dependencies.get_auth_services),
    ) -> Response:
        found: auth_gate.RequestAuth = self._require_form_administrator(request, services, csrf_token)
        return self._run_then_show_groups(
            services, found, lambda: services.groups.delete_group(found.actor, group_name)
        )

    # ------------------------------------------------------------------------------------------------------------------
    def add_mapping(
        self,
        request: Request,
        csrf_token: str = Form("", alias="_csrf"),
        provider_name: str = Form(""),
        provider_group_name: str = Form(""),
        group_name: str = Form(""),
        services: auth_services.AuthServices = Depends(dependencies.get_auth_services),
    ) -> Response:
        # what a provider calls a group becomes a group of this server, so the rules for it apply to its people
        found: auth_gate.RequestAuth = self._require_form_administrator(request, services, csrf_token)
        mapping: group_mapping_record.GroupMappingRecord = group_mapping_record.GroupMappingRecord(
            provider_name=provider_name, provider_group_name=provider_group_name.strip(), group_name=group_name
        )
        return self._run_then_show_groups(services, found, lambda: services.groups.add_mapping(found.actor, mapping))

    # ------------------------------------------------------------------------------------------------------------------
    def remove_mapping(
        self,
        request: Request,
        csrf_token: str = Form("", alias="_csrf"),
        provider_name: str = Form(""),
        provider_group_name: str = Form(""),
        group_name: str = Form(""),
        services: auth_services.AuthServices = Depends(dependencies.get_auth_services),
    ) -> Response:
        found: auth_gate.RequestAuth = self._require_form_administrator(request, services, csrf_token)
        mapping: group_mapping_record.GroupMappingRecord = group_mapping_record.GroupMappingRecord(
            provider_name=provider_name, provider_group_name=provider_group_name, group_name=group_name
        )
        return self._run_then_show_groups(
            services, found, lambda: services.groups.remove_mapping(found.actor, mapping)
        )

    # ------------------------------------------------------------------------------------------------------------------
    def _send_groups(
        self,
        services: auth_services.AuthServices,
        found: auth_gate.RequestAuth,
        error_message: str | None,
        status_code: int,
    ) -> HTMLResponse:
        # the groups with what depends on each, the providers' groups that can be mapped, and the forms that change them
        groups: list[group_record.GroupRecord] = services.groups.list_groups(found.actor)
        mappings: list[group_mapping_record.GroupMappingRecord] = services.groups.list_mappings(found.actor)
        provider_groups: list[provider_group_record.ProviderGroupRecord] = services.groups.list_provider_groups(
            found.actor
        )
        providers: list[oidc_settings.ProviderView] = services.oidc_settings.list_providers(found.actor)

        # each group with the users in it, the rules about it and the provider groups mapped to it
        rows: list[GroupRow] = []
        group_names: list[str] = []
        for group in groups:
            mapped_from: list[MappingRow] = []
            for mapping in mappings:
                if mapping.group_name == group.group_name:
                    mapping_row: MappingRow = MappingRow(
                        provider_name=mapping.provider_name,
                        provider_group_name=mapping.provider_group_name,
                        group_name=mapping.group_name,
                    )
                    mapped_from.append(mapping_row)
            rule_count: int = 0
            for rule in services.auth.config.rules:
                if rule.group_name == group.group_name:
                    rule_count += 1
            member_count: int = user_group_record.UserGroupRecord.count_by_group_name(
                services.session, group.group_name
            )
            row: GroupRow = GroupRow(
                group_name=group.group_name,
                description=group.description,
                member_count=member_count,
                rule_count=rule_count,
                mapped_from=mapped_from,
                is_removable=(
                    member_count == 0
                    and rule_count == 0
                    and not mapped_from
                    and group.group_name != services.auth.config.administrators_group_name
                ),
            )
            rows.append(row)
            group_names.append(group.group_name)

        # the groups that providers sent when people logged in, with what each is mapped to
        provider_group_rows: list[ProviderGroupRow] = []
        for provider_group in provider_groups:
            mapped_to_names: list[str] = []
            for mapping in mappings:
                is_same_provider: bool = mapping.provider_name == provider_group.provider_name
                if is_same_provider and mapping.provider_group_name == provider_group.provider_group_name:
                    mapped_to_names.append(mapping.group_name)
            provider_group_row: ProviderGroupRow = ProviderGroupRow(
                provider_name=provider_group.provider_name,
                provider_group_name=provider_group.provider_group_name,
                seen_at=provider_group.seen_datetime.isoformat(),
                mapped_to_text=", ".join(mapped_to_names),
            )
            provider_group_rows.append(provider_group_row)

        provider_names: list[str] = []
        for provider in providers:
            provider_names.append(provider.settings.provider_name)

        head: page_renderer.PageHead = self._make_head(
            services,
            found,
            "Groups",
            "admin-groups",
            [page_renderer.Crumb(label="Admin"), page_renderer.Crumb(label="Groups")],
        )
        model: GroupsModel = GroupsModel(
            rows=rows,
            provider_group_rows=provider_group_rows,
            provider_names=provider_names,
            group_names=group_names,
            csrf_token=head.user.csrf_token if head.user is not None else "",
            error_message=error_message,
        )
        return self._send("groups", head, model, status_code)

    # ------------------------------------------------------------------------------------------------------------------
    def _run_then_show_groups(
        self, services: auth_services.AuthServices, found: auth_gate.RequestAuth, action: Callable[[], None]
    ) -> Response:
        response: Response
        try:
            action()
            response = RedirectResponse(_GROUPS_PATH, status_code=HTTP_303_SEE_OTHER)
        except auth_error.AuthError as error:
            services.session.rollback()
            response = self._send_groups(services, found, error.message, page_resource.get_status_code(error))
        return response
