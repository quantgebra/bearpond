import uuid
from datetime import datetime, timezone

from ..core import auth_error
from ..core import auth_service
from ..core import user_record
from . import external_identity
from . import oidc_provider_settings
from ..core import provider_group_record


# ======================================================================================================================
class OidcAuthentication:

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================

    # ------------------------------------------------------------------------------------------------------------------
    def __init__(self, auth: auth_service.AuthService) -> None:
        # logging in through an OIDC provider: once the provider has vouched for a person, they are found or recorded
        # as a user and logged in with the core of authentication, which is the only thing that it knows
        self._auth: auth_service.AuthService = auth

    # ------------------------------------------------------------------------------------------------------------------
    def login(
        self, provider: oidc_provider_settings.OidcProviderSettings, identity: external_identity.ExternalIdentity
    ) -> auth_service.Login:
        # logs in a person that a provider vouches for; the person is known by the issuer and what it calls them, never
        # by an email address, and what the provider says about their name and groups is taken anew at every login
        user: user_record.UserRecord | None = user_record.UserRecord.get_by_issuer_and_subject(
            self._auth.session, identity.issuer, identity.subject
        )
        if user is None:
            user = user_record.UserRecord(
                user_id=uuid.uuid4().hex,
                issuer=identity.issuer,
                subject=identity.subject,
                username=identity.username,
                display_name=identity.display_name,
                email=identity.email,
                is_disabled=False,
                created_datetime=datetime.now(timezone.utc),
            )
        else:
            user.username = identity.username
            user.display_name = identity.display_name
            user.email = identity.email

        if user.is_disabled:
            raise auth_error.AuthError("disabled", "This account has been disabled.")

        # remember which groups the provider sent, so that an administrator can pick them to map
        provider_group_names: list[str] = auth_service.AuthService.clean_group_names_leniently(
            identity.provider_group_names
        )
        provider_group_record.ProviderGroupRecord.record_seen(
            self._auth.session, provider.provider_name, provider_group_names, datetime.now(timezone.utc)
        )

        return self._auth.create_login(user, provider.provider_name, provider_group_names)
