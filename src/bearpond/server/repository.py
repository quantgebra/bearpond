import re
import uuid
from pathlib import Path

from .. import types
from . import config
from . import metadata_store
from . import object_store
from . import utils
from . import transaction


# ======================================================================================================================
class RepositoryError(Exception):
    pass


# ======================================================================================================================
class Repository:
    COMMITS_DIR_NAME = "commits"

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def _validate_hive_parquet_path(rel_path: str) -> None:
        # the convention itself lives in types.py, shared with the client — here it's just mapped to the
        # transaction error vocabulary
        try:
            types.validate_hive_parquet_path(rel_path)
        except ValueError as e:
            raise transaction.TransactionValidationError(str(e)) from e

    # ------------------------------------------------------------------------------------------------------------------
    def __init__(self, repo_dir: Path, store: metadata_store.MetadataStore | None = None) -> None:
        # the repository's entire on-disk layout: content-addressed objects, in-flight uploads, exported manifests
        self.objects_root: Path = repo_dir / "objects"
        self.staging_root: Path = repo_dir / "staging"
        self.manifest_root: Path = repo_dir / "manifests"
        self.latest_pointer: Path = self.manifest_root / "_latest"
        self.object_store: object_store.ObjectStore = object_store.ObjectStore(self.objects_root)
        # the metadata store is the system of record — injectable so tests (or future backends) can swap it
        self.metadata_store: metadata_store.MetadataStore = (
            store if store is not None else metadata_store.SqliteMetadataStore(repo_dir / "bearpond.sqlite")
        )

    # ------------------------------------------------------------------------------------------------------------------
    def _commits_dir(self) -> Path:
        return self.manifest_root / self.COMMITS_DIR_NAME

    # ------------------------------------------------------------------------------------------------------------------
    def _export_commit(self, record: metadata_store.CommitRecord) -> None:
        # manifests and commit records on disk are the durable audit trail — and the rebuild source if the sqlite
        # store is ever lost. Exports are idempotent, so recover() can simply re-run them for every record.
        # export exactly the version this commit produced — under the commit lock that is the current seq, but
        # asking by seq keeps this correct for recover() re-exports of older records too
        manifest: types.Manifest | None = self.metadata_store.get_manifest(record.seq)
        if manifest is None:
            raise RepositoryError(f"commit {record.txn_uuid} recorded but the store has no manifest for seq {record.seq}")

        self.manifest_root.mkdir(parents=True, exist_ok=True)
        manifest_name: str = f"manifest-{manifest.seq:08d}.json"
        manifest_path: Path = self.manifest_root / manifest_name
        if not manifest_path.exists():
            utils.atomic_write(manifest_path, manifest.model_dump_json(indent=2).encode())
            utils.atomic_write(self.latest_pointer, manifest_name.encode())

        commits_dir: Path = self._commits_dir()
        commits_dir.mkdir(parents=True, exist_ok=True)
        record_path: Path = commits_dir / f"{record.txn_uuid}.json"
        if not record_path.exists():
            utils.atomic_write(record_path, record.model_dump_json(indent=2).encode())

    # ------------------------------------------------------------------------------------------------------------------
    def record_commit(
        self,
        txn_uuid: str,
        declared: dict[str, transaction.FileMeta],
        removed: dict[str, transaction.FileMeta],
        user: str | None = None,
        reason: str | None = None,
    ) -> metadata_store.CommitRecord:
        # the commit point: one atomic metadata transaction flips every pointer at once. The export afterwards can
        # only fail benignly — recover() re-exports anything missing at next startup.
        try:
            record: metadata_store.CommitRecord = self.metadata_store.commit_changes(
                txn_uuid,
                added=[types.FileMetadata(path=p, size=m.size, sha256=m.sha256) for p, m in declared.items()],
                removed=[types.FileMetadata(path=p, size=m.size, sha256=m.sha256) for p, m in removed.items()],
                user=user,
                reason=reason,
            )
        except metadata_store.MetadataConflictError as e:
            raise transaction.TransactionConflictError(str(e)) from e
        self._export_commit(record)
        return record

    # ------------------------------------------------------------------------------------------------------------------
    def recover(self) -> None:
        # sqlite recovers itself (its own journal/WAL); startup just re-exports anything a crash interrupted, so
        # the on-disk audit trail always reflects the store. Idempotent — safe on every boot.
        for record in self.metadata_store.get_commit_record_list():
            self._export_commit(record)

    # ------------------------------------------------------------------------------------------------------------------
    def begin_transaction(self, manifest: types.TransactionManifest) -> transaction.Transaction:
        # a transaction that neither adds nor removes anything isn't meaningful
        if not manifest.added and not manifest.removed:
            raise transaction.TransactionValidationError("transaction must add or remove at least one file")

        # build the declared set, rejecting a path named more than once in the same manifest
        declared: dict[str, transaction.FileMeta] = {}
        for declared_file in manifest.added:
            self._validate_hive_parquet_path(declared_file.path)
            if declared_file.path in declared:
                raise transaction.TransactionValidationError(f"path declared more than once: {declared_file.path}")
            declared[declared_file.path] = transaction.FileMeta(size=declared_file.size, sha256=declared_file.sha256)

        # build the removed set, rejecting a path named more than once in the same manifest
        removed: dict[str, transaction.FileMeta] = {}
        for removed_file in manifest.removed:
            if removed_file.path in removed:
                raise transaction.TransactionValidationError(f"path removed more than once: {removed_file.path}")
            removed[removed_file.path] = transaction.FileMeta(size=removed_file.size, sha256=removed_file.sha256)

        # a path can't be both a new file this transaction adds and one it removes
        overlap: set = set(declared) & set(removed)
        if overlap:
            raise transaction.TransactionValidationError(f"paths cannot be both added and removed: {sorted(overlap)}")

        # fail-fast checks against the current mapping — the real guarantees are re-checked atomically at commit
        # time inside the store; these just save a client from uploading bytes for a transaction that can't commit
        for rel_path, meta in removed.items():
            current: types.FileMetadata | None = self.metadata_store.get_file_metadata(rel_path)
            if current is None:
                raise transaction.TransactionValidationError(
                    f"cannot remove a path that doesn't exist in the lake: {rel_path}"
                )
            if current.sha256 != meta.sha256:
                raise transaction.TransactionConflictError(
                    f"{rel_path} has changed since it was declared for removal — remove is only safe against the "
                    f"exact content that was observed"
                )
        for rel_path, meta in declared.items():
            existing: types.FileMetadata | None = self.metadata_store.get_file_metadata(rel_path)
            if existing is not None:
                if existing.sha256 != meta.sha256:
                    raise transaction.TransactionConflictError(
                        f"{rel_path} already exists in the lake with different content — "
                        f"modifying an existing file is only allowed via compaction"
                    )
                # re-adding identical content would change nothing — a no-op commit is a client bug, not a success
                raise transaction.TransactionConflictError(
                    f"{rel_path} already exists in the lake with identical content — re-adding it is a no-op"
                )

        # only create the transaction's staging directory once everything above has passed validation
        txn: transaction.Transaction = transaction.Transaction(uuid.uuid4().hex, self)
        txn.txn_dir.mkdir(parents=True)
        txn.save_declared(declared)
        txn.save_removed(removed)
        return txn

    # ------------------------------------------------------------------------------------------------------------------
    def get_transaction(self, txn_uuid: str) -> transaction.Transaction:
        txn: transaction.Transaction = transaction.Transaction(txn_uuid, self)
        if not txn.txn_dir.is_dir():
            raise transaction.TransactionNotFoundError(f"unknown transaction: {txn_uuid}")
        return txn


