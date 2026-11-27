import argparse
import getpass
import os
import sys
from pathlib import Path
from typing import NamedTuple

import httpx

from bearpond.protocol import types

from . import bearpond_client
from . import credential_store
from . import server_client

TOKEN_ENV_VAR = "BEARPOND_TOKEN"
# where `bearpond login` keeps a key for each server
CREDENTIALS_PATH: Path = Path.home() / ".config" / "bearpond" / "credentials"
TOKEN_HELP: str = (
    f"API key (bp_...) or shared token (default: ${TOKEN_ENV_VAR}, then the key saved by `bearpond login`)"
)


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
def resolve_token(cli_token: str | None, server_url: str) -> str | None:
    # the key to send to a server: --token first, then the environment, then what `bearpond login` saved for it
    token: str | None = cli_token or os.environ.get(TOKEN_ENV_VAR) or None
    if token is None:
        store: credential_store.CredentialStore = credential_store.CredentialStore(CREDENTIALS_PATH)
        token = store.get_token(server_url)
    return token


# ----------------------------------------------------------------------------------------------------------------------
def cmd_login(args: argparse.Namespace) -> None:
    # checks the key against the server, so a mistyped one is never saved, and then keeps it for that server
    server_url: str = credential_store.normalize_server_url(args.server)
    token: str = args.token or read_token()
    if not token:
        raise server_client.BearpondError("no key given: make one on the server's API keys page (/account/keys)")

    # a key that the server does not accept is not saved
    repo_names: list[str]
    with server_client.ServerClient(server_url, "", token=token) as client:
        try:
            repo_names = client.list_repositories()
        except server_client.BearpondError as error:
            raise server_client.BearpondError(f"{server_url} did not accept the key ({error})") from error

    # the key is kept for the server, and the person is told what it gets them
    store: credential_store.CredentialStore = credential_store.CredentialStore(CREDENTIALS_PATH)
    store.save_token(server_url, token)
    plural_ending: str = "y" if len(repo_names) == 1 else "ies"
    print(f"Logged in to {server_url}; the key is saved in {CREDENTIALS_PATH}")
    print(f"You can read {len(repo_names)} repositor{plural_ending}: {', '.join(repo_names)}")
    if os.environ.get(TOKEN_ENV_VAR):
        print(f"Note: ${TOKEN_ENV_VAR} is set, and is sent instead of the saved key until you unset it")


# ----------------------------------------------------------------------------------------------------------------------
def read_token() -> str:
    # the key, typed without being shown, or piped in, as in `echo $KEY | bearpond login URL`
    token: str
    if sys.stdin.isatty():
        token = getpass.getpass("API key: ").strip()
    else:
        token = sys.stdin.readline().strip()
    return token


# ----------------------------------------------------------------------------------------------------------------------
def cmd_logout(args: argparse.Namespace) -> None:
    # forgets the saved key; the key itself still works until it is revoked on the server
    server_url: str = credential_store.normalize_server_url(args.server)
    store: credential_store.CredentialStore = credential_store.CredentialStore(CREDENTIALS_PATH)
    if store.remove_token(server_url):
        print(f"Forgot the key for {server_url}. To stop it working, revoke it on {server_url}/account/keys")
    else:
        print(f"There was no saved key for {server_url}")


# ----------------------------------------------------------------------------------------------------------------------
def cmd_repo_create(args: argparse.Namespace) -> None:
    repo_url: RepoUrl = split_repo_url(args.url)
    token: str | None = resolve_token(args.token, repo_url.server)
    with server_client.ServerClient(repo_url.server, repo_url.repo, token=token) as client:
        client.create_repository(repo_url.repo)
    print(f"Created repository: {args.url}")


# ----------------------------------------------------------------------------------------------------------------------
def cmd_clone(args: argparse.Namespace) -> None:
    repo_url: RepoUrl = split_repo_url(args.url)
    target: Path = args.dir if args.dir is not None else Path(repo_url.repo)
    query: bearpond_client.SubsetQuery | None = None
    if args.query is not None:
        query = bearpond_client.SubsetQuery(groups=args.query)
    token: str | None = resolve_token(args.token, repo_url.server)
    bearpond_client.BearpondClient.clone(repo_url.server, repo_url.repo, target, token=token, query=query)
    print(f"Cloned {args.url} into {target}")


