import hashlib
import os
from collections.abc import AsyncIterator
from pathlib import Path

from . import utils


# ======================================================================================================================
class ObjectStoreError(Exception):
    pass


# ======================================================================================================================
class ObjectStore:

    # content-addressed object storage: every distinct file content exists exactly once, named by its sha256 —
    # objects/ab/abcdef123... Vocabulary for this class: an object's ADDRESS is the sha256 hash of its contents,
    # and its LOCATION is the physical on-disk spot the address maps to (distinct from a file's logical PATH in
    # the lake). Because the name is the content, insertion is idempotent and crash-safe — upload() verifies both
    # the size and the hash before the object is published at its address.

    # ------------------------------------------------------------------------------------------------------------------
    def __init__(self, objects_root: Path) -> None:
        self.objects_root: Path = objects_root

    # ------------------------------------------------------------------------------------------------------------------
    def get_location_for_address(self, address: str) -> Path:
        # one level of fanout (objects/ab/abcdef...) so a large lake doesn't pile every object into one directory
        return self.objects_root / address[:2] / address

    # ------------------------------------------------------------------------------------------------------------------
    def contains_address(self, address: str) -> bool:
        return self.get_location_for_address(address).is_file()

    # ------------------------------------------------------------------------------------------------------------------
    async def upload(self, address: str, stream: AsyncIterator[bytes], expected_size: int) -> None:
        # stream the content to a temp file next to its final location while computing size and sha256. Only when
        # both match the declared values do we publish the object at its content-addressed path. If the object is
        # already stored, the temp file is discarded — but we still consume and verify the stream so a client that
        # misdeclares its hash fails fast instead of having the wrong content silently committed under that path.
        dest_path: Path = self.get_location_for_address(address)
        tmp_path: Path = dest_path.with_suffix(dest_path.suffix + ".tmp")

        size: int = 0
        digest: hashlib._Hash = hashlib.sha256()
        tmp_path.parent.mkdir(parents=True, exist_ok=True)
        with tmp_path.open("wb") as f:
            async for chunk in stream:
                size += len(chunk)
                digest.update(chunk)
                f.write(chunk)
            # flush before the rename so an acknowledged upload is a durable upload
            f.flush()
            os.fsync(f.fileno())
        actual_address: str = digest.hexdigest()

        if actual_address != address:
            tmp_path.unlink(missing_ok=True)
            raise ObjectStoreError(
                f"sha256 mismatch: expected {address}, got {actual_address}"
            )
        if size != expected_size:
            tmp_path.unlink(missing_ok=True)
            raise ObjectStoreError(
                f"uploaded size does not match declared size for {address}: "
                f"expected {expected_size}, got {size}"
            )

        if dest_path.exists():
            tmp_path.unlink(missing_ok=True)
        else:
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(tmp_path, dest_path)
            utils.fsync_dir(dest_path.parent)
