import sqlalchemy
from sqlalchemy import orm

from . import base
from . import grant
from . import grant_rule
from . import group_record


# ======================================================================================================================
class GrantRuleRecord(base.Base):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================
    __tablename__ = "grant_rules"

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def get_all_rules(session: orm.Session) -> list[grant_rule.GrantRule]:
        # one stored row per grant, so a group with several grants has several rules, which are applied together
        statement: sqlalchemy.Select = sqlalchemy.select(GrantRuleRecord).order_by(GrantRuleRecord.rule_id)
        result: list[grant_rule.GrantRule] = []
        for record in session.scalars(statement):
            stored_grant: grant.Grant = grant.Grant(
                permission=record.permission, subject=record.subject, subject_kind=record.subject_kind
            )
            result.append(grant_rule.GrantRule(group_name=record.group_name, grants=[stored_grant]))
        return result

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def replace_all(session: orm.Session, rules: list[grant_rule.GrantRule]) -> None:
        session.execute(sqlalchemy.delete(GrantRuleRecord))
        for rule in rules:
            for each_grant in rule.grants:
                session.add(
                    GrantRuleRecord(
                        group_name=rule.group_name,
                        permission=each_grant.permission,
                        subject=each_grant.subject,
                        subject_kind=each_grant.subject_kind,
                    )
                )
        session.flush()

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    rule_id: orm.Mapped[int] = orm.mapped_column(primary_key=True, autoincrement=True)
    group_name: orm.Mapped[str] = orm.mapped_column(
        sqlalchemy.ForeignKey(group_record.GroupRecord.group_name, ondelete="CASCADE"), index=True
    )
    permission: orm.Mapped[str]
    subject: orm.Mapped[str | None]
    subject_kind: orm.Mapped[str | None]
