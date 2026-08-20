import os
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
    # the lake's hive layout). Because the name is the content, insertion is idempotent and crash-safe — and
    # insert() verifies the hash rather than trusting it, so the contract can't silently break.

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
    def insert(self, address: str, staged_path: Path) -> None:
        # the address is a sha256 hash of the file contents, let's verify that the client isn't lying :)
        actual_address: str = utils.sha256_file(staged_path)
        if actual_address != address:
            raise ObjectStoreError(
                f"staged file content does not match its address: {staged_path} "
                f"(expected {actual_address}, got {address})"
            )

        # calculate the physical destination path and "move" the object using native atomic operations
        dest_path: Path = self.get_location_for_address(address)
        if dest_path.exists():
            # if the object is already stored there is nothing to do since the content is already there
            staged_path.unlink(missing_ok=True)
        elif staged_path.is_file():
            # we do NOT have a destination file AND the staged file exists
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staged_path, dest_path)
            utils.fsync_dir(dest_path.parent)
        else:
            # a staged file can be missing ONLY if an attempted commit moved the file, then crashed before finishing.
            # But then, the dest_path would exist.
            raise ObjectStoreError(f"staged file missing and object not stored: {staged_path} (address {address})")
