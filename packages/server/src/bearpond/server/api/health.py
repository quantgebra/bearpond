import sqlalchemy
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import orm

from . import dependencies

# no auth dependency here, deliberately: whatever starts the server asks this whether it is up, before anyone logs in
router: APIRouter = APIRouter(tags=["health"])


# ======================================================================================================================
class HealthInfo(BaseModel):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    status: str


# ----------------------------------------------------------------------------------------------------------------------
@router.get("/health")
def get_health(session: orm.Session = Depends(dependencies.get_session, scope="function")) -> HealthInfo:
    # the server is up when it answers and its database does too; a database that does not answer is an error
    session.execute(sqlalchemy.text("SELECT 1"))
    return HealthInfo(status="ok")
