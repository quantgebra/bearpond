# bearpond

The client and command-line tool for [bearpond](https://github.com/quantgebra/bearpond), a small transactional data lake: versioned collections of files with atomic commits, content-addressed storage, and verified local mirrors.

`bearpond` talks to a bearpond server over HTTP. It is an independent package that shares only the wire-format types in `bearpond-protocol`; installing it never pulls in any server code. To run a server, install [`bearpond-server`](https://github.com/quantgebra/bearpond/tree/main/packages/server).

Requires Python 3.11+.

```bash
pip install bearpond
```

## Usage

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

**Revert to a prior version.** `revert` records a brand-new commit whose content matches an earlier manifest version — like `git revert`, not `git reset`: every commit in between stays in the history untouched, nothing is rewritten or deleted. No file bytes move over the wire — content is content-addressed, so the server already has everything it needs:

```bash
bearpond revert --seq 42 -m "rolling back a bad ingest"
# creates a new commit that restores the lake to how it looked at manifest-00000042;
# run `bearpond pull` afterward to update your local workspace to the new commit
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

**Auth:** the server checks a bearer token when `BEARPOND_TOKEN` is set (unset = open, for local use). The CLI reads the same variable. Real multi-user auth is on the roadmap.


## More

See the [project README](https://github.com/quantgebra/bearpond#readme) for what bearpond is, the concepts behind it, and the alternatives it is compared against.

## License

Apache License 2.0.
