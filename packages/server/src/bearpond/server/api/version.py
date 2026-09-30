import importlib.metadata

from fastapi import APIRouter

from bearpond.protocol import types

# no auth dependency here, deliberately — checking protocol compatibility shouldn't require already knowing
# whether a token is needed
router: APIRouter = APIRouter(tags=["version"])


# ----------------------------------------------------------------------------------------------------------------------
def _server_version() -> str:
    result: str = "unknown"
    try:
        result = importlib.metadata.version("bearpond-server")
    except importlib.metadata.PackageNotFoundError:
        pass
    return result


# ----------------------------------------------------------------------------------------------------------------------
@router.get("/version")
def get_version() -> types.VersionInfo:
    return types.VersionInfo(protocol_version=types.PROTOCOL_VERSION, server_version=_server_version())
