import sqlalchemy
from sqlalchemy import orm

from ..core import base
from . import oidc_provider_settings


# ======================================================================================================================
class OidcProviderRecord(base.Base):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================
    __tablename__ = "oidc_providers"

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def from_settings(settings: oidc_provider_settings.OidcProviderSettings) -> "OidcProviderRecord":
        result: OidcProviderRecord = OidcProviderRecord(
            provider_name=settings.provider_name,
            label=settings.label,
            issuer=settings.issuer,
            client_id=settings.client_id,
            scopes=settings.scopes,
            authorize_parameters=dict(settings.authorize_parameters),
            groups_claim=settings.groups_claim,
            username_claim=settings.username_claim,
            name_claim=settings.name_claim,
            email_claim=settings.email_claim,
            logout=settings.logout,
            is_insecure_http_allowed=settings.is_insecure_http_allowed,
        )
        return result

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def get_settings_by_provider_name(
        session: orm.Session, provider_name: str
    ) -> oidc_provider_settings.OidcProviderSettings | None:
        record: OidcProviderRecord | None = session.get(OidcProviderRecord, provider_name)
        result: oidc_provider_settings.OidcProviderSettings | None = None
        if record is not None:
            result = record.to_settings()
        return result

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def get_all_settings(session: orm.Session) -> list[oidc_provider_settings.OidcProviderSettings]:
        statement: sqlalchemy.Select = sqlalchemy.select(OidcProviderRecord).order_by(OidcProviderRecord.provider_name)
        result: list[oidc_provider_settings.OidcProviderSettings] = []
        for record in session.scalars(statement):
            result.append(record.to_settings())
        return result

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def save_settings(session: orm.Session, settings: oidc_provider_settings.OidcProviderSettings) -> None:
        # adds the provider, or changes it when it was saved before
        record: OidcProviderRecord | None = session.get(OidcProviderRecord, settings.provider_name)
        if record is None:
            session.add(OidcProviderRecord.from_settings(settings))
        else:
            record.update_from_settings(settings)
        session.flush()

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def delete_by_provider_name(session: orm.Session, provider_name: str) -> None:
        session.execute(sqlalchemy.delete(OidcProviderRecord).where(OidcProviderRecord.provider_name == provider_name))

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    provider_name: orm.Mapped[str] = orm.mapped_column(primary_key=True)
    label: orm.Mapped[str]
    issuer: orm.Mapped[str]
    client_id: orm.Mapped[str]
    scopes: orm.Mapped[list[str]] = orm.mapped_column(sqlalchemy.JSON)
    authorize_parameters: orm.Mapped[dict[str, str]] = orm.mapped_column(sqlalchemy.JSON)
    groups_claim: orm.Mapped[str]
    username_claim: orm.Mapped[str]
    name_claim: orm.Mapped[str]
    email_claim: orm.Mapped[str]
    logout: orm.Mapped[str] = orm.mapped_column(sqlalchemy.String(16))
    is_insecure_http_allowed: orm.Mapped[bool]

    # ------------------------------------------------------------------------------------------------------------------
    def to_settings(self) -> oidc_provider_settings.OidcProviderSettings:
        # what the provider's client and the pages need, which is a value that does not belong to any session
        result: oidc_provider_settings.OidcProviderSettings = oidc_provider_settings.OidcProviderSettings(
            provider_name=self.provider_name,
            label=self.label,
            issuer=self.issuer,
            client_id=self.client_id,
            scopes=list(self.scopes),
            authorize_parameters=dict(self.authorize_parameters),
            groups_claim=self.groups_claim,
            username_claim=self.username_claim,
            name_claim=self.name_claim,
            email_claim=self.email_claim,
            logout=self.logout,  # type: ignore[arg-type]
            is_insecure_http_allowed=self.is_insecure_http_allowed,
        )
        return result

    # ------------------------------------------------------------------------------------------------------------------
    def update_from_settings(self, settings: oidc_provider_settings.OidcProviderSettings) -> None:
        self.label = settings.label
        self.issuer = settings.issuer
        self.client_id = settings.client_id
        self.scopes = list(settings.scopes)
        self.authorize_parameters = dict(settings.authorize_parameters)
        self.groups_claim = settings.groups_claim
        self.username_claim = settings.username_claim
        self.name_claim = settings.name_claim
        self.email_claim = settings.email_claim
        self.logout = settings.logout
        self.is_insecure_http_allowed = settings.is_insecure_http_allowed
