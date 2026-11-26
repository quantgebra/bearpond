import argparse
from typing import Callable

import sqlalchemy
from sqlalchemy import orm

from . import auth_services
from . import auth_system
from .core import auth_error
from .local import local_authentication


# ======================================================================================================================
class AuthTool:

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================

    # ------------------------------------------------------------------------------------------------------------------
    def __init__(
        self, system: auth_system.AuthSystem, engine: sqlalchemy.Engine, output: Callable[[str], None]
    ) -> None:
        # the program for the person who runs the service, for what cannot be done in the user interface because
        # nobody can log in yet or everybody is locked out: making an invitation, and a link to choose a new password;
        # it works on the same database as the service and, like the service, is run on the machine that it is on; the
        # application makes the system from its own settings and calls this with the arguments of its command
        self._system: auth_system.AuthSystem = system
        self._engine: sqlalchemy.Engine = engine
        self._output: Callable[[str], None] = output

    # ------------------------------------------------------------------------------------------------------------------
    def run(self, args: list[str]) -> int:
        # runs one command and says how it went: 0 when it did what was asked, 1 when it could not
        parser: argparse.ArgumentParser = self._make_parser()
        namespace: argparse.Namespace = parser.parse_args(args)
        exit_code: int = 0
        with orm.Session(self._engine, expire_on_commit=False) as session:
            services: auth_services.AuthServices = self._system.bind(session)
            try:
                if namespace.command == "create-invite":
                    self._create_invite(services, namespace.username, namespace.display_name, namespace.group)
                elif namespace.command == "reset-link":
                    self._reset_link(services, namespace.username)
                else:
                    self._show_first_administrator(services)
                session.commit()
            except auth_error.AuthError as error:
                session.rollback()
                self._output(f"error: {error.message}")
                exit_code = 1
        return exit_code

    # ------------------------------------------------------------------------------------------------------------------
    def _make_parser(self) -> argparse.ArgumentParser:
        parser: argparse.ArgumentParser = argparse.ArgumentParser(prog="auth", description="manage the accounts")
        commands = parser.add_subparsers(dest="command", required=True)

        invite_parser: argparse.ArgumentParser = commands.add_parser("create-invite", help="make an account")
        invite_parser.add_argument("username")
        invite_parser.add_argument("--display-name", default="")
        invite_parser.add_argument("--group", action="append", default=[])

        reset_parser: argparse.ArgumentParser = commands.add_parser(
            "reset-link", help="make a link to choose a new password"
        )
        reset_parser.add_argument("username")

        commands.add_parser("first-administrator", help="make the first administrator, or a new link for it")
        return parser

    # ------------------------------------------------------------------------------------------------------------------
    def _create_invite(
        self, services: auth_services.AuthServices, username: str, display_name: str, group_names: list[str]
    ) -> None:
        request: local_authentication.InviteRequest = local_authentication.InviteRequest(
            username=username, display_name=display_name, group_names=group_names
        )
        link: local_authentication.InviteLink = services.local.invite(None, request)
        self._say_link(f'The account "{username}" is made.', link)

    # ------------------------------------------------------------------------------------------------------------------
    def _reset_link(self, services: auth_services.AuthServices, username: str) -> None:
        user_id: str | None = services.local.find_user_id(username)
        if user_id is None:
            raise auth_error.AuthError("invalid_request", f'There is no account named "{username}".')

        link: local_authentication.InviteLink = services.local.reset_link(None, user_id)
        self._say_link(f'A new password for "{username}" is chosen at:', link)

    # ------------------------------------------------------------------------------------------------------------------
    def _show_first_administrator(self, services: auth_services.AuthServices) -> None:
        outcome: local_authentication.FirstAdministratorOutcome = services.local.ensure_first_administrator()
        if outcome.link is None:
            self._output("There is nothing to do: the service has its accounts.")
        else:
            self._say_link(f'The first administrator "{outcome.username}" chooses a password at:', outcome.link)
        if outcome.warning is not None:
            self._output(f"warning: {outcome.warning}")

    # ------------------------------------------------------------------------------------------------------------------
    def _say_link(self, headline: str, link: local_authentication.InviteLink) -> None:
        self._output(headline)
        self._output(f"  {link.url}")
        self._output(f"The link works once and stops working at {link.expires_datetime.isoformat()}.")
