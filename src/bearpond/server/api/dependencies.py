import os

from fastapi import HTTPException, Request
from starlette.status import HTTP_401_UNAUTHORIZED


# ----------------------------------------------------------------------------------------------------------------------
def require_auth(request: Request) -> None:
    # auth is opt-in — with no BEARPOND_TOKEN set on the server, every request is let through unchecked
    token: str | None = os.environ.get("BEARPOND_TOKEN")
    if token and request.headers.get("authorization") != f"Bearer {token}":
        raise HTTPException(HTTP_401_UNAUTHORIZED, "missing or invalid bearer token")
