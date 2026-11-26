from fastapi import APIRouter, Depends, HTTPException
from starlette.status import HTTP_404_NOT_FOUND

from bearpond.protocol import types
from .. import repository
from . import dependencies

router: APIRouter = APIRouter(
    prefix="/repos/{repo_name}", tags=["manifest"], dependencies=[Depends(dependencies.require_read)]
)


# ----------------------------------------------------------------------------------------------------------------------
@router.get("/manifest")
def get_manifest(
    repo_name: str,
    seq: int | None = None,
    repo: repository.Repository = Depends(dependencies.get_repository),
) -> types.Manifest:
    # reading the seq first and the manifest by seq gives a consistent snapshot even if a commit lands between the two
    target_seq: int = seq if seq is not None else repo.get_current_seq()
    manifest: types.Manifest | None = repo.get_manifest(target_seq)
    if manifest is None:
        if seq is not None:
            raise HTTPException(HTTP_404_NOT_FOUND, f"no manifest for seq {seq}")
        raise HTTPException(HTTP_404_NOT_FOUND, "no manifest has been generated yet")
    return manifest
