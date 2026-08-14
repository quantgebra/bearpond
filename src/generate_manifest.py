import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

DATA_ROOT = Path(__file__).resolve().parents[2] / "polymarket-data" / "data"
MANIFEST_ROOT = Path(__file__).resolve().parents[2] / "polymarket-data" / "manifests"
HASH_CACHE_PATH = MANIFEST_ROOT / ".hash_cache.sqlite"
LATEST_POINTER = MANIFEST_ROOT / "_latest"
KEEP_MANIFESTS = 5
CHUNK_SIZE = 1024 * 1024


def open_cache(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS hash_cache ("
        "path TEXT PRIMARY KEY, size INTEGER, mtime_ns INTEGER, sha256 TEXT)"
    )
    return conn


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def hash_with_cache(conn: sqlite3.Connection, path: Path, rel_path: str) -> str:
    # mtime+size match means the file wasn't touched since we last hashed it
    stat = path.stat()
    row = conn.execute(
        "SELECT size, mtime_ns, sha256 FROM hash_cache WHERE path = ?", (rel_path,)
    ).fetchone()
    if row and row[0] == stat.st_size and row[1] == stat.st_mtime_ns:
        return row[2]

    digest = sha256_file(path)
    conn.execute(
        "INSERT OR REPLACE INTO hash_cache (path, size, mtime_ns, sha256) VALUES (?, ?, ?, ?)",
        (rel_path, stat.st_size, stat.st_mtime_ns, digest),
    )
    return digest


def read_latest_manifest() -> dict | None:
    if not LATEST_POINTER.exists():
        return None
    manifest_name = LATEST_POINTER.read_text().strip()
    manifest_path = MANIFEST_ROOT / manifest_name
    if not manifest_path.exists():
        return None
    return json.loads(manifest_path.read_text())


def atomic_write(path: Path, content: str) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(content)
    os.replace(tmp_path, path)


def prune_old_manifests(keep: int = KEEP_MANIFESTS) -> None:
    # Each manifest is a full snapshot, so old ones aren't needed for replay —
    # keep a handful around only for eyeballing recent history.
    manifests = sorted(MANIFEST_ROOT.glob("manifest-*.json"))
    for stale_path in manifests[:-keep] if len(manifests) > keep else []:
        stale_path.unlink()


def generate_manifest() -> None:
    if not DATA_ROOT.exists():
        print(f"Data root not found: {DATA_ROOT}", file=sys.stderr)
        sys.exit(1)

    conn = open_cache(HASH_CACHE_PATH)
    parquet_files = sorted(DATA_ROOT.rglob("*.parquet"))

    files = []
    for path in parquet_files:
        rel_path = path.relative_to(DATA_ROOT).as_posix()
        files.append({
            "path": rel_path,
            "size": path.stat().st_size,
            "sha256": hash_with_cache(conn, path, rel_path),
        })
    conn.commit()
    conn.close()

    previous = read_latest_manifest()
    previous_files = {f["path"]: (f["size"], f["sha256"]) for f in previous["files"]} if previous else {}
    current_files = {f["path"]: (f["size"], f["sha256"]) for f in files}

    if previous_files == current_files:
        print(f"No changes since manifest-{previous['seq']:08d} ({len(files)} files) — skipping")
        return

    seq = (previous["seq"] + 1) if previous else 1
    manifest = {
        "seq": seq,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "files": files,
    }

    MANIFEST_ROOT.mkdir(parents=True, exist_ok=True)
    manifest_name = f"manifest-{seq:08d}.json"
    atomic_write(MANIFEST_ROOT / manifest_name, json.dumps(manifest, indent=2))
    atomic_write(LATEST_POINTER, manifest_name)
    prune_old_manifests()

    added = set(current_files) - set(previous_files)
    removed = set(previous_files) - set(current_files)
    changed = {p for p in (set(current_files) & set(previous_files)) if current_files[p] != previous_files[p]}
    print(
        f"Wrote {manifest_name}: {len(files)} files "
        f"(+{len(added)} -{len(removed)} ~{len(changed)})"
    )


if __name__ == "__main__":
    generate_manifest()