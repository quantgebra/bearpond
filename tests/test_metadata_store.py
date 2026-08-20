import conftest
from bearpond import types
from bearpond.server import metadata_store
from bearpond.server import repository

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
def test_get_current_file_metadata_list_filters_by_prefix(repo: repository.Repository) -> None:
    conftest.commit_files(repo, {PATH_A: CONTENT_A, PATH_B: CONTENT_B, "year=2025/month=01/c.parquet": b"gamma"})

    store: metadata_store.MetadataStore = repo.metadata_store
    assert [f.path for f in store.get_current_file_metadata_list("year=2024/")] == [PATH_A, PATH_B]
    assert [f.path for f in store.get_current_file_metadata_list("year=2024/month=02/")] == [PATH_B]
    assert store.get_current_file_metadata_list("year=1999/") == []
    # no prefix still returns the whole live mapping
    assert len(store.get_current_file_metadata_list()) == 3


# ----------------------------------------------------------------------------------------------------------------------
def test_get_manifest_created_at_matches_its_commit(repo: repository.Repository) -> None:
    conftest.commit_files(repo, {PATH_A: CONTENT_A})
    conftest.commit_files(repo, {PATH_B: CONTENT_B})

    store: metadata_store.MetadataStore = repo.metadata_store
    records: list[metadata_store.CommitRecord] = store.get_commit_record_list()

    first: types.Manifest | None = store.get_manifest(1)
    second: types.Manifest | None = store.get_manifest(2)
    assert first is not None and first.created_at == records[0].committed_at
    assert second is not None and second.created_at == records[1].committed_at


# ----------------------------------------------------------------------------------------------------------------------
def test_commit_ids_form_a_chain(repo: repository.Repository) -> None:
    conftest.commit_files(repo, {PATH_A: CONTENT_A})
    conftest.commit_files(repo, {PATH_B: CONTENT_B})
    conftest.remove_files(repo, {PATH_A: CONTENT_A})

    records: list[metadata_store.CommitRecord] = repo.metadata_store.get_commit_record_list()

    # the first commit has no parent; every later one names its predecessor's id, like git
    assert records[0].parent_commit_id is None
    assert records[1].parent_commit_id == records[0].commit_id
    assert records[2].parent_commit_id == records[1].commit_id
    assert len({r.commit_id for r in records}) == 3


# ----------------------------------------------------------------------------------------------------------------------
def test_commit_id_is_self_certifying(repo: repository.Repository) -> None:
    # anyone holding a record can recompute its id and prove it hasn't been altered
    conftest.commit_files(repo, {PATH_A: CONTENT_A})

    record: metadata_store.CommitRecord | None = repo.metadata_store.get_commit_record_list()[0]
    recomputed: str = metadata_store.compute_commit_id(
        record.parent_commit_id,
        record.seq,
        record.committed_at,
        record.added,
        record.removed,
        record.user,
        record.reason,
    )
    assert recomputed == record.commit_id
