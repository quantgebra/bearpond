import hashlib
import hmac
import secrets


# ----------------------------------------------------------------------------------------------------------------------
def new_token() -> str:
    # 32 random bytes written so that they can be put in a cookie or a link, which cannot be guessed
    return secrets.token_urlsafe(32)


# ----------------------------------------------------------------------------------------------------------------------
def hash_token(token: str) -> str:
    # what is stored in place of a secret, so that the database cannot be used to log in with it; the secrets are
    # random and long, so a fast hash is enough
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ----------------------------------------------------------------------------------------------------------------------
def is_same_token(first_token: str, second_token: str) -> bool:
    # compares the hashes, so that the time taken does not depend on how much of the two secrets match
    first_hash: bytes = hashlib.sha256(first_token.encode("utf-8")).digest()
    second_hash: bytes = hashlib.sha256(second_token.encode("utf-8")).digest()
    return hmac.compare_digest(first_hash, second_hash)
