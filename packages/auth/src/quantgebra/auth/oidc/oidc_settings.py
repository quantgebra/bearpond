from pydantic import BaseModel

from ..core import actor
from ..core import auth_config
from ..core import auth_error
from ..core import auth_service
from ..core import group_mapping_record
from ..core import provider_group_record
from . import oidc_provider
from . import oidc_provider_settings
from . import oidc_providers
from . import oidc_secret_store
from . import oidc_provider_record


# ======================================================================================================================
class ProviderView(BaseModel):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    settings: oidc_provider_settings.OidcProviderSettings
    is_secret_present: bool


# ======================================================================================================================
class ProviderCheck(BaseModel):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    has_worked: bool
    message: str


# ======================================================================================================================
class OidcSettings:

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================

    # ------------------------------------------------------------------------------------------------------------------
    def __init__(
        self,
        auth: auth_service.AuthService,
        secrets: oidc_secret_store.OidcSecretStore,
        providers: oidc_providers.OidcProviders,
    ) -> None:
        # the OIDC providers that people can log in through, which administrators change: adding, changing and
        # removing a provider, finding out whether one can be reached, and keeping the secret that an administrator
        # enters for one in a file of its own, never in the database and never shown again
        self._auth: auth_service.AuthService = auth
        self._secrets: oidc_secret_store.OidcSecretStore = secrets
        self._providers: oidc_providers.OidcProviders = providers

    # ------------------------------------------------------------------------------------------------------------------
    def list_providers(self, requesting_actor: actor.Actor) -> list[ProviderView]:
        self._auth.require_admin(requesting_actor)
        result: list[ProviderView] = []
        for settings in oidc_provider_record.OidcProviderRecord.get_all_settings(self._auth.session):
            view: ProviderView = ProviderView(
                settings=settings, is_secret_present=self._secrets.has_secret(settings.provider_name)
            )
            result.append(view)
        return result

    # ------------------------------------------------------------------------------------------------------------------
    def add_provider(
        self, requesting_actor: actor.Actor, settings: oidc_provider_settings.OidcProviderSettings, secret: str
    ) -> None:
        self._auth.require_admin(requesting_actor)

        if settings.provider_name == auth_config.LOCAL_NAME:
            raise auth_error.AuthError(
                "invalid_request", f'"{auth_config.LOCAL_NAME}" is not a name a provider can have.'
            )

        if (
            oidc_provider_record.OidcProviderRecord.get_settings_by_provider_name(
                self._auth.session, settings.provider_name
            )
            is not None
        ):
            raise auth_error.AuthError(
                "invalid_request", f'There is already a provider named "{settings.provider_name}".'
            )

        if secret.strip() == "":
            raise auth_error.AuthError("invalid_request", "Enter the client secret of the provider.")

        self._secrets.save_secret(settings.provider_name, secret)
        oidc_provider_record.OidcProviderRecord.save_settings(self._auth.session, settings)
        self._providers.forget_provider(settings.provider_name)

    # ------------------------------------------------------------------------------------------------------------------
    def update_provider(
        self, requesting_actor: actor.Actor, settings: oidc_provider_settings.OidcProviderSettings, secret: str | None
    ) -> None:
        # changes a provider; the secret is only given when it is to be changed
        self._auth.require_admin(requesting_actor)

        if (
            oidc_provider_record.OidcProviderRecord.get_settings_by_provider_name(
                self._auth.session, settings.provider_name
            )
            is None
        ):
            raise auth_error.AuthError("invalid_request", "There is no such provider.")

        if secret is not None and secret.strip() != "":
            self._secrets.save_secret(settings.provider_name, secret)

        oidc_provider_record.OidcProviderRecord.save_settings(self._auth.session, settings)
        self._providers.forget_provider(settings.provider_name)

    # ------------------------------------------------------------------------------------------------------------------
    def remove_provider(self, requesting_actor: actor.Actor, provider_name: str) -> None:
        # removes a provider with its secret, its mappings and the groups it sent; refused when that would leave nobody
        # able to administer
        self._auth.require_admin(requesting_actor)

        if (
            oidc_provider_record.OidcProviderRecord.get_settings_by_provider_name(self._auth.session, provider_name)
            is None
        ):
            raise auth_error.AuthError("invalid_request", "There is no such provider.")

        remaining_administrator_mappings: int = 0
        for mapping in group_mapping_record.GroupMappingRecord.get_all(self._auth.session):
            is_administrators: bool = mapping.group_name == self._auth.config.administrators_group_name
            if is_administrators and mapping.provider_name != provider_name:
                remaining_administrator_mappings += 1
        if not self._auth.config.can_have_administrator(remaining_administrator_mappings > 0):
            raise auth_error.AuthError(
                "invalid_request", "Removing this provider would lock everyone out: nobody else could administer."
            )

        oidc_provider_record.OidcProviderRecord.delete_by_provider_name(self._auth.session, provider_name)
        self._secrets.delete_secret(provider_name)
        group_mapping_record.GroupMappingRecord.delete_by_provider_name(self._auth.session, provider_name)
        provider_group_record.ProviderGroupRecord.delete_by_provider_name(self._auth.session, provider_name)
        self._providers.forget_provider(provider_name)

    # ------------------------------------------------------------------------------------------------------------------
    def check_provider(self, requesting_actor: actor.Actor, provider_name: str) -> ProviderCheck:
        # finds out whether a provider can be used with what is entered for it, and what went wrong when it cannot
        self._auth.require_admin(requesting_actor)
        provider: oidc_provider.OidcProvider | None = self._providers.find_provider(self._auth.session, provider_name)
        if provider is None:
            raise auth_error.AuthError("invalid_request", "There is no such provider.")

        result: ProviderCheck = ProviderCheck(has_worked=True, message="The provider can be used.")
        try:
            provider.check()
        except (oidc_provider.OidcLoginError, auth_error.AuthError) as error:
            result = ProviderCheck(has_worked=False, message=str(error))
        return result
