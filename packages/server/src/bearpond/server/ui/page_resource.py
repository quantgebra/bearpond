from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from quantgebra.auth import auth_services
from quantgebra.auth.core import auth_error
from quantgebra.auth.core import token_secrets
from quantgebra.auth.http import auth_gate

from ..api import dependencies
from . import page_renderer


# ======================================================================================================================
class LoginRequiredError(Exception):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================

    # ------------------------------------------------------------------------------------------------------------------
    def __init__(self, next_path: str) -> None:
        # nobody is logged in, so the person is sent to log in, and back to the page they asked for afterwards
        super().__init__(f"login required for {next_path}")
        self.next_path: str = next_path


# ======================================================================================================================
class PageRefusedError(Exception):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================

    # ------------------------------------------------------------------------------------------------------------------
    def __init__(self, status_code: int, message: str) -> None:
        # a page that cannot be shown, such as one about something that is not there or that the person may not see;
        # the message is shown to the person as it is
        super().__init__(message)
        self.status_code: int = status_code
        self.message: str = message


# ======================================================================================================================
class PageResource:

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def compute_initials(display_name: str) -> str:
        # the two letters in the badge of the person who is logged in
        words: list[str] = display_name.split()
        initials: str = "?"
        if len(words) >= 2:
            initials = (words[0][0] + words[1][0]).upper()
        elif len(words) == 1:
            initials = words[0][:2].upper()
        return initials

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================

    # ------------------------------------------------------------------------------------------------------------------
    def __init__(self, renderer: page_renderer.PageRenderer) -> None:
        # one group of pages of the user interface, such as the repositories: the routes that read what the pages show
        # and give it to the renderer as a model of plain data, which the template only lays out
        self.renderer: page_renderer.PageRenderer = renderer

    # ------------------------------------------------------------------------------------------------------------------
    def create_router(self) -> APIRouter:
        router: APIRouter = APIRouter(include_in_schema=False)
        self.add_routes(router)
        return router

    # ------------------------------------------------------------------------------------------------------------------
    def add_routes(self, router: APIRouter) -> None:
        raise NotImplementedError

    # ------------------------------------------------------------------------------------------------------------------
    def _require_viewer(self, request: Request, services: auth_services.AuthServices) -> auth_gate.RequestAuth:
        # somebody who may see the server; a person who is not logged in is sent to log in, unless nobody can log in
        # because the server only takes the shared token
        found: auth_gate.RequestAuth | None = dependencies.get_auth_gate().recognize(request, services)
        if found is None:
            if services.auth.config.mode == "enforced":
                raise LoginRequiredError(self._make_next_path(request))
            else:
                raise PageRefusedError(
                    403,
                    "This server only lets in scripts that send its shared token. To log in from a browser, set "
                    "auth.mode to enforced in server.yaml.",
                )

        # a person who has logged in but been put in no group yet may see nothing
        baseline_name: str | None = services.auth.config.catalog.baseline_name
        if baseline_name is not None and not found.actor.can(baseline_name):
            raise PageRefusedError(
                403, "You are logged in, but you are not allowed to see anything yet. Ask an administrator to put you "
                "in a group."
            )
        return found

    # ------------------------------------------------------------------------------------------------------------------
    def _make_next_path(self, request: Request) -> str:
        next_path: str = request.url.path
        if request.url.query:
            next_path = f"{next_path}?{request.url.query}"
        return next_path

    # ------------------------------------------------------------------------------------------------------------------
    def _require_form_viewer(
        self, request: Request, services: auth_services.AuthServices, csrf_token: str
    ) -> auth_gate.RequestAuth:
        # a form that changes something has to come from this server's own pages, and carry the secret of the login
        # that the page was made for, so that another site cannot make the browser submit it
        dependencies.get_auth_gate().check_origin(request, services)
        found: auth_gate.RequestAuth = self._require_viewer(request, services)
        if found.session is not None and not token_secrets.is_same_token(csrf_token, found.session.csrf_token):
            raise PageRefusedError(403, "This form is out of date. Go back, reload the page and try again.")
        return found

    # ------------------------------------------------------------------------------------------------------------------
    def _require_administrator(self, request: Request, services: auth_services.AuthServices) -> auth_gate.RequestAuth:
        # somebody who may administer, for the pages that change who may do what
        found: auth_gate.RequestAuth = self._require_viewer(request, services)
        if not found.actor.can(services.auth.config.catalog.admin_name):
            raise PageRefusedError(403, "Only administrators may see this page.")
        return found

    # ------------------------------------------------------------------------------------------------------------------
    def _require_form_administrator(
        self, request: Request, services: auth_services.AuthServices, csrf_token: str
    ) -> auth_gate.RequestAuth:
        found: auth_gate.RequestAuth = self._require_form_viewer(request, services, csrf_token)
        if not found.actor.can(services.auth.config.catalog.admin_name):
            raise PageRefusedError(403, "Only administrators may do this.")
        return found

    # ------------------------------------------------------------------------------------------------------------------
    def _make_head(
        self,
        services: auth_services.AuthServices,
        found: auth_gate.RequestAuth,
        title: str,
        section: str,
        crumbs: list[page_renderer.Crumb],
    ) -> page_renderer.PageHead:
        # the head of a page in the shell, with the person who is logged in at the foot of the menu
        user_menu: page_renderer.UserMenu = page_renderer.UserMenu(
            display_name=found.actor.display_name,
            initials=PageResource.compute_initials(found.actor.display_name),
            csrf_token=found.session.csrf_token if found.session is not None else "",
            is_administrator=found.actor.can(services.auth.config.catalog.admin_name),
            is_development=services.auth.config.mode == "none",
        )
        return page_renderer.PageHead(title=title, section=section, crumbs=crumbs, user=user_menu)

    # ------------------------------------------------------------------------------------------------------------------
    def _send(
        self, view_name: str, head: page_renderer.PageHead, page: BaseModel, status_code: int = 200
    ) -> HTMLResponse:
        return HTMLResponse(self.renderer.render(view_name, head, page), status_code=status_code)


# ----------------------------------------------------------------------------------------------------------------------
def make_login_path(next_path: str) -> str:
    return f"/login?next={quote(next_path, safe='/')}"


# ----------------------------------------------------------------------------------------------------------------------
def get_status_code(error: auth_error.AuthError) -> int:
    # the status of a page that shows what an administrator was refused, and why
    status_code: int = 400
    if error.kind == "forbidden":
        status_code = 403
    return status_code
