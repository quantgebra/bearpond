import hashlib
import os
from pathlib import Path

CHUNK_SIZE = 1024 * 1024

# note on durability: fsync flushes the kernel's page cache to the storage device. On macOS it still doesn't flush
# the device's own volatile write cache (that needs fcntl(F_FULLFSYNC)) — real power-loss guarantees assume a
# Linux deployment, where fsync means what it says.


# ======================================================================================================================
class InvalidPathError(Exception):
    # raised by safe_join on path traversal attempts — the API layer maps this to a 400
    pass


# ----------------------------------------------------------------------------------------------------------------------
def safe_join(base: Path, rel_path: str) -> Path:
    # reject path traversal — rel_path comes straight from the URL or a client, and must resolve to somewhere inside base
    candidate: Path = (base / rel_path).resolve()
    if not candidate.is_relative_to(base.resolve()):
        raise InvalidPathError(f"invalid path: {rel_path}")
    return candidate


# ----------------------------------------------------------------------------------------------------------------------
def sha256_file(path: Path) -> str:
    # stream the file in chunks rather than reading it whole — files in the lake can be multiple GB
    digest: hashlib._Hash = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ----------------------------------------------------------------------------------------------------------------------
def fsync_dir(path: Path) -> None:
    # makes directory-entry changes (renames, unlinks) inside path durable — fsyncing a file doesn't fsync the
    # directory that names it, so a rename is only power-loss-safe once its parent directory has been fsynced
    fd: int = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


# ----------------------------------------------------------------------------------------------------------------------
def atomic_write(path: Path, content: bytes) -> None:
    # write to a temp file, fsync it, rename over the real one, then fsync the directory — so a reader never
    # observes a half-written file, and the written file is durable before this call returns
    tmp_path: Path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("wb") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)
    fsync_dir(path.parent)
