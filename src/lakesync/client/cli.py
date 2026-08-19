import argparse
import os
import sys
from pathlib import Path

from .. import types
from . import lakesync_client

SERVER_ENV_VAR = "LAKESYNC_SERVER"
TOKEN_ENV_VAR = "LAKESYNC_TOKEN"


# ----------------------------------------------------------------------------------------------------------------------
def cmd_sync(args: argparse.Namespace) -> None:
    with lakesync_client.LakesyncClient(args.server, token=args.token) as active_client:
        active_client.sync(args.target, dry_run=args.dry_run)


# ----------------------------------------------------------------------------------------------------------------------
def cmd_upload(args: argparse.Namespace) -> None:
    local_root: Path = args.dir
    files: dict[str, Path] = {
        path.relative_to(local_root).as_posix(): path
        for path in sorted(local_root.rglob("*.parquet"))
    }
    if not files:
        print(f"No .parquet files found under {local_root}", file=sys.stderr)
        sys.exit(1)

    print(f"Uploading {len(files)} file(s) from {local_root} as one transaction...")
    with lakesync_client.LakesyncClient(args.server, token=args.token) as active_client:
        declared: list[types.FileMeta] = active_client.declare_files(files)
        txn_id: str = active_client.begin_transaction(declared)
        result: types.CommitResponse
        try:
            active_client.upload_files(txn_id, files)
            result = active_client.commit(txn_id)
        except Exception:
            # best-effort cleanup — an upload failure or interrupted commit shouldn't leave an orphaned transaction
            active_client.abort(txn_id)
            raise
    print(f"Committed: manifest-{result.seq:08d}, +{result.files_added_count} -{result.files_removed_count} file(s)")


# ----------------------------------------------------------------------------------------------------------------------
def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--server", default=os.environ.get(SERVER_ENV_VAR),
        required=SERVER_ENV_VAR not in os.environ,
        help=f"Lakesync server base URL (default: ${SERVER_ENV_VAR})",
    )
    parser.add_argument(
        "--token", default=os.environ.get(TOKEN_ENV_VAR),
        help=f"Bearer auth token (default: ${TOKEN_ENV_VAR})",
    )


# ----------------------------------------------------------------------------------------------------------------------
def main() -> None:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(description="Lakesync client CLI")
    subparsers: argparse._SubParsersAction = parser.add_subparsers(dest="command", required=True)

    sync_parser: argparse.ArgumentParser = subparsers.add_parser(
        "sync", help="Sync a local mirror against the server's latest manifest"
    )
    add_common_args(sync_parser)
    sync_parser.add_argument("--target", type=Path, required=True, help="Local directory to sync into")
    sync_parser.add_argument("--dry-run", action="store_true")
    sync_parser.set_defaults(func=cmd_sync)

    upload_parser: argparse.ArgumentParser = subparsers.add_parser(
        "upload", help="Upload all .parquet files under a directory as one transaction"
    )
    add_common_args(upload_parser)
    upload_parser.add_argument(
        "--dir", type=Path, required=True,
        help="Local directory whose contents mirror the target hive layout",
    )
    upload_parser.set_defaults(func=cmd_upload)

    args: argparse.Namespace = parser.parse_args()
    try:
        args.func(args)
    except lakesync_client.LakesyncError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


# ----------------------------------------------------------------------------------------------------------------------
if __name__ == "__main__":
    main()
