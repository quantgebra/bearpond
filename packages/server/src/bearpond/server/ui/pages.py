from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel
from starlette.staticfiles import StaticFiles
from starlette.status import HTTP_303_SEE_OTHER

from . import access_page
from . import account_page
from . import api_keys_page
from . import groups_page
from . import invite_page
from . import login_page
from . import page_renderer
from . import page_resource
from . import repositories_page
from . import users_page

STATIC_DIR: Path = Path(__file__).parent / "static"


# ======================================================================================================================
class RefusedModel(BaseModel):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    status_code: int
    message: str


# ----------------------------------------------------------------------------------------------------------------------
def mount_pages(app: FastAPI) -> None:
    # the user interface is made on the server, a page at a time: each group of pages is a PageResource whose routes
    # fill in a template; the stylesheet and the one small script are served as they are
    renderer: page_renderer.PageRenderer = page_renderer.PageRenderer()
    resources: list[page_resource.PageResource] = [
        login_page.LoginPage(renderer),
        invite_page.InvitePage(renderer),
        account_page.AccountPage(renderer),
        api_keys_page.ApiKeysPage(renderer),
        repositories_page.RepositoriesPage(renderer),
        users_page.UsersPage(renderer),
        groups_page.GroupsPage(renderer),
        access_page.AccessPage(renderer),
    ]
    for resource in resources:
        app.include_router(resource.create_router())
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    def redirect_home() -> Response:
        return RedirectResponse("/repositories", status_code=HTTP_303_SEE_OTHER)

    app.add_api_route("/", redirect_home, methods=["GET"], include_in_schema=False)

    # a page that needs a login sends the person to log in, and one that cannot be shown says why, as a page
    def handle_login_required(request: Request, error: Exception) -> Response:
        assert isinstance(error, page_resource.LoginRequiredError)
        return RedirectResponse(page_resource.make_login_path(error.next_path), status_code=HTTP_303_SEE_OTHER)

    def handle_page_refused(request: Request, error: Exception) -> Response:
        assert isinstance(error, page_resource.PageRefusedError)
        title: str = "Not found" if error.status_code == 404 else "Refused"
        head: page_renderer.PageHead = page_renderer.PageHead(title=title, section="", is_bare=True)
        model: RefusedModel = RefusedModel(status_code=error.status_code, message=error.message)
        return HTMLResponse(renderer.render("refused", head, model), status_code=error.status_code)

    app.add_exception_handler(page_resource.LoginRequiredError, handle_login_required)
    app.add_exception_handler(page_resource.PageRefusedError, handle_page_refused)
