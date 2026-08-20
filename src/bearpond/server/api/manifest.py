from fastapi import APIRouter, Depends, HTTPException
from starlette.status import HTTP_404_NOT_FOUND

from ... import types
from .. import metadata_store
from .. import repository
from . import dependencies

router: APIRouter = APIRouter(tags=["manifest"], dependencies=[Depends(dependencies.require_auth)])


# ----------------------------------------------------------------------------------------------------------------------
@router.get("/manifest")
def get_manifest() -> types.Manifest:
    store: metadata_store.MetadataStore = repository.get_repository().metadata_store
    # reading the seq first and the manifest by seq gives a consistent snapshot even if a commit lands between the two
    manifest: types.Manifest | None = store.get_manifest(store.get_current_seq())
    if manifest is None:
        raise HTTPException(HTTP_404_NOT_FOUND, "no manifest has been generated yet")
    return manifest
