import os
from pathlib import Path
from urllib.parse import SplitResult, urlsplit

from pydantic import BaseModel


# ======================================================================================================================
class StoredCredentials(BaseModel):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    # the key for each server, by the server's address as normalize_server_url writes it
    servers: dict[str, str] = {}


# ======================================================================================================================
class CredentialStore:

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================

    # ------------------------------------------------------------------------------------------------------------------
    def __init__(self, path: Path) -> None:
        # the keys that `bearpond login` saved, one for each server, in a file that only its owner can read
        self.path: Path = path

    # ------------------------------------------------------------------------------------------------------------------
    def get_token(self, server_url: str) -> str | None:
        return self._load().servers.get(normalize_server_url(server_url))

    # ------------------------------------------------------------------------------------------------------------------
    def save_token(self, server_url: str, token: str) -> None:
        credentials: StoredCredentials = self._load()
        credentials.servers[normalize_server_url(server_url)] = token
        self._save(credentials)

    # ------------------------------------------------------------------------------------------------------------------
    def remove_token(self, server_url: str) -> bool:
        # forgets the key of the server; False when there was none
        credentials: StoredCredentials = self._load()
        removed_token: str | None = credentials.servers.pop(normalize_server_url(server_url), None)
        if removed_token is not None:
            self._save(credentials)
        return removed_token is not None

    # ------------------------------------------------------------------------------------------------------------------
    def _load(self) -> StoredCredentials:
        credentials: StoredCredentials = StoredCredentials()
        if self.path.is_file():
            credentials = StoredCredentials.model_validate_json(self.path.read_text(encoding="utf-8"))
        return credentials

    # ------------------------------------------------------------------------------------------------------------------
    def _save(self, credentials: StoredCredentials) -> None:
        # the folder and the file are only the owner's, and the file is written whole and then moved into place, so a
        # crash never leaves half of it, nor a moment when others could read it
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary_path: Path = self.path.with_name(f".{self.path.name}.tmp")
        descriptor: int = os.open(temporary_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as temporary_file:
            temporary_file.write(credentials.model_dump_json(indent=2))
        os.replace(temporary_path, self.path)


# ----------------------------------------------------------------------------------------------------------------------
def normalize_server_url(server_url: str) -> str:
    # one address for one server however it is typed: the scheme and host in lower case, and no slash at the end
    parts: SplitResult = urlsplit(server_url.strip())
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}{parts.path.rstrip('/')}"
