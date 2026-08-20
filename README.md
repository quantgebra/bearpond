# bearpond

A small, transactional data lake — a pond, really — where bears (pandas, Polars) come to play.

bearpond is a data server and sync client for versioned collections of [Apache Parquet](https://parquet.apache.org/) files. It gives you the semantics of a [lakehouse](https://www.databricks.com/glossary/data-lakehouse) at single-machine scale: content-addressed storage, atomic commits, and point-in-time manifests — without running Spark, a metastore, or an object store.

## What it does

- **Transactional writes.** Clients add and remove files through an explicit transaction lifecycle: *begin* (declare the change), *upload*, then *commit* or *abort*. A commit is a single atomic metadata transaction — readers never observe a half-applied change, and no-op commits are rejected.
- **Content-addressed storage.** Every distinct file content is stored exactly once, named by its sha256 (`objects/ab/abcdef…`). The name *is* the content — insertion verifies the hash, so integrity is enforced, not assumed.
- **Versioned manifests.** Every commit produces a manifest — the full list of files in the lake at that version (`manifest-00000042.json`). Sync clients read the manifest, never directory listings, and can verify every byte they download.
- **Git-style commit chain.** Each commit record carries a `commit_hash` computed over the change, its parent, its timestamp, and its author — so the history is self-certifying and tamper-evident.
- **Safe sync.** `bearpond sync` maintains a local mirror of the lake in hive-partition layout, fetching only what changed and pruning what was removed — ready for pandas, Polars, DuckDB, or any Parquet reader.
- **Crash-safe by construction.** Immutable objects plus an atomic SQLite metadata commit mean a crash at any point leaves the lake either exactly on a version or cleanly retryable. Startup reconciles the exported manifests with the store automatically.

## Concepts

| Concept | What it is |
|---|---|
| **object** | a file's content, stored once under its sha256 in `objects/` |
| **path** | a file's logical location in the lake, e.g. `year=2024/month=01/part.parquet` (hive layout, `.parquet` only) |
| **transaction** | an in-flight unit of work (begin → upload → commit or abort); aborts leave no trace |
| **commit** | the permanent record of a committed transaction: `commit_hash`, `seq`, timestamp, author, full file list |
| **seq** | the lake's version number — increments on every commit |
| **manifest** | the snapshot of the lake at a seq — what sync clients consume |

Two rules keep the model honest: a path that exists cannot be added again (modification is a separate operation, compaction), and a removal is only safe against the exact content the client observed — so concurrent writers conflict loudly instead of corrupting silently.

## Quickstart

Requires Python 3.11+.

```bash
pip install bearpond   # or: pip install -e . from a clone
```

**Run the server.** Create a config directory with a `server.yaml`:

```yaml
# relative paths resolve against this file's directory
repo_root: ../my-pond
```

```bash
export BEARPOND_CONFIG_DIR=/path/to/config
uvicorn bearpond.server.api.app:app --port 8000
```

**Commit files.** Like git, staging is local and the server interaction is one short, atomic action — `add` records file metadata in a local staging file (`_staged.json`), and `commit` begins a transaction, uploads everything staged, and commits in one go:

```bash
export BEARPOND_SERVER=http://localhost:8000
bearpond add ./out/year=2024            # a directory expands to the .parquet files beneath it
bearpond rm ./out/year=2024/month=01/part.parquet   # stage a removal (offline, like add)
bearpond status                         # staged / untracked / modified / missing
bearpond commit -m "january trades"     # begin + upload + commit as one short transaction
```

**Sync a local mirror:**

```bash
bearpond sync --target ./mirror
# ./mirror now holds the lake in hive layout, tracked in ./mirror/_synced.json
```

**Auth:** the server checks a bearer token when `BEARPOND_TOKEN` is set (unset = open, for local use). The CLI reads the same variable. Real multi-user auth is on the roadmap (below).

## HTTP API

| Method & path | Purpose |
|---|---|
| `POST /transactions` | begin a transaction (declare added/removed files) |
| `PUT /transactions/{txn_uuid}/files/{path}` | upload one declared file's content |
| `POST /transactions/{txn_uuid}/commit` | commit (idempotent — safe to retry) |
| `DELETE /transactions/{txn_uuid}` | abort |
| `GET /transactions/{txn_uuid}` | transaction status (`open` / `committed`) |
| `GET /manifest` | the current manifest (files, sizes, sha256, seq) |
| `GET /files/{path}` | download a file, or list a prefix (`?limit=&cursor=`) |

Errors are JSON `{"detail": …}` with sensible statuses: 400 invalid, 401 unauthenticated, 404 unknown, 409 conflict with another writer.

## How it works

```
repo_root/
  objects/ab/abcdef…        # content-addressed files (the name is the sha256)
  staging/<txn_uuid>/       # in-flight uploads, invisible until commit
  manifests/                # exported audit trail: manifest-*.json, commits/*.json
  bearpond.sqlite           # the metadata store: path → object mapping, commit history
```

The **metadata store** (SQLite) is the system of record: one `objects` table holding every path ever added with the txn/seq that added and (eventually) removed it, and a `commits` table with the full, hash-chained history. The manifest files on disk are an export of that state — an audit trail and a rebuild source — not the source of truth themselves. Serving reads from the store, with history retained, is also what makes point-in-time queries possible (any manifest seq can be reconstructed).

A commit is *content before pointers*: staged files are placed into the object store first (idempotent — the name is the hash), then one atomic SQLite transaction flips every path mapping at once and appends the commit record.

Layout of this repo:

```
src/bearpond/
  types.py            # the wire protocol shared by server and client
  server/             # FastAPI app, repository, transaction, metadata store, object store
  client/             # sync/upload client + CLI
tests/                # pytest suite (domain, API, client end-to-end)
```

## Development

```bash
pip install -e ".[dev]"
python -m pytest        # 60+ tests: domain, API over live HTTP, end-to-end sync
```

## Roadmap

- **Multi-user auth** — per-user tokens and read/write roles (today: single shared bearer token)
- **Garbage collection** — reclaim objects no longer referenced by any retained version
- **Retention policies** — how long removed content stays recoverable
- **`rebuild-db`** — reconstruct the metadata store from the exported manifests/commits
- **Compaction** — the sanctioned way to modify an existing path

## License

TBD — see [LICENSE](LICENSE) when added. (Likely MIT; not yet decided.)
