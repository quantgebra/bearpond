from fastapi import APIRouter, Depends

from bearpond.protocol import types
from .. import commit_record
from .. import repository
from . import dependencies

router: APIRouter = APIRouter(
    prefix="/repos/{repo_name}", tags=["commits"], dependencies=[Depends(dependencies.require_read)]
)


# ----------------------------------------------------------------------------------------------------------------------
@router.get("/commits")
def get_commit_history(
    repo_name: str,
    limit: int = 20,
    before_seq: int | None = None,
    repo: repository.Repository = Depends(dependencies.get_repository),
) -> types.CommitHistoryPage:
    # the repo's commit history, newest first, keyset-paginated by seq (before_seq = a previous page's next_cursor)
    return commit_record.CommitRecord.get_commit_page(repo.session, repo.name, limit, before_seq)
