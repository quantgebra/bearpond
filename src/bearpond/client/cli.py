import argparse
import os
import sys
from pathlib import Path

from .. import types
from . import bearpond_client
from . import server_client

SERVER_ENV_VAR = "BEARPOND_SERVER"
TOKEN_ENV_VAR = "BEARPOND_TOKEN"


# ----------------------------------------------------------------------------------------------------------------------
def cmd_add(args: argparse.Namespace) -> None:
    # staging is purely local — no server contact until commit
    client: bearpond_client.BearpondClient = bearpond_client.BearpondClient(Path.cwd())
    staged: list[types.FileMetadata] = client.add(args.paths)
    for entry in staged:
        print(f"  staged: {entry.path}")
    print(f"Staged {len(staged)} file(s)")


# ----------------------------------------------------------------------------------------------------------------------
def cmd_rm(args: argparse.Namespace) -> None:
    # staging removals is purely local — no server contact until commit
    client: bearpond_client.BearpondClient = bearpond_client.BearpondClient(Path.cwd())
    manifest: types.TransactionManifest = client.rm(args.paths)
    print(f"Staged: +{len(manifest.added)} addition(s), -{len(manifest.removed)} removal(s)")


# ----------------------------------------------------------------------------------------------------------------------
def cmd_status(args: argparse.Namespace) -> None:
    client: bearpond_client.BearpondClient = bearpond_client.BearpondClient(Path.cwd())
    status: bearpond_client.WorkspaceStatus = client.status()

    print(f"Workspace at manifest-{status.workspace_seq:08d}")
    for path in status.pending:
        print(f"  pending sync:  {path}")
    for path in status.staged_added:
        print(f"  staged add:    {path.path}")
    for path in status.staged_removed:
        print(f"  staged remove: {path.path}")
    for path in status.modified:
        print(f"  modified:      {path}")
    for path in status.untracked:
        print(f"  untracked:     {path}")
    for path in status.missing:
        print(f"  missing:       {path}")


# ----------------------------------------------------------------------------------------------------------------------
def cmd_commit(args: argparse.Namespace) -> None:
    # the one short-lived server interaction: begin, upload everything staged, commit — all in one go
    client: bearpond_client.BearpondClient = bearpond_client.BearpondClient(Path.cwd())
    with server_client.ServerClient(args.server, token=args.token) as server:
        result: types.CommitResponse = client.commit(server, message=args.message)
    print(f"Committed: manifest-{result.seq:08d}, +{result.files_added_count} -{result.files_removed_count} file(s)")


# ----------------------------------------------------------------------------------------------------------------------
def cmd_sync(args: argparse.Namespace) -> None:
    client: bearpond_client.BearpondClient = bearpond_client.BearpondClient(args.target)
    with server_client.ServerClient(args.server, token=args.token) as server:
        client.sync(server, dry_run=args.dry_run)


# ----------------------------------------------------------------------------------------------------------------------
def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--server", default=os.environ.get(SERVER_ENV_VAR),
        required=SERVER_ENV_VAR not in os.environ,
        help=f"Bearpond server base URL (default: ${SERVER_ENV_VAR})",
    )
    parser.add_argument(
        "--token", default=os.environ.get(TOKEN_ENV_VAR),
        help=f"Bearer auth token (default: ${TOKEN_ENV_VAR})",
    )


# ----------------------------------------------------------------------------------------------------------------------
def main() -> None:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(description="Bearpond client CLI")
    subparsers: argparse._SubParsersAction = parser.add_subparsers(dest="command", required=True)

    add_parser: argparse.ArgumentParser = subparsers.add_parser(
        "add", help="Stage files (or directories of .parquet files) for the next commit — purely local"
    )
    add_parser.add_argument("paths", type=Path, nargs="+", help="Files or directories to stage")
    add_parser.set_defaults(func=cmd_add)

    rm_parser: argparse.ArgumentParser = subparsers.add_parser(
        "rm", help="Stage tracked files for removal from the lake — purely local"
    )
    rm_parser.add_argument("paths", type=Path, nargs="+", help="Files to stage for removal")
    rm_parser.set_defaults(func=cmd_rm)

    status_parser: argparse.ArgumentParser = subparsers.add_parser(
        "status", help="Show how the working directory differs from the lake — purely local"
    )
    status_parser.set_defaults(func=cmd_status)

    commit_parser: argparse.ArgumentParser = subparsers.add_parser(
        "commit", help="Upload everything staged and commit it to the lake as one transaction"
    )
    add_common_args(commit_parser)
    commit_parser.add_argument("-m", "--message", default=None, help="Commit message recorded on the commit record")
    commit_parser.set_defaults(func=cmd_commit)

    sync_parser: argparse.ArgumentParser = subparsers.add_parser(
        "sync", help="Sync a local mirror against the server's latest manifest"
    )
    add_common_args(sync_parser)
    sync_parser.add_argument("--target", type=Path, required=True, help="Local directory to sync into")
    sync_parser.add_argument("--dry-run", action="store_true")
    sync_parser.set_defaults(func=cmd_sync)

    args: argparse.Namespace = parser.parse_args()
    try:
        args.func(args)
    except server_client.BearpondError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


# ----------------------------------------------------------------------------------------------------------------------
if __name__ == "__main__":
    main()
