# Code Style Rules

Binding conventions for this codebase. Follow them for all new code. This list grows over time as more rules get added.

## 1. Function/method "hats"

Every function and method definition is preceded by a full-width, 120-character comment line — `-` for functions/methods, `=` for classes.

Function/method hat (120 characters total):
```
# ----------------------------------------------------------------------------------------------------------------------
```

Class hat (120 characters total):
```
# ======================================================================================================================
```

Example:
```python
# ======================================================================================================================
@dataclass
class ServerConfig:
    repo_root: Path


# ----------------------------------------------------------------------------------------------------------------------
def config_dir_from_env() -> Path | None:
    ...
```

## 2. Single return per function/method

Every function/method has exactly one `return` statement, placed at the end. Don't early-return in the middle of a function body — restructure instead (accumulate into a result variable, use if/elif/else branches that all fall through to one final return, etc.).

Reasoning: an early return that short-circuits execution is easy to miss on a read-through. Missing it means reading to the end of the function while wondering about a case that was already handled higher up. One return at the end means nothing is silently short-circuited.

This applies to `return` statements specifically. Guard-clause `raise`s at the top of a function to validate preconditions are fine and don't count against this rule — exceptions are error signaling, not the kind of silent short-circuit this rule targets. *(This carve-out is an interpretation, not something explicitly confirmed — flag it for correction if that's wrong.)*

## 3. Type hints on every local variable

Every local variable assignment gets a type hint, not just function parameters and return types. This matters most when the variable's value comes from a function or method call, since the type isn't otherwise visible at the call site without navigating to the definition.

```python
# Not this:
result = some_module.some_function()

# This:
result: dict = some_module.some_function()
```

## 4. Import modules, not names, for project code

Never `from some_module import some_function` and then call `some_function()` bare. Import the containing module and call through it, so the call site makes clear where the function came from without checking the imports:

```python
# Not this:
from ..dependencies import require_auth
...
Depends(require_auth)

# This:
from .. import dependencies
...
Depends(dependencies.require_auth)
```

This applies to first-party code within this project. Standard library and third-party package imports (`from pathlib import Path`, `from fastapi import FastAPI`) follow normal ecosystem convention and are exempt. *(Also an interpretation — confirm or correct.)*

## 5. Return-value order matches the function name's order

When a function/method name enumerates multiple values (via "and," or by listing them), the values it returns — e.g. a tuple — come back in that same order.

```python
# Not this:
def sha256_and_read(local_path: Path) -> tuple[bytes, str]:
    ...
    return data, digest

# This:
def sha256_and_read(local_path: Path) -> tuple[str, bytes]:
    ...
    return digest, data
```

## 6. No bare `dict` for known shapes

Never type a parameter or return value as bare `dict`, `dict[str, Any]`, or `list[dict]` when the dict actually has a fixed, known shape. Define a `pydantic.BaseModel` capturing the real keys and value types instead (not a `TypedDict` — see rule 14 for why).

```python
# Not this:
def begin_transaction(self, files: list[dict]) -> str:
    ...

# This:
class DeclaredFile(BaseModel):
    path: str
    sha256: str
    size: int

def begin_transaction(self, files: list[DeclaredFile]) -> str:
    ...
```

Positional/array-shaped data (e.g. something that serializes to/from a JSON array like `[size, sha256]` rather than a JSON object with named keys) doesn't fit `TypedDict` — use `NamedTuple` instead:

```python
# Not this: a JSON array [size, sha256] typed as bare dict values
def load_local_state(target_dir: Path) -> dict:
    ...

# This:
class SyncedFileState(NamedTuple):
    size: int
    sha256: str

def load_local_state(target_dir: Path) -> dict[str, SyncedFileState]:
    ...
```

## 7. Shared fields appear in the same order everywhere

