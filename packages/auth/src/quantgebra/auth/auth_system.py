import logging
from datetime import datetime, timezone
from pathlib import Path

import httpx
import sqlalchemy
from sqlalchemy import orm
from sqlalchemy import exc as sqlalchemy_exc

from . import auth_services
from .core import auth_config
from .core import group_record
from .local import local_authentication
from .local import login_throttle
from .oidc import oidc_providers
from .oidc import oidc_secret_store
from .oidc import pending_logins

logger: logging.Logger = logging.getLogger(__name__)


# ======================================================================================================================
class AuthSystem:

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================

    # ------------------------------------------------------------------------------------------------------------------
    def __init__(
        self,
        config: auth_config.AuthConfig,
        secrets_directory: Path,
        transport: httpx.BaseTransport | None = None,
        throttle: login_throttle.LoginThrottle | None = None,
    ) -> None:
        # what authentication keeps between requests: the settings in use, the secrets of providers, the slowing down
        # of password guesses, the logins waiting at a provider, and what has been found out about the providers; the
        # users and their logins are in the database that the application owns, and each request gets services over
        # its own session of it
        self.current_config: auth_config.CurrentAuthConfig = auth_config.CurrentAuthConfig(config)
        self.secrets: oidc_secret_store.OidcSecretStore = oidc_secret_store.OidcSecretStore(secrets_directory)
        self.throttle: login_throttle.LoginThrottle = throttle or login_throttle.LoginThrottle()
        self.pending_logins: pending_logins.PendingLogins = pending_logins.PendingLogins()
        self.providers: oidc_providers.OidcProviders = oidc_providers.OidcProviders(self.secrets, transport)

    # ------------------------------------------------------------------------------------------------------------------
    def bind(self, session: orm.Session) -> auth_services.AuthServices:
        # the services for a request, over the session of the request; whoever made the session commits it
        return auth_services.AuthServices(
            session, self.current_config, self.secrets, self.throttle, self.pending_logins, self.providers
        )

    # ------------------------------------------------------------------------------------------------------------------
    def start(self, engine: sqlalchemy.Engine) -> local_authentication.FirstAdministratorOutcome:
        # puts the settings that administrators changed in use, makes the groups that the rules name, and makes sure
        # that a service with nobody who can log in has a first administrator; the outcome says what the person who
        # runs the service has to be told, such as the link that lets the first administrator choose a password; the
        # tables have to be there, which is for the application to see to, with the migrations of this package
        outcome: local_authentication.FirstAdministratorOutcome = local_authentication.FirstAdministratorOutcome(
            kind="not_needed"
        )
        with orm.Session(engine, expire_on_commit=False) as session:
            services: auth_services.AuthServices = self.bind(session)
            services.settings.load_stored_settings()

            # the groups that the rules name exist, so that people can be put in them
            group_names: list[str] = [self.current_config.config.administrators_group_name]
            for rule in self.current_config.config.rules:
                group_names.append(rule.group_name)
            group_record.GroupRecord.ensure_exist(session, group_names, datetime.now(timezone.utc))

            # say so when a rule gives a permission that the application does not have any more, since it gives nobody
            # anything and that is easy to miss
            for dangling_grant in services.settings.find_dangling_grants():
                logger.warning(
                    f"the rule for the group {dangling_grant.group_name} gives {dangling_grant.grant.permission}, "
                    f"which does nothing: {dangling_grant.problem}"
                )

            # two servers that start together may both see that there is nobody, and only one can make the account
            try:
                outcome = services.local.ensure_first_administrator()
                session.commit()
            except sqlalchemy_exc.IntegrityError:
                session.rollback()
                logger.warning("another server made the first administrator at the same time")
        return outcome
