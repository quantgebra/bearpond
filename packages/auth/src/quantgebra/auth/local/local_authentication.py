import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal

from pydantic import BaseModel

from ..core import actor
from ..core import auth_config
from ..core import auth_error
from ..core import auth_service
from ..core import session_record
from ..core import token_secrets
from ..core import user_record
from . import invite_record
from . import local_credential_record
from . import login_throttle
from . import password_hasher
from ..core import group_record
from ..core import user_group_record

_INVITE_HOURS: int = 7 * 24
_RESET_HOURS: int = 24
_FIRST_ADMINISTRATOR_USERNAME: str = "admin"
_USER_NAME: re.Pattern = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")


# ======================================================================================================================
class InviteLink(BaseModel):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    url: str
    expires_datetime: datetime


# ======================================================================================================================
class InviteInfo(BaseModel):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    username: str
    display_name: str
    purpose: Literal["invite", "reset"]


# ======================================================================================================================
class InviteRequest(BaseModel):

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
class FirstAdministratorOutcome(BaseModel):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    kind: Literal["not_needed", "link"]
    username: str | None = None
    link: InviteLink | None = None
    warning: str | None = None


# ======================================================================================================================
class LocalAuthentication:

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def is_username(text: str) -> bool:
        # a user name for an account of the service: 2 to 64 lower case letters, digits and . _ -, starting with one
        return _USER_NAME.match(text) is not None

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================

    # ------------------------------------------------------------------------------------------------------------------
    def __init__(self, auth: auth_service.AuthService, throttle: login_throttle.LoginThrottle | None = None) -> None:
        # logging in with an account of the service: a user name and a password, the invitations and links that let
        # people choose theirs, changing it, and the first administrator that a new service makes; it makes a login
        # with the core of authentication once it knows who the person is, and knows no other way of logging in
        self._auth: auth_service.AuthService = auth
        self._throttle: login_throttle.LoginThrottle = throttle or login_throttle.LoginThrottle()

    # ------------------------------------------------------------------------------------------------------------------
    def login(self, username: str, password: str, address: str) -> auth_service.Login:
        # logs in with the user name and password of an account of the service; after a few wrong passwords the user
        # name, and the address they came from, are refused for a while, and every failure says the same, so that a
        # person cannot find out which names have accounts
        name: str = username.strip().lower()
        user_key: str = f"user:{name}"
        address_key: str = f"address:{address}"
        seconds_locked: int = max(
            self._throttle.get_seconds_locked(user_key), self._throttle.get_seconds_locked(address_key)
        )

        if not self._auth.config.is_local_enabled:
            raise auth_error.AuthError(
                "invalid_credentials", "Logging in with an account of this service is not enabled."
            )

        if seconds_locked > 0:
            minutes_locked: int = (seconds_locked + 59) // 60
            raise auth_error.AuthError("throttled", f"Too many attempts. Try again in {minutes_locked} minute(s).")

        # check the password; an account that does not exist costs as much time as one that does
        user: user_record.UserRecord | None = user_record.UserRecord.get_by_issuer_and_username(
            self._auth.session, auth_config.LOCAL_NAME, name
        )
        credential: local_credential_record.LocalCredentialRecord | None = None
        if user is not None:
            credential = local_credential_record.LocalCredentialRecord.get_by_user_id(self._auth.session, user.user_id)
        password_hash: str | None = None
        if credential is not None:
            password_hash = credential.password_hash
        does_match: bool = password_hasher.PasswordHasher.is_password_correct(password_hash, password)

        if user is None or not does_match or user.is_disabled:
            self._throttle.record_failure(user_key)
            self._throttle.record_failure(address_key)
            raise auth_error.AuthError("invalid_credentials", "The user name or the password is not right.")

        self._throttle.record_success(user_key)
        return self._auth.create_login(user, auth_config.LOCAL_NAME, [])

    # ------------------------------------------------------------------------------------------------------------------
    def invite(self, creator: actor.Actor | None, request: InviteRequest) -> InviteLink:
        # makes an account of the service for a person, who is let choose their password with the link that is
        # returned; the account cannot be used before that; only an administrator may do this, or the command that
        # makes the first one
        username: str = request.username.strip().lower()
        if creator is not None:
            self._auth.require_admin(creator)

        if not LocalAuthentication.is_username(username):
            raise auth_error.AuthError(
                "invalid_request",
                "A user name is 2 to 64 characters: lower case letters, digits and . _ - only, starting with one.",
            )

        if (
            user_record.UserRecord.get_by_issuer_and_username(self._auth.session, auth_config.LOCAL_NAME, username)
            is not None
        ):
            raise auth_error.AuthError("invalid_request", f'There is already an account named "{username}".')

        # an administrator can only put the user in groups that exist; the command that makes the first one makes
        # the group of administrators
        group_names: list[str] = auth_service.AuthService.clean_group_names(request.group_names)
        missing_names: list[str] = group_record.GroupRecord.get_missing_group_names(self._auth.session, group_names)
        if creator is not None and missing_names:
            raise auth_error.AuthError(
                "invalid_request", f"There is no group named {', '.join(missing_names)}: add it first."
            )

        user: user_record.UserRecord = self._save_new_local_user(username, request.display_name, group_names)
        creator_id: str = "command" if creator is None else creator.user_id
        return self._create_link(user, creator_id, "invite")

    # ------------------------------------------------------------------------------------------------------------------
    def reset_link(self, creator: actor.Actor | None, user_id: str) -> InviteLink:
        # makes a link for the owner of an account of the service to choose a new password with; only for
        # administrators, or for the command that is run on the server itself
        if creator is not None:
            self._auth.require_admin(creator)

        user: user_record.UserRecord | None = user_record.UserRecord.get_by_user_id(self._auth.session, user_id)
        if user is None or user.issuer != auth_config.LOCAL_NAME:
            raise auth_error.AuthError("invalid_request", "Only accounts of this service have passwords to reset.")

        creator_id: str = "command" if creator is None else creator.user_id
        return self._create_link(user, creator_id, "reset")

    # ------------------------------------------------------------------------------------------------------------------
    def ensure_first_administrator(self) -> FirstAdministratorOutcome:
        # makes sure that a service that has nobody who can log in has a first administrator, once, when it starts:
        # the account "admin", in the group of administrators, made with a link that lets its owner choose a password
        # (a new link on every start, until it is used); only when accounts of the service are used, and only while
        # there are no accounts at all; nothing is ever changed once there are accounts
        outcome: FirstAdministratorOutcome = FirstAdministratorOutcome(kind="not_needed")

        if self._auth.config.mode == "enforced" and self._auth.config.is_local_enabled:
            user_count: int = user_record.UserRecord.count_all(self._auth.session)
            pending_user: user_record.UserRecord | None = user_record.UserRecord.get_by_issuer_and_username(
                self._auth.session, auth_config.LOCAL_NAME, _FIRST_ADMINISTRATOR_USERNAME
            )
            has_password: bool = (
                pending_user is not None
                and local_credential_record.LocalCredentialRecord.get_by_user_id(
                    self._auth.session, pending_user.user_id
                )
                is not None
            )
            is_waiting: bool = user_count == 1 and pending_user is not None and not has_password
            warning: str | None = self._get_administrators_warning()

            if user_count == 0:
                request: InviteRequest = InviteRequest(
                    username=_FIRST_ADMINISTRATOR_USERNAME,
                    display_name="Administrator",
                    group_names=[self._auth.config.administrators_group_name],
                )
                link: InviteLink = self.invite(None, request)
                outcome = FirstAdministratorOutcome(
                    kind="link", username=_FIRST_ADMINISTRATOR_USERNAME, link=link, warning=warning
                )
            elif is_waiting and pending_user is not None:
                # the first start made the account and the link, and nobody has used the link yet
                link = self.reset_link(None, pending_user.user_id)
                outcome = FirstAdministratorOutcome(
                    kind="link", username=pending_user.username, link=link, warning=warning
                )

        return outcome

    # ------------------------------------------------------------------------------------------------------------------
    def get_invite_info(self, token: str) -> InviteInfo | None:
        # what the page of an invitation shows, None for a link that cannot be used
        invite: invite_record.InviteRecord | None = self._find_open_invite(token)
        result: InviteInfo | None = None
        if invite is not None:
            user: user_record.UserRecord | None = user_record.UserRecord.get_by_user_id(
                self._auth.session, invite.user_id
            )
            if user is not None:
                result = InviteInfo(username=user.username, display_name=user.display_name, purpose=invite.purpose)
        return result

    # ------------------------------------------------------------------------------------------------------------------
    def accept_invite(self, token: str, password: str) -> auth_service.Login:
        # lets the person who holds a link choose the password of the account it is for, uses the link up, ends the
        # logins the account had, and logs the person in
        invite: invite_record.InviteRecord | None = self._find_open_invite(token)
        if invite is None:
            raise auth_error.AuthError("invalid_invite", "This link has been used, has expired, or is not right.")

        user: user_record.UserRecord | None = user_record.UserRecord.get_by_user_id(self._auth.session, invite.user_id)
        if user is None:
            raise auth_error.AuthError("invalid_invite", "This link has been used, has expired, or is not right.")

        problem: str | None = password_hasher.PasswordHasher.find_problem_with(password, user.username)
        if problem is not None:
            raise auth_error.AuthError("weak_password", problem)

        # use the link up, set the password, and end the logins that the account had
        now_datetime: datetime = datetime.now(timezone.utc)
        invite.used_datetime = now_datetime
        local_credential_record.LocalCredentialRecord.set_password(
            self._auth.session,
            user.user_id,
            password_hasher.PasswordHasher.hash_password(password),
            now_datetime,
        )
        invite.save(self._auth.session)
        session_record.SessionRecord.delete_by_user_id_keeping(self._auth.session, user.user_id, None)

        return self._auth.create_login(user, auth_config.LOCAL_NAME, [])

    # ------------------------------------------------------------------------------------------------------------------
    def change_password(
        self,
        requesting_actor: actor.Actor,
        session: session_record.SessionRecord,
        current_password: str,
        new_password: str,
    ) -> None:
        # changes the password of the account that the actor is logged in to, for which the present one is needed;
        # the other logins of the account end
        user: user_record.UserRecord | None = user_record.UserRecord.get_by_user_id(
            self._auth.session, requesting_actor.user_id
        )
        if user is None or user.issuer != auth_config.LOCAL_NAME:
            raise auth_error.AuthError("invalid_request", "Only accounts of this service have a password to change.")

        credential: local_credential_record.LocalCredentialRecord | None = (
            local_credential_record.LocalCredentialRecord.get_by_user_id(self._auth.session, user.user_id)
        )
        current_hash: str | None = None
        if credential is not None:
            current_hash = credential.password_hash
        if not password_hasher.PasswordHasher.is_password_correct(current_hash, current_password):
            raise auth_error.AuthError("invalid_credentials", "The present password is not right.")

        problem: str | None = password_hasher.PasswordHasher.find_problem_with(new_password, user.username)
        if problem is not None:
            raise auth_error.AuthError("weak_password", problem)

        local_credential_record.LocalCredentialRecord.set_password(
            self._auth.session,
            user.user_id,
            password_hasher.PasswordHasher.hash_password(new_password),
            datetime.now(timezone.utc),
        )
        session_record.SessionRecord.delete_by_user_id_keeping(self._auth.session, user.user_id, session.token_hash)

    # ------------------------------------------------------------------------------------------------------------------
    def find_user_id(self, username: str) -> str | None:
        # the account of the service with the user name, for the command that is run on the server itself
        user: user_record.UserRecord | None = user_record.UserRecord.get_by_issuer_and_username(
            self._auth.session, auth_config.LOCAL_NAME, username.strip().lower()
        )
        result: str | None = None
        if user is not None:
            result = user.user_id
        return result

    # ------------------------------------------------------------------------------------------------------------------
    def find_user_ids_with_passwords(self, user_ids: list[str]) -> set[str]:
        # the ids among the given ones of the accounts that have a password, which a page of users shows
        return local_credential_record.LocalCredentialRecord.get_user_ids_with_passwords(self._auth.session, user_ids)

    # ------------------------------------------------------------------------------------------------------------------
    def _create_link(
        self, user: user_record.UserRecord, creator_id: str, purpose: Literal["invite", "reset"]
    ) -> InviteLink:
        # makes the link for a person to choose their password with, and records it with the hash of its secret only
        token: str = token_secrets.new_token()
        hours: int = _INVITE_HOURS
        if purpose == "reset":
            hours = _RESET_HOURS

        now_datetime: datetime = datetime.now(timezone.utc)
        expires_datetime: datetime = now_datetime + timedelta(hours=hours)
        invite: invite_record.InviteRecord = invite_record.InviteRecord(
            token_hash=token_secrets.hash_token(token),
            user_id=user.user_id,
            purpose=purpose,
            created_by=creator_id,
            created_datetime=now_datetime,
            expires_datetime=expires_datetime,
        )
        invite.save(self._auth.session)

        base_url: str = self._auth.config.base_url.rstrip("/")
        return InviteLink(url=f"{base_url}/invite/{token}", expires_datetime=expires_datetime)

    # ------------------------------------------------------------------------------------------------------------------
    def _save_new_local_user(self, username: str, display_name: str, group_names: list[str]) -> user_record.UserRecord:
        # records a new account of the service in the groups, which can log in when it has a password and not before
        user_id: str = uuid.uuid4().hex
        user: user_record.UserRecord = user_record.UserRecord(
            user_id=user_id,
            issuer=auth_config.LOCAL_NAME,
            subject=user_id,
            username=username,
            display_name=display_name.strip() or username,
            email=None,
            is_disabled=False,
            created_datetime=datetime.now(timezone.utc),
        )
        user.save(self._auth.session)
        group_record.GroupRecord.ensure_exist(self._auth.session, group_names, datetime.now(timezone.utc))
        user_group_record.UserGroupRecord.replace_for_user(self._auth.session, user_id, group_names)
        return user

    # ------------------------------------------------------------------------------------------------------------------
    def _get_administrators_warning(self) -> str | None:
        # a warning when the rules do not make the group of administrators administrators, since the first account
        # would then be able to do nothing; None when they do
        warning: str | None = None
        if not self._auth.config.does_give_administrators_the_admin_permission():
            warning = (
                f'The rules do not give the group "{self._auth.config.administrators_group_name}" the '
                f'"{self._auth.config.catalog.admin_name}" permission, so this account cannot do anything. '
                "Add such a rule."
            )
        return warning

    # ------------------------------------------------------------------------------------------------------------------
    def _find_open_invite(self, token: str) -> invite_record.InviteRecord | None:
        # the invitation that the secret in a link stands for, when it can still be used; None when it was used, has run
        # out, or is no invitation, or when its user may not log in
        invite: invite_record.InviteRecord | None = invite_record.InviteRecord.get_by_token_hash(
            self._auth.session, token_secrets.hash_token(token)
        )
        result: invite_record.InviteRecord | None = None
        if invite is not None and invite.used_datetime is None:
            if invite.expires_datetime > datetime.now(timezone.utc):
                user: user_record.UserRecord | None = user_record.UserRecord.get_by_user_id(
                    self._auth.session, invite.user_id
                )
                if user is not None and not user.is_disabled:
                    result = invite
        return result
