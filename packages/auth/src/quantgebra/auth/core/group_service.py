from datetime import datetime, timezone

from . import actor
from . import auth_error
from . import auth_service
from . import group_mapping_record
from . import group_record
from . import provider_group_record
from . import user_group_record


# ======================================================================================================================
class GroupService:

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================

    # ------------------------------------------------------------------------------------------------------------------
    def __init__(self, auth: auth_service.AuthService) -> None:
        self._auth: auth_service.AuthService = auth

    # ------------------------------------------------------------------------------------------------------------------
    def list_groups(self, requesting_actor: actor.Actor) -> list[group_record.GroupRecord]:
        self._auth.require_admin(requesting_actor)
        return group_record.GroupRecord.get_all(self._auth.session)

    # ------------------------------------------------------------------------------------------------------------------
    def create_group(self, requesting_actor: actor.Actor, group_name: str, description: str) -> None:
        # makes a group that rules give permissions to and that users are put in
        self._auth.require_admin(requesting_actor)

        if not auth_service.AuthService.is_group_name(group_name):
            raise auth_error.AuthError(
                "invalid_request", f'"{group_name}" is not a group name: use letters, digits and . _ : - only.'
            )

        if group_record.GroupRecord.get_by_group_name(self._auth.session, group_name) is not None:
            raise auth_error.AuthError("invalid_request", f'There is already a group named "{group_name}".')

        group: group_record.GroupRecord = group_record.GroupRecord(
            group_name=group_name, description=description.strip(), created_datetime=datetime.now(timezone.utc)
        )
        group.save(self._auth.session)

    # ------------------------------------------------------------------------------------------------------------------
    def describe_group(self, requesting_actor: actor.Actor, group_name: str, description: str) -> None:
        # says what a group is for, which the pages that pick a group show beside it
        self._auth.require_admin(requesting_actor)

        group: group_record.GroupRecord | None = group_record.GroupRecord.get_by_group_name(
            self._auth.session, group_name
        )
        if group is None:
            raise auth_error.AuthError("invalid_request", "There is no such group.")

        group.description = description.strip()
        group.save(self._auth.session)

    # ------------------------------------------------------------------------------------------------------------------
    def delete_group(self, requesting_actor: actor.Actor, group_name: str) -> None:
        # removes a group that nothing depends on: no users in it, no provider groups mapped to it, no rule about it
        self._auth.require_admin(requesting_actor)

        if group_record.GroupRecord.get_by_group_name(self._auth.session, group_name) is None:
            raise auth_error.AuthError("invalid_request", "There is no such group.")

        if group_name == self._auth.config.administrators_group_name:
            raise auth_error.AuthError("invalid_request", "The group of administrators cannot be removed.")

        if user_group_record.UserGroupRecord.count_by_group_name(self._auth.session, group_name) > 0:
            raise auth_error.AuthError("invalid_request", "Take the users out of the group first.")

        if group_mapping_record.GroupMappingRecord.count_by_group_name(self._auth.session, group_name) > 0:
            raise auth_error.AuthError("invalid_request", "Remove the provider groups mapped to the group first.")

        for rule in self._auth.config.rules:
            if rule.group_name == group_name:
                raise auth_error.AuthError("invalid_request", "Remove the rules about the group first.")

        group_record.GroupRecord.delete_by_group_name(self._auth.session, group_name)

    # ------------------------------------------------------------------------------------------------------------------
    def list_provider_groups(self, requesting_actor: actor.Actor) -> list[provider_group_record.ProviderGroupRecord]:
        # the groups that providers have sent, which are what an administrator picks from to map
        self._auth.require_admin(requesting_actor)
        return provider_group_record.ProviderGroupRecord.get_all(self._auth.session)

    # ------------------------------------------------------------------------------------------------------------------
    def list_mappings(self, requesting_actor: actor.Actor) -> list[group_mapping_record.GroupMappingRecord]:
        self._auth.require_admin(requesting_actor)
        return group_mapping_record.GroupMappingRecord.get_all(self._auth.session)

    # ------------------------------------------------------------------------------------------------------------------
    def add_mapping(self, requesting_actor: actor.Actor, mapping: group_mapping_record.GroupMappingRecord) -> None:
        # a provider's group is mapped to a group of the server, so that the same rules apply to everyone
        self._auth.require_admin(requesting_actor)

        if group_record.GroupRecord.get_by_group_name(self._auth.session, mapping.group_name) is None:
            raise auth_error.AuthError("invalid_request", f'There is no group named "{mapping.group_name}".')

        mapping.save_if_absent(self._auth.session)

    # ------------------------------------------------------------------------------------------------------------------
    def remove_mapping(self, requesting_actor: actor.Actor, mapping: group_mapping_record.GroupMappingRecord) -> None:
        self._auth.require_admin(requesting_actor)
        mapping.delete(self._auth.session)
