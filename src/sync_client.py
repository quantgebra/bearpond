import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

DEFAULT_SOURCE_ROOT = Path(__file__).resolve().parents[2] / "polymarket-data"
CHUNK_SIZE = 1024 * 1024
SYNC_STATE_NAME = "_synced.json"


def read_manifest(source_root: Path) -> dict:
    manifest_dir = source_root / "manifests"
    latest_pointer = manifest_dir / "_latest"
    if not latest_pointer.exists():
        print(f"No manifest found at {manifest_dir} — has generate_manifest.py been run?", file=sys.stderr)
        sys.exit(1)
    manifest_name = latest_pointer.read_text().strip()
    return json.loads((manifest_dir / manifest_name).read_text())


def load_local_state(target_root: Path) -> dict:
    state_path = target_root / SYNC_STATE_NAME
    if not state_path.exists():
        return {}
    return json.loads(state_path.read_text())


def save_local_state(target_root: Path, state: dict) -> None:
    state_path = target_root / SYNC_STATE_NAME
    tmp_path = state_path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(state, indent=2))
    os.replace(tmp_path, state_path)


def fetch_file(source_data_root: Path, rel_path: str, dest_path: Path) -> str:
    # Stand-in for an HTTP GET — copies from the server's data dir while
    # hashing the bytes as they stream, so verification costs nothing extra.
    # This is the one function an HTTP client swaps out later.
    src_path = source_data_root / rel_path
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = dest_path.with_suffix(dest_path.suffix + ".tmp")
    digest = hashlib.sha256()
    with src_path.open("rb") as src, tmp_path.open("wb") as dst:
        for chunk in iter(lambda: src.read(CHUNK_SIZE), b""):
            digest.update(chunk)
            dst.write(chunk)
    checksum = digest.hexdigest()
    os.replace(tmp_path, dest_path)
    return checksum


def prune_empty_dirs(root: Path, start: Path) -> None:
    current = start.parent
    while current != root and current.exists() and not any(current.iterdir()):
        current.rmdir()
        current = current.parent


def sync(source_root: Path, target_root: Path, dry_run: bool = False) -> None:
    manifest = read_manifest(source_root)
    source_data_root = source_root / "data"
    target_root.mkdir(parents=True, exist_ok=True)

    manifest_files = {f["path"]: (f["size"], f["sha256"]) for f in manifest["files"]}
    local_state = load_local_state(target_root)

    to_download = [p for p in manifest_files if tuple(local_state.get(p, [])) != manifest_files[p]]
    to_delete = [p for p in local_state if p not in manifest_files]

    print(
        f"manifest-{manifest['seq']:08d}: {len(manifest_files)} files "
        f"({len(to_download)} to fetch, {len(to_delete)} to prune)"
    )

    if dry_run:
        for p in to_download:
            print(f"  would fetch: {p}")
        for p in to_delete:
            print(f"  would prune: {p}")
        return

    # State is saved after every single file, not once at the end, so a
    # crash or dropped connection mid-sync loses at most the file in flight —
    # rerunning just picks up where it left off instead of redoing everything.
    state = dict(local_state)

    for rel_path in to_download:
        dest_path = target_root / rel_path
        expected_size, expected_sha256 = manifest_files[rel_path]
        actual_sha256 = fetch_file(source_data_root, rel_path, dest_path)
        if actual_sha256 != expected_sha256:
            dest_path.unlink(missing_ok=True)
            raise RuntimeError(f"checksum mismatch after fetching {rel_path}")
        state[rel_path] = [expected_size, actual_sha256]
        save_local_state(target_root, state)
        print(f"  fetched: {rel_path}")

    for rel_path in to_delete:
        dest_path = target_root / rel_path
        dest_path.unlink(missing_ok=True)
        prune_empty_dirs(target_root, dest_path)
        del state[rel_path]
        save_local_state(target_root, state)
        print(f"  pruned: {rel_path}")

    print(
        f"Synced to manifest-{manifest['seq']:08d}: "
        f"+{len(to_download)} -{len(to_delete)} (total {len(state)} files)"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync a local Parquet lake mirror against a manifest.")
    parser.add_argument(
        "--source", type=Path, default=DEFAULT_SOURCE_ROOT,
        help="Root containing manifests/ and data/ (default: sibling polymarket-data repo)",
    )
    parser.add_argument("--target", type=Path, required=True, help="Local directory to sync into")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    sync(args.source, args.target, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
