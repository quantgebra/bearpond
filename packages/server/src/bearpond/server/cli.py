import argparse
import dataclasses
import logging
import os
import sys
from pathlib import Path

import uvicorn
from quantgebra.auth import auth_tool

from . import config
from . import server as server_module
from .api import app as app_module

# the database the server keeps everything in, when it is not the one in server.yaml: a PostgreSQL address has a
# password in it, which belongs in the environment of the process rather than in a file
DATABASE_URL_ENV_VAR: str = "BEARPOND_DATABASE_URL"
CONFIG_DIR_HELP: str = f"the folder that has server.yaml in it (default: ${config.CONFIG_DIR_ENV_VAR})"


# ----------------------------------------------------------------------------------------------------------------------
def resolve_server_config(cli_config_dir: Path | None) -> config.ServerConfig:
    # the settings in server.yaml of the folder given, or of $BEARPOND_CONFIG_DIR; the shared token and the database
    # address come from the environment
    config_dir: Path | None = config.config_dir_from_env()
    if cli_config_dir is not None:
        config_dir = cli_config_dir.expanduser().resolve()
    if config_dir is None:
        raise SystemExit(f"error: give --config-dir, or set {config.CONFIG_DIR_ENV_VAR}")

    server_config: config.ServerConfig = config.load_server_config(config_dir, config.static_token_from_env())
    database_url: str | None = os.environ.get(DATABASE_URL_ENV_VAR)
    if database_url:
        server_config = dataclasses.replace(server_config, database_url=database_url)
    return server_config


# ----------------------------------------------------------------------------------------------------------------------
def cmd_serve(args: argparse.Namespace) -> int:
    # the server runs until it is stopped; what it logs, the link for the first administrator among it, goes to the
    # standard error with the time in front, for the log file that whoever started it keeps
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    server_module.configure(resolve_server_config(args.config_dir))
    uvicorn.run(app_module.app, host=args.host, port=args.port, log_config=None)
    return 0


# ----------------------------------------------------------------------------------------------------------------------
def cmd_auth(args: argparse.Namespace) -> int:
    # what the person who runs the server does when nobody can log in: the library's program, on the server's database
    srv: server_module.Server = server_module.configure(resolve_server_config(args.config_dir))
    tool: auth_tool.AuthTool = auth_tool.AuthTool(srv.auth.system, srv.engine, print)
    exit_code: int = tool.run(args.auth_args)
    server_module.close()
    return exit_code


# ----------------------------------------------------------------------------------------------------------------------
def main() -> None:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(description="Bearpond server")
    subparsers: argparse._SubParsersAction = parser.add_subparsers(dest="command", required=True)

    serve_parser: argparse.ArgumentParser = subparsers.add_parser("serve", help="Run the server")
    serve_parser.add_argument("--config-dir", type=Path, default=None, help=CONFIG_DIR_HELP)
    serve_parser.add_argument("--host", default="127.0.0.1", help="the address to listen on (default: 127.0.0.1)")
    serve_parser.add_argument("--port", type=int, default=8000, help="the port to listen on (default: 8000)")
    serve_parser.set_defaults(func=cmd_serve)

    auth_parser: argparse.ArgumentParser = subparsers.add_parser(
        "auth",
        help="Make an account, a link to choose a new password, or the first administrator's link: "
        "auth {create-invite,reset-link,first-administrator} ...",
    )
    auth_parser.add_argument("--config-dir", type=Path, default=None, help=CONFIG_DIR_HELP)
    auth_parser.add_argument("auth_args", nargs=argparse.REMAINDER, help="the command and its arguments")
    auth_parser.set_defaults(func=cmd_auth)

    args: argparse.Namespace = parser.parse_args()
    sys.exit(args.func(args))


# ----------------------------------------------------------------------------------------------------------------------
if __name__ == "__main__":
    main()
