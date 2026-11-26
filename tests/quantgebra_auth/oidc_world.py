from pathlib import Path

import sqlalchemy
from quantgebra.auth.core import actor
from quantgebra.auth.core import auth_service
from quantgebra.auth.oidc import external_identity
from quantgebra.auth.oidc import oidc_authentication
from quantgebra.auth.oidc import oidc_provider
from quantgebra.auth.oidc import oidc_provider_settings
from quantgebra.auth.oidc import oidc_providers
from quantgebra.auth.oidc import oidc_secret_store
from quantgebra.auth.oidc import oidc_settings
from quantgebra.auth.oidc import pending_logins

from . import auth_world
from . import fake_oidc_provider

REDIRECT_URI: str = f"{auth_world.BASE_URL}/auth/oidc/idp/callback"


# ----------------------------------------------------------------------------------------------------------------------
def make_provider_settings() -> oidc_provider_settings.OidcProviderSettings:
    return oidc_provider_settings.OidcProviderSettings(
        provider_name="idp",
        label="Our identity provider",
        issuer=fake_oidc_provider.ISSUER,
        client_id=fake_oidc_provider.CLIENT_ID,
    )


# ======================================================================================================================
class OidcWorld(auth_world.AuthWorld):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================

    # ------------------------------------------------------------------------------------------------------------------
    def __init__(self, engine: sqlalchemy.Engine, secrets_directory: Path) -> None:
        self.fake_provider: fake_oidc_provider.FakeOidcProvider = fake_oidc_provider.FakeOidcProvider()
        super().__init__(engine, secrets_directory, None, self.fake_provider.transport)
        self.secrets: oidc_secret_store.OidcSecretStore = self.system.secrets
        self.providers: oidc_providers.OidcProviders = self.services.providers
        self.oidc_settings: oidc_settings.OidcSettings = self.services.oidc_settings
        self.oidc: oidc_authentication.OidcAuthentication = self.services.oidc
        self.pending_logins: pending_logins.PendingLogins = self.services.pending_logins

    # ------------------------------------------------------------------------------------------------------------------
    def add_provider(self) -> actor.Actor:
        # the provider is added by the first administrator, with the secret that the provider gave them
        administrator: actor.Actor = self.make_administrator()
        self.oidc_settings.add_provider(administrator, make_provider_settings(), fake_oidc_provider.CLIENT_SECRET)
        return administrator

    # ------------------------------------------------------------------------------------------------------------------
    def find_provider(self) -> oidc_provider.OidcProvider:
        provider: oidc_provider.OidcProvider | None = self.providers.find_provider(self.session, "idp")
        assert provider is not None
        return provider

    # ------------------------------------------------------------------------------------------------------------------
    def log_in_through_provider(self, claims: dict[str, object]) -> auth_service.Login:
        # a person goes to the provider, logs in there, and is sent back: the whole of the flow, step by step
        provider: oidc_provider.OidcProvider = self.find_provider()
        start: oidc_provider.LoginStart = provider.begin(REDIRECT_URI)
        callback_url: str = self.fake_provider.approve(start.url, claims)
        identity: external_identity.ExternalIdentity = provider.finish(callback_url, start.pending)
        return self.oidc.login(provider.settings, identity)
