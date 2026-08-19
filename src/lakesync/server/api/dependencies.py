import os

from fastapi import HTTPException, Request
from starlette.status import HTTP_401_UNAUTHORIZED


# ----------------------------------------------------------------------------------------------------------------------
def require_auth(request: Request) -> None:
    # auth is opt-in — with no LAKESYNC_TOKEN set on the server, every request is let through unchecked
    token: str | None = os.environ.get("LAKESYNC_TOKEN")
    if token and request.headers.get("authorization") != f"Bearer {token}":
        raise HTTPException(HTTP_401_UNAUTHORIZED, "missing or invalid bearer token")
