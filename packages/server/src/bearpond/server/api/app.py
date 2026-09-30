from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.status import (
    HTTP_400_BAD_REQUEST,
    HTTP_404_NOT_FOUND,
    HTTP_409_CONFLICT,
    HTTP_500_INTERNAL_SERVER_ERROR,
)

from .. import metadata_store, repository, server as server_module, transaction, utils
from .. import ui as ui_module
from . import commits, files, manifest, repos, transactions, version


# ----------------------------------------------------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    # the metadata store (Postgres/SQLite) is the source of truth and recovers itself; no startup reconciliation is
    # required now that exported manifest files and staging directories have been removed
    yield


app: FastAPI = FastAPI(lifespan=lifespan)
app.include_router(version.router)
app.include_router(repos.router)
app.include_router(commits.router)
app.include_router(transactions.router)
app.include_router(manifest.router)
app.include_router(files.router)
app.include_router(ui_module.router)
ui_module.mount_ui(app)


# ----------------------------------------------------------------------------------------------------------------------
# maps Transaction's plain domain exceptions to HTTP responses, in one place, so the route handlers never need to know
# these exceptions exist — this is what makes Transaction usable and testable without any of FastAPI in the picture
@app.exception_handler(transaction.TransactionNotFoundError)
def handle_transaction_not_found(request: Request, exc: transaction.TransactionNotFoundError) -> JSONResponse:
    return JSONResponse(status_code=HTTP_404_NOT_FOUND, content={"detail": str(exc)})


# ----------------------------------------------------------------------------------------------------------------------
@app.exception_handler(transaction.TransactionValidationError)
def handle_transaction_validation_error(request: Request, exc: transaction.TransactionValidationError) -> JSONResponse:
    return JSONResponse(status_code=HTTP_400_BAD_REQUEST, content={"detail": str(exc)})


# ----------------------------------------------------------------------------------------------------------------------
@app.exception_handler(transaction.TransactionConflictError)
def handle_transaction_conflict(request: Request, exc: transaction.TransactionConflictError) -> JSONResponse:
    return JSONResponse(status_code=HTTP_409_CONFLICT, content={"detail": str(exc)})


# ----------------------------------------------------------------------------------------------------------------------
# a RepositoryNotFoundError is a client-side 404; any other RepositoryError means the lake's own bookkeeping is
# broken — surface it as a 500 so a client fails loudly instead of mistaking corruption for an empty lake
@app.exception_handler(repository.RepositoryNotFoundError)
def handle_repository_not_found(request: Request, exc: repository.RepositoryNotFoundError) -> JSONResponse:
    return JSONResponse(status_code=HTTP_404_NOT_FOUND, content={"detail": str(exc)})


# ----------------------------------------------------------------------------------------------------------------------
@app.exception_handler(repository.RepositoryExistsError)
def handle_repository_exists(request: Request, exc: repository.RepositoryExistsError) -> JSONResponse:
    return JSONResponse(status_code=HTTP_409_CONFLICT, content={"detail": str(exc)})


# ----------------------------------------------------------------------------------------------------------------------
@app.exception_handler(repository.InvalidRepositoryNameError)
def handle_invalid_repository_name(request: Request, exc: repository.InvalidRepositoryNameError) -> JSONResponse:
    return JSONResponse(status_code=HTTP_400_BAD_REQUEST, content={"detail": str(exc)})


# ----------------------------------------------------------------------------------------------------------------------
@app.exception_handler(repository.RepositoryError)
def handle_repository_error(request: Request, exc: repository.RepositoryError) -> JSONResponse:
    return JSONResponse(status_code=HTTP_500_INTERNAL_SERVER_ERROR, content={"detail": str(exc)})


# ----------------------------------------------------------------------------------------------------------------------
# a MetadataConflictError should normally be translated to TransactionConflictError inside the repository — if one
# ever escapes, it's still a conflict, not a server fault
@app.exception_handler(metadata_store.MetadataConflictError)
def handle_metadata_conflict(request: Request, exc: metadata_store.MetadataConflictError) -> JSONResponse:
    return JSONResponse(status_code=HTTP_409_CONFLICT, content={"detail": str(exc)})


# ----------------------------------------------------------------------------------------------------------------------
@app.exception_handler(metadata_store.MetadataStoreError)
def handle_metadata_store_error(request: Request, exc: metadata_store.MetadataStoreError) -> JSONResponse:
    return JSONResponse(status_code=HTTP_500_INTERNAL_SERVER_ERROR, content={"detail": str(exc)})


# ----------------------------------------------------------------------------------------------------------------------
@app.exception_handler(utils.InvalidPathError)
def handle_invalid_path(request: Request, exc: utils.InvalidPathError) -> JSONResponse:
    return JSONResponse(status_code=HTTP_400_BAD_REQUEST, content={"detail": str(exc)})
