from fastapi import APIRouter, Depends

from ... import types
from .. import server as server_module
from . import dependencies

router: APIRouter = APIRouter(prefix="/repos", tags=["repos"], dependencies=[Depends(dependencies.require_auth)])


# ----------------------------------------------------------------------------------------------------------------------
@router.get("")
def list_repositories(server: server_module.Server = Depends(dependencies.get_server)) -> list[types.RepositoryInfo]:
    repos: list[types.RepositoryInfo] = []
    for name in server.list_repositories():
        repos.append(types.RepositoryInfo(name=name))
    return repos


# ----------------------------------------------------------------------------------------------------------------------
@router.post("", status_code=201)
def create_repository(
    request: types.CreateRepositoryRequest,
    server: server_module.Server = Depends(dependencies.get_server),
) -> types.RepositoryInfo:
    # an admin operation today (any authenticated caller); gains a role check when multi-user auth lands
    server.create_repository(request.name)
    return types.RepositoryInfo(name=request.name)
