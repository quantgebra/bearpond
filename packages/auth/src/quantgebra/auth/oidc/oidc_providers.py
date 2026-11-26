import threading

import httpx
from sqlalchemy import orm

from . import oidc_provider
from . import oidc_provider_settings
from . import oidc_provider_record
from . import oidc_secret_store


# ======================================================================================================================
class OidcProviders:

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================

    # ------------------------------------------------------------------------------------------------------------------
    def __init__(
        self, secrets: oidc_secret_store.OidcSecretStore, transport: httpx.BaseTransport | None = None
    ) -> None:
        # the providers that people can log in through, each made from its settings and kept for as long as its
        # settings stay the same, since what it has found out about its provider is worth keeping; this outlives a
        # request, so the settings are read through the session of the request that asks
        self._secrets: oidc_secret_store.OidcSecretStore = secrets
        self._transport: httpx.BaseTransport | None = transport
        self._lock: threading.Lock = threading.Lock()
        self._providers_by_name: dict[str, oidc_provider.OidcProvider] = {}

    # ------------------------------------------------------------------------------------------------------------------
    def find_provider(self, session: orm.Session, provider_name: str) -> oidc_provider.OidcProvider | None:
        settings: oidc_provider_settings.OidcProviderSettings | None = (
            oidc_provider_record.OidcProviderRecord.get_settings_by_provider_name(session, provider_name)
        )
        result: oidc_provider.OidcProvider | None = None
        with self._lock:
            cached: oidc_provider.OidcProvider | None = self._providers_by_name.get(provider_name)
            if settings is None:
                self._providers_by_name.pop(provider_name, None)
            elif cached is not None and cached.settings == settings:
                result = cached
            else:
                result = oidc_provider.OidcProvider(settings, self._secrets, self._transport)
                self._providers_by_name[provider_name] = result
        return result

    # ------------------------------------------------------------------------------------------------------------------
    def forget_provider(self, provider_name: str) -> None:
        # drops what is kept of a provider that was removed
        with self._lock:
            self._providers_by_name.pop(provider_name, None)
