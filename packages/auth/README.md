# quantgebra-auth

Authentication and authorization for quantgebra's Python services: accounts of the service itself, logging in through
OIDC providers, sessions, groups, and the rules that turn groups into permissions. It is the Python counterpart of the
auth of YardDog; `bearpond-server` uses it, and BullPen is meant to.

Requires Python 3.11+.

```bash
pip install quantgebra-auth             # the library
pip install "quantgebra-auth[fastapi]"  # and the FastAPI integration
```

## What it does

- **Accounts of the service.** A user name and a password, kept as an argon2id hash. Passwords need 12 characters and
  nothing else. A wrong password and an unknown name are refused in the same words and take the same time, and too many
  failures lock the name, and the address, for a while. People choose their passwords with single-use links, which an
  administrator makes for them, and which are stored as hashes.
- **The first administrator.** A new service with no accounts makes the account `admin`, in the group of
  administrators, and a link to choose its password with, which it hands to the person who runs the service. Nothing is
  taken from the environment, and the link is made again on every start until it has been used.
- **OIDC providers.** The authorization code flow with PKCE, a state and a nonce; the id token is checked (signature,
  issuer, audience, expiry, nonce, and only asymmetric algorithms). Where the groups, the user name and the rest are in
  the token is set for each provider. A person is known by the issuer and what it calls them, never by an email address.
  Client secrets are entered by an administrator and kept by the service in a file of its own named after the provider,
  never in the database and never shown again.
- **Sessions.** A random secret in an `HttpOnly`, `SameSite=Lax` cookie, of which only a hash is stored. Anything that
  changes something needs the `Origin` of the service and the secret of the login in `X-CSRF-Token`.
- **API keys and service accounts.** A script or CI sends a key as `Authorization: Bearer <key>` instead of logging
  in. A key stands for a user and acts with the groups that an administrator gave that user (never ones a provider
  sent, since there is no login); it needs no `X-CSRF-Token`, because a browser never sends it on its own. Every key
  starts with the prefix the application sets in `AuthConfig` (BearPond's is `bp_`), only a hash of it is kept, it can
  be made to expire, and revoking it takes effect at once. People make keys for themselves; administrators make them
  for anyone, and make service accounts: users for programs, which have no password and cannot log in. These are in
  `apikey/`, which uses only `core`.
- **Groups and rules.** The groups are the service's own. What a provider calls a group is mapped to them, and
  administrators can put users in them too. Rules give a group permissions, optionally for one subject (write, for
  one repository). What a person may do is worked out on every request, so a change takes effect at once.

## Concepts

Who may do what comes down to a few small pieces. Following one request through them is the quickest way to see how
they fit.

**Permission.** One thing a person can be allowed to do, such as `read`, `write` or `admin`. The application decides
what permissions there are; the library has none of its own. Each is a `PermissionDefinition`: a `name`, a `label` and
`description` for pages to show, and `subject_kinds`, the kinds of thing it can be limited to (`["repository"]` for
`write`, nothing for `admin`, which is about everything).

**PermissionDefinitionCatalog.** The application's complete list of permissions, plus the two that have a special meaning:

- the **administrator permission** (`admin_name`), which covers everything everywhere, whatever is asked for;
- the **baseline permission** (`baseline_name`, such as `view`), which anyone who holds any grant at all holds too. It
  answers "may this person see the service?", so someone who has been given nothing yet can log in but sees nothing.
  It is optional: `None` means there is no such thing.

The catalog is what the library checks a rule against. A rule that names a permission that does not exist, or limits it
to a kind of subject that it cannot be limited to, is refused when an administrator saves it. The catalog is also what
turns a grant into a yes or a no.

**Grant.** A permission that has been given, optionally for one subject only: `Grant(permission="write")` is for every
repository, and `Grant(permission="write", subject="alpha", subject_kind="repository")` is for `alpha` alone. The
subject and its kind go together or not at all. What a subject is, and what its id looks like, is the application's: a
repository name here.

**Group.** A name that rules give permissions to and that people are put in. The groups are the service's own. People
get into them in two ways: an administrator puts them there, or a provider's group is **mapped** to one (the group
`idp-admins` at the OIDC provider to the group `administrators` here), so that the same rules apply to everyone, however
they logged in.

**GrantRule.** "The people in this group are given these grants." The rules are kept in the database and changed by
administrators. The application supplies the rules that a new service starts with.

**Actor.** The answer to "who is making this request, and what may they do?": the user, the groups they are in right
now, and the grants that the rules give those groups. It is worked out again on every request, never stored, so a change
to a user's groups or to the rules takes effect on their next request. `actor.can("write", "alpha")` is how the
application asks.

### How a request gets decided

```
cookie  -->  login (session)  -->  user  -->  groups  -->  grants  -->  actor.can(...)
                                               ^  put there by an administrator
                                               ^  mapped from the provider's groups, as sent when the person logged in
```

`can(permission, *subject_ids)` is true when any grant of the actor covers the permission asked for:

1. a grant of the administrator permission covers anything;
2. any grant at all covers the baseline permission;
3. otherwise the grant has to be for that very permission, and either have no subject (every subject) or name one of the
   subjects given.

### An example

