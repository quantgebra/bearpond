import os
from pathlib import Path

from ..core import auth_error
from . import oidc_provider_settings


# ======================================================================================================================
class OidcSecretStore:

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================

    # ------------------------------------------------------------------------------------------------------------------
    def __init__(self, directory: Path) -> None:
        # the client secrets that administrators enter, each in a file of its own named after its provider, which only
        # the user of the service can read; never in the database and never shown again
        self._directory: Path = directory

    # ------------------------------------------------------------------------------------------------------------------
    def has_secret(self, provider_name: str) -> bool:
        return self._get_path(provider_name).is_file()

    # ------------------------------------------------------------------------------------------------------------------
    def get_secret(self, provider_name: str) -> str:
        # the secret as it is right now; a secret that is not there is an error that names the provider
        secret: str = ""
        try:
            secret = self._get_path(provider_name).read_text(encoding="utf-8").strip()
        except OSError:
            secret = ""

        if secret == "":
            raise auth_error.AuthError(
                "invalid_request", f'The OIDC provider "{provider_name}" has no client secret: enter it again.'
            )
        return secret

    # ------------------------------------------------------------------------------------------------------------------
    def save_secret(self, provider_name: str, secret: str) -> None:
        secret_path: Path = self._get_path(provider_name)
        self._directory.mkdir(parents=True, exist_ok=True)
        descriptor: int = os.open(secret_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as secret_file:
            secret_file.write(secret.strip())

    # ------------------------------------------------------------------------------------------------------------------
    def delete_secret(self, provider_name: str) -> None:
        self._get_path(provider_name).unlink(missing_ok=True)

    # ------------------------------------------------------------------------------------------------------------------
    def _get_path(self, provider_name: str) -> Path:
        # the name is checked so that it cannot lead out of the folder
        if not oidc_provider_settings.OidcProviderSettings.is_provider_name(provider_name):
            raise auth_error.AuthError("invalid_request", f'"{provider_name}" is not a provider name.')
        return self._directory / provider_name
