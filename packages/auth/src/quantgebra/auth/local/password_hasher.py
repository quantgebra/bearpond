import functools
import uuid

import argon2
from argon2 import exceptions as argon2_exceptions

_MINIMUM_PASSWORD_LENGTH: int = 12
_MAXIMUM_PASSWORD_LENGTH: int = 256
_HASHER: argon2.PasswordHasher = argon2.PasswordHasher()


# ----------------------------------------------------------------------------------------------------------------------
@functools.lru_cache(maxsize=1)
def _get_nobodys_hash() -> str:
    # a hash that nobody has the password of, made once when it is first needed and checked when an account does not
    # exist, so that logging in to one takes as long as logging in to another
    return _HASHER.hash(uuid.uuid4().hex)


# ======================================================================================================================
class PasswordHasher:

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def find_problem_with(password: str, username: str) -> str | None:
        # what is wrong with a password that is not acceptable, None for one that is; length is what makes a password
        # hard to guess, so there is no rule about what characters it must have
        problem: str | None = None
        if len(password) < _MINIMUM_PASSWORD_LENGTH:
            problem = f"The password must be at least {_MINIMUM_PASSWORD_LENGTH} characters long."
        elif len(password) > _MAXIMUM_PASSWORD_LENGTH:
            problem = f"The password must be at most {_MAXIMUM_PASSWORD_LENGTH} characters long."
        elif password.lower() == username.lower():
            problem = "The password must not be the same as the user name."
        return problem

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def hash_password(password: str) -> str:
        return _HASHER.hash(password)

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def is_password_correct(password_hash: str | None, password: str) -> bool:
        # whether a password is the one that a hash is of; for no hash, which an account without a password has, it
        # takes as long as it does for one and is never true
        result: bool = False
        try:
            if password_hash is None:
                _HASHER.verify(_get_nobodys_hash(), password)
            else:
                result = _HASHER.verify(password_hash, password)
        except (argon2_exceptions.VerifyMismatchError, argon2_exceptions.InvalidHashError):
            result = False
        return result

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
