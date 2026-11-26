from urllib.parse import quote

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel
from quantgebra.auth import auth_services
from quantgebra.auth.http import auth_gate

from bearpond.protocol import types
from .. import commit_record
from .. import object_record
from .. import server as server_module
from .. import server_auth
from ..api import dependencies
from . import page_renderer
from . import page_resource

_FILES_PER_PAGE: int = 200
_COMMITS_PER_PAGE: int = 50
_RECENT_COMMIT_COUNT: int = 8
_SHORT_HASH_LENGTH: int = 12


# ======================================================================================================================
class RepositoryRow(BaseModel):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    name: str
    href: str
    seq: int
    file_count: int
    byte_count: int
    size_text: str
    # when the last commit was made, None for a repository that nothing has been committed to
    committed_at: str | None
    short_commit_hash: str | None


# ======================================================================================================================
class RepositoriesModel(BaseModel):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    query: str
    rows: list[RepositoryRow]
    repository_count: int
    file_count: int
    size_text: str
    committed_at: str | None


# ======================================================================================================================
class PathCrumb(BaseModel):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    label: str
    href: str


# ======================================================================================================================
class DirectoryRow(BaseModel):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    name: str
    href: str


# ======================================================================================================================
class FileRow(BaseModel):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    path: str
    # the path below the directory that is shown, which is the whole path where no directory is
    name: str
    short_sha256: str
    sha256: str
    size_text: str
    download_href: str


# ======================================================================================================================
class UpdatedFileRow(BaseModel):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    path: str
    old_short_sha256: str
    old_size_text: str
    new_short_sha256: str
    new_size_text: str


# ======================================================================================================================
class CommitRow(BaseModel):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    seq: int
    href: str
    committed_at: str
    user: str | None
    reason: str | None
    added_count: int
    removed_count: int
    updated_count: int
    short_commit_hash: str


# ======================================================================================================================
class RepositoryModel(BaseModel):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    name: str
    seq: int
    file_count: int
    size_text: str
    committed_at: str | None
    short_commit_hash: str | None
    path_crumbs: list[PathCrumb]
    directories: list[DirectoryRow]
    files: list[FileRow]
    # the next page of files in the directory, None on the last one
    next_page_href: str | None
    recent_commits: list[CommitRow]
    commits_href: str


# ======================================================================================================================
class CommitsModel(BaseModel):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    name: str
    commits: list[CommitRow]
    older_href: str | None
    # a link back to the newest commits, on any page but the first
    newest_href: str | None


# ======================================================================================================================
class CommitModel(BaseModel):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    name: str
    seq: int
    commit_hash: str
    parent_commit_hash: str | None
    parent_href: str | None
    txn_uuid: str
    committed_at: str
    user: str | None
    reason: str | None
    added: list[FileRow]
    removed: list[FileRow]
    updated: list[UpdatedFileRow]


