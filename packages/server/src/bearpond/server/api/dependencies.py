from collections.abc import Iterator

from fastapi import Depends, Request
from quantgebra.auth import auth_services
from quantgebra.auth.http import auth_gate
from sqlalchemy import orm

from .. import repository
from .. import server as server_module
from .. import server_auth


# ----------------------------------------------------------------------------------------------------------------------
def get_server() -> server_module.Server:
    # the running server process has a single configured Server instance; auto-configures from BEARPOND_CONFIG_DIR on
    # first use if nothing has called configure() explicitly
    return server_module.get_server()


# ----------------------------------------------------------------------------------------------------------------------
def get_session() -> Iterator[orm.Session]:
    # the session of the request is made here and shared by everything that needs it, authentication and the repository
    # alike, so that what the request did is kept together when it has gone well and thrown away when it has not; every
    # use of it is Depends(get_session, scope="function"), which commits before the response is sent, since by default
    # FastAPI closes it after, and a client could then send its next request before what this one did was saved
    server: server_module.Server = server_module.get_server()
    with orm.Session(server.engine, expire_on_commit=False) as session:
        try:
            yield session
            session.commit()
        except BaseException:
            session.rollback()
            raise


# ----------------------------------------------------------------------------------------------------------------------
def get_auth_services(
    session: orm.Session = Depends(get_session, scope="function"),
) -> auth_services.AuthServices:
    return server_module.get_server().auth.system.bind(session)


# ----------------------------------------------------------------------------------------------------------------------
def get_repository(
    repo_name: str, session: orm.Session = Depends(get_session, scope="function")
) -> repository.Repository:
    # the repository that the request is about, over the session of the request
    return server_module.get_server().get_repository(session, repo_name)


# ----------------------------------------------------------------------------------------------------------------------
def get_auth_gate() -> auth_gate.AuthGate:
    return server_module.get_server().auth.gate


# ----------------------------------------------------------------------------------------------------------------------
def require_user(
    request: Request, services: auth_services.AuthServices = Depends(get_auth_services)
) -> auth_gate.RequestAuth:
    # anybody who may see the server at all; what they may see of the repositories is up to the route
    return get_auth_gate().require_actor(request, services)


# ----------------------------------------------------------------------------------------------------------------------
def require_read(
    request: Request, services: auth_services.AuthServices = Depends(get_auth_services)
) -> auth_gate.RequestAuth:
    # the permission to read the repository that the request is about
    return get_auth_gate().check_permission(request, services, server_auth.PERMISSION_READ, "repo_name")


# ----------------------------------------------------------------------------------------------------------------------
def require_write(
    request: Request, services: auth_services.AuthServices = Depends(get_auth_services)
) -> auth_gate.RequestAuth:
    # the permission to write the repository that the request is about
    return get_auth_gate().check_permission(request, services, server_auth.PERMISSION_WRITE, "repo_name")


# ----------------------------------------------------------------------------------------------------------------------
def require_admin(
    request: Request, services: auth_services.AuthServices = Depends(get_auth_services)
) -> auth_gate.RequestAuth:
    # the permission to administer everything, such as making a repository
    return get_auth_gate().check_permission(request, services, server_auth.PERMISSION_ADMIN)