# ======================================================================================================================
class RepositoryNotFoundError(RepositoryError):
    pass


# ======================================================================================================================
class RepositoryExistsError(RepositoryError):
    pass


# ======================================================================================================================
class InvalidRepositoryNameError(RepositoryError):
    pass


# a repo name must be a safe single path segment — it becomes a directory under repos_root and a URL component
_REPO_NAME: re.Pattern = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")

# the server's repositories, resolved lazily by name from the configured repos_root
_repos_root: Path | None = None
_repositories: dict[str, Repository] = {}


# ----------------------------------------------------------------------------------------------------------------------
def configure(server_config: config.ServerConfig) -> None:
    global _repos_root
    _repos_root = server_config.repos_root
    _repositories.clear()


# ----------------------------------------------------------------------------------------------------------------------
def _require_repos_root() -> Path:
    if _repos_root is None:
        raise RuntimeError("bearpond server is not configured — set BEARPOND_CONFIG_DIR before startup")
    return _repos_root


# ----------------------------------------------------------------------------------------------------------------------
def get_repository(name: str) -> Repository:
    repos_root: Path = _require_repos_root()
    if name not in _repositories:
        if not _REPO_NAME.match(name):
            raise RepositoryNotFoundError(f"invalid repository name: {name}")
        repo_dir: Path = repos_root / name
        if not repo_dir.is_dir():
            raise RepositoryNotFoundError(f"unknown repository: {name}")
        _repositories[name] = Repository(repo_dir)
    return _repositories[name]


# ----------------------------------------------------------------------------------------------------------------------
def create_repository(name: str) -> Repository:
    repos_root: Path = _require_repos_root()
    if not _REPO_NAME.match(name):
        raise InvalidRepositoryNameError(f"invalid repository name: {name}")
    repo_dir: Path = repos_root / name
    if repo_dir.exists():
        raise RepositoryExistsError(f"repository already exists: {name}")
    repo_dir.mkdir(parents=True)
    _repositories[name] = Repository(repo_dir)
    return _repositories[name]


# ----------------------------------------------------------------------------------------------------------------------
def list_repositories() -> list[str]:
    repos_root: Path = _require_repos_root()
    repos: list[str] = []
    if repos_root.is_dir():
        repos = sorted(p.name for p in repos_root.iterdir() if p.is_dir())
    return repos


# auto-configure from BEARPOND_CONFIG_DIR so importing this module is enough to get a working registry in the
# server process — tests call configure() directly instead and don't need the env var set
_env_config_dir: Path | None = config.config_dir_from_env()
if _env_config_dir is not None:
    configure(config.load_server_config(_env_config_dir))
