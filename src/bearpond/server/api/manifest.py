from fastapi import APIRouter, Depends, HTTPException
from starlette.status import HTTP_404_NOT_FOUND

from ... import types
from .. import metadata_store
from .. import repository
from . import dependencies

router: APIRouter = APIRouter(prefix="/repos/{repo_name}", tags=["manifest"], dependencies=[Depends(dependencies.require_auth)])


# ----------------------------------------------------------------------------------------------------------------------
@router.get("/manifest")
def get_manifest(repo_name: str, seq: int | None = None) -> types.Manifest:
    store: metadata_store.MetadataStore = repository.get_repository(repo_name).metadata_store
    # reading the seq first and the manifest by seq gives a consistent snapshot even if a commit lands between the two
    target_seq: int = seq if seq is not None else store.get_current_seq()
    manifest: types.Manifest | None = store.get_manifest(target_seq)
    if manifest is None:
        if seq is not None:
            raise HTTPException(HTTP_404_NOT_FOUND, f"no manifest for seq {seq}")
        raise HTTPException(HTTP_404_NOT_FOUND, "no manifest has been generated yet")
    return manifest
