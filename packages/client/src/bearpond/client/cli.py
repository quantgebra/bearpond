import argparse
import os
import sys
from pathlib import Path
from typing import NamedTuple

from bearpond.protocol import types

from . import bearpond_client
from . import server_client

TOKEN_ENV_VAR = "BEARPOND_TOKEN"


# ======================================================================================================================
class RepoUrl(NamedTuple):
    server: str
    repo: str


# ----------------------------------------------------------------------------------------------------------------------
def split_repo_url(url: str) -> RepoUrl:
    # "<server>/<repo>" — a trailing /repos/ prefix on the server part is accepted and normalized away
    server, sep, repo = url.rstrip("/").rpartition("/")
    if not sep or not repo or not server:
        raise server_client.BearpondError(f"URL must look like <server>/<repo>: {url}")
    if server.endswith("/repos"):
        server = server[: -len("/repos")]
    return RepoUrl(server=server, repo=repo)


# ----------------------------------------------------------------------------------------------------------------------
def cmd_repo_create(args: argparse.Namespace) -> None:
    repo_url: RepoUrl = split_repo_url(args.url)
    with server_client.ServerClient(repo_url.server, repo_url.repo, token=args.token) as client:
        client.create_repository(repo_url.repo)
    print(f"Created repository: {args.url}")


# ----------------------------------------------------------------------------------------------------------------------
def cmd_clone(args: argparse.Namespace) -> None:
    repo_url: RepoUrl = split_repo_url(args.url)
    target: Path = args.dir if args.dir is not None else Path(repo_url.repo)
    query: bearpond_client.SubsetQuery | None = None
    if args.query is not None:
        query = bearpond_client.SubsetQuery(groups=args.query)
    bearpond_client.BearpondClient.clone(repo_url.server, repo_url.repo, target, token=args.token, query=query)
    print(f"Cloned {args.url} into {target}")


# ----------------------------------------------------------------------------------------------------------------------
def cmd_add(args: argparse.Namespace) -> None:
    # staging is purely local — no server contact until commit
    client: bearpond_client.BearpondClient = bearpond_client.BearpondClient.discover(Path.cwd())
    staged: list[types.FileMetadata] = client.add(args.paths)
    for entry in staged:
        print(f"  staged: {entry.path}")
    print(f"Staged {len(staged)} file(s)")


# ----------------------------------------------------------------------------------------------------------------------
def cmd_rm(args: argparse.Namespace) -> None:
    # staging removals is purely local — no server contact until commit
    client: bearpond_client.BearpondClient = bearpond_client.BearpondClient.discover(Path.cwd())
    manifest: types.TransactionManifest = client.rm(args.paths)
    print(f"Staged: +{len(manifest.added)} addition(s), -{len(manifest.removed)} removal(s)")


# ----------------------------------------------------------------------------------------------------------------------
def cmd_status(args: argparse.Namespace) -> None:
    client: bearpond_client.BearpondClient = bearpond_client.BearpondClient.discover(Path.cwd())
    status: bearpond_client.WorkspaceStatus = client.status()

    query_label: str = status.query.label()
    print(f"Workspace at manifest-{status.workspace_seq:08d} — {query_label}")
    for path in status.pending:
        print(f"  pending pull:  {path}")
    for path in status.staged_added:
        print(f"  staged add:    {path.path}")
    for path in status.staged_removed:
        print(f"  staged remove: {path.path}")
    for path in status.staged_updated:
        print(f"  staged update: {path.path}")
    for path in status.modified:
        print(f"  modified:      {path}")
    for path in status.untracked:
        print(f"  untracked:     {path}")
    for path in status.missing:
        print(f"  missing:       {path}")


# ----------------------------------------------------------------------------------------------------------------------
def cmd_commit(args: argparse.Namespace) -> None:
    # the one short-lived server interaction: begin, upload everything staged, commit — all in one go
    client: bearpond_client.BearpondClient = bearpond_client.BearpondClient.discover(Path.cwd())
    with client.connect(token=args.token) as server:
        result: types.CommitResponse = client.commit(server, message=args.message)
    print(
        f"Committed: manifest-{result.seq:08d}, "
        f"+{result.files_added_count} -{result.files_removed_count} ~{result.files_updated_count} file(s)"
    )


# ----------------------------------------------------------------------------------------------------------------------
def cmd_pull(args: argparse.Namespace) -> None:
    client: bearpond_client.BearpondClient = bearpond_client.BearpondClient.discover(Path.cwd())
    query: bearpond_client.SubsetQuery | None = None
    if args.query is not None:
        query = bearpond_client.SubsetQuery(groups=args.query)
    with client.connect(token=args.token) as server:
        client.pull(server, dry_run=args.dry_run, seq=args.seq, query=query)