When the same piece of data (e.g. a file's path/size/hash) shows up across multiple structures — a manifest entry, a `TypedDict`, a pydantic model, literal dict/JSON construction — the shared fields are declared and constructed in the same order in every one of them. Pick one canonical order and use it everywhere that shape appears, rather than letting each definition drift independently.

This project's canonical order for a file record is `path, sha256, size`.

## 8. One file per domain class; its utilities live in that same file

A domain class — a class that's a primary, named concept in the codebase (e.g. `BearpondClient`) — gets its own file, named after the class in snake_case: `BearpondClient` lives in `bearpond_client.py`.

Utility functions and small supporting/utility classes that exist to support that domain class and aren't shared with anything else (exceptions like `BearpondError`, pure functions like `load_local_state`) live in the same file as the domain class, not split out into a separate utilities module.

```
bearpond/client/
  bearpond_client.py   # BearpondError, SyncedFileState, load_local_state(), ..., class BearpondClient
  cli.py
```

**Exception: types shared across independently-deployable components go in a dedicated shared module, not duplicated in each.** `bearpond`'s server and client are meant to stay independently deployable, so they don't import from each other — but the wire-format types they both need to agree on exactly (`DeclaredFile`, `TransactionManifest`, `Manifest`, ...) live in `bearpond/protocol.py`, a module with no dependency on either side, that both `server/` and `client/` import. This is not the same as sharing implementation — `protocol.py` holds only the data shapes exchanged over HTTP, nothing about how either side handles them. Server-only types that happen to look similar (`FileMeta`, `CommitRecord` — persisted to local server state, never sent to the client) stay put in the server file that uses them; moving something to `protocol.py` is warranted only when both sides actually need the same definition.

## 9. Class body layout: blank line after the declaration; static/class-level members before instance members

Leave exactly one empty line between the `class Foo:` line and the first member (field or method) of the class.

Within the class body, static/class-level members come first, before any instance members — in this order:
1. Class-level fields (attributes declared directly in the class body, not inside `__init__`)
2. `@staticmethod` and `@classmethod` methods
3. Instance members: `__init__`, then other instance methods (dunder methods, private helpers, public methods)

```python
class BearpondClient:

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        ...

    # ------------------------------------------------------------------------------------------------------------------
    def __init__(self, base_url: str, token: str | None = None) -> None:
        ...

    # ------------------------------------------------------------------------------------------------------------------
    def close(self) -> None:
        ...
```

## 10. A helper goes next to its (near-)exclusive caller

When a method exists mainly to be called by one other method in the same class, place its definition immediately before that caller — don't let unrelated methods sit between them. This is definition-before-use ordering applied at the method-pair level, same as `download_file` already sits directly before `sync`, its only caller.

If the caller uses several such helpers in sequence, order them to match the call sequence, ending with the caller itself:

```python
def upload_file(self, ...): ...    # called in a loop, right before its caller
def upload_files(self, ...): ...   # the caller — loops over upload_file, nothing else
```

## 11. A step of a caller-driven lifecycle never performs another step on its own

When a class exposes a multi-step lifecycle as separate public methods — e.g. `begin_transaction` → `upload_files` → `commit` or `abort` — no single method in that lifecycle may silently perform a *different* step on the caller's behalf. Each exposed step does exactly its own step, nothing more, so the caller keeps full control over when — and whether — to advance to the next one.

```python
# Not this: upload_files() secretly owns the whole lifecycle
def upload_files(self, files: dict[str, Path]) -> CommitResponse:
    txn_id = self.begin_transaction(self.declare_files(files))
    try:
        for rel_path, path in files.items():
            self.upload_file(txn_id, rel_path, path.read_bytes())
        return self.commit(txn_id)          # caller never chose to commit
    except Exception:
        self.abort(txn_id)                  # caller never chose to abort
        raise

# This: upload_files() does only its own step; the caller drives the rest
def upload_files(self, txn_id: str, files: dict[str, Path]) -> list[DeclaredFile]:
    return [self.upload_file(txn_id, rel_path, path.read_bytes()) for rel_path, path in files.items()]
```

Why this matters: if `upload_files()` silently commits or aborts, a caller can no longer inspect intermediate state, decide *not* to commit, or add more files before committing — the four-independent-steps abstraction the class advertises becomes false for that one method.

This rule is about steps of a protocol the class explicitly exposes for the *caller* to drive — it is not a ban on one method calling another in general. A private implementation helper that isn't itself an independently-exposed lifecycle step (`download_file` called from inside `sync`) is fine, because `sync` is the sole sanctioned way to perform a sync, not one step of a multi-step protocol the caller is meant to orchestrate themselves.

If a one-call convenience wrapper that does perform the whole lifecycle is genuinely wanted, give it its own distinctly-named method rather than folding that behavior into a method whose name and signature already promise it performs only one step.

## 12. Code comments

Comments explain what each section of code is doing, and why when that isn't obvious from the code alone. Don't number the steps ("1. first we...", "2. then we...") — describe what's happening, and the reasoning behind it, in prose.

Prefer a single line per comment. Only go multi-line when the explanation genuinely doesn't fit on one line — that's the exception, not the default.

Comments start lowercase, even full sentences — this applies at the start of a comment, not just to continuations.

```python
# ensure that the txn_id is valid
txn_dir: Path = require_txn_dir(txn_id)

# only files in the original TransactionManifest can be uploaded
declared: dict[str, FileMeta] = load_declared(txn_dir)
if rel_path not in declared:
    raise HTTPException(HTTP_400_BAD_REQUEST, f"path was not declared when the transaction was opened: {rel_path}")
```

## 13. Construct a model by calling it, not with a dict literal

When building a value whose type is a `pydantic.BaseModel`, invoke it as a constructor with keyword arguments rather than writing a `{"key": value}` literal. This is the standard way a `BaseModel` is built, and it's what makes rule 14's attribute access possible in the first place — a dict literal assigned to a model-typed variable only satisfies the type checker, it doesn't actually produce an instance of the model.

```python
# Not this:
declared[declared_file.path] = {
    "sha256": declared_file.sha256,
    "size": declared_file.size,
    "supersedes": declared_file.supersedes,
}

# This:
declared[declared_file.path] = DeclaredMeta(
    sha256=declared_file.sha256,
    size=declared_file.size,
    supersedes=declared_file.supersedes,
)
```

This applies everywhere a model is constructed, including inside comprehensions and with `**` used to conditionally include an optional field:

```python
DirectoryEntry(
    name=entry.name,
    type="directory" if entry.is_dir() else "file",
    **({"size": entry.stat().st_size} if entry.is_file() else {}),
)
```

This is specifically about constructing a *named* model. A plain `dict[K, V]` built via a literal or comprehension (e.g. `{f.path: SyncedFileState(...) for f in manifest.files}`) isn't an instance of any model and isn't affected by this rule.

## 14. Access a known shape's fields with `.attribute`, never `["key"]`

When a value's type is a `pydantic.BaseModel` (or any other class with real attributes — `NamedTuple`, `dataclass`), read its fields with dot notation, not dict-style subscripting.

```python
# Not this:
check_no_unsafe_collision(rel_path, meta["sha256"])
for old_path in meta["supersedes"]:
    ...

# This:
check_no_unsafe_collision(rel_path, meta.sha256)
for old_path in meta.supersedes:
    ...
```

This is the reason rule 6 calls for `BaseModel` rather than `TypedDict` for a known shape: a `TypedDict` is a plain `dict` at runtime and *only* supports `["key"]` access — there's no `.attribute` form available for it, no matter how it was constructed. Getting real attribute access means the type actually needs to be a `BaseModel` (or `NamedTuple`/`dataclass`), not a matter of writing the access differently.

This rule is about a value whose type is a known model — it doesn't apply to a plain `dict[K, V]` (e.g. `declared[rel_path]` where `declared: dict[str, DeclaredMeta]` — the outer subscript is an ordinary dict lookup by key; it's `.sha256` on the `DeclaredMeta` result that this rule is about).

