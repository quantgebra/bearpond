# bearpond-server

The server for [bearpond](https://github.com/quantgebra/bearpond), a small transactional data lake: versioned collections of files with atomic commits, content-addressed storage, and verified local mirrors.

One server process hosts many repositories, all sharing one SQLite metadata database and one content-addressed object store, and serves the web UI and HTTP API described below. Clients use the [`bearpond`](https://github.com/quantgebra/bearpond/tree/main/packages/client) package.

Requires Python 3.11+.

```bash
pip install bearpond-server     # FastAPI, uvicorn, PyYAML; also installs bearpond-protocol and the bearpond-server command
```

## Running the server

Create a config directory with a `server.yaml` pointing at the server's two storage locations — one server hosts many repositories, all sharing one database and one object store:

```yaml
# relative paths resolve against this file's directory; these are also the defaults
db_path: bearpond.sqlite
object_store_root: objects

# optional: everything the server keeps in a database (repositories, commits, users and logins) is in one database,
# by default the SQLite file at db_path; PostgreSQL needs `pip install "bearpond-server[postgres]"`
# database_url: postgresql+psycopg://user:password@host/database
```

```bash
bearpond-server serve --config-dir /path/to/config --port 8000
```

`--config-dir` can be left out when `BEARPOND_CONFIG_DIR` names the folder, and `--host` is `127.0.0.1` unless you give another. `BEARPOND_DATABASE_URL`, when it is set, takes the place of `database_url` in `server.yaml`, so that a PostgreSQL password can be kept in the environment rather than in a file. The server logs to standard error, with the time on every line. `uvicorn bearpond.server.api.app:app` with `BEARPOND_CONFIG_DIR` set also runs it, for development.

`GET /health` answers `{"status": "ok"}` when the server and its database are up. It needs no login, so whatever starts the server can wait on it.

On Windows, set the variable with `set BEARPOND_CONFIG_DIR=C:\path\to\config` (cmd.exe) or `$env:BEARPOND_CONFIG_DIR = "C:\path\to\config"` (PowerShell). bearpond runs on Linux, macOS, and Windows; on Windows, directory-level fsync durability is unavailable and skipped (file-level fsyncs and SQLite transactions are unaffected).

## Authentication

By default the server lets everyone in, which suits local use. To make people log in, add an `auth` section to `server.yaml`:

```yaml
auth:
  mode: enforced                          # none (the default) or enforced
  base_url: https://bearpond.example.com  # the address people reach the server at; links and redirects use it
  session_duration_minutes: 480
  secrets_dir: auth-secrets               # the client secrets of OIDC providers, one file for each
```

Authentication comes from [`quantgebra-auth`](https://github.com/quantgebra/bearpond/tree/main/packages/auth).

- **First administrator.** A server with no accounts makes the account `admin` when it starts and writes a one-time link to its log: open it to choose the password. The link is made again on every start until it has been used.
- **When nobody can log in,** the person who runs the server makes links on the server's machine, with the same config folder: `bearpond-server auth --config-dir DIR first-administrator` (a new link for `admin`), `... create-invite NAME --group GROUP` (a new account), and `... reset-link NAME` (a new password).
- **Permissions.** `read` and `write` can be limited to one repository, and `admin` does everything: it is the only permission that makes a repository. Whoever has any permission may see the server. Out of the box the group `viewers` reads every repository, `writers` reads and writes every repository (and may revert), and `administrators` administer; administrators can add groups and change the rules.
- **Authors.** A person who is logged in is the author of what they commit, whatever the client sends as the user.
- **Logging in.** The endpoints are under `/auth` (`options`, `login`, `logout`, `me`, `password`, `invite/{token}` and, for OIDC providers, `oidc/{provider}/login`). A person who is logged in has a cookie, and anything that changes something also sends the `X-CSRF-Token` that `login` and `me` return.
- **Scripts, the command line and CI** send an API key as `Authorization: Bearer bp_…` (the command line saves one with `bearpond login <server>`). People make keys for themselves at `/account/keys`; administrators make them for anyone there, and make service accounts (users for programs, which cannot log in) for CI. A key may do what its user's groups may, can be given an expiry, and is revoked on the same page. The older shared token still works: the server reads it from `BEARPOND_TOKEN`, and whoever sends it is an administrator. With `mode: none` and that variable set, the server accepts only the shared token, as it always has; API keys need `mode: enforced`.

With `mode: enforced` the web UI sends anyone who is not logged in to `/login`. Invitation and reset links open `/invite/{token}`, where the person chooses a password, and `/account` lets them change it.

## Web UI

bearpond-server makes its web pages itself, from Jinja2 templates shipped in the package, so there is nothing to build. Once the server is running, open `http://localhost:8000/`. The pages are `/repositories` (every repository you may read, with its size and last commit), `/repositories/{name}` (its directories and files, browsed a directory at a time, with download links and the latest commits), `/repositories/{name}/commits` and `/repositories/{name}/commits/{seq}` (the history, and what one commit added, removed and changed), plus `/login`, `/invite/{token}`, `/account` and `/account/keys` (API keys). Administrators also see `/admin/users` (invite people and hand them the one-time link, set their groups, make password-reset links, turn accounts off and on), `/admin/groups` (add, describe and remove groups, and map an OIDC provider's groups onto them) and `/admin/access` (the rules that give a group read, write or admin, for every repository or one; the OIDC providers with their client secrets; how long a login lasts and whether accounts of this server may log in). A change that would leave nobody able to administer is refused. How the pages are built is covered in the [project README](https://github.com/quantgebra/bearpond#development).

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

## Releases

`scripts/build_release.py` builds a release of the server: one archive, `dist/release/bearpond-server-<version>-<stamp>.tgz`, that holds every wheel the server needs, so installing it downloads nothing. Run the script with the Python the server will run on, on the platform it will run on, because some of the wheels are compiled for one Python and one platform:

```bash
python3.11 scripts/build_release.py                                       # dist/release/
python3.11 scripts/build_release.py --package-repository ../yarddog-packages  # also put it where YardDog finds it
```

The archive holds `wheels/`, `requirements.txt` (each wheel pinned by its hash), `BUILD-INFO` (the version, the git commit and whether the tree had uncommitted changes, the Python and the platform), `INSTALL` (installing by hand), `LICENSE` and `NOTICE`. To install it, make a venv with that Python and run `pip install --no-index --find-links wheels --require-hashes -r requirements.txt`. YardDog's deploy of BearPond does that once for each release, under `releases/<version>/`, writes `server.yaml` from the environment's variables, and keeps the database, the objects and the secrets of login providers in `data/`, which outlives every release.

## More

See the [project README](https://github.com/quantgebra/bearpond#readme) for what bearpond is, the concepts behind it, and the roadmap.

## License

Apache License 2.0.
