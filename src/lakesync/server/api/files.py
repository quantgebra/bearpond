from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from starlette.status import HTTP_404_NOT_FOUND

from .. import paths
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
    # doubles as a raw file download (what the client's sync/download_file use) and a directory browser for debugging
    file_path: Path = paths.safe_join(repository.get_repository().data_root, rel_path)

    result: DirectoryListing | FileResponse
    if file_path.is_dir():
        entries: list[Path] = sorted(file_path.iterdir(), key=lambda p: p.name)
        result = DirectoryListing(
            path=rel_path,
            entries=[
                DirectoryEntry(
                    name=entry.name,
                    type="directory" if entry.is_dir() else "file",
                    **({"size": entry.stat().st_size} if entry.is_file() else {}),
                )
                for entry in entries
            ],
        )
    elif file_path.is_file():
        result = FileResponse(file_path)
    else:
        raise HTTPException(HTTP_404_NOT_FOUND, f"file not found: {rel_path}")
    return result
