import conftest
from bearpond import types
from bearpond.server import metadata_store
from bearpond.server import repository
from bearpond.server import sqlite_server_metadata_store

PATH_A = "year=2024/month=01/a.parquet"
PATH_B = "year=2024/month=02/b.parquet"
CONTENT_A = b"alpha parquet bytes"
CONTENT_B = b"beta parquet bytes"


# ----------------------------------------------------------------------------------------------------------------------
def paths_of(manifest: types.Manifest | None) -> list[str]:
    return sorted(f.path for f in manifest.files) if manifest is not None else []


# ----------------------------------------------------------------------------------------------------------------------
def test_get_manifest_reconstructs_historical_seqs(repo: repository.Repository) -> None:
    conftest.commit_files(repo, {PATH_A: CONTENT_A})  # seq 1
    conftest.commit_files(repo, {PATH_B: CONTENT_B})  # seq 2
    conftest.remove_files(repo, {PATH_A: CONTENT_A})  # seq 3

    store: metadata_store.MetadataStore = repo.metadata_store

    # each seq reconstructs the lake exactly as it was — additions visible from their seq on, removals absent from theirs
    assert paths_of(store.get_manifest(1)) == [PATH_A]
    assert paths_of(store.get_manifest(2)) == [PATH_A, PATH_B]
    assert paths_of(store.get_manifest(3)) == [PATH_B]

    # the never-committed state and not-yet-existing versions both read as absent
    assert store.get_manifest(0) is None
    assert store.get_manifest(99) is None


# ----------------------------------------------------------------------------------------------------------------------
def test_list_directory_splits_files_and_subdirectories(repo: repository.Repository) -> None:
    conftest.commit_files(
        repo,
        {PATH_A: CONTENT_A, PATH_B: CONTENT_B, "year=2025/month=01/c.parquet": b"gamma", "top.parquet": b"top"},
    )
    store: metadata_store.MetadataStore = repo.metadata_store

    # a prefix whose children are all deeper rolls up into directory names, not files
    year_page: metadata_store.DirectoryPage = store.list_directory("year=2024/", limit=100)
    assert year_page.files == []
    assert year_page.directories == ["year=2024/month=01", "year=2024/month=02"]
    assert year_page.next_cursor is None

    # the leaf prefix lists its files directly
    month_page: metadata_store.DirectoryPage = store.list_directory("year=2024/month=01/", limit=100)
    assert [f.path for f in month_page.files] == [PATH_A]
    assert month_page.directories == []

    # the root lists top-level files alongside top-level directories
    root_page: metadata_store.DirectoryPage = store.list_directory("", limit=100)
    assert [f.path for f in root_page.files] == ["top.parquet"]
    assert root_page.directories == ["year=2024", "year=2025"]

    # a prefix with nothing under it is empty, not an error
    empty_page: metadata_store.DirectoryPage = store.list_directory("year=1999/", limit=100)
    assert empty_page.files == [] and empty_page.directories == []


# ----------------------------------------------------------------------------------------------------------------------
def test_list_directory_paginates_files_by_cursor(repo: repository.Repository) -> None:
    files: dict[str, bytes] = {f"year=2024/month=01/part-{i}.parquet": f"content-{i}".encode() for i in range(5)}
    conftest.commit_files(repo, files)
    store: metadata_store.MetadataStore = repo.metadata_store

    first_page: metadata_store.DirectoryPage = store.list_directory("year=2024/month=01/", limit=2)
    assert [f.path for f in first_page.files] == [
        "year=2024/month=01/part-0.parquet",
        "year=2024/month=01/part-1.parquet",
    ]
    assert first_page.next_cursor == "year=2024/month=01/part-1.parquet"

    # the cursor picks up exactly where the last page stopped
    rest: list[str] = []
    cursor: str | None = first_page.next_cursor
    while cursor is not None:
        page: metadata_store.DirectoryPage = store.list_directory("year=2024/month=01/", limit=2, cursor=cursor)
        rest.extend(f.path for f in page.files)
        cursor = page.next_cursor
    assert rest == [
        "year=2024/month=01/part-2.parquet",
        "year=2024/month=01/part-3.parquet",
        "year=2024/month=01/part-4.parquet",
    ]


# ----------------------------------------------------------------------------------------------------------------------
def test_get_commit_record_page_paginates_newest_first(repo: repository.Repository) -> None:
    conftest.commit_files(repo, {PATH_A: CONTENT_A})
    conftest.commit_files(repo, {PATH_B: CONTENT_B})
    conftest.remove_files(repo, {PATH_A: CONTENT_A})
    store: metadata_store.MetadataStore = repo.metadata_store

    first: types.CommitHistoryPage = store.get_commit_record_page(limit=2)
    assert [r.seq for r in first.records] == [3, 2]
    assert first.next_cursor == 2

    # the cursor picks up exactly where the last page stopped, and the chain links across the page boundary
    second: types.CommitHistoryPage = store.get_commit_record_page(limit=2, before_seq=first.next_cursor)
    assert [r.seq for r in second.records] == [1]
    assert second.next_cursor is None
    assert first.records[-1].parent_commit_hash == second.records[0].commit_hash


