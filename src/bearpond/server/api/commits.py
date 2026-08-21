from fastapi import APIRouter, Depends

from ... import types
from .. import repository
from . import dependencies

router: APIRouter = APIRouter(prefix="/repos/{repo_name}", tags=["commits"], dependencies=[Depends(dependencies.require_auth)])


# ----------------------------------------------------------------------------------------------------------------------
@router.get("/commits")
def get_commit_history(repo_name: str, limit: int = 20, before_seq: int | None = None) -> types.CommitHistoryPage:
    # the repo's commit history, newest first, keyset-paginated by seq (before_seq = a previous page's next_cursor)
    return repository.get_repository(repo_name).metadata_store.get_commit_record_page(limit, before_seq)
