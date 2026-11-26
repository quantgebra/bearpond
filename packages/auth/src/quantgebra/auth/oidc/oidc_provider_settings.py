import re
from typing import Literal

from pydantic import BaseModel, field_validator

_PROVIDER_NAME: re.Pattern = re.compile(r"^[a-z0-9][a-z0-9-]*$")


# ======================================================================================================================
class OidcProviderSettings(BaseModel):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def is_provider_name(text: str) -> bool:
        # what a provider's name looks like, which is also the name of the file of its secret
        return _PROVIDER_NAME.match(text) is not None

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    provider_name: str
    label: str
    issuer: str
    client_id: str
    scopes: list[str] = ["openid", "profile", "email"]
    authorize_parameters: dict[str, str] = {}
    groups_claim: str = "groups"
    username_claim: str = "preferred_username"
    name_claim: str = "name"
    email_claim: str = "email"
    logout: Literal["none", "standard", "auth0"] = "none"
    is_insecure_http_allowed: bool = False

    # ------------------------------------------------------------------------------------------------------------------
    @field_validator("provider_name")
    @classmethod
    def _check_provider_name(cls, value: str) -> str:
        if not OidcProviderSettings.is_provider_name(value):
            raise ValueError("a provider name is lower case letters, digits and dashes, starting with one")
        return value

    # ------------------------------------------------------------------------------------------------------------------
    @field_validator("issuer")
    @classmethod
    def _check_issuer_is_an_address(cls, value: str) -> str:
        if not (value.startswith("https://") or value.startswith("http://")):
            raise ValueError("an issuer is an https or http address")
        return value