The catalog of `bearpond-server`: `view` is the baseline, `admin` is the administrator permission, and `read` and `write`
can be limited to a repository. The rules of a new server: `viewers` get `read`, `writers` get `read` and `write`, and
`administrators` get `admin`. An administrator then adds a group `alpha-team` with `Grant("write", subject="alpha",
subject_kind="repository")`.

| person is in | `can("view")` | `can("read", "beta")` | `can("write", "alpha")` | `can("write", "beta")` | `can("admin")` |
|---|---|---|---|---|---|
| no group | no | no | no | no | no |
| `viewers` | yes | yes | no | no | no |
| `alpha-team` | yes | no | yes | no | no |
| `writers` | yes | yes | yes | yes | no |
| `administrators` | yes | yes | yes | yes | yes |

Someone in `alpha-team` can see the service (they hold a grant), write to `alpha`, and nothing else.

## Storage

Everything is kept with SQLAlchemy 2.0 in a schema of its own, `auth`, so the tables can share a PostgreSQL database
with the application's. The application owns the engine and the sessions:

- **Queries are on the entities.** Every query against a table is a static method of its entity class, such as
  `UserRecord.get_by_issuer_and_username(session, issuer, username)`, and there are no repository classes in between.
- **One session for each request.** `AuthSystem.bind(session)` makes the services over the session of the request. The library flushes what it changes and never commits, so whoever made the session decides when everything the
  request did is kept, and it is kept or thrown away together with the application's own work if they share the session.
- **Migrations are part of the package**, as an Alembic branch with its own version table (`auth.auth_alembic_version`).
  `auth_schema.upgrade(engine)` brings the tables up to date and makes them when there are none, so it can run at every
  start, next to the application's own migrations.
- **PostgreSQL and SQLite.** PostgreSQL is what a service runs on. SQLite has no schemas, so `auth_schema.make_sqlite_engine(url)`
  makes an engine that leaves the schema out and keeps to foreign keys, for local use and tests. The tests run on both,
  and need docker for the PostgreSQL half (they are skipped without it).
- **Moments** are stored in UTC and come back with their time zone, on both; a moment that does not say its time zone is
  refused.

## Using it

The permissions are the application's: it says what there is, and which one is the administrator's.

```python
catalog = permission_definition_catalog.PermissionDefinitionCatalog(
    [
        permission_definition.PermissionDefinition(
            name="read", label="Read", description="read repositories", subject_kinds=["repository"]
        ),
        permission_definition.PermissionDefinition(
            name="write", label="Write", description="write repositories", subject_kinds=["repository"]
        ),
        permission_definition.PermissionDefinition(name="admin", label="Administer", description="everything"),
    ],
    admin_name="admin",
    baseline_name="read",  # anyone who may do something may see it
)
config = auth_config.AuthConfig("enforced", "https://data.example.com", "administrators", catalog, 480, True, rules)

engine = sqlalchemy.create_engine("postgresql+psycopg://user:password@host/database")
auth_schema.upgrade(engine)
system = auth_system.AuthSystem(config, Path("auth-secrets"))
outcome = system.start(engine)  # tell the person who runs the service about outcome.link, when there is one


def get_services() -> Iterator[auth_services.AuthServices]:
    with orm.Session(engine, expire_on_commit=False) as session:
        try:
            yield system.bind(session)
            session.commit()
        except BaseException:
            session.rollback()
            raise


gate = auth_gate.AuthGate()
app.include_router(auth_router.AuthRouter(get_services, lambda: gate).router)


def require_write(request: Request, services: auth_services.AuthServices = Depends(get_services)):
    return gate.check_permission(request, services, "write", "repo_name")


@app.post("/repos/{repo_name}")
def write_repository(repo_name: str, found=Depends(require_write)): ...
```

`AuthRouter` takes the dependency that makes the services of a request and a function that returns the gate, which it
calls on every request, so that an application that is configured when it first has something to do can make it at
import. It makes the endpoints under `/auth`: `options`, `login`, `logout`, `me`, `password`, `invite/{token}`, and
`oidc/{provider}/login` and `oidc/{provider}/callback`. What the pages for logging in and for choosing a password look
like is the application's, and the links that the library makes point at `/invite/{token}` of the service's own address.

An application that has a page for logging in passes its path as `login_page_path`. A login through a provider that
does not work then sends the person back to that page as `{login_page_path}?error={code}&next={path}` instead of
answering with JSON, and the page shows `LOGIN_FAILURE_MESSAGES[code]`. The codes are `failed`, `expired`, `cancelled`,
`disabled` and `unknown_provider`. Only a code travels in the address, so nobody can put words of their own on the page
with a link.

Reading environment variables and command line flags is the application's too: the library takes values, never names of
variables. `AuthTool` is the program for the person who runs the service (`create-invite`, `reset-link`,
`first-administrator`), which an application calls with the arguments of its own command.

## Layout

```
core/    the entities and the schema, users, sessions, groups, grants, rules, settings; knows no way of logging in
local/   passwords, invitations and the first administrator; uses core
oidc/    providers, their secrets, and logging in through them; uses core
apikey/  api keys and service accounts; uses core
http/    FastAPI cookies, the gate and the endpoints; uses all of them
```

## Not yet

- Groups that the service makes for what it holds, such as one for each repository.

## License

Apache License 2.0.