# ----------------------------------------------------------------------------------------------------------------------
def cmd_add(args: argparse.Namespace) -> None:
    # staging is purely local — no server contact until commit
    client: bearpond_client.BearpondClient = bearpond_client.BearpondClient.discover(Path.cwd())
    # only what changed is staged and listed: a file the same as the workspace has it is left as it is
    staged: list[types.FileMetadata] = client.add(args.paths)
    for entry in staged:
        print(f"  staged: {entry.path}")
    if staged:
        print(f"Staged {len(staged)} file(s)")
    else:
        print("Nothing to stage: the files are as the workspace has them")


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
    for added_file in status.staged_added:
        print(f"  staged add:    {added_file.path}")
    for removed_file in status.staged_removed:
        print(f"  staged remove: {removed_file.path}")
    for updated_file in status.staged_updated:
        print(f"  staged update: {updated_file.path}")
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
    with client.connect(token=resolve_token(args.token, client.get_server_url())) as server:
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
    with client.connect(token=resolve_token(args.token, client.get_server_url())) as server:
        client.pull(server, dry_run=args.dry_run, seq=args.seq, query=query)


# ----------------------------------------------------------------------------------------------------------------------
def cmd_log(args: argparse.Namespace) -> None:
    client: bearpond_client.BearpondClient = bearpond_client.BearpondClient.discover(Path.cwd())
    with client.connect(token=resolve_token(args.token, client.get_server_url())) as server:
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
    with client.connect(token=resolve_token(args.token, client.get_server_url())) as server:
        result: types.CommitResponse = server.revert(args.seq, reason=args.message)
    print(f"Reverted to manifest-{args.seq:08d}: new manifest-{result.seq:08d}")


# ----------------------------------------------------------------------------------------------------------------------
def main() -> None:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(description="Bearpond client CLI")
    subparsers: argparse._SubParsersAction = parser.add_subparsers(dest="command", required=True)

    login_parser: argparse.ArgumentParser = subparsers.add_parser(
        "login", help="Save an API key for a server, so the other commands send it: <server>"
    )
    login_parser.add_argument("server", help="the server's address, e.g. http://localhost:8000")
    login_parser.add_argument("--token", default=None, help="the key; asked for (without echoing it) when left out")
    login_parser.set_defaults(func=cmd_login)

    logout_parser: argparse.ArgumentParser = subparsers.add_parser(
        "logout", help="Forget the API key saved for a server: <server>"
    )
    logout_parser.add_argument("server", help="the server's address, e.g. http://localhost:8000")
    logout_parser.set_defaults(func=cmd_logout)

    repo_create_parser: argparse.ArgumentParser = subparsers.add_parser(
        "repo-create", help="Create a new repository on the server: <server>/<repo>"
    )
    repo_create_parser.add_argument("url", help="<server-url>/<repo-name>, e.g. http://localhost:8000/trades")
    repo_create_parser.add_argument("--token", default=None, help=TOKEN_HELP)
    repo_create_parser.set_defaults(func=cmd_repo_create)

    clone_parser: argparse.ArgumentParser = subparsers.add_parser(
        "clone", help="Clone a repository into a new workspace: <server>/<repo> [dir]"
    )
    clone_parser.add_argument("url", help="<server-url>/<repo-name>, e.g. http://localhost:8000/trades")
    clone_parser.add_argument("dir", type=Path, nargs="?", default=None, help="Target directory (default: the repo name)")
    clone_parser.add_argument("--token", default=None, help=TOKEN_HELP)
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
    commit_parser.add_argument("--token", default=None, help=TOKEN_HELP)
    commit_parser.add_argument("-m", "--message", default=None, help="Commit message recorded on the commit record")
    commit_parser.set_defaults(func=cmd_commit)

    pull_parser: argparse.ArgumentParser = subparsers.add_parser(
        "pull", help="Pull the latest manifest from the server, updating the local mirror"
    )
    pull_parser.add_argument("--token", default=None, help=TOKEN_HELP)
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
    log_parser.add_argument("--token", default=None, help=TOKEN_HELP)
    log_parser.add_argument("--limit", type=int, default=20, help="Maximum number of commits to show")
    log_parser.set_defaults(func=cmd_log)

    revert_parser: argparse.ArgumentParser = subparsers.add_parser(
        "revert", help="Revert the lake to a prior manifest version, recorded as a new commit"
    )
    revert_parser.add_argument("--token", default=None, help=TOKEN_HELP)
    revert_parser.add_argument("--seq", type=int, required=True, help="The manifest version to revert to")
    revert_parser.add_argument("-m", "--message", default=None, help="Commit message recorded on the resulting commit")
    revert_parser.set_defaults(func=cmd_revert)

    args: argparse.Namespace = parser.parse_args()
    try:
        args.func(args)
    except server_client.BearpondError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except httpx.TransportError as e:
        # the server could not be reached at all, which is said in a line rather than a traceback
        print(f"Error: cannot reach the server: {e}", file=sys.stderr)
        sys.exit(1)


# ----------------------------------------------------------------------------------------------------------------------
if __name__ == "__main__":
    main()
