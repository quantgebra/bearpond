from datetime import datetime, timezone

from pydantic import BaseModel

from . import actor
from . import auth_config
from . import auth_error
from . import auth_service
from . import grant
from . import grant_rule
from . import permission_definition
from . import group_record
from . import group_mapping_record
from . import auth_settings_record
from . import grant_rule_record

_LONGEST_SESSION_MINUTES: int = 43200


# ======================================================================================================================
class DanglingGrant(BaseModel):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    group_name: str
    grant: grant.Grant
    problem: str


# ======================================================================================================================
class AuthSettingsService:

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================

    # ------------------------------------------------------------------------------------------------------------------
    def __init__(self, auth: auth_service.AuthService) -> None:
        self._auth: auth_service.AuthService = auth

    # ------------------------------------------------------------------------------------------------------------------
    def load_stored_settings(self) -> None:
        # makes the settings of the service the stored ones, when an administrator has changed them before
        stored_settings: auth_config.StoredAuthSettings | None = (
            auth_settings_record.AuthSettingsRecord.get_stored_settings(self._auth.session)
        )
        if stored_settings is not None:
            stored_rules: list[grant_rule.GrantRule] = grant_rule_record.GrantRuleRecord.get_all_rules(
                self._auth.session
            )
            self._auth.config = self._auth.config.with_settings(stored_settings, stored_rules)

    # ------------------------------------------------------------------------------------------------------------------
    def update_settings(self, requesting_actor: actor.Actor, settings: auth_config.StoredAuthSettings) -> None:
        # how long a login lasts and whether accounts of the server are used; settings that would leave nobody able to
        # administer are refused
        self._auth.require_admin(requesting_actor)

        if not 1 <= settings.session_duration_minutes <= _LONGEST_SESSION_MINUTES:
            raise auth_error.AuthError(
                "invalid_request", f"A login lasts from 1 to {_LONGEST_SESSION_MINUTES} minutes."
            )

        new_config: auth_config.AuthConfig = self._auth.config.with_settings(settings, self._auth.config.rules)
        self._check_somebody_can_administer(new_config)

        # the rules in use are kept with the settings from now on, and a rule is about a group that has to be there
        self._ensure_groups_of(self._auth.config.rules)
        auth_settings_record.AuthSettingsRecord.save_stored_settings(self._auth.session, settings)
        grant_rule_record.GrantRuleRecord.replace_all(self._auth.session, self._auth.config.rules)
        self._auth.config = new_config

    # ------------------------------------------------------------------------------------------------------------------
    def replace_rules(self, requesting_actor: actor.Actor, rules: list[grant_rule.GrantRule]) -> None:
        # what the groups may do; every rule has to be about a group that exists and give permissions that there are
        self._auth.require_admin(requesting_actor)

        group_names: list[str] = []
        for rule in rules:
            group_names.append(rule.group_name)
            self._check_grants_are_known(rule)

        missing_names: list[str] = group_record.GroupRecord.get_missing_group_names(self._auth.session, group_names)
        if missing_names:
            raise auth_error.AuthError(
                "invalid_request", f"There is no group named {', '.join(missing_names)}: add it first."
            )

        current_settings: auth_config.StoredAuthSettings = auth_config.StoredAuthSettings(
            session_duration_minutes=self._auth.config.session_duration_minutes,
            is_local_enabled=self._auth.config.is_local_enabled,
        )
        new_config: auth_config.AuthConfig = self._auth.config.with_settings(current_settings, rules)
        self._check_somebody_can_administer(new_config)

        auth_settings_record.AuthSettingsRecord.save_stored_settings(self._auth.session, current_settings)
        grant_rule_record.GrantRuleRecord.replace_all(self._auth.session, rules)
        self._auth.config = new_config

    # ------------------------------------------------------------------------------------------------------------------
    def find_dangling_grants(self) -> list[DanglingGrant]:
        # the grants of the rules in use that the catalog no longer agrees with, such as one for a permission that a
        # newer version of the application has removed; they are inert, so nothing breaks, but nobody gets what the rule
        # was meant to give, which is worth knowing
        dangling_grants: list[DanglingGrant] = []
        for rule in self._auth.config.rules:
            for each_grant in rule.grants:
                problem: str | None = self._find_problem_with(each_grant)
                if problem is not None:
                    dangling_grants.append(DanglingGrant(group_name=rule.group_name, grant=each_grant, problem=problem))
        return dangling_grants

    # ------------------------------------------------------------------------------------------------------------------
    def _check_grants_are_known(self, rule: grant_rule.GrantRule) -> None:
        for each_grant in rule.grants:
            problem: str | None = self._find_problem_with(each_grant)
            if problem is not None:
                raise auth_error.AuthError("invalid_request", problem)

    # ------------------------------------------------------------------------------------------------------------------
    def _find_problem_with(self, each_grant: grant.Grant) -> str | None:
        # what is wrong with a grant according to the catalog, None for one that is fine
        definition: permission_definition.PermissionDefinition | None = self._auth.config.catalog.find_definition(
            each_grant.permission
        )
        problem: str | None = None
        if definition is None:
            problem = f'There is no permission "{each_grant.permission}".'
        elif each_grant.subject_kind is not None and each_grant.subject_kind not in definition.subject_kinds:
            problem = f'The permission "{each_grant.permission}" cannot be limited to a {each_grant.subject_kind}.'
        return problem

    # ------------------------------------------------------------------------------------------------------------------
    def _check_somebody_can_administer(self, new_config: auth_config.AuthConfig) -> None:
        has_provider_administrators: bool = (
            group_mapping_record.GroupMappingRecord.count_by_group_name(
                self._auth.session, new_config.administrators_group_name
            )
            > 0
        )
        if not new_config.can_have_administrator(has_provider_administrators):
            raise auth_error.AuthError(
                "invalid_request",
                "These settings would leave nobody who can administer, which would lock everyone out.",
            )

    # ------------------------------------------------------------------------------------------------------------------
    def _ensure_groups_of(self, rules: list[grant_rule.GrantRule]) -> None:
        group_names: list[str] = []
        for rule in rules:
            group_names.append(rule.group_name)
        group_record.GroupRecord.ensure_exist(self._auth.session, group_names, datetime.now(timezone.utc))
