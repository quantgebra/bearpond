import os

from fastapi import Depends, HTTPException, Request
from starlette.status import HTTP_401_UNAUTHORIZED

from .. import server as server_module


# ----------------------------------------------------------------------------------------------------------------------
def require_auth(request: Request) -> None:
    # auth is opt-in — with no BEARPOND_TOKEN set on the server, every request is let through unchecked
    token: str | None = os.environ.get("BEARPOND_TOKEN")
    if token and request.headers.get("authorization") != f"Bearer {token}":
        raise HTTPException(HTTP_401_UNAUTHORIZED, "missing or invalid bearer token")


# ----------------------------------------------------------------------------------------------------------------------
def get_server() -> server_module.Server:
    # the running server process has a single configured Server instance; auto-configures from BEARPOND_CONFIG_DIR on
    # first use if nothing has called configure() explicitly
    return server_module.get_server()
