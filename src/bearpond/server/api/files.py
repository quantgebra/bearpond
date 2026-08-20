from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from starlette.status import HTTP_404_NOT_FOUND

from ... import types
from .. import metadata_store
from .. import repository
from . import dependencies

router: APIRouter = APIRouter(tags=["files"], dependencies=[Depends(dependencies.require_auth)])


# ======================================================================================================================
class ListedFile(BaseModel):
    name: str
    size: int


# ======================================================================================================================
class DirectoryListing(BaseModel):
    path: str
    files: list[ListedFile]
    directories: list[str]
    next_cursor: str | None = None


# ----------------------------------------------------------------------------------------------------------------------
@router.get("/files/{rel_path:path}", response_model=None)
def get_file(rel_path: str, limit: int = 1000, cursor: str | None = None) -> DirectoryListing | FileResponse:
    # doubles as a raw file download (what the client's sync/download_file use) and an S3-style prefix+delimiter
    # directory listing (Contents + CommonPrefixes, files paginated via limit/cursor) for browsing and debugging
    repo: repository.Repository = repository.get_repository()

    result: DirectoryListing | FileResponse
    current: types.FileMetadata | None = repo.metadata_store.get_file_metadata(rel_path)
    if current is not None:
        result = FileResponse(repo.object_store.get_location_for_address(current.sha256))
    else:
        prefix: str = rel_path.rstrip("/") + "/" if rel_path else ""
        page: metadata_store.DirectoryPage = repo.metadata_store.list_directory(prefix, limit, cursor)
        if not page.files and not page.directories and cursor is None and rel_path:
            raise HTTPException(HTTP_404_NOT_FOUND, f"file not found: {rel_path}")
        result = DirectoryListing(
            path=rel_path,
            files=[ListedFile(name=f.path[len(prefix):], size=f.size) for f in page.files],
            directories=[d[len(prefix):] for d in page.directories],
            next_cursor=page.next_cursor,
        )
    return result
