from collections.abc import Callable
from pathlib import Path

import httpx
import sqlalchemy
from quantgebra.auth import auth_services
from quantgebra.auth import auth_system
from quantgebra.auth.core import actor
from quantgebra.auth.core import auth_config
from quantgebra.auth.core import auth_service
from quantgebra.auth.core import auth_settings_service
from quantgebra.auth.core import grant
from quantgebra.auth.core import grant_rule
from quantgebra.auth.core import group_service
from quantgebra.auth.core import permission_definition
from quantgebra.auth.core import permission_definition_catalog
from quantgebra.auth.local import local_authentication
from quantgebra.auth.local import login_throttle
from sqlalchemy import orm

WorldFactory = Callable[[login_throttle.LoginThrottle | None], "AuthWorld"]

BASE_URL: str = "http://127.0.0.1:8420"
STRONG_PASSWORD: str = "correct horse battery staple"


# ----------------------------------------------------------------------------------------------------------------------
def make_catalog() -> permission_definition_catalog.PermissionDefinitionCatalog:
    # the permissions of a small service: read and write can be limited to one repository, admin does everything
    definitions: list[permission_definition.PermissionDefinition] = [
        permission_definition.PermissionDefinition(
            name="read", label="Read", description="read repositories", subject_kinds=["repository"]
        ),
        permission_definition.PermissionDefinition(
            name="write", label="Write", description="write repositories", subject_kinds=["repository"]
        ),
        permission_definition.PermissionDefinition(name="admin", label="Administer", description="everything"),
    ]
    return permission_definition_catalog.PermissionDefinitionCatalog(definitions, "admin", "read")


# ----------------------------------------------------------------------------------------------------------------------
def make_rules() -> list[grant_rule.GrantRule]:
    rules: list[grant_rule.GrantRule] = [
        grant_rule.GrantRule(group_name="readers", grants=[grant.Grant(permission="read")]),
        grant_rule.GrantRule(group_name="writers", grants=[grant.Grant(permission="write")]),
        grant_rule.GrantRule(group_name="administrators", grants=[grant.Grant(permission="admin")]),
    ]
    return rules


# ----------------------------------------------------------------------------------------------------------------------
def make_config() -> auth_config.AuthConfig:
    return auth_config.AuthConfig("enforced", BASE_URL, "administrators", make_catalog(), 480, True, make_rules())


# ======================================================================================================================
class AuthWorld:

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================

    # ------------------------------------------------------------------------------------------------------------------
    def __init__(
        self,
        engine: sqlalchemy.Engine,
        secrets_directory: Path,
        throttle: login_throttle.LoginThrottle | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        # everything a service of the library is made of, over the database of the engine; the services work in one
        # session, which is flushed and never committed, so what a test does is rolled back with it
        self.engine: sqlalchemy.Engine = engine
        self.secrets_directory: Path = secrets_directory
        self.config: auth_config.AuthConfig = make_config()
        self.system: auth_system.AuthSystem = auth_system.AuthSystem(
            self.config, secrets_directory, transport, throttle
        )
        self.session: orm.Session = orm.Session(engine, expire_on_commit=False)
        self.services: auth_services.AuthServices = self.system.bind(self.session)
        self.auth: auth_service.AuthService = self.services.auth
        self.local: local_authentication.LocalAuthentication = self.services.local
        self.groups: group_service.GroupService = self.services.groups
        self.settings: auth_settings_service.AuthSettingsService = self.services.settings
        self._other_sessions: list[orm.Session] = []

    # ------------------------------------------------------------------------------------------------------------------
    def make_other_request(self) -> auth_services.AuthServices:
        # the services of another request, which has a session of its own and sees only what has been committed
        other_session: orm.Session = orm.Session(self.engine, expire_on_commit=False)
        self._other_sessions.append(other_session)
        return self.system.bind(other_session)

    # ------------------------------------------------------------------------------------------------------------------
    def close(self) -> None:
        # gives up what the sessions of the world hold, which includes the locks that a database keeps for them
        for other_session in self._other_sessions:
            other_session.rollback()
            other_session.close()
        self.session.rollback()
        self.session.close()

    # ------------------------------------------------------------------------------------------------------------------
    def make_administrator(self) -> actor.Actor:
        # the first administrator, logged in, the way a person who followed the link would be
        outcome: local_authentication.FirstAdministratorOutcome = self.local.ensure_first_administrator()
        assert outcome.link is not None
        login: auth_service.Login = self.local.accept_invite(token_of(outcome.link), STRONG_PASSWORD)
        authenticated: auth_service.AuthenticatedRequest | None = self.auth.authenticate(login.token)
        assert authenticated is not None
        return authenticated.actor

    # ------------------------------------------------------------------------------------------------------------------
    def make_user(self, administrator: actor.Actor, username: str, group_names: list[str]) -> auth_service.Login:
        # an account that an administrator invited, whose owner followed the link and chose the usual password
        request: local_authentication.InviteRequest = local_authentication.InviteRequest(
            username=username, display_name=username.title(), group_names=group_names
        )
        link: local_authentication.InviteLink = self.local.invite(administrator, request)
        return self.local.accept_invite(token_of(link), STRONG_PASSWORD)


# ----------------------------------------------------------------------------------------------------------------------
def token_of(link: local_authentication.InviteLink) -> str:
    # the secret at the end of an invitation's address
    return link.url.rsplit("/", 1)[1]
