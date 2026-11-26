from typing import Self

from pydantic import BaseModel, model_validator

from . import permission_definition_catalog


# ======================================================================================================================
class Grant(BaseModel):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    permission: str
    subject: str | None = None
    subject_kind: str | None = None

    # ------------------------------------------------------------------------------------------------------------------
    @model_validator(mode="after")
    def _check_subject_and_kind_go_together(self) -> Self:
        if (self.subject is None) != (self.subject_kind is None):
            raise ValueError("give both the subject and its kind, or neither")
        return self

    # ------------------------------------------------------------------------------------------------------------------
    def does_cover(
        self,
        permission_name: str,
        subject_ids: list[str],
        catalog: permission_definition_catalog.PermissionDefinitionCatalog,
    ) -> bool:
        # the administrator permission covers everything everywhere, and any permission covers the baseline one
        # (someone who may do something may see it); otherwise the permission has to be the one wanted, for the subject
        result: bool = False
        if self.permission == catalog.admin_name or permission_name == catalog.baseline_name:
            result = True
        elif self.permission == permission_name:
            result = self.subject is None or self.subject in subject_ids
        return result
