from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from starlette.status import HTTP_404_NOT_FOUND

from bearpond.protocol import types
from .. import object_record
from .. import repository
from . import dependencies

router: APIRouter = APIRouter(
    prefix="/repos/{repo_name}", tags=["files"], dependencies=[Depends(dependencies.require_read)]
)


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
def get_file(
    repo_name: str,
    rel_path: str,
    limit: int = 1000,
    cursor: str | None = None,
    repo: repository.Repository = Depends(dependencies.get_repository),
) -> DirectoryListing | FileResponse:
    # doubles as a raw file download (what the client's sync/download_file use) and an S3-style prefix+delimiter
    # directory listing (Contents + CommonPrefixes, files paginated via limit/cursor) for browsing and debugging
    result: DirectoryListing | FileResponse
    current: types.FileMetadata | None = object_record.ObjectRecord.get_file_by_path(repo.session, repo.name, rel_path)
    if current is not None:
        result = FileResponse(repo.object_store.get_location_for_address(current.sha256))
    else:
        prefix: str = rel_path.rstrip("/") + "/" if rel_path else ""
        page: object_record.DirectoryPage = object_record.ObjectRecord.get_directory_page(
            repo.session, repo.name, prefix, limit, cursor
        )
        if not page.files and not page.directories and cursor is None and rel_path:
            raise HTTPException(HTTP_404_NOT_FOUND, f"file not found: {rel_path}")
        listed_files: list[ListedFile] = []
        for f in page.files:
            listed_files.append(ListedFile(name=f.path[len(prefix) :], size=f.size))
        result = DirectoryListing(
            path=rel_path,
            files=listed_files,
            directories=[d[len(prefix) :] for d in page.directories],
            next_cursor=page.next_cursor,
        )
    return result
