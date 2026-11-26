from typing import Literal

from pydantic import BaseModel

from . import grant
from . import grant_rule
from . import permission_definition_catalog

LOCAL_NAME: str = "local"
# the issuer of service accounts: users for programs, which have no password and are only reached with api keys
SERVICE_NAME: str = "service"
# what an actor made from an api key logged in with, since there is no provider and no login
API_KEY_NAME: str = "api-key"


# ======================================================================================================================
class StoredAuthSettings(BaseModel):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    session_duration_minutes: int
    is_local_enabled: bool


# ======================================================================================================================
class AuthConfig:

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================

    # ------------------------------------------------------------------------------------------------------------------
    def __init__(
        self,
        mode: Literal["none", "enforced"],
        base_url: str,
        administrators_group_name: str,
        catalog: permission_definition_catalog.PermissionDefinitionCatalog,
        session_duration_minutes: int,
        is_local_enabled: bool,
        rules: list[grant_rule.GrantRule],
        api_key_prefix: str = "key_",
    ) -> None:
        self.mode: Literal["none", "enforced"] = mode
        self.base_url: str = base_url
        self.administrators_group_name: str = administrators_group_name
        self.catalog: permission_definition_catalog.PermissionDefinitionCatalog = catalog
        self.session_duration_minutes: int = session_duration_minutes
        self.is_local_enabled: bool = is_local_enabled
        self.rules: list[grant_rule.GrantRule] = rules

        # what every api key of the application starts with, so a leaked one is told apart from a cookie and found by a
        # scanner of secrets, such as "bp_" for BearPond
        self.api_key_prefix: str = api_key_prefix

    # ------------------------------------------------------------------------------------------------------------------
    def with_settings(self, settings: StoredAuthSettings, rules: list[grant_rule.GrantRule]) -> "AuthConfig":
        # these settings with the stored ones in place of their own, since the mode and the address are not stored
        result: AuthConfig = AuthConfig(
            self.mode,
            self.base_url,
            self.administrators_group_name,
            self.catalog,
            settings.session_duration_minutes,
            settings.is_local_enabled,
            rules,
            self.api_key_prefix,
        )
        return result

    # ------------------------------------------------------------------------------------------------------------------
    def are_cookies_secure(self) -> bool:
        # cookies are only sent over https when the server is reached at an https address
        return self.base_url.startswith("https://")

    # ------------------------------------------------------------------------------------------------------------------
    def does_give_administrators_the_admin_permission(self) -> bool:
        grants: list[grant.Grant] = grant_rule.GrantRule.get_grants_for(self.rules, [self.administrators_group_name])
        result: bool = False
        for each_grant in grants:
            if each_grant.permission == self.catalog.admin_name:
                result = True
        return result

    # ------------------------------------------------------------------------------------------------------------------
    def can_have_administrator(self, has_provider_administrators: bool) -> bool:
        # whether somebody could be an administrator: an account of the server in the group of administrators, or
        # anyone a provider vouches for who is mapped into it; settings that leave nobody would lock everyone out of
        # the settings that change them
        has_local_administrators: bool = self.is_local_enabled and self.does_give_administrators_the_admin_permission()
        return self.mode == "none" or has_local_administrators or has_provider_administrators


# ======================================================================================================================
class CurrentAuthConfig:

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================

    # ------------------------------------------------------------------------------------------------------------------
    def __init__(self, config: AuthConfig) -> None:
        # the settings in use, which every request's services read and which an administrator's change replaces, so it
        # is the one thing about the settings that outlives a request
        self.config: AuthConfig = config
