from fastapi import APIRouter, Depends
from quantgebra.auth.http import auth_gate
from sqlalchemy import orm

from bearpond.protocol import types
from .. import server as server_module
from .. import server_auth
from . import dependencies

router: APIRouter = APIRouter(prefix="/repos", tags=["repos"])


# ----------------------------------------------------------------------------------------------------------------------
@router.get("")
def list_repositories(
    found: auth_gate.RequestAuth = Depends(dependencies.require_user),
    session: orm.Session = Depends(dependencies.get_session, scope="function"),
    server: server_module.Server = Depends(dependencies.get_server),
) -> list[types.RepositoryInfo]:
    # only the repositories that the person may read
    repos: list[types.RepositoryInfo] = []
    for name in server.list_repositories(session):
        if found.actor.can(server_auth.PERMISSION_READ, name):
            repos.append(types.RepositoryInfo(name=name))
    return repos


# ----------------------------------------------------------------------------------------------------------------------
@router.post("", status_code=201)
def create_repository(
    request: types.CreateRepositoryRequest,
    found: auth_gate.RequestAuth = Depends(dependencies.require_admin),
    session: orm.Session = Depends(dependencies.get_session, scope="function"),
    server: server_module.Server = Depends(dependencies.get_server),
) -> types.RepositoryInfo:
    server.create_repository(session, request.name)
    return types.RepositoryInfo(name=request.name)
