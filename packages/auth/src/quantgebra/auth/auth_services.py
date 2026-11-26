from sqlalchemy import orm

from .apikey import api_key_authentication
from .core import auth_config
from .core import auth_service
from .core import auth_settings_service
from .core import group_service
from .local import local_authentication
from .local import login_throttle
from .oidc import oidc_authentication
from .oidc import oidc_providers
from .oidc import oidc_secret_store
from .oidc import oidc_settings
from .oidc import pending_logins


# ======================================================================================================================
class AuthServices:

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================

    # ------------------------------------------------------------------------------------------------------------------
    def __init__(
        self,
        session: orm.Session,
        current_config: auth_config.CurrentAuthConfig,
        secrets: oidc_secret_store.OidcSecretStore,
        throttle: login_throttle.LoginThrottle,
        pending: pending_logins.PendingLogins,
        providers: oidc_providers.OidcProviders,
    ) -> None:
        # everything that serves one request: the repositories and services over the session of the request, together
        # with the few things that are shared between requests, because what they remember has to outlast one
        self.session: orm.Session = session
        self.auth: auth_service.AuthService = auth_service.AuthService(current_config, session)
        self.local: local_authentication.LocalAuthentication = local_authentication.LocalAuthentication(
            self.auth, throttle
        )
        self.oidc: oidc_authentication.OidcAuthentication = oidc_authentication.OidcAuthentication(self.auth)
        self.groups: group_service.GroupService = group_service.GroupService(self.auth)
        self.settings: auth_settings_service.AuthSettingsService = auth_settings_service.AuthSettingsService(self.auth)
        self.oidc_settings: oidc_settings.OidcSettings = oidc_settings.OidcSettings(self.auth, secrets, providers)
        self.providers: oidc_providers.OidcProviders = providers
        self.pending_logins: pending_logins.PendingLogins = pending
        self.api_keys: api_key_authentication.ApiKeyAuthentication = api_key_authentication.ApiKeyAuthentication(
            self.auth
        )
