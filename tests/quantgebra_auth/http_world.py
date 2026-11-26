from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import sqlalchemy
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient
from quantgebra.auth import auth_services
from quantgebra.auth import auth_system
from quantgebra.auth.core import actor
from quantgebra.auth.core import auth_config
from quantgebra.auth.core import auth_service
from quantgebra.auth.http import auth_gate
from quantgebra.auth.http import auth_router
from quantgebra.auth.local import local_authentication
from sqlalchemy import orm

from . import auth_world
from . import fake_oidc_provider
from . import oidc_world


# ======================================================================================================================
class HttpWorld:

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================

    # ------------------------------------------------------------------------------------------------------------------
    def __init__(self, engine: sqlalchemy.Engine, secrets_directory: Path, mode: str = "enforced") -> None:
        # a small service with the library's endpoints and two routes of its own, one that needs the permission to read
        # a repository and one that needs the permission to write it; every request has a session of its own, which is
        # committed when the request has gone well, the way an application owns it
        self.fake_provider: fake_oidc_provider.FakeOidcProvider = fake_oidc_provider.FakeOidcProvider()
        self.engine: sqlalchemy.Engine = engine
        config: auth_config.AuthConfig = auth_config.AuthConfig(
            "none" if mode == "none" else "enforced",
            auth_world.BASE_URL,
            "administrators",
            auth_world.make_catalog(),
            480,
            True,
            auth_world.make_rules(),
        )
        self.system: auth_system.AuthSystem = auth_system.AuthSystem(
            config, secrets_directory, self.fake_provider.transport
        )
        self.gate: auth_gate.AuthGate = auth_gate.AuthGate()
        self.app: FastAPI = FastAPI()

        def get_services() -> Iterator[auth_services.AuthServices]:
            with orm.Session(engine, expire_on_commit=False) as session:
                try:
                    yield self.system.bind(session)
                    session.commit()
                except BaseException:
                    session.rollback()
                    raise

        self.app.include_router(auth_router.AuthRouter(get_services, lambda: self.gate).router)

        def require_read(
            request: Request, services: auth_services.AuthServices = Depends(get_services)
        ) -> auth_gate.RequestAuth:
            return self.gate.check_permission(request, services, "read", "repo_name")

        def require_write(
            request: Request, services: auth_services.AuthServices = Depends(get_services)
        ) -> auth_gate.RequestAuth:
            return self.gate.check_permission(request, services, "write", "repo_name")

        @self.app.get("/repos/{repo_name}")
        def read_repository(repo_name: str, found: auth_gate.RequestAuth = Depends(require_read)) -> dict[str, str]:
            return {"repo": repo_name, "user": found.actor.username}

        @self.app.post("/repos/{repo_name}")
        def write_repository(repo_name: str, found: auth_gate.RequestAuth = Depends(require_write)) -> dict[str, str]:
            return {"repo": repo_name, "user": found.actor.username}

        self.client: TestClient = TestClient(self.app, base_url=auth_world.BASE_URL, follow_redirects=False)

    # ------------------------------------------------------------------------------------------------------------------
    def start(self) -> local_authentication.FirstAdministratorOutcome:
        return self.system.start(self.engine)

    # ------------------------------------------------------------------------------------------------------------------
    def make_other_client(self) -> TestClient:
        # another browser, with no cookies of its own
        return TestClient(self.app, base_url=auth_world.BASE_URL, follow_redirects=False)

    # ------------------------------------------------------------------------------------------------------------------
    def log_first_administrator_in(self, client: TestClient) -> str:
        # follows the link that the service printed at its start; returns the secret of the login's forms
        outcome: local_authentication.FirstAdministratorOutcome = self.start()
        assert outcome.link is not None
        response: httpx.Response = client.post(
            f"/auth/invite/{auth_world.token_of(outcome.link)}", json={"password": auth_world.STRONG_PASSWORD}
        )
        assert response.status_code == 200, response.text
        return str(response.json()["csrf_token"])

    # ------------------------------------------------------------------------------------------------------------------
    @contextmanager
    def open_services(self) -> Iterator[auth_services.AuthServices]:
        # the services over a session of their own, committed when what is done in them has gone well, for what has
        # to be done to the service outside of a request, such as setting it up
        with orm.Session(self.engine, expire_on_commit=False) as session:
            yield self.system.bind(session)
            session.commit()

    # ------------------------------------------------------------------------------------------------------------------
    def get_actor(self, services: auth_services.AuthServices, client: TestClient) -> actor.Actor:
        token: str | None = client.cookies.get(self.gate.cookie_name)
        assert token is not None
        authenticated: auth_service.AuthenticatedRequest | None = services.auth.authenticate(token)
        assert authenticated is not None
        return authenticated.actor

    # ------------------------------------------------------------------------------------------------------------------
    def add_provider(self, client: TestClient) -> None:
        with orm.Session(self.engine, expire_on_commit=False) as session:
            services: auth_services.AuthServices = self.system.bind(session)
            services.oidc_settings.add_provider(
                self.get_actor(services, client),
                oidc_world.make_provider_settings(),
                fake_oidc_provider.CLIENT_SECRET,
            )
            session.commit()

    # ------------------------------------------------------------------------------------------------------------------
    def finish_login_at_provider(self, client: TestClient, claims: dict[str, object], next_path: str = "/") -> str:
        # the person is sent to the provider, logs in there, and is sent back; returns where the service sent them on
        started: httpx.Response = client.get(f"/auth/oidc/idp/login?next={next_path}")
        assert started.status_code == 303
        callback_url: str = self.fake_provider.approve(started.headers["location"], claims)
        query: str = urlparse(callback_url).query
        finished: httpx.Response = client.get(f"/auth/oidc/idp/callback?{query}")
        assert finished.status_code == 303, finished.text
        assert parse_qs(query)["state"]
        return str(finished.headers["location"])
