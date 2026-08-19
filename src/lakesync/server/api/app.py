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

from .. import repository, transaction
from . import files, manifest, transactions


# ----------------------------------------------------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    # finish any commit a crash interrupted before accepting traffic — after this, the lake is guaranteed to sit
    # exactly on a manifest version, never halfway between two
    repository.get_repository().recover()
    yield


app: FastAPI = FastAPI(lifespan=lifespan)
app.include_router(transactions.router)
app.include_router(manifest.router)
app.include_router(files.router)


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
# a RepositoryError means the lake's own bookkeeping is broken (e.g. a dangling manifest pointer) — surface it as a
# 500 so a sync client fails loudly instead of mistaking corruption for an empty lake and pruning its whole mirror
@app.exception_handler(repository.RepositoryError)
def handle_repository_error(request: Request, exc: repository.RepositoryError) -> JSONResponse:
    return JSONResponse(status_code=HTTP_500_INTERNAL_SERVER_ERROR, content={"detail": str(exc)})
