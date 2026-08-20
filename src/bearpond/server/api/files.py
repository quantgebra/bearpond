from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from starlette.status import HTTP_404_NOT_FOUND

from ... import types
from .. import repository
from . import dependencies

router: APIRouter = APIRouter(tags=["files"], dependencies=[Depends(dependencies.require_auth)])


# ======================================================================================================================
class DirectoryEntry(BaseModel):
    name: str
    type: str
    size: int | None = None


# ======================================================================================================================
class DirectoryListing(BaseModel):
    path: str
    entries: list[DirectoryEntry]


# ----------------------------------------------------------------------------------------------------------------------
@router.get("/files/{rel_path:path}", response_model=None)
def get_file(rel_path: str) -> DirectoryListing | FileResponse:
    # doubles as a raw file download (what the client's sync/download_file use) and a directory browser for
    # debugging — both answered from the metadata store, since objects live under their hash, not their path
    repo: repository.Repository = repository.get_repository()

    result: DirectoryListing | FileResponse
    file_metadata: types.FileMetadata | None = repo.metadata_store.get_file_metadata(rel_path)
    if file_metadata is not None:
        result = FileResponse(repo.object_store.get_location_for_address(file_metadata.sha256))
    else:
        # not a file — interpret rel_path as a directory prefix and list its immediate children; the store
        # prefix-filters in sqlite (an index range scan), so we never pull the whole lake's mapping for a listing
        prefix: str = rel_path.rstrip("/") + "/" if rel_path else ""
        entries: dict[str, DirectoryEntry] = {}
        for stored in repo.metadata_store.get_current_file_metadata_list(prefix if prefix else None):
            if stored.path.startswith(prefix):
                head, sep, _ = stored.path[len(prefix):].partition("/")
                if sep:
                    entries[head] = DirectoryEntry(name=head, type="directory")
                elif head not in entries:
                    entries[head] = DirectoryEntry(name=head, type="file", size=stored.size)
        if not entries and rel_path:
            raise HTTPException(HTTP_404_NOT_FOUND, f"file not found: {rel_path}")
        result = DirectoryListing(path=rel_path, entries=[entries[name] for name in sorted(entries)])
    return result