# ======================================================================================================================
class RepositoriesPage(page_resource.PageResource):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def make_repository_href(repo_name: str) -> str:
        return f"/repositories/{quote(repo_name)}"

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def make_commit_row(repo_name: str, commit: types.CommitRecord) -> CommitRow:
        return CommitRow(
            seq=commit.seq,
            href=f"{RepositoriesPage.make_repository_href(repo_name)}/commits/{commit.seq}",
            committed_at=commit.committed_at,
            user=commit.user,
            reason=commit.reason,
            added_count=len(commit.added),
            removed_count=len(commit.removed),
            updated_count=len(commit.updated),
            short_commit_hash=commit.commit_hash[:_SHORT_HASH_LENGTH],
        )

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def make_path_crumbs(repo_name: str, directory_path: str) -> list[PathCrumb]:
        # the repository's root and then each directory down to the one that is shown, each a link to itself
        repository_href: str = RepositoriesPage.make_repository_href(repo_name)
        path_crumbs: list[PathCrumb] = [PathCrumb(label=repo_name, href=repository_href)]
        walked_path: str = ""
        for segment in directory_path.split("/") if directory_path else []:
            walked_path = f"{walked_path}/{segment}" if walked_path else segment
            path_crumbs.append(PathCrumb(label=segment, href=f"{repository_href}?path={quote_path(walked_path)}"))
        return path_crumbs

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def make_file_row(repo_name: str, file: types.FileMetadata, prefix: str) -> FileRow:
        return FileRow(
            path=file.path,
            name=file.path[len(prefix) :],
            short_sha256=file.sha256[:_SHORT_HASH_LENGTH],
            sha256=file.sha256,
            size_text=format_size(file.size),
            download_href=f"/repos/{quote(repo_name)}/files/{quote_path(file.path)}",
        )

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================

    # ------------------------------------------------------------------------------------------------------------------
    def add_routes(self, router: APIRouter) -> None:
        router.add_api_route("/repositories", self.show_repositories, methods=["GET"], response_class=HTMLResponse)
        router.add_api_route(
            "/repositories/{repo_name}", self.show_repository, methods=["GET"], response_class=HTMLResponse
        )
        router.add_api_route(
            "/repositories/{repo_name}/commits", self.show_commits, methods=["GET"], response_class=HTMLResponse
        )
        router.add_api_route(
            "/repositories/{repo_name}/commits/{seq}", self.show_commit, methods=["GET"], response_class=HTMLResponse
        )

    # ------------------------------------------------------------------------------------------------------------------
    def show_repositories(
        self,
        request: Request,
        query: str = Query("", alias="q"),
        services: auth_services.AuthServices = Depends(dependencies.get_auth_services),
    ) -> Response:
        # the repositories that the person may read, whose name has what was searched for in it, with the size and the
        # last commit of each; the totals are over all of them, not only the ones the search kept
        found: auth_gate.RequestAuth = self._require_viewer(request, services)
        all_rows: list[RepositoryRow] = []
        for repo_name in server_module.get_server().list_repositories(services.session):
            if found.actor.can(server_auth.PERMISSION_READ, repo_name):
                row: RepositoryRow = self._make_repository_row(services, repo_name)
                all_rows.append(row)

        # the search only narrows the table
        shown_rows: list[RepositoryRow] = []
        for row in all_rows:
            if query.lower() in row.name.lower():
                shown_rows.append(row)

        # the totals, and the newest of the last commits, for the cards above the table
        file_count: int = 0
        byte_count: int = 0
        committed_at: str | None = None
        for row in all_rows:
            file_count += row.file_count
            byte_count += row.byte_count
            if row.committed_at is not None and (committed_at is None or row.committed_at > committed_at):
                committed_at = row.committed_at

        model: RepositoriesModel = RepositoriesModel(
            query=query,
            rows=shown_rows,
            repository_count=len(all_rows),
            file_count=file_count,
            size_text=format_size(byte_count),
            committed_at=committed_at,
        )
        head: page_renderer.PageHead = self._make_head(
            services, found, "Repositories", "repositories", [page_renderer.Crumb(label="Repositories")]
        )
        return self._send("repositories", head, model)

    # ------------------------------------------------------------------------------------------------------------------
    def show_repository(
        self,
        request: Request,
        repo_name: str,
        directory_path: str = Query("", alias="path"),
        cursor: str | None = None,
        services: auth_services.AuthServices = Depends(dependencies.get_auth_services),
    ) -> Response:
        # one repository as it is now: the directories and files under the directory that is browsed, a page of files
        # at a time, and the latest commits
        found: auth_gate.RequestAuth = self._require_repository_reader(request, services, repo_name)
        directory_path = directory_path.strip("/")
        prefix: str = f"{directory_path}/" if directory_path else ""
        directory_page: object_record.DirectoryPage = object_record.ObjectRecord.get_directory_page(
            services.session, repo_name, prefix, _FILES_PER_PAGE, cursor
        )
        if directory_path and not directory_page.files and not directory_page.directories:
            raise page_resource.PageRefusedError(404, f"There is no directory {directory_path} in {repo_name}.")

        # the rows of the directories and files under the directory
        repository_href: str = RepositoriesPage.make_repository_href(repo_name)
        directories: list[DirectoryRow] = []
        for directory_name in directory_page.directories:
            directory_href: str = f"{repository_href}?path={quote_path(directory_name)}"
            directories.append(DirectoryRow(name=directory_name[len(prefix) :], href=directory_href))
        files: list[FileRow] = []
        for file in directory_page.files:
            file_row: FileRow = RepositoriesPage.make_file_row(repo_name, file, prefix)
            files.append(file_row)
        next_page_href: str | None = None
        if directory_page.next_cursor is not None:
            next_cursor: str = quote_path(directory_page.next_cursor)
            next_page_href = f"{repository_href}?path={quote_path(directory_path)}&cursor={next_cursor}"

        # the latest commits, and the version that the repository is at
        commit_page: types.CommitHistoryPage = commit_record.CommitRecord.get_commit_page(
            services.session, repo_name, _RECENT_COMMIT_COUNT
        )
        recent_commits: list[CommitRow] = []
        for commit in commit_page.records:
            commit_row: CommitRow = RepositoriesPage.make_commit_row(repo_name, commit)
            recent_commits.append(commit_row)
        summary: RepositoryRow = self._make_repository_row(services, repo_name)

        model: RepositoryModel = RepositoryModel(
            name=repo_name,
            seq=summary.seq,
            file_count=summary.file_count,
            size_text=summary.size_text,
            committed_at=summary.committed_at,
            short_commit_hash=summary.short_commit_hash,
            path_crumbs=RepositoriesPage.make_path_crumbs(repo_name, directory_path),
            directories=directories,
            files=files,
            next_page_href=next_page_href,
            recent_commits=recent_commits,
            commits_href=f"{repository_href}/commits",
        )
        crumbs: list[page_renderer.Crumb] = [
            page_renderer.Crumb(label="Repositories", href="/repositories"),
            page_renderer.Crumb(label=repo_name),
        ]
        head: page_renderer.PageHead = self._make_head(services, found, repo_name, "repositories", crumbs)
        return self._send("repository", head, model)

    # ------------------------------------------------------------------------------------------------------------------
    def show_commits(
        self,
        request: Request,
        repo_name: str,
        before_seq: int | None = Query(None, alias="before"),
        services: auth_services.AuthServices = Depends(dependencies.get_auth_services),
    ) -> Response:
        # the history of a repository, newest first, a page at a time
        found: auth_gate.RequestAuth = self._require_repository_reader(request, services, repo_name)
        commit_page: types.CommitHistoryPage = commit_record.CommitRecord.get_commit_page(
            services.session, repo_name, _COMMITS_PER_PAGE, before_seq
        )
        commits: list[CommitRow] = []
        for commit in commit_page.records:
            commit_row: CommitRow = RepositoriesPage.make_commit_row(repo_name, commit)
            commits.append(commit_row)

        repository_href: str = RepositoriesPage.make_repository_href(repo_name)
        model: CommitsModel = CommitsModel(
            name=repo_name,
            commits=commits,
            older_href=(
                f"{repository_href}/commits?before={commit_page.next_cursor}"
                if commit_page.next_cursor is not None
                else None
            ),
            newest_href=f"{repository_href}/commits" if before_seq is not None else None,
        )
        crumbs: list[page_renderer.Crumb] = [
            page_renderer.Crumb(label="Repositories", href="/repositories"),
            page_renderer.Crumb(label=repo_name, href=repository_href),
            page_renderer.Crumb(label="Commits"),
        ]
        head: page_renderer.PageHead = self._make_head(services, found, f"{repo_name} commits", "repositories", crumbs)
        return self._send("commits", head, model)

    # ------------------------------------------------------------------------------------------------------------------
    def show_commit(
        self,
        request: Request,
        repo_name: str,
        seq: int,
        services: auth_services.AuthServices = Depends(dependencies.get_auth_services),
    ) -> Response:
        # one commit: who made it and why, what it changed, and where it sits in the chain of commits
        found: auth_gate.RequestAuth = self._require_repository_reader(request, services, repo_name)
        commit: types.CommitRecord | None = commit_record.CommitRecord.get_commit_by_seq(
            services.session, repo_name, seq
        )
        if commit is None:
            raise page_resource.PageRefusedError(404, f"There is no commit {seq} in {repo_name}.")

        # what the commit added, removed and changed
        added: list[FileRow] = []
        for file in commit.added:
            added_row: FileRow = RepositoriesPage.make_file_row(repo_name, file, "")
            added.append(added_row)
        removed: list[FileRow] = []
        for file in commit.removed:
            removed_row: FileRow = RepositoriesPage.make_file_row(repo_name, file, "")
            removed.append(removed_row)
        updated: list[UpdatedFileRow] = []
        for updated_file in commit.updated:
            updated_row: UpdatedFileRow = UpdatedFileRow(
                path=updated_file.path,
                old_short_sha256=updated_file.old_sha256[:_SHORT_HASH_LENGTH],
                old_size_text=format_size(updated_file.old_size),
                new_short_sha256=updated_file.new_sha256[:_SHORT_HASH_LENGTH],
                new_size_text=format_size(updated_file.new_size),
            )
            updated.append(updated_row)

        repository_href: str = RepositoriesPage.make_repository_href(repo_name)
        model: CommitModel = CommitModel(
            name=repo_name,
            seq=commit.seq,
            commit_hash=commit.commit_hash,
            parent_commit_hash=commit.parent_commit_hash,
            parent_href=f"{repository_href}/commits/{commit.seq - 1}" if commit.seq > 1 else None,
            txn_uuid=commit.txn_uuid,
            committed_at=commit.committed_at,
            user=commit.user,
            reason=commit.reason,
            added=added,
            removed=removed,
            updated=updated,
        )
        crumbs: list[page_renderer.Crumb] = [
            page_renderer.Crumb(label="Repositories", href="/repositories"),
            page_renderer.Crumb(label=repo_name, href=repository_href),
            page_renderer.Crumb(label="Commits", href=f"{repository_href}/commits"),
            page_renderer.Crumb(label=str(seq)),
        ]
        head: page_renderer.PageHead = self._make_head(
            services, found, f"{repo_name} commit {seq}", "repositories", crumbs
        )
        return self._send("commit", head, model)

    # ------------------------------------------------------------------------------------------------------------------
    def _make_repository_row(self, services: auth_services.AuthServices, repo_name: str) -> RepositoryRow:
        # the version a repository is at, how much is in it now, and its last commit
        seq: int = commit_record.CommitRecord.get_current_seq(services.session, repo_name)
        live_files: dict[str, types.FileMetadata] = object_record.ObjectRecord.get_live_files_by_path(
            services.session, repo_name
        )
        byte_count: int = 0
        for file in live_files.values():
            byte_count += file.size
        head_commit: types.CommitRecord | None = commit_record.CommitRecord.get_commit_by_seq(
            services.session, repo_name, seq
        )
        return RepositoryRow(
            name=repo_name,
            href=RepositoriesPage.make_repository_href(repo_name),
            seq=seq,
            file_count=len(live_files),
            byte_count=byte_count,
            size_text=format_size(byte_count),
            committed_at=head_commit.committed_at if head_commit is not None else None,
            short_commit_hash=head_commit.commit_hash[:_SHORT_HASH_LENGTH] if head_commit is not None else None,
        )

    # ------------------------------------------------------------------------------------------------------------------
    def _require_repository_reader(
        self, request: Request, services: auth_services.AuthServices, repo_name: str
    ) -> auth_gate.RequestAuth:
        # somebody who may read the repository; whether there is one by that name is only said to them
        found: auth_gate.RequestAuth = self._require_viewer(request, services)
        if not found.actor.can(server_auth.PERMISSION_READ, repo_name):
            raise page_resource.PageRefusedError(403, f"You may not read the repository {repo_name}.")
        if repo_name not in server_module.get_server().list_repositories(services.session):
            raise page_resource.PageRefusedError(404, f"There is no repository named {repo_name}.")
        return found


# ----------------------------------------------------------------------------------------------------------------------
def format_size(byte_count: int) -> str:
    # a size as people read it, in powers of 1024
    units: list[str] = ["B", "KB", "MB", "GB", "TB"]
    size: float = float(byte_count)
    unit_index: int = 0
    while size >= 1024 and unit_index < len(units) - 1:
        size = size / 1024
        unit_index += 1
    text: str = f"{byte_count} B" if unit_index == 0 else f"{size:.1f} {units[unit_index]}"
    return text


# ----------------------------------------------------------------------------------------------------------------------
def quote_path(path: str) -> str:
    # a path in a link, with its slashes and the equal signs of hive partitions left as they are, so it reads plainly
    return quote(path, safe="/=")
