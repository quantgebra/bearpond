from pydantic import BaseModel, Field

from . import grant


# ======================================================================================================================
class GrantRule(BaseModel):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def get_grants_for(rules: list["GrantRule"], group_names: list[str]) -> list[grant.Grant]:
        # the grants that the rules give to someone in the groups
        result: list[grant.Grant] = []
        for rule in rules:
            if rule.does_apply_to(group_names):
                result.extend(rule.grants)
        return result

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    group_name: str
    grants: list[grant.Grant] = Field(min_length=1)

    # ------------------------------------------------------------------------------------------------------------------
    def does_apply_to(self, group_names: list[str]) -> bool:
        return self.group_name in group_names
