import logging
import re
import uuid
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel

from ..core import actor
from ..core import auth_config
from ..core import auth_error
from ..core import auth_service
from ..core import group_record
from ..core import token_secrets
from ..core import user_group_record
from ..core import user_record
from . import api_key_record

logger: logging.Logger = logging.getLogger(__name__)

_NAME_LENGTH: int = 64
_MAXIMUM_DAYS: int = 3650
_SHOWN_CHARACTERS: int = 5
_NOTE_USE_MINUTES: int = 5
_USER_NAME: re.Pattern = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")
_CONTROL_CHARACTERS: re.Pattern = re.compile(r"[\x00-\x1f\x7f]")


# ======================================================================================================================
class ApiKeyRequest(BaseModel):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    # the user that the key acts as
    user_id: str
    key_name: str
    # after how many days the key stops working, None for a key that never does
    expires_in_days: int | None


# ======================================================================================================================
class ApiKeyView(BaseModel):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    key_id: str
    prefix: str
    key_name: str
    user_id: str
    owner_display_name: str
    owner_username: str
    is_owner_service_account: bool
    created_datetime: datetime
    expires_datetime: datetime | None
    last_used_datetime: datetime | None
    is_expired: bool


# ======================================================================================================================
class NewApiKey(BaseModel):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    # the secret, which is only known now: the service keeps a hash of it
    token: str
    key: ApiKeyView


# ======================================================================================================================
class ServiceAccountRequest(BaseModel):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    username: str
    display_name: str
    group_names: list[str]