## 15. No bare-declare-then-unpack; return a `NamedTuple` instead

Python forbids annotations inside tuple unpacking, so a function returning `tuple[A, B]` forces callers into bare declarations followed by an unannotated unpack — which is rule 3 compliance at the price of readability:

```python
# Not this:
def load_state() -> tuple[int, dict[str, SyncedFileState]]: ...

old_seq: int
synced: dict[str, SyncedFileState]
old_seq, synced = load_state()

# This:
class SyncedState(NamedTuple):
    seq: int
    files: dict[str, SyncedFileState]

def load_state() -> SyncedState: ...

state: SyncedState = load_state()
```

When you reach for the bare-declare-then-unpack pattern, treat it as a smell: the function should return a `NamedTuple` (rule 6 already calls for this on positional shapes), giving callers one annotated variable and attribute access (rule 14) instead.

## 16. No compound `A and B` condition leading a branch chain

When a branch chain's first condition is a conjunction (`if A and B:`), every later branch carries the rest of the truth table implicitly — the reader has to reconstruct which combinations of A and B each `elif`/`else` actually covers. Nest instead, so every branch carries exactly one condition:

```python
# Not this:
if rel_path in staged_added and rel_path not in workspace_files:
    del staged_added[rel_path]
elif rel_path not in workspace_files:
    raise BearpondError(f"not tracked: {rel_path}")
else:
    stage_removal(rel_path)

# This:
if rel_path not in workspace_files:
    if rel_path in staged_added:
        del staged_added[rel_path]
    else:
        raise BearpondError(f"not tracked: {rel_path}")
else:
    stage_removal(rel_path)
```

