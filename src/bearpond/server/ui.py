"""Web UI support for bearpond.

The UI is a React single-page application built from the web/ directory. In development it is served by Vite's
own dev server, which proxies API calls to the FastAPI backend. In production (or after `npm run build`) the built
static files live in src/bearpond/server/static/ and are served directly by FastAPI.
"""

from pathlib import Path

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from starlette.staticfiles import StaticFiles

router: APIRouter = APIRouter(prefix="/ui", tags=["ui"])

STATIC_DIR: Path = Path(__file__).parent / "static"
ASSETS_DIR: Path = STATIC_DIR / "assets"


# ----------------------------------------------------------------------------------------------------------------------
@router.get("/health")
def ui_health() -> JSONResponse:
    # a lightweight endpoint the React app can use to confirm the UI backend is reachable
    return JSONResponse({"status": "ok"})


# ----------------------------------------------------------------------------------------------------------------------
def mount_ui(app: FastAPI) -> None:
    # Serve the built React app. API routes are registered before this is called, so they take precedence.
    # Vite places hashed JS/CSS under /assets and keeps index.html at the root, so we mount assets explicitly
    # and return index.html for the root path and for all client-side routes handled by React Router.
    if not (STATIC_DIR / "index.html").exists():
        @app.get("/")
        def ui_not_built(request: Request) -> JSONResponse:  # noqa: ARG001
            return JSONResponse(
                {
                    "detail": "bearpond web UI is not built. "
                    "Run 'npm run build' in the web/ directory, then restart the server."
                },
                status_code=404,
            )
        return

    if ASSETS_DIR.exists():
        app.mount("/assets", StaticFiles(directory=ASSETS_DIR), name="static-assets")

    @app.get("/")
    def serve_index(request: Request) -> FileResponse:  # noqa: ARG001
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/{catchall:path}")
    def serve_spa(catchall: str, request: Request) -> FileResponse:  # noqa: ARG001
        return FileResponse(STATIC_DIR / "index.html")
