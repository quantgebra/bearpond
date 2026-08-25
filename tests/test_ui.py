from collections.abc import Iterator

import httpx
import pytest


# ----------------------------------------------------------------------------------------------------------------------
@pytest.fixture
def ui(live_server: str) -> Iterator[httpx.Client]:
    with httpx.Client(base_url=live_server, timeout=30.0) as http:
        yield http


# ----------------------------------------------------------------------------------------------------------------------
def test_ui_health_endpoint(ui: httpx.Client) -> None:
    response: httpx.Response = ui.get("/ui/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# ----------------------------------------------------------------------------------------------------------------------
def test_ui_serves_index_and_assets(ui: httpx.Client) -> None:
    # the production build must exist for these tests; the scaffold includes a built copy in the package
    index: httpx.Response = ui.get("/")
    assert index.status_code == 200
    assert "text/html" in index.headers.get("content-type", "")
    assert "<div id=\"root\"></div>" in index.text

    # client-side routes should fall back to index.html so React Router can handle them
    spa_route: httpx.Response = ui.get("/repos/some-repo")
    assert spa_route.status_code == 200
    assert "text/html" in spa_route.headers.get("content-type", "")