The same applies to sequential guard raises — `if A and B: raise X` followed by `if A: raise Y` is the same truth table in disguise; nest both raises under one `if A:`. A compound condition in a *single* if with no chain is fine: there it's one logical test, not a decision tree the reader has to unfold. Nested chains also surface truth-table rows the compound form hides — a case that "can't happen" often turns out to be a case nobody handled.

## 17. No pass-through wrapper methods

A method whose only job is to call another method with an identical signature is not an abstraction — it's maintenance debt: every signature change must be made twice, and the wrapper invites the two names to drift apart. If the wrapped method should be public, make it public; if it shouldn't, call the private one directly. One of the pair must go.

```python
# Not this:
def _tracked_files(self) -> dict[str, FileState]: ...
def tracked_files(self) -> dict[str, FileState]:
    return self._tracked_files()

# This:
def tracked_files(self) -> dict[str, FileState]: ...
```

A method only earns a place on a class if it *adds* something — validation, error translation, coordination across collaborators. Forwarding unchanged is not adding something.

## 18. Public methods are porcelain; keep machinery private

A class's public methods should be the things you'd want a stranger to call: the operations the class exists for, plus read-only *reports* computed for consumers (e.g. `status()`). The data structures those operations and reports are built from — bookkeeping maps, raw state-file loads — are plumbing and stay private, even when a report doesn't literally duplicate them (a report may deliberately abstract over internal bookkeeping rather than expose it).

```python
# Not this: internals made public because one caller happened to want them
def staged_manifest(self) -> types.TransactionManifest: ...
def tracked_files(self) -> dict[str, FileState]: ...

# This: the report is the public read; the pieces behind it are private
def status(self) -> WorkspaceStatus: ...
def _staged_manifest(self) -> types.TransactionManifest: ...
def _tracked_files(self) -> dict[str, FileState]: ...
```

If a piece of internal state genuinely gains outside consumers, promote it then — but default to private. The report should be the only read most clients ever need.

## 19. Test the positive in an if/else

`if not A: foo; else: bar` is `if A: bar; else: foo` — but the second form is dramatically more readable, because the affirmative case comes first and the reader doesn't carry a negation across the branch. When an if has an else, phrase the test positively:

```python
# Not this:
if rel_path not in tracked:
    raise BearpondError(f"not tracked: {rel_path}")
else:
    stage_removal(rel_path)

# This:
if rel_path in tracked:
    stage_removal(rel_path)
else:
    raise BearpondError(f"not tracked: {rel_path}")
```

This is about if/else specifically: a guard clause with no else (`if not valid: raise`) is fine — there the negation *is* the exceptional case, and there's no branch for the reader to carry it across. `x is not None` also counts as positive phrasing (it tests "we have a value"), not a negative.

## 20. Cohesive stateful resources become a class

