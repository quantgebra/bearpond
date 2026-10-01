# bearpond

A small, transactional data lake — a pond, really — where bears (Panda and Polar) come to play.

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
pip install bearpond            # client only: clone, add, commit, pull, revert
pip install bearpond-server     # the server, a separate distribution
```

```bash
export BEARPOND_CONFIG_DIR=/path/to/config          # holds server.yaml
uvicorn bearpond.server.api.app:app --port 8000

bearpond repo-create http://localhost:8000/trades
bearpond clone http://localhost:8000/trades ./trades
cd trades
bearpond add ./out/year=2024
bearpond commit -m "january trades"
bearpond pull
```

Full instructions live with each package, see below.

## Packages

bearpond is three independent distributions. The client and server share only the wire-format types.

| Package | PyPI name | What it is |
|---|---|---|
| [`packages/client`](packages/client) | `bearpond` | workspace client and CLI: add, rm, commit, pull, revert, status, log |
| [`packages/server`](packages/server) | `bearpond-server` | FastAPI server, metadata and object stores, web UI, HTTP API, configuration |
| [`packages/protocol`](packages/protocol) | `bearpond-protocol` | the wire format shared by client and server |

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
packages/
  protocol/src/bearpond/protocol/  # the wire protocol shared by server and client (bearpond-protocol on PyPI)
  server/src/bearpond/server/      # FastAPI app, repository, transaction, metadata store, object store, static UI files (bearpond-server)
  client/src/bearpond/client/      # workspace client + CLI (add/rm/commit/pull/revert/status) (bearpond)
web/                  # React + TypeScript web UI source (Vite)
tests/                # pytest suite (domain, API, client end-to-end) — spans all three packages
```

## Development

```bash
pip install -e packages/protocol -e packages/server -e "packages/client[dev]"
python -m pytest        # 130 tests: domain, API over live HTTP, end-to-end client flows
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
npm run build           # outputs to packages/server/src/bearpond/server/static/
```

## Roadmap

- **Multi-user auth** — per-user tokens and read/write roles (today: single shared bearer token)
- **Garbage collection** — reclaim objects no longer referenced by any retained version
- **Retention policies** — how long removed content stays recoverable
- **Compaction** — combine smaller files into larger ones by adding new aggregate paths and removing the now-redundant small ones
- **Pluggable backends** — Postgres metadata store and S3 object store for stateless HA
- **Web UI administration** — admin and user management in the web UI (browsing repositories, manifests, and commit history already ships)

## License

Apache License 2.0 — see [LICENSE](LICENSE).
