import base64
import hashlib
import hmac
import secrets
from typing import cast
from urllib.parse import parse_qs, quote, urlencode, urlparse

import httpx
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import KeySet, KeySetSerialization
from pydantic import BaseModel

from . import claim_paths
from . import external_identity
from . import oidc_provider_settings
from . import oidc_secret_store

_ALGORITHMS: list[str] = ["RS256", "RS384", "RS512", "ES256", "ES384", "ES512", "PS256", "PS384", "PS512"]
_CLOCK_LEEWAY_SECONDS: int = 60
_REQUEST_TIMEOUT_SECONDS: float = 10.0


# ----------------------------------------------------------------------------------------------------------------------
def _make_code_challenge(verifier: str) -> str:
    # the S256 challenge of a PKCE verifier: the url-safe base64 of its sha256, without padding
    digest: bytes = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


# ======================================================================================================================
class OidcLoginError(Exception):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    pass


# ======================================================================================================================
class PendingLogin(BaseModel):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    state: str
    nonce: str
    verifier: str
    redirect_uri: str


# ======================================================================================================================
class LoginStart(BaseModel):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    url: str
    pending: PendingLogin


# ======================================================================================================================
class ProviderMetadata(BaseModel):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    end_session_endpoint: str | None = None
    token_endpoint_auth_methods_supported: list[str] = []


