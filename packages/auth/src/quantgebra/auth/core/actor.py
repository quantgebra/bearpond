from . import grant
from . import permission_definition_catalog


# ======================================================================================================================
class Actor:

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================

    # ------------------------------------------------------------------------------------------------------------------
    def __init__(
        self,
        user_id: str,
        username: str,
        display_name: str,
        issuer: str,
        group_names: list[str],
        grants: list[grant.Grant],
        catalog: permission_definition_catalog.PermissionDefinitionCatalog,
    ) -> None:
        # who is making the request, and what the rules say they may do; made again for every request from the user's
        # groups, so it is never stored
        self.user_id: str = user_id
        self.username: str = username
        self.display_name: str = display_name
        self.issuer: str = issuer
        self.group_names: list[str] = group_names
        self.grants: list[grant.Grant] = grants
        self.catalog: permission_definition_catalog.PermissionDefinitionCatalog = catalog

    # ------------------------------------------------------------------------------------------------------------------
    def can(self, permission_name: str, *subject_ids: str) -> bool:
        # whether the user may do something, for the subject it is about when that matters, given by its id and then
        # the ids of what holds it; the grants for the subject are looked at, and then those for what holds it
        result: bool = False
        for each_grant in self.grants:
            if each_grant.does_cover(permission_name, list(subject_ids), self.catalog):
                result = True
        return result
