from fastapi import APIRouter, Depends

from ... import types
from .. import repository
from . import dependencies

router: APIRouter = APIRouter(prefix="/repos", tags=["repos"], dependencies=[Depends(dependencies.require_auth)])


# ----------------------------------------------------------------------------------------------------------------------
@router.get("")
def list_repositories() -> list[types.RepositoryInfo]:
    return [types.RepositoryInfo(name=name) for name in repository.list_repositories()]


# ----------------------------------------------------------------------------------------------------------------------
@router.post("", status_code=201)
def create_repository(request: types.CreateRepositoryRequest) -> types.RepositoryInfo:
    # an admin operation today (any authenticated caller); gains a role check when multi-user auth lands
    repository.create_repository(request.name)
    return types.RepositoryInfo(name=request.name)