# ======================================================================================================================
class OidcProvider:

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================

    # ------------------------------------------------------------------------------------------------------------------
    def __init__(
        self,
        settings: oidc_provider_settings.OidcProviderSettings,
        secrets: oidc_secret_store.OidcSecretStore,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        # one OIDC provider that people can log in through: it finds out about the provider from its discovery
        # document, sends people to log in with the authorization code flow with PKCE, a state and a nonce, and checks
        # the token that comes back (signature, issuer, audience, expiry, nonce); what differs between providers, such
        # as where the groups are in the token, comes from its settings; the tokens are used once and thrown away
        self.settings: oidc_provider_settings.OidcProviderSettings = settings
        self._secrets: oidc_secret_store.OidcSecretStore = secrets
        self._transport: httpx.BaseTransport | None = transport
        self._metadata: ProviderMetadata | None = None

    # ------------------------------------------------------------------------------------------------------------------
    def check(self) -> None:
        # finds out whether the provider can be used: its discovery document is fetched, which fails when the address
        # is wrong or the provider cannot be reached, and its secret has to be there
        self._get_metadata()
        self._secrets.get_secret(self.settings.provider_name)

    # ------------------------------------------------------------------------------------------------------------------
    def begin(self, redirect_uri: str) -> LoginStart:
        # where to send a person to log in, which brings them back to the redirect address, and what to remember
        # until they do
        metadata: ProviderMetadata = self._get_metadata()
        pending: PendingLogin = PendingLogin(
            state=secrets.token_urlsafe(32),
            nonce=secrets.token_urlsafe(32),
            verifier=secrets.token_urlsafe(64),
            redirect_uri=redirect_uri,
        )

        # the parameters of the provider's own come first, so that the ones that make the flow safe cannot be replaced
        parameters: dict[str, str] = dict(self.settings.authorize_parameters)
        parameters["response_type"] = "code"
        parameters["client_id"] = self.settings.client_id
        parameters["redirect_uri"] = redirect_uri
        parameters["scope"] = " ".join(self.settings.scopes)
        parameters["code_challenge"] = _make_code_challenge(pending.verifier)
        parameters["code_challenge_method"] = "S256"
        parameters["state"] = pending.state
        parameters["nonce"] = pending.nonce

        separator: str = "&" if "?" in metadata.authorization_endpoint else "?"
        return LoginStart(url=f"{metadata.authorization_endpoint}{separator}{urlencode(parameters)}", pending=pending)

    # ------------------------------------------------------------------------------------------------------------------
    def finish(self, callback_url: str, pending: PendingLogin) -> external_identity.ExternalIdentity:
        # completes a login: exchanges the code in the address the provider sent the person back to for tokens, checks
        # them, and says who the person is in the terms of this service; a token without a subject is not accepted
        query: dict[str, list[str]] = parse_qs(urlparse(callback_url).query)
        if "error" in query:
            raise OidcLoginError(f"the provider refused the login: {query['error'][0]}")

        state: str = query.get("state", [""])[0]
        code: str = query.get("code", [""])[0]
        if code == "" or not hmac.compare_digest(state.encode("utf-8"), pending.state.encode("utf-8")):
            raise OidcLoginError("the answer does not belong to the login that was started")

        metadata: ProviderMetadata = self._get_metadata()
        id_token: str = self._exchange_code(metadata, code, pending)
        claims: dict[str, object] = self._verify_id_token(metadata, id_token, pending.nonce)
        return self._read_identity(claims)

    # ------------------------------------------------------------------------------------------------------------------
    def get_logout_url(self, return_to: str) -> str | None:
        # where to send a person who logged out, so that the provider ends its own login too, according to its settings;
        # None when logging out of this service is all that is done
        address: str | None = None
        if self.settings.logout == "standard":
            metadata: ProviderMetadata = self._get_metadata()
            if metadata.end_session_endpoint is not None:
                query: str = urlencode({"client_id": self.settings.client_id, "post_logout_redirect_uri": return_to})
                address = f"{metadata.end_session_endpoint}?{query}"
        elif self.settings.logout == "auth0":
            query = urlencode({"client_id": self.settings.client_id, "returnTo": return_to})
            address = f"{self.settings.issuer.rstrip('/')}/v2/logout?{query}"
        return address

    # ------------------------------------------------------------------------------------------------------------------
    def _require_secure(self, address: str) -> None:
        if address.startswith("http://") and not self.settings.is_insecure_http_allowed:
            raise OidcLoginError(f"{address} is not https, which this provider is not allowed to use")

    # ------------------------------------------------------------------------------------------------------------------
    def _fetch_json(self, address: str) -> dict[str, object]:
        self._require_secure(address)
        try:
            with httpx.Client(transport=self._transport, timeout=_REQUEST_TIMEOUT_SECONDS) as http:
                response: httpx.Response = http.get(address)
            response.raise_for_status()
            document: dict[str, object] = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise OidcLoginError(f"{address} could not be read: {error}") from error
        return document

    # ------------------------------------------------------------------------------------------------------------------
    def _get_metadata(self) -> ProviderMetadata:
        # what the provider's discovery document says, which is fetched the first time it is needed and kept; a
        # provider that cannot be reached is tried again the next time and not remembered as unreachable
        if self._metadata is None:
            address: str = f"{self.settings.issuer.rstrip('/')}/.well-known/openid-configuration"
            document: dict[str, object] = self._fetch_json(address)
            try:
                metadata: ProviderMetadata = ProviderMetadata.model_validate(document)
            except ValueError as error:
                raise OidcLoginError(
                    f"the discovery document of {self.settings.issuer} is not right: {error}"
                ) from error

            if metadata.issuer.rstrip("/") != self.settings.issuer.rstrip("/"):
                raise OidcLoginError(f"the discovery document says the issuer is {metadata.issuer}")

            self._require_secure(metadata.authorization_endpoint)
            self._require_secure(metadata.token_endpoint)
            self._require_secure(metadata.jwks_uri)
            self._metadata = metadata
        return self._metadata

    # ------------------------------------------------------------------------------------------------------------------
    def _exchange_code(self, metadata: ProviderMetadata, code: str, pending: PendingLogin) -> str:
        # trades the code for tokens at the provider's token endpoint and returns the id token
        secret: str = self._secrets.get_secret(self.settings.provider_name)
        form: dict[str, str] = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": pending.redirect_uri,
            "code_verifier": pending.verifier,
        }

        # the secret goes in the authorization header, unless the provider says that it only takes it in the body
        methods: list[str] = metadata.token_endpoint_auth_methods_supported
        is_secret_in_body: bool = (
            bool(methods) and "client_secret_basic" not in methods and "client_secret_post" in methods
        )
        if is_secret_in_body:
            form["client_id"] = self.settings.client_id
            form["client_secret"] = secret

        try:
            with httpx.Client(transport=self._transport, timeout=_REQUEST_TIMEOUT_SECONDS) as http:
                if is_secret_in_body:
                    response: httpx.Response = http.post(metadata.token_endpoint, data=form)
                else:
                    basic_auth: httpx.BasicAuth = httpx.BasicAuth(
                        quote(self.settings.client_id, safe=""), quote(secret, safe="")
                    )
                    response = http.post(metadata.token_endpoint, data=form, auth=basic_auth)
            if response.status_code != 200:
                raise OidcLoginError(f"the token endpoint answered {response.status_code}")
            tokens: dict[str, object] = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise OidcLoginError(f"the token endpoint could not be used: {error}") from error

        id_token: object = tokens.get("id_token")
        if not isinstance(id_token, str):
            raise OidcLoginError("the provider sent no id token")
        return id_token

    # ------------------------------------------------------------------------------------------------------------------
    def _verify_id_token(self, metadata: ProviderMetadata, id_token: str, nonce: str) -> dict[str, object]:
        # checks the signature against the provider's keys, and the issuer, audience, expiry and nonce
        try:
            key_set: KeySet = KeySet.import_key_set(cast(KeySetSerialization, self._fetch_json(metadata.jwks_uri)))
            token: jwt.Token = jwt.decode(id_token, key_set, algorithms=_ALGORITHMS)
            registry: jwt.JWTClaimsRegistry = jwt.JWTClaimsRegistry(
                leeway=_CLOCK_LEEWAY_SECONDS,
                iss={"essential": True, "value": metadata.issuer},
                aud={"essential": True, "values": [self.settings.client_id]},
                exp={"essential": True},
                sub={"essential": True},
            )
            registry.validate(token.claims)
        except (JoseError, ValueError) as error:
            raise OidcLoginError(f"the id token is not valid: {error}") from error

        verified: dict[str, object] = dict(token.claims)
        if not hmac.compare_digest(str(verified.get("nonce", "")).encode("utf-8"), nonce.encode("utf-8")):
            raise OidcLoginError("the id token was not made for this login")
        return verified

    # ------------------------------------------------------------------------------------------------------------------
    def _read_identity(self, claims: dict[str, object]) -> external_identity.ExternalIdentity:
        subject: str | None = claim_paths.get_claim_text(claims, "sub")
        if subject is None:
            raise OidcLoginError(f'The provider "{self.settings.provider_name}" did not say who logged in.')

        email: str | None = claim_paths.get_claim_text(claims, self.settings.email_claim)
        username: str = claim_paths.get_claim_text(claims, self.settings.username_claim) or email or subject
        display_name: str = claim_paths.get_claim_text(claims, self.settings.name_claim) or username
        result: external_identity.ExternalIdentity = external_identity.ExternalIdentity(
            issuer=self.settings.issuer,
            subject=subject,
            username=username,
            display_name=display_name,
            email=email,
            provider_group_names=claim_paths.get_claim_list(claims, self.settings.groups_claim),
        )
        return result
