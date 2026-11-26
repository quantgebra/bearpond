import base64
import hashlib
import secrets
import time
from urllib.parse import parse_qs, urlparse

import httpx
from joserfc import jwt
from joserfc.jwk import KeySet, RSAKey

ISSUER: str = "https://idp.example"
CLIENT_ID: str = "bearpond-client"
CLIENT_SECRET: str = "the-client-secret"


# ======================================================================================================================
class FakeOidcProvider:

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================

    # ------------------------------------------------------------------------------------------------------------------
    def __init__(self) -> None:
        # an OIDC provider that answers in memory: the discovery document, its keys, and the token endpoint, which
        # checks the client secret and the PKCE verifier the way a real one does
        self.key: RSAKey = RSAKey.generate_key(2048, parameters={"kid": "fake-key", "use": "sig"})
        self.transport: httpx.MockTransport = httpx.MockTransport(self._answer)
        self.is_reachable: bool = True
        self.audience: str = CLIENT_ID
        self.seconds_until_expiry: int = 300
        self.nonce_override: str | None = None
        self.signing_key: RSAKey = self.key
        self._issued_by_code: dict[str, dict[str, str]] = {}
        self._claims_by_code: dict[str, dict[str, object]] = {}

    # ------------------------------------------------------------------------------------------------------------------
    def approve(self, authorization_url: str, claims: dict[str, object]) -> str:
        # what the provider does once the person has logged in there: it sends them back to the redirect address with
        # a code, and remembers what the code stands for
        query: dict[str, list[str]] = parse_qs(urlparse(authorization_url).query)
        code: str = secrets.token_urlsafe(16)
        self._issued_by_code[code] = {
            "nonce": query["nonce"][0],
            "challenge": query["code_challenge"][0],
            "redirect_uri": query["redirect_uri"][0],
        }
        self._claims_by_code[code] = claims
        return f"{query['redirect_uri'][0]}?code={code}&state={query['state'][0]}"

    # ------------------------------------------------------------------------------------------------------------------
    def _answer(self, request: httpx.Request) -> httpx.Response:
        path: str = request.url.path
        response: httpx.Response
        if not self.is_reachable:
            response = httpx.Response(503, text="down")
        elif path == "/.well-known/openid-configuration":
            response = httpx.Response(200, json=self._make_discovery_document())
        elif path == "/jwks":
            response = httpx.Response(200, json=KeySet([self.key]).as_dict(private=False))
        elif path == "/token":
            response = self._answer_token_request(request)
        else:
            response = httpx.Response(404)
        return response

    # ------------------------------------------------------------------------------------------------------------------
    def _make_discovery_document(self) -> dict[str, object]:
        document: dict[str, object] = {
            "issuer": ISSUER,
            "authorization_endpoint": f"{ISSUER}/authorize",
            "token_endpoint": f"{ISSUER}/token",
            "jwks_uri": f"{ISSUER}/jwks",
            "end_session_endpoint": f"{ISSUER}/logout",
            "token_endpoint_auth_methods_supported": ["client_secret_basic"],
        }
        return document

    # ------------------------------------------------------------------------------------------------------------------
    def _answer_token_request(self, request: httpx.Request) -> httpx.Response:
        form: dict[str, list[str]] = parse_qs(request.content.decode("utf-8"))
        code: str = form.get("code", [""])[0]
        issued: dict[str, str] | None = self._issued_by_code.pop(code, None)
        response: httpx.Response

        # the client has to be the one the code was made for, with its secret and the verifier of its challenge
        expected_basic: str = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode("utf-8")).decode("ascii")
        verifier: str = form.get("code_verifier", [""])[0]
        digest: bytes = hashlib.sha256(verifier.encode("ascii")).digest()
        challenge: str = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")

        if issued is None:
            response = httpx.Response(400, json={"error": "invalid_grant"})
        elif request.headers.get("authorization") != f"Basic {expected_basic}":
            response = httpx.Response(401, json={"error": "invalid_client"})
        elif challenge != issued["challenge"] or form["redirect_uri"][0] != issued["redirect_uri"]:
            response = httpx.Response(400, json={"error": "invalid_grant"})
        else:
            response = httpx.Response(200, json={"id_token": self._make_id_token(code, issued["nonce"])})
        return response

    # ------------------------------------------------------------------------------------------------------------------
    def _make_id_token(self, code: str, nonce: str) -> str:
        now_seconds: int = int(time.time())
        claims: dict[str, object] = {
            "iss": ISSUER,
            "aud": self.audience,
            "exp": now_seconds + self.seconds_until_expiry,
            "iat": now_seconds,
            "nonce": self.nonce_override or nonce,
        }
        claims.update(self._claims_by_code[code])
        return jwt.encode({"alg": "RS256", "kid": "fake-key"}, claims, self.signing_key)
