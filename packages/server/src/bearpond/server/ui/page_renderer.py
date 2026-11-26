from pathlib import Path

import jinja2
from pydantic import BaseModel

TEMPLATES_DIR: Path = Path(__file__).parent / "templates"


# ======================================================================================================================
class Crumb(BaseModel):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    label: str
    # None for the last crumb, which is the page itself
    href: str | None = None


# ======================================================================================================================
class UserMenu(BaseModel):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    display_name: str
    initials: str
    # the secret that every form of the login carries, empty when nobody logs in
    csrf_token: str
    is_administrator: bool
    # nobody logs in, because the server lets everyone in
    is_development: bool


# ======================================================================================================================
class PageHead(BaseModel):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    title: str
    # which entry of the menu is lit
    section: str
    crumbs: list[Crumb] = []
    user: UserMenu | None = None
    # a page without the menu, such as the one for logging in, since there is nowhere to go yet
    is_bare: bool = False


# ======================================================================================================================
class MenuEntry(BaseModel):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    href: str
    label: str
    icon_name: str
    is_current: bool


# ======================================================================================================================
class MenuGroup(BaseModel):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    heading: str
    entries: list[MenuEntry]


# ======================================================================================================================
class Shell(BaseModel):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    head: PageHead
    menu_groups: list[MenuGroup]


# ======================================================================================================================
class PageRenderer:

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def make_menu_groups(head: PageHead) -> list[MenuGroup]:
        # the menu of the sidebar, grouped, with the entry of the page's section lit; a bare page has none
        menu_groups: list[MenuGroup] = []
        if not head.is_bare:
            data_entries: list[MenuEntry] = [
                MenuEntry(
                    href="/repositories",
                    label="Repositories",
                    icon_name="database",
                    is_current=head.section == "repositories",
                ),
            ]
            menu_groups.append(MenuGroup(heading="Data", entries=data_entries))
            if head.user is not None and head.user.is_administrator:
                admin_entries: list[MenuEntry] = [
                    MenuEntry(
                        href="/admin/users", label="Users", icon_name="users", is_current=head.section == "admin-users"
                    ),
                    MenuEntry(
                        href="/admin/groups",
                        label="Groups",
                        icon_name="groups",
                        is_current=head.section == "admin-groups",
                    ),
                    MenuEntry(
                        href="/admin/access",
                        label="Access",
                        icon_name="shield",
                        is_current=head.section == "admin-access",
                    ),
                ]
                menu_groups.append(MenuGroup(heading="Admin", entries=admin_entries))
            if head.user is not None and not head.user.is_development:
                account_entries: list[MenuEntry] = [
                    MenuEntry(href="/account", label="Account", icon_name="user", is_current=head.section == "account"),
                    MenuEntry(
                        href="/account/keys",
                        label="API keys",
                        icon_name="key",
                        is_current=head.section == "account-keys",
                    ),
                ]
                menu_groups.append(MenuGroup(heading="You", entries=account_entries))
        return menu_groups

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================

    # ------------------------------------------------------------------------------------------------------------------
    def __init__(self, templates_dir: Path = TEMPLATES_DIR) -> None:
        # every page is a template that extends layout.html; everything a template is given is escaped unless the
        # template says otherwise, so nothing that a page shows can be taken for markup, and a name that a template
        # uses but the model does not have is a mistake of the program that fails at once instead of showing nothing
        self._environment: jinja2.Environment = jinja2.Environment(
            loader=jinja2.FileSystemLoader(templates_dir),
            autoescape=True,
            undefined=jinja2.StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
        )

    # ------------------------------------------------------------------------------------------------------------------
    def render(self, view_name: str, head: PageHead, page: BaseModel) -> str:
        # the page that the template makes of the page's model, inside the shell, as html
        template: jinja2.Template = self._environment.get_template(f"{view_name}.html")
        shell: Shell = Shell(head=head, menu_groups=PageRenderer.make_menu_groups(head))
        return template.render(shell=shell, page=page)
