from . import permission_definition


# ======================================================================================================================
class PermissionDefinitionCatalog:

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================

    # ------------------------------------------------------------------------------------------------------------------
    def __init__(
        self, definitions: list[permission_definition.PermissionDefinition], admin_name: str, baseline_name: str | None
    ) -> None:
        # every permission that the application has; rules and grants can only name these
        self.definitions: list[permission_definition.PermissionDefinition] = definitions

        # the one permission that covers everything everywhere, whatever it is asked for
        self.admin_name: str = admin_name

        # the permission that anyone who holds any grant holds too, such as "view"; None when there is no such thing
        self.baseline_name: str | None = baseline_name

        # the administrator permission has to be one of the permissions there are
        if admin_name not in self.get_names():
            raise ValueError(f"the administrator permission {admin_name} is not in the catalog")

    # ------------------------------------------------------------------------------------------------------------------
    def get_names(self) -> list[str]:
        names: list[str] = []
        for definition in self.definitions:
            names.append(definition.name)
        return names

    # ------------------------------------------------------------------------------------------------------------------
    def find_definition(self, permission_name: str) -> permission_definition.PermissionDefinition | None:
        # what a rule is checked against: whether the permission exists, and which kinds of subject it can be limited to
        result: permission_definition.PermissionDefinition | None = None
        for definition in self.definitions:
            if definition.name == permission_name:
                result = definition
        return result
