from fastapi import Request, Response

from ..core import auth_config


# ----------------------------------------------------------------------------------------------------------------------
def get_cookie(request: Request, cookie_name: str) -> str | None:
    return request.cookies.get(cookie_name)


# ----------------------------------------------------------------------------------------------------------------------
def set_session_cookie(response: Response, config: auth_config.AuthConfig, cookie_name: str, token: str) -> None:
    # the secret of a login goes in a cookie that the page's scripts cannot read, that other sites cannot make the
    # browser send along with a form they submit, that is only sent over https when the service is reached over it,
    # and that ends with the login
    response.set_cookie(
        key=cookie_name,
        value=token,
        max_age=config.session_duration_minutes * 60,
        path="/",
        httponly=True,
        samesite="lax",
        secure=config.are_cookies_secure(),
    )


# ----------------------------------------------------------------------------------------------------------------------
def clear_session_cookie(response: Response, config: auth_config.AuthConfig, cookie_name: str) -> None:
    response.delete_cookie(key=cookie_name, path="/", httponly=True, samesite="lax", secure=config.are_cookies_secure())