# ======================================================================================================================
class ApiKeyAuthentication:

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def check_key_name(key_name: str) -> str:
        # the name of a key as it is kept: trimmed, and refused when it is empty, too long or has control characters
        trimmed_name: str = key_name.strip()
        if not 1 <= len(trimmed_name) <= _NAME_LENGTH or _CONTROL_CHARACTERS.search(trimmed_name) is not None:
            raise auth_error.AuthError(
                "invalid_request", f"A key needs a name of 1 to {_NAME_LENGTH} characters, without control characters."
            )
        return trimmed_name

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def compute_expires_datetime(expires_in_days: int | None, now_datetime: datetime) -> datetime | None:
        # when a key made now stops working, None for one that never does
        expires_datetime: datetime | None = None
        if expires_in_days is not None:
            if 1 <= expires_in_days <= _MAXIMUM_DAYS:
                expires_datetime = now_datetime + timedelta(days=expires_in_days)
            else:
                raise auth_error.AuthError(
                    "invalid_request", f"A key lasts 1 to {_MAXIMUM_DAYS} days, or does not expire."
                )
        return expires_datetime

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================

    # ------------------------------------------------------------------------------------------------------------------
    def __init__(self, auth: auth_service.AuthService) -> None:
        # how a script or CI gets in without logging in: a key that stands for a user is sent with each request; it is
        # made by the user it acts as or by an administrator, who also makes the service accounts that keys are mostly
        # for; a key acts with the groups that an administrator gave its user, never with ones a provider sent, since
        # there is no login, and it makes no session
        self._auth: auth_service.AuthService = auth

    # ------------------------------------------------------------------------------------------------------------------
    def create_key(self, requesting_actor: actor.Actor, request: ApiKeyRequest) -> NewApiKey:
        # a user makes keys for themselves and an administrator for anyone; the secret that comes back is the only time
        # it is known
        if request.user_id != requesting_actor.user_id:
            self._auth.require_admin(requesting_actor)

        owner: user_record.UserRecord | None = user_record.UserRecord.get_by_user_id(
            self._auth.session, request.user_id
        )
        if owner is None:
            raise auth_error.AuthError("invalid_request", "There is no such user.")
        if owner.is_disabled:
            raise auth_error.AuthError("invalid_request", "The account is disabled, so a key for it would not work.")

        # the secret starts with the application's prefix; what is kept is its hash and the first few characters
        key_name: str = ApiKeyAuthentication.check_key_name(request.key_name)
        now_datetime: datetime = datetime.now(timezone.utc)
        token: str = self._auth.config.api_key_prefix + token_secrets.new_token()
        key: api_key_record.ApiKeyRecord = api_key_record.ApiKeyRecord(
            key_id=uuid.uuid4().hex,
            token_hash=token_secrets.hash_token(token),
            prefix=token[: len(self._auth.config.api_key_prefix) + _SHOWN_CHARACTERS],
            key_name=key_name,
            user_id=owner.user_id,
            created_by_user_id=requesting_actor.user_id,
            created_datetime=now_datetime,
            expires_datetime=ApiKeyAuthentication.compute_expires_datetime(request.expires_in_days, now_datetime),
            last_used_datetime=None,
        )
        key.save(self._auth.session)
        logger.info(f"{requesting_actor.username} made the api key {key_name} ({key.prefix}) for {owner.username}")

        return NewApiKey(token=token, key=self._make_view(key, owner, now_datetime))

    # ------------------------------------------------------------------------------------------------------------------
    def list_own_keys(self, requesting_actor: actor.Actor) -> list[ApiKeyView]:
        # the keys that act as the person asking, newest first
        keys: list[api_key_record.ApiKeyRecord] = api_key_record.ApiKeyRecord.get_all_by_user_id(
            self._auth.session, requesting_actor.user_id
        )
        return self._make_views(keys)

    # ------------------------------------------------------------------------------------------------------------------
    def list_all_keys(self, requesting_actor: actor.Actor) -> list[ApiKeyView]:
        # every key, newest first; only for administrators
        self._auth.require_admin(requesting_actor)
        return self._make_views(api_key_record.ApiKeyRecord.get_all(self._auth.session))

    # ------------------------------------------------------------------------------------------------------------------
    def revoke_key(self, requesting_actor: actor.Actor, key_id: str) -> None:
        # a key stops working at once, which cannot be undone; the user it acts as may do it, an administrator for any
        key: api_key_record.ApiKeyRecord | None = api_key_record.ApiKeyRecord.get_by_key_id(self._auth.session, key_id)
        if key is None:
            raise auth_error.AuthError("invalid_request", "There is no such key.")
        if key.user_id != requesting_actor.user_id:
            self._auth.require_admin(requesting_actor)

        api_key_record.ApiKeyRecord.delete_by_key_id(self._auth.session, key.key_id)
        logger.info(f"{requesting_actor.username} revoked the api key {key.key_name} ({key.prefix})")

    # ------------------------------------------------------------------------------------------------------------------
    def authenticate(self, token: str) -> actor.Actor | None:
        # the user that the secret of a key stands for, None when it is not the secret of a key that has not been
        # revoked or run out, or when its user is disabled; what the user may do is worked out now
        result: actor.Actor | None = None
        if token.startswith(self._auth.config.api_key_prefix):
            key: api_key_record.ApiKeyRecord | None = api_key_record.ApiKeyRecord.get_by_token_hash(
                self._auth.session, token_secrets.hash_token(token)
            )
            now_datetime: datetime = datetime.now(timezone.utc)
            if key is not None and not key.is_expired(now_datetime):
                user: user_record.UserRecord | None = user_record.UserRecord.get_by_user_id(
                    self._auth.session, key.user_id
                )
                if user is not None and not user.is_disabled:
                    self._note_use(key, now_datetime)
                    result = self._auth.make_actor(user, auth_config.API_KEY_NAME, [])
        return result

    # ------------------------------------------------------------------------------------------------------------------
    def create_service_account(
        self, requesting_actor: actor.Actor, request: ServiceAccountRequest
    ) -> user_record.UserRecord:
        # a user for a program, such as CI: it has no password and cannot log in, and is only reached with the keys that
        # are made for it; only administrators make them
        self._auth.require_admin(requesting_actor)

        username: str = request.username.strip().lower()
        if _USER_NAME.match(username) is None:
            raise auth_error.AuthError(
                "invalid_request",
                "A user name is 2 to 64 characters: lower case letters, digits and . _ - only, starting with one.",
            )
        existing: user_record.UserRecord | None = user_record.UserRecord.get_by_issuer_and_username(
            self._auth.session, auth_config.SERVICE_NAME, username
        )
        if existing is not None:
            raise auth_error.AuthError("invalid_request", f'There is already a service account named "{username}".')
        group_names: list[str] = auth_service.AuthService.clean_group_names(request.group_names)
        missing_names: list[str] = group_record.GroupRecord.get_missing_group_names(self._auth.session, group_names)
        if missing_names:
            raise auth_error.AuthError(
                "invalid_request", f"There is no group named {', '.join(missing_names)}: add it first."
            )

        user_id: str = uuid.uuid4().hex
        user: user_record.UserRecord = user_record.UserRecord(
            user_id=user_id,
            issuer=auth_config.SERVICE_NAME,
            subject=user_id,
            username=username,
            display_name=request.display_name.strip() or username,
            email=None,
            is_disabled=False,
            created_datetime=datetime.now(timezone.utc),
        )
        user.save(self._auth.session)
        user_group_record.UserGroupRecord.replace_for_user(self._auth.session, user_id, group_names)
        logger.info(f"{requesting_actor.username} made the service account {username} in {', '.join(group_names)}")
        return user

    # ------------------------------------------------------------------------------------------------------------------
    def _make_view(
        self, key: api_key_record.ApiKeyRecord, owner: user_record.UserRecord | None, now_datetime: datetime
    ) -> ApiKeyView:
        # a key as the pages show it, never with its secret
        return ApiKeyView(
            key_id=key.key_id,
            prefix=key.prefix,
            key_name=key.key_name,
            user_id=key.user_id,
            owner_display_name=owner.display_name if owner is not None else "unknown user",
            owner_username=owner.username if owner is not None else "",
            is_owner_service_account=owner is not None and owner.issuer == auth_config.SERVICE_NAME,
            created_datetime=key.created_datetime,
            expires_datetime=key.expires_datetime,
            last_used_datetime=key.last_used_datetime,
            is_expired=key.is_expired(now_datetime),
        )

    # ------------------------------------------------------------------------------------------------------------------
    def _make_views(self, keys: list[api_key_record.ApiKeyRecord]) -> list[ApiKeyView]:
        now_datetime: datetime = datetime.now(timezone.utc)
        views: list[ApiKeyView] = []
        for key in keys:
            owner: user_record.UserRecord | None = user_record.UserRecord.get_by_user_id(
                self._auth.session, key.user_id
            )
            view: ApiKeyView = self._make_view(key, owner, now_datetime)
            views.append(view)
        return views

    # ------------------------------------------------------------------------------------------------------------------
    def _note_use(self, key: api_key_record.ApiKeyRecord, now_datetime: datetime) -> None:
        # notes when the key was used, but not again within a few minutes, so a busy script does not write every time
        last_used_datetime: datetime | None = key.last_used_datetime
        wait: timedelta = timedelta(minutes=_NOTE_USE_MINUTES)
        if last_used_datetime is None or now_datetime - last_used_datetime >= wait:
            key.last_used_datetime = now_datetime
            key.save(self._auth.session)
