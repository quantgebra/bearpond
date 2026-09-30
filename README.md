# bearpond

A small, transactional data lake — a pond, really — where bears (pandas, Polars) come to play.

bearpond is a central data server and sync client for versioned collections of files, built for teams. It gives you the semantics of a [lakehouse](https://www.databricks.com/glossary/data-lakehouse) without the lakehouse machinery. Bearpond includes content-addressed storage, atomic commits, and point-in-time manifests from one server process, a metadata store, and an object store — no Spark, no metastore. The default setup uses SQLite and local disk; the architecture is aimed at swapping in Postgres and S3 for HA deployments.

## Who it's for

bearpond is built for a small team with central data: a handful of ETL processes writing, a handful of analysts and system processes reading. The archetype is a research/quant team that has outgrown the usual small-team plumbing — ETL jobs dropping files into S3, Dropbox syncing them out to analysts, naming conventions holding it all together — and wants correctness (atomicity, integrity, history) without adopting the Spark/Iceberg/object-store stack to get it. Think: daily bars for 10K equity symbols, intraday data for futures, crypto, and prediction markets, produced by a fleet of batch jobs and consumed from desks and notebooks.

- **Writers** are batch processes making a handful of commits a day, each adding or removing whole files — yesterday's daily bars, the latest intraday partitions.
- **Readers** run `bearpond pull` to maintain a verified local mirror in hive-partition layout, then query with pandas, Polars, DuckDB, or any data tool. The mirror replaces the Dropbox share: incremental, content-verified, and always a complete, consistent version of the lake.

**The sweet spot: coarse files, not fine ones.** bearpond addresses and transfers whole files and records the full file list in every commit, so it rewards few large files over many tiny ones. Prefer `date=2026-08-21/equities.parquet` — all symbols in one columnar file, filtered by symbol at query time, which DuckDB and Polars do trivially — over one file per symbol per day. A handful of commits a day, each touching hundreds of files at most, is the design center.

## What it does

- **Transactional writes.** Clients add and remove files through an explicit transaction lifecycle: *begin* (declare the change), *upload*, then *commit* or *abort*. A commit is a single atomic metadata transaction — readers never observe a half-applied change, and no-op commits are rejected.
- **Content-addressed storage.** Every distinct file content is stored exactly once, named by its sha256 (`objects/ab/abcdef…`). The name *is* the content — insertion verifies the hash, so integrity is enforced, not assumed.
- **Versioned manifests.** Every commit produces a manifest — the full list of files in the lake at that version. The metadata store is the source of truth; any seq can be reconstructed on demand. Clients read the manifest, never directory listings, and can verify every byte they download.
- **Git-style commit chain.** Each commit record carries a `commit_hash` computed over the change, its parent, its timestamp, and its author — so the history is self-certifying and tamper-evident.
- **Safe pull.** `bearpond pull` maintains a local mirror of the lake in hive-partition layout, fetching only what changed and pruning what was removed — ready for pandas, Polars, DuckDB, or any data tool. Subset pulls let a workspace track only the partitions it needs.
- **Web UI.** A React interface for browsing repositories, manifests, and commit history is served by the same FastAPI process.
- **Crash-safe by construction.** Immutable objects plus an atomic metadata commit mean a crash at any point leaves the lake either exactly on a version or cleanly retryable. Objects are placed directly in the content-addressed store during upload, so there is no staging state to reconcile on startup.

## Why bearpond — and the alternatives

The honest comparison for a small team centralizing its data:

| Approach | Where it falls short for this use case |
|---|---|
| **S3 + Dropbox + conventions** (the usual status quo) | Readers can observe a half-written day; concurrent writers interleave silently; the sync tool mirrors bytes without knowing what a complete dataset looks like; no integrity checks, no history |
| **S3 + DuckDB/pandas, queried in place** | No atomic multi-file commits and no versions; safe concurrent writers need a locking story you build yourself |
| **Delta Lake (delta-rs)** | Table-format machinery you may not need; safe multi-writer on plain storage still requires external locking infrastructure |
| **LakeFS** | The right semantics (git-for-data), but real operational weight — its own metadata store and gateway — aimed at object-store scale |
| **Iceberg / DuckLake** | Table-oriented catalogs whose readers need engine/catalog support; more machinery than a versioned file collection needs |

bearpond's side of the trade: atomic all-or-nothing commits across many files, loud 409 conflicts instead of silent corruption, content-verified mirrors, and a tamper-evident hash-chained history — from a server process you can read end to end.

## What bearpond is not

- **A query engine.** The read model is pull-then-query-locally; there is no server-side SQL. At this scale that's a feature — local DuckDB over a pulled mirror is faster than any network query — but it means everyone works from a mirror.
- **A table format.** No schema enforcement or evolution, no `VERSION AS OF` inside your DataFrame library — bearpond versions *files*, not tables. If you need query-engine-integrated time travel, that's Delta or Iceberg.

## Concepts

| Concept | What it is |
|---|---|
| **object** | a file's content, stored once under its sha256 in `objects/` |
| **path** | a file's logical location in the lake, e.g. `year=2024/month=01/part.parquet`. Hive-style `key=value` directories are recommended for subset pulls but not required. |
| **transaction** | an in-flight unit of work (begin → upload → commit or abort); aborts drop the transaction state and leave uploaded objects as orphans for GC |
| **commit** | the permanent record of a committed transaction: `commit_hash`, `seq`, timestamp, author, full file list |
| **seq** | the lake's version number — increments on every commit |
| **manifest** | the snapshot of the lake at a seq — what clients consume |

Three rules keep the model honest: a path can only be added if it does not already exist; a removal is only safe against the exact content the client observed; and an update is only safe against the exact old content the client observed, producing a new content pointer under the same path — so concurrent writers conflict loudly instead of corrupting silently. Compaction creates new aggregate files under new paths and removes the old ones.

## Quickstart

Requires Python 3.11+.

```bash
pip install bearpond            # client only — clone, add, commit, pull (or: pip install -e . from a clone)
pip install bearpond[server]    # + FastAPI/uvicorn/PyYAML — needed to run the server itself
```

**Run the server.** Create a config directory with a `server.yaml` pointing at the server's two storage locations — one server hosts many repositories, all sharing one metadata database and one object store:

```yaml
# relative paths resolve against this file's directory; these are also the defaults
db_path: bearpond.sqlite
object_store_root: objects
```

```bash
export BEARPOND_CONFIG_DIR=/path/to/config
uvicorn bearpond.server.api.app:app --port 8000
```

On Windows, set the variable with `set BEARPOND_CONFIG_DIR=C:\path\to\config` (cmd.exe) or `$env:BEARPOND_CONFIG_DIR = "C:\path\to\config"` (PowerShell). bearpond runs on Linux, macOS, and Windows; on Windows, directory-level fsync durability is unavailable and skipped (file-level fsyncs and SQLite transactions are unaffected).

**Create a repository and clone it.** Workspaces are always bound to a remote repo — `clone` is the only way one comes into being, and afterwards no command needs a `--server` flag (the binding lives in `.bearpond/config.json`):

```bash
bearpond repo-create http://localhost:8000/trades
bearpond clone http://localhost:8000/trades ./trades
cd trades

# clone straight into a filtered view instead of cloning full and then pulling with a query —
# clone accepts the same --query syntax as pull (see "Pull a subset (a filtered view)" below)
bearpond clone http://localhost:8000/trades ./trades-2024-01 --query month=01
```

**Commit files.** Like git, staging is local and the server interaction is one short, atomic action — `add` records file metadata in the staging area (`.bearpond/staged.json`), and `commit` begins a transaction, uploads everything staged, and commits in one go. All run from inside the cloned workspace:

```bash
bearpond add ./out/year=2024            # a directory expands to the files beneath it
bearpond add ./out/year=2024/month=01/part.parquet  # re-adding a tracked file stages an update
bearpond add ./out/year=2024/month=01/data.csv      # CSV, JSON, ORC, raw text, etc. are all valid
bearpond rm ./out/year=2024/month=01/part.parquet   # stage a removal (offline, like add)
bearpond status                         # staged add/remove/update / untracked / modified / missing / pending
bearpond commit -m "january trades"     # begin + upload + commit as one short transaction
bearpond log --limit 10                 # commit history: hash, seq, author, message
```

**Pull the latest:**

```bash
bearpond pull
# the workspace now holds a verified local mirror, with .bearpond/manifest.json
# recording exactly which server manifest it represents
```

**Pull a specific version:**

```bash
bearpond pull --seq 42
# mirror the lake as of manifest seq 42 instead of the latest
```

**Pull a subset (a filtered view):**

```bash
bearpond pull --query month=01
# workspace now tracks only paths whose logical path contains month=01
# files that no longer match are pruned; the saved query is in .bearpond/query.json

bearpond pull --query month=01 day=15
# multiple terms are ANDed: the path must contain every segment

bearpond pull --query month=01 --query month=02
# repeat --query to OR whole groups: paths matching month=01 OR month=02

bearpond pull --query trades 2024-08-22
# arbitrary paths work too; each term must appear as a /-separated segment

bearpond pull --query
# switch back to a full mirror (no filter)

bearpond pull
# re-sync the current saved view (full mirror or filtered view)
```

Switching queries or `--seq` requires a clean workspace — no in-progress pull and no staged changes. The workspace records the full server manifest in `.bearpond/manifest.json`, the current query in `.bearpond/query.json`, and the paths it has taken responsibility for in `.bearpond/pulled.json`.

Hive-style `key=value` directories are the most useful convention for subset pulls, but they are not required.

**Auth:** the server checks a bearer token when `BEARPOND_TOKEN` is set (unset = open, for local use). The CLI reads the same variable. Real multi-user auth is on the roadmap (below).

## Web UI

bearpond ships with a small React web interface for browsing repositories, manifests, and commit history. It is served by the same FastAPI process on `/`.

**Production use:** build the frontend once and restart the server:

```bash
cd web
npm install
npm run build
```

The build output lands in `src/bearpond/server/static/`, which FastAPI serves. Then start the server normally:

```bash
export BEARPOND_CONFIG_DIR=/path/to/config
uvicorn bearpond.server.api.app:app --port 8000
```

Open `http://localhost:8000/` to browse repositories.

**Development:** run the FastAPI backend and Vite dev server side by side. The dev server proxies API calls to the backend:

Terminal 1:
```bash
export BEARPOND_CONFIG_DIR=/path/to/config
uvicorn bearpond.server.api.app:app --port 8000
```

Terminal 2:
```bash
cd web
npm run dev
```

Open `http://localhost:5173/` for live-reload development.

## HTTP API

| Method & path | Purpose |
|---|---|
| `GET /repos` | list repositories |
| `POST /repos` | create a repository (`{"name": …}`) |
| `POST /repos/{repo}/transactions` | begin a transaction (declare added/removed/updated files, plus `user`/`reason`) |
| `PUT /repos/{repo}/transactions/{txn_uuid}/files/{path}` | upload one declared file's content |
| `POST /repos/{repo}/transactions/{txn_uuid}/commit` | commit (idempotent — safe to retry) |
| `DELETE /repos/{repo}/transactions/{txn_uuid}` | abort |
| `GET /repos/{repo}/transactions/{txn_uuid}` | transaction status (`open` / `committed`) |
| `GET /repos/{repo}/manifest` | the current manifest (files, sha256, sizes, seq) |
| `GET /repos/{repo}/commits` | commit history, newest first (`?limit=&before_seq=`) |
| `GET /repos/{repo}/files/{path}` | download a file, or list a prefix (`?limit=&cursor=`) |

Errors are JSON `{"detail": …}` with sensible statuses: 400 invalid, 401 unauthenticated, 404 unknown, 409 conflict with another writer.

## How it works

```
bearpond.sqlite             # server-wide metadata store: repo → path → object mapping, hash-chained commit
                            # history, and in-flight transaction state (added/removed/updated/uploaded files)
objects/ab/abcdef…          # server-wide content-addressed object store (the name is the sha256)
```

The **metadata store** (SQLite) is the system of record: one database per server, with `objects` and `commits` tables scoped by repository. It holds every path ever added with the txn/seq that added and (eventually) removed it, plus the full hash-chained commit history. Manifests are reconstructed from the store on demand, not exported to disk. Serving reads from the store, with history retained, is what makes point-in-time queries possible (any manifest seq can be reconstructed).

A commit is *content before pointers*: files are streamed directly to the content-addressed object store during upload (idempotent — the name is the hash), then one atomic metadata transaction flips every path mapping at once and appends the commit record. Aborted transactions leave orphan objects; a background garbage collector reclaims any object not referenced by a retained version.

Layout of this repo:

```
src/bearpond/
  types.py            # the wire protocol shared by server and client
  server/             # FastAPI app, repository, transaction, metadata store, object store, static UI files
  client/             # workspace client + CLI (add/rm/commit/pull/status)
web/                  # React + TypeScript web UI source (Vite)
tests/                # pytest suite (domain, API, client end-to-end)
```

## Development

```bash
pip install -e ".[dev]"
python -m pytest        # 115 tests: domain, API over live HTTP, end-to-end client flows
```

The web UI lives in `web/` and uses Vite + React + TypeScript. To work on it:

```bash
cd web
npm install
npm run dev             # serves the UI on http://localhost:5173 with API proxy to :8000
```

Build it before packaging or testing the served version:

```bash
cd web
npm run build           # outputs to src/bearpond/server/static/
```

## Roadmap

- **Multi-user auth** — per-user tokens and read/write roles (today: single shared bearer token)
- **Garbage collection** — reclaim objects no longer referenced by any retained version
- **Retention policies** — how long removed content stays recoverable
- **Compaction** — combine smaller files into larger ones by adding new aggregate paths and removing the now-redundant small ones
- **Pluggable backends** — Postgres metadata store and S3 object store for stateless HA
- **Point-in-time pull** — `pull --seq N` to mirror the lake as of any version (the store already reconstructs any seq server-side) *(implemented)*
- **Subset pull** — `pull --query key=value ...` to mirror only files whose path contains the given segments *(implemented)*
- **Web UI** — React interface for browsing repositories, manifests, and commit history *(scaffolded; admin and user management via the UI is future work)*

## License

TBD — see [LICENSE](LICENSE) when added. (Likely MIT; not yet decided.)
