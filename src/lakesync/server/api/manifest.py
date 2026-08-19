from fastapi import APIRouter, Depends, HTTPException
from starlette.status import HTTP_404_NOT_FOUND

from ... import types
from .. import repository
from . import dependencies

router: APIRouter = APIRouter(tags=["manifest"], dependencies=[Depends(dependencies.require_auth)])


# ----------------------------------------------------------------------------------------------------------------------
@router.get("/manifest")
def get_manifest() -> types.Manifest:
    manifest: types.Manifest | None = repository.get_repository().latest()
    if manifest is None:
        raise HTTPException(HTTP_404_NOT_FOUND, "no manifest has been generated yet")
    return manifest
