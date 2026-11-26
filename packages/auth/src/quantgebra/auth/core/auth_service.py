import re
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

from pydantic import BaseModel
from sqlalchemy import orm

from . import actor
from . import auth_config
from . import auth_error
from . import grant
from . import grant_rule
from . import session_record
from . import token_secrets
from . import user_record
from . import group_record
from . import user_group_record
from . import group_mapping_record

_GROUP_NAME: re.Pattern = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")


# ======================================================================================================================
class Login(NamedTuple):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    token: str
    session: session_record.SessionRecord


# ======================================================================================================================
class AuthenticatedRequest(NamedTuple):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    actor: actor.Actor
    session: session_record.SessionRecord


# ======================================================================================================================
class UserSummary(BaseModel):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    user_id: str
    username: str
    display_name: str
    issuer: str
    email: str | None
    group_names: list[str]
    is_disabled: bool
    last_login_datetime: datetime | None


# ======================================================================================================================
class AuthService:

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def clean_group_names(group_names: list[str]) -> list[str]:
        # the groups in a list as they are, trimmed, without empty ones and doubles; a name that is not one is an error
        cleaned_names: list[str] = []
        for group_name in group_names:
            trimmed_name: str = group_name.strip()
            if trimmed_name != "":
                if not AuthService.is_group_name(trimmed_name):
                    raise auth_error.AuthError(
                        "invalid_request",
                        f'"{trimmed_name}" is not a group name: use letters, digits and . _ : - only, up to 64 '
                        "characters.",
                    )
                if trimmed_name not in cleaned_names:
                    cleaned_names.append(trimmed_name)
        return sorted(cleaned_names)

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def is_group_name(name: str) -> bool:
        return _GROUP_NAME.match(name) is not None

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def clean_group_names_leniently(provider_group_names: list[str]) -> list[str]:
        # the groups that a provider sent, without the ones that cannot be written in a rule, which are never a match
        # anyway
        kept_names: set[str] = set()
        for provider_group_name in provider_group_names:
            if 0 < len(provider_group_name) <= 256:
                kept_names.add(provider_group_name)
        return sorted(kept_names)

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================

    # ------------------------------------------------------------------------------------------------------------------
    def __init__(self, current_config: auth_config.CurrentAuthConfig, session: orm.Session) -> None:
        # the settings are the ones in use, which are replaced when an administrator changes them, so they are read
        # from where every request's service reads them and not kept
        self._current_config: auth_config.CurrentAuthConfig = current_config
        self.session: orm.Session = session

    # ------------------------------------------------------------------------------------------------------------------
    @property
    def config(self) -> auth_config.AuthConfig:
        return self._current_config.config

    # ------------------------------------------------------------------------------------------------------------------
    @config.setter
    def config(self, value: auth_config.AuthConfig) -> None:
        self._current_config.config = value

    # ------------------------------------------------------------------------------------------------------------------
    def get_development_actor(self) -> actor.Actor:
        # the person who is the only user when nobody logs in, who may do everything
        admin_grant: grant.Grant = grant.Grant(permission=self.config.catalog.admin_name)
        result: actor.Actor = actor.Actor(
            "development",
            "developer",
            "Development user",
            "none",
            [self.config.administrators_group_name],
            [admin_grant],
            self.config.catalog,
        )
        return result

    # ------------------------------------------------------------------------------------------------------------------
    def create_login(self, user: user_record.UserRecord, provider_name: str, provider_group_names: list[str]) -> Login:
        # makes a login of the user, which lasts as long as the settings say; a method of logging in calls it once it
        # has found out that the person is who they say they are
        token: str = token_secrets.new_token()
        now_datetime: datetime = datetime.now(timezone.utc)
        duration: timedelta = timedelta(minutes=self.config.session_duration_minutes)
        session: session_record.SessionRecord = session_record.SessionRecord(
            token_hash=token_secrets.hash_token(token),
            user_id=user.user_id,
            provider_name=provider_name,
            provider_group_names=provider_group_names,
            csrf_token=token_secrets.new_token(),
            created_datetime=now_datetime,
            expires_datetime=now_datetime + duration,
        )

        # forget the logins that have run out, and remember this one with the time the user last logged in
        session_record.SessionRecord.delete_expired_before(self.session, now_datetime)
        user.last_login_datetime = now_datetime
        user.save(self.session)
        session.save(self.session)

        return Login(token, session)

    # ------------------------------------------------------------------------------------------------------------------
    def authenticate(self, token: str) -> AuthenticatedRequest | None:
        # recognizes the person who holds the secret that a cookie carries, None for a secret that is not one of a login
        # that is still going on and of a user who may still log in; what the person may do is worked out now from their
        # groups, those that the provider sent when they logged in and those that an administrator gave them
        session: session_record.SessionRecord | None = session_record.SessionRecord.get_by_token_hash(
            self.session, token_secrets.hash_token(token)
        )
        result: AuthenticatedRequest | None = None

        if session is not None and session.expires_datetime > datetime.now(timezone.utc):
            user: user_record.UserRecord | None = user_record.UserRecord.get_by_user_id(self.session, session.user_id)

            if user is not None and not user.is_disabled:
                given_names: list[str] = user_group_record.UserGroupRecord.get_group_names_by_user_id(
                    self.session, user.user_id
                )
                mapped_names: list[str] = group_mapping_record.GroupMappingRecord.get_group_names_by_provider_groups(
                    self.session, session.provider_name, session.provider_group_names
                )
                group_names: list[str] = sorted(set(mapped_names) | set(given_names))
                grants: list[grant.Grant] = grant_rule.GrantRule.get_grants_for(self.config.rules, group_names)
                request_actor: actor.Actor = actor.Actor(
                    user.user_id,
                    user.username,
                    user.display_name,
                    user.issuer,
                    group_names,
                    grants,
                    self.config.catalog,
                )
                result = AuthenticatedRequest(request_actor, session)

        return result

    # ------------------------------------------------------------------------------------------------------------------
    def logout(self, token: str) -> None:
        session_record.SessionRecord.delete_by_token_hash(self.session, token_secrets.hash_token(token))

    # ------------------------------------------------------------------------------------------------------------------
    def require_admin(self, requesting_actor: actor.Actor) -> None:
        if not requesting_actor.can(self.config.catalog.admin_name):
            raise auth_error.AuthError("forbidden", "Only administrators may do this.")

    # ------------------------------------------------------------------------------------------------------------------
    def list_users(self, requesting_actor: actor.Actor) -> list[UserSummary]:
        # every user with the groups an administrator gave them; only for administrators
        self.require_admin(requesting_actor)
        result: list[UserSummary] = []
        for user in user_record.UserRecord.get_all(self.session):
            summary: UserSummary = UserSummary(
                user_id=user.user_id,
                username=user.username,
                display_name=user.display_name,
                issuer=user.issuer,
                email=user.email,
                group_names=user_group_record.UserGroupRecord.get_group_names_by_user_id(self.session, user.user_id),
                is_disabled=user.is_disabled,
                last_login_datetime=user.last_login_datetime,
            )
            result.append(summary)
        return result

    # ------------------------------------------------------------------------------------------------------------------
    def set_user_groups(self, requesting_actor: actor.Actor, user_id: str, group_names: list[str]) -> None:
        # puts the user in exactly the given groups, besides the ones a provider sends; only for administrators
        self.require_admin(requesting_actor)

        if user_record.UserRecord.get_by_user_id(self.session, user_id) is None:
            raise auth_error.AuthError("invalid_request", "There is no such user.")

        cleaned_names: list[str] = AuthService.clean_group_names(group_names)
        missing_names: list[str] = group_record.GroupRecord.get_missing_group_names(self.session, cleaned_names)
        if missing_names:
            raise auth_error.AuthError(
                "invalid_request", f"There is no group named {', '.join(missing_names)}: add it first."
            )

        user_group_record.UserGroupRecord.replace_for_user(self.session, user_id, cleaned_names)

    # ------------------------------------------------------------------------------------------------------------------
    def set_user_disabled(self, requesting_actor: actor.Actor, user_id: str, is_disabled: bool) -> None:
        # stops a user from logging in, ending their logins, or lets them again; administrators cannot disable
        # themselves, so that there is always one who can undo it
        self.require_admin(requesting_actor)

        user: user_record.UserRecord | None = user_record.UserRecord.get_by_user_id(self.session, user_id)
        if user is None:
            raise auth_error.AuthError("invalid_request", "There is no such user.")

        if is_disabled and user.user_id == requesting_actor.user_id:
            raise auth_error.AuthError("invalid_request", "You cannot disable your own account.")

        user.is_disabled = is_disabled
        user.save(self.session)
        if is_disabled:
            session_record.SessionRecord.delete_by_user_id_keeping(self.session, user.user_id, None)
