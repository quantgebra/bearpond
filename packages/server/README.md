# bearpond-server

The server for [bearpond](https://github.com/quantgebra/bearpond), a small transactional data lake: versioned collections of files with atomic commits, content-addressed storage, and verified local mirrors.

One server process hosts many repositories, all sharing one SQLite metadata database and one content-addressed object store, and serves the web UI and HTTP API described below. Clients use the [`bearpond`](https://github.com/quantgebra/bearpond/tree/main/packages/client) package.

Requires Python 3.11+.

```bash
pip install bearpond-server     # FastAPI, uvicorn, PyYAML; also installs bearpond-protocol
```

## Running the server

Create a config directory with a `server.yaml` pointing at the server's two storage locations — one server hosts many repositories, all sharing one metadata database and one object store:

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

**Auth:** the server checks a bearer token when `BEARPOND_TOKEN` is set (unset = open, for local use). The CLI reads the same variable. Real multi-user auth is on the roadmap.

## Web UI

bearpond ships with a small React web interface for browsing repositories, manifests, and commit history. It is bundled in the package and served by the same FastAPI process on `/`, so once the server is running, open `http://localhost:8000/`. Working on the UI itself is covered in the [project README](https://github.com/quantgebra/bearpond#development).

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

## Storage

```
bearpond.sqlite             # server-wide metadata store: repo → path → object mapping, hash-chained commit
                            # history, and in-flight transaction state (added/removed/updated/uploaded files)
objects/ab/abcdef…          # server-wide content-addressed object store (the name is the sha256)
```

The metadata store is the system of record, and a commit is *content before pointers*: files are streamed into the object store during upload, then one atomic metadata transaction flips every path mapping and appends the commit record. The [project README](https://github.com/quantgebra/bearpond#how-it-works) explains the design in more detail.

## More

See the [project README](https://github.com/quantgebra/bearpond#readme) for what bearpond is, the concepts behind it, and the roadmap.

## License

Apache License 2.0.