# ----------------------------------------------------------------------------------------------------------------------
def cmd_log(args: argparse.Namespace) -> None:
    client: bearpond_client.BearpondClient = bearpond_client.BearpondClient.discover(Path.cwd())
    with client.connect(token=args.token) as server:
        page: types.CommitHistoryPage = server.get_commit_history(limit=args.limit)
    for record in page.records:
        author: str = record.user or "-"
        reason: str = record.reason or "(no message)"
        print(f"commit {record.commit_hash[:12]}  manifest-{record.seq:08d}")
        print(f"Author: {author}    Date: {record.committed_at}")
        print(f"    {reason}")
        print(f"    +{len(record.added)} -{len(record.removed)} ~{len(record.updated)} file(s)")
        print()


# ----------------------------------------------------------------------------------------------------------------------
def cmd_revert(args: argparse.Namespace) -> None:
    client: bearpond_client.BearpondClient = bearpond_client.BearpondClient.discover(Path.cwd())
    with client.connect(token=args.token) as server:
        result: types.CommitResponse = server.revert(args.seq, reason=args.message)
    print(f"Reverted to manifest-{args.seq:08d}: new manifest-{result.seq:08d}")


# ----------------------------------------------------------------------------------------------------------------------
def main() -> None:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(description="Bearpond client CLI")
    subparsers: argparse._SubParsersAction = parser.add_subparsers(dest="command", required=True)

    repo_create_parser: argparse.ArgumentParser = subparsers.add_parser(
        "repo-create", help="Create a new repository on the server: <server>/<repo>"
    )
    repo_create_parser.add_argument("url", help="<server-url>/<repo-name>, e.g. http://localhost:8000/trades")
    repo_create_parser.add_argument("--token", default=os.environ.get(TOKEN_ENV_VAR), help=f"Bearer auth token (default: ${TOKEN_ENV_VAR})")
    repo_create_parser.set_defaults(func=cmd_repo_create)

    clone_parser: argparse.ArgumentParser = subparsers.add_parser(
        "clone", help="Clone a repository into a new workspace: <server>/<repo> [dir]"
    )
    clone_parser.add_argument("url", help="<server-url>/<repo-name>, e.g. http://localhost:8000/trades")
    clone_parser.add_argument("dir", type=Path, nargs="?", default=None, help="Target directory (default: the repo name)")
    clone_parser.add_argument("--token", default=os.environ.get(TOKEN_ENV_VAR), help=f"Bearer auth token (default: ${TOKEN_ENV_VAR})")
    clone_parser.add_argument(
        "--query",
        nargs="*",
        action="append",
        default=None,
        help="Clone only files whose path contains these segments (AND within one --query, OR across repeats: "
             "--query year=2024 month=01 --query year=2025 month=02); omit --query for a full mirror (the default)",
    )
    clone_parser.set_defaults(func=cmd_clone)

    add_parser: argparse.ArgumentParser = subparsers.add_parser(
        "add", help="Stage files (or directories of files) for the next commit — purely local"
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
    commit_parser.add_argument("--token", default=os.environ.get(TOKEN_ENV_VAR), help=f"Bearer auth token (default: ${TOKEN_ENV_VAR})")
    commit_parser.add_argument("-m", "--message", default=None, help="Commit message recorded on the commit record")
    commit_parser.set_defaults(func=cmd_commit)

    pull_parser: argparse.ArgumentParser = subparsers.add_parser(
        "pull", help="Pull the latest manifest from the server, updating the local mirror"
    )
    pull_parser.add_argument("--token", default=os.environ.get(TOKEN_ENV_VAR), help=f"Bearer auth token (default: ${TOKEN_ENV_VAR})")
    pull_parser.add_argument("--dry-run", action="store_true")
    pull_parser.add_argument("--seq", type=int, default=None, help="Pull a specific manifest version instead of the latest")
    pull_parser.add_argument(
        "--query",
        nargs="*",
        action="append",
        default=None,
        help="Pull only files whose path contains these segments (AND within one --query, OR across repeats: "
             "--query year=2024 month=01 --query year=2025 month=02); "
             "pass --query with no values to switch back to a full mirror",
    )
    pull_parser.set_defaults(func=cmd_pull)

    log_parser: argparse.ArgumentParser = subparsers.add_parser(
        "log", help="Show the repository's commit history, newest first"
    )
    log_parser.add_argument("--token", default=os.environ.get(TOKEN_ENV_VAR), help=f"Bearer auth token (default: ${TOKEN_ENV_VAR})")
    log_parser.add_argument("--limit", type=int, default=20, help="Maximum number of commits to show")
    log_parser.set_defaults(func=cmd_log)

    revert_parser: argparse.ArgumentParser = subparsers.add_parser(
        "revert", help="Revert the lake to a prior manifest version, recorded as a new commit"
    )
    revert_parser.add_argument("--token", default=os.environ.get(TOKEN_ENV_VAR), help=f"Bearer auth token (default: ${TOKEN_ENV_VAR})")
    revert_parser.add_argument("--seq", type=int, required=True, help="The manifest version to revert to")
    revert_parser.add_argument("-m", "--message", default=None, help="Commit message recorded on the resulting commit")
    revert_parser.set_defaults(func=cmd_revert)

    args: argparse.Namespace = parser.parse_args()
    try:
        args.func(args)
    except server_client.BearpondError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


# ----------------------------------------------------------------------------------------------------------------------
if __name__ == "__main__":
    main()