# ----------------------------------------------------------------------------------------------------------------------
def test_get_commit_record_by_any_key(repo: repository.Repository) -> None:
    conftest.commit_files(repo, {PATH_A: CONTENT_A})
    record: types.CommitRecord = repo.metadata_store.get_commit_record_list()[0]

    by_txn: types.CommitRecord | None = repo.metadata_store.get_commit_record_by_txn_uuid(record.txn_uuid)
    by_hash: types.CommitRecord | None = repo.metadata_store.get_commit_record_by_commit_hash(record.commit_hash)
    by_seq: types.CommitRecord | None = repo.metadata_store.get_commit_record_by_commit_seq(record.seq)

    assert by_txn == record
    assert by_hash == record
    assert by_seq == record


# ----------------------------------------------------------------------------------------------------------------------
def test_get_commit_record_by_any_key_returns_none_for_unknown(repo: repository.Repository) -> None:
    assert repo.metadata_store.get_commit_record_by_txn_uuid("no-such-txn") is None
    assert repo.metadata_store.get_commit_record_by_commit_hash("0" * 64) is None
    assert repo.metadata_store.get_commit_record_by_commit_seq(999) is None


# ----------------------------------------------------------------------------------------------------------------------
def test_get_manifest_created_at_matches_its_commit(repo: repository.Repository) -> None:
    conftest.commit_files(repo, {PATH_A: CONTENT_A})
    conftest.commit_files(repo, {PATH_B: CONTENT_B})

    store: metadata_store.MetadataStore = repo.metadata_store
    records: list[types.CommitRecord] = store.get_commit_record_list()

    first: types.Manifest | None = store.get_manifest(1)
    second: types.Manifest | None = store.get_manifest(2)
    assert first is not None and first.created_at == records[0].committed_at
    assert second is not None and second.created_at == records[1].committed_at


# ----------------------------------------------------------------------------------------------------------------------
def test_commit_hashs_form_a_chain(repo: repository.Repository) -> None:
    conftest.commit_files(repo, {PATH_A: CONTENT_A})
    conftest.commit_files(repo, {PATH_B: CONTENT_B})
    conftest.remove_files(repo, {PATH_A: CONTENT_A})

    records: list[types.CommitRecord] = repo.metadata_store.get_commit_record_list()

    # the first commit has no parent; every later one names its predecessor's id, like git
    assert records[0].parent_commit_hash is None
    assert records[1].parent_commit_hash == records[0].commit_hash
    assert records[2].parent_commit_hash == records[1].commit_hash
    assert len({r.commit_hash for r in records}) == 3


# ----------------------------------------------------------------------------------------------------------------------
def test_commit_hash_is_self_certifying(repo: repository.Repository) -> None:
    # anyone holding a record can recompute its id and prove it hasn't been altered
    conftest.commit_files(repo, {PATH_A: CONTENT_A})

    record: types.CommitRecord | None = repo.metadata_store.get_commit_record_list()[0]
    recomputed: str = sqlite_server_metadata_store.compute_commit_hash(
        record.parent_commit_hash,
        record.seq,
        record.committed_at,
        record.added,
        record.removed,
        record.updated,
        record.user,
        record.reason,
    )
    assert recomputed == record.commit_hash


# ----------------------------------------------------------------------------------------------------------------------
def test_commit_hash_ignores_added_and_removed_order() -> None:
    # the lists are semantically sets — the same change in a different order must hash identically
    added: list[types.FileMetadata] = [conftest.file_meta(PATH_A, CONTENT_A), conftest.file_meta(PATH_B, CONTENT_B)]
    removed: list[types.FileMetadata] = [conftest.file_meta("year=2023/x.parquet", b"x"), conftest.file_meta("year=2023/y.parquet", b"y")]
    updated: list[types.UpdatedFileMetadata] = []

    forward: str = sqlite_server_metadata_store.compute_commit_hash(
        None, 1, "2026-01-01T00:00:00+00:00", added, removed, updated, None, None
    )
    shuffled: str = sqlite_server_metadata_store.compute_commit_hash(
        None, 1, "2026-01-01T00:00:00+00:00", list(reversed(added)), list(reversed(removed)), list(reversed(updated)), None, None
    )
    assert forward == shuffled