When a domain concept owns multiple related, mutable resources that are initialized together, shared by other entities, and torn down together, represent it as a class. Don't leave the resources as module-level globals accessed through module-level functions that merely pretend to be methods of an invisible object.

```python
# Not this: a set of globals masquerading as "the server"
_repos_root: Path | None = None
_metadata_store: MetadataStore | None = None
_object_store: ObjectStore | None = None
_repositories: dict[str, Repository] = {}

def configure(config: ServerConfig) -> None: ...
def get_repository(name: str) -> Repository: ...
def create_repository(name: str) -> Repository: ...
def close() -> None: ...

# This: the concept is explicit, state is encapsulated, and Repository can receive its Server as a collaborator
class Server:
    def __init__(self, config: ServerConfig) -> None:
        self.metadata_store: MetadataStore = SqliteMetadataStore(config.db_path)
        self.object_store: ObjectStore = ObjectStore(config.object_store_root)
        self._repos: dict[str, Repository] = {}

    def get_repository(self, name: str) -> Repository: ...
    def create_repository(self, name: str) -> Repository: ...
    def close(self) -> None: ...
```

Signals that a concept wants to be a class:

1. **Multiple pieces of mutable state always travel together** and must be initialized in a specific order.
2. **A lifecycle** (open/configure, use, close/recover) applies to the whole group atomically.
3. **Shared resources** are consumed by other domain objects that need a stable place to obtain them.
4. **You're about to swap implementations** (SQLite ↔ Postgres, local disk ↔ S3) and want the boundary behind a stable object rather than scattered global references.
5. **Tests repeatedly reconfigure the same bundle** of state, which forces awkward "reset the globals" teardown.

Pure behavior with no state can stay as module functions. A single global constant or configuration flag can stay as module data. But once the concept becomes "the thing that holds and manages these resources," make the thing real.

## 21. Constructor parameters and instance assignments share order

When a constructor takes parameters that become instance variables, assign those variables in the same order the parameters appear. Derived fields are grouped with the parameter they are derived from.

```python
# Not this: param 4 (obj_store) assigned before param 3 (store)
def __init__(self, name: str, repo_dir: Path, store: MetadataStore, obj_store: ObjectStore) -> None:
    self.name = name
    self.staging_root = repo_dir / "staging"
    self.manifest_root = repo_dir / "manifests"
    self.latest_pointer = self.manifest_root / "_latest"
    self.object_store = obj_store
    self.metadata_store = store

# This: assignments follow parameter order; derived fields stay with their source parameter
def __init__(self, name: str, repo_dir: Path, store: MetadataStore, obj_store: ObjectStore) -> None:
    self.name = name
    self.staging_root = repo_dir / "staging"
    self.manifest_root = repo_dir / "manifests"
    self.latest_pointer = self.manifest_root / "_latest"
    self.metadata_store = store
    self.object_store = obj_store
```

The goal is to make the parameter list, the constructor body, and the object's shape easy to scan together. When dependencies force a different order, keep direct parameter-to-attribute assignments in parameter order and place derived fields with their source.

## 22. Pass the domain object, not its decomposed fields

When a method operates on a domain concept that already has a named class (e.g. `Transaction`), pass the object itself rather than pulling its fields apart and sending them as separate parameters. The caller already has the object; decomposing it at the API boundary adds noise, invites parameter-order bugs, and makes later additions to the concept painful.

```python
# Not this:
self.metadata_store.create_transaction(txn.txn_uuid, txn.added, txn.removed, txn.updated)

# This:
self.metadata_store.store_transaction(txn)
```

The same applies when updating state derived from the object:

```python
# Not this:
self.repo.metadata_store.save_transaction_uploaded(self.txn_uuid, self.uploaded)

# This:
self.repo.metadata_store.save_transaction_uploaded(self)
```

**Exception:** operations that are purely about identity — e.g. `delete_transaction(txn_uuid)` or `get_transaction(txn_uuid)` — legitimately take only the identifier because the object does not yet exist (or no longer exists) on the called side. Once the callee needs the object's state, pass the object.
