import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import sqlalchemy
from quantgebra.auth import auth_services
from quantgebra.auth import auth_system
from quantgebra.auth.core import auth_config
from quantgebra.auth.core import grant
from quantgebra.auth.core import grant_rule
from quantgebra.auth.core import permission_definition
from quantgebra.auth.core import permission_definition_catalog
from quantgebra.auth.local import local_authentication
from sqlalchemy import orm

from . import config
from . import token_auth_gate

logger: logging.Logger = logging.getLogger(__name__)

PERMISSION_VIEW: str = "view"
PERMISSION_READ: str = "read"
PERMISSION_WRITE: str = "write"
PERMISSION_ADMIN: str = "admin"
SUBJECT_KIND_REPOSITORY: str = "repository"
ADMINISTRATORS_GROUP: str = "administrators"
# what BearPond's api keys start with, so a leaked one is easy to recognize
API_KEY_PREFIX: str = "bp_"
SESSION_COOKIE_NAME: str = "bearpond_session"


# ======================================================================================================================
class ServerAuth:

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def make_catalog() -> permission_definition_catalog.PermissionDefinitionCatalog:
        # what a person can be allowed to do: see the server at all (which anyone who may do something may), read a
        # repository, write it (which includes reverting it), and administer, which is everything everywhere; read and
        # write can be limited to one repository
        definitions: list[permission_definition.PermissionDefinition] = [
            permission_definition.PermissionDefinition(
                name=PERMISSION_VIEW, label="View", description="see that the server is there and who you are"
            ),
            permission_definition.PermissionDefinition(
                name=PERMISSION_READ,
                label="Read",
                description="see a repository and pull from it",
                subject_kinds=[SUBJECT_KIND_REPOSITORY],
            ),
            permission_definition.PermissionDefinition(
                name=PERMISSION_WRITE,
                label="Write",
                description="commit to a repository, and revert it",
                subject_kinds=[SUBJECT_KIND_REPOSITORY],
            ),
            permission_definition.PermissionDefinition(
                name=PERMISSION_ADMIN,
                label="Administer",
                description="everything everywhere, including repositories, users, groups and providers",
            ),
        ]
        return permission_definition_catalog.PermissionDefinitionCatalog(definitions, PERMISSION_ADMIN, PERMISSION_VIEW)

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def make_default_rules() -> list[grant_rule.GrantRule]:
        # viewers read everything, writers read and write everything, and administrators do everything
        rules: list[grant_rule.GrantRule] = [
            grant_rule.GrantRule(group_name="viewers", grants=[grant.Grant(permission=PERMISSION_READ)]),
            grant_rule.GrantRule(
                group_name="writers",
                grants=[grant.Grant(permission=PERMISSION_READ), grant.Grant(permission=PERMISSION_WRITE)],
            ),
            grant_rule.GrantRule(group_name=ADMINISTRATORS_GROUP, grants=[grant.Grant(permission=PERMISSION_ADMIN)]),
        ]
        return rules

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================

    # ------------------------------------------------------------------------------------------------------------------
    def __init__(self, server_config: config.ServerConfig, engine: sqlalchemy.Engine) -> None:
        # who is making a request to the server and what they may do: the library's system of users, logins and rules,
        # in front of which stands a gate that also takes the shared token
        settings: config.AuthSettings = server_config.auth
        auth_settings: auth_config.AuthConfig = auth_config.AuthConfig(
            "enforced" if settings.mode == "enforced" else "none",
            settings.base_url,
            ADMINISTRATORS_GROUP,
            ServerAuth.make_catalog(),
            settings.session_duration_minutes,
            True,
            ServerAuth.make_default_rules(),
            API_KEY_PREFIX,
        )

        # users and logins live in the database of the server, which hands the library a session of it for every
        # request, and the secrets of providers in a folder next to the metadata database
        secrets_dir: Path = settings.secrets_dir or (server_config.db_path.parent / "auth-secrets")
        self.engine: sqlalchemy.Engine = engine
        self.system: auth_system.AuthSystem = auth_system.AuthSystem(auth_settings, secrets_dir)
        self.gate: token_auth_gate.TokenAuthGate = token_auth_gate.TokenAuthGate(
            server_config.static_token, SESSION_COOKIE_NAME
        )

    # ------------------------------------------------------------------------------------------------------------------
    def start(self) -> None:
        # puts the settings that administrators changed in use, and says how the first administrator gets in when the
        # server has nobody who can log in yet
        outcome: local_authentication.FirstAdministratorOutcome = self.system.start(self.engine)
        if outcome.link is not None:
            logger.warning(
                f'the first administrator "{outcome.username}" chooses a password at {outcome.link.url} '
                f"(works once, until {outcome.link.expires_datetime.isoformat()})"
            )
        if outcome.warning is not None:
            logger.warning(outcome.warning)

    # ------------------------------------------------------------------------------------------------------------------
    @contextmanager
    def open_services(self) -> Iterator[auth_services.AuthServices]:
        # the services over a session of their own, committed when what is done in them has gone well; a request has
        # its own, made by a dependency of the api, and this is for what is done outside of one
        with orm.Session(self.engine, expire_on_commit=False) as session:
            try:
                yield self.system.bind(session)
                session.commit()
            except BaseException:
                session.rollback()
                raise
