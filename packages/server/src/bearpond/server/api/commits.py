from fastapi import APIRouter, Depends

from bearpond.protocol import types
from .. import server as server_module
from . import dependencies

router: APIRouter = APIRouter(prefix="/repos/{repo_name}", tags=["commits"], dependencies=[Depends(dependencies.require_auth)])


# ----------------------------------------------------------------------------------------------------------------------
@router.get("/commits")
def get_commit_history(
    repo_name: str,
    limit: int = 20,
    before_seq: int | None = None,
    server: server_module.Server = Depends(dependencies.get_server),
) -> types.CommitHistoryPage:
    # the repo's commit history, newest first, keyset-paginated by seq (before_seq = a previous page's next_cursor)
    return server.get_repository(repo_name).metadata_store.get_commit_record_page(limit, before_seq)
