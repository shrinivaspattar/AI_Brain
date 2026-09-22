from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.db.session import get_db
from app.main import app


def test_root_serves_frontend_index() -> None:
    client = TestClient(app)

    response = client.get("/")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "AI_Brain" in response.text


def test_frontend_static_assets_are_served() -> None:
    client = TestClient(app)

    style_response = client.get("/style.css")
    script_response = client.get("/app.js")

    assert style_response.status_code == 200
    assert "text/css" in style_response.headers["content-type"]

    assert script_response.status_code == 200
    assert "javascript" in script_response.headers["content-type"]


def test_static_mount_does_not_shadow_api_routes() -> None:
    client = TestClient(app)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() != {}


def test_root_index_includes_import_jobs_nav() -> None:
    client = TestClient(app)

    response = client.get("/")

    assert "Import Jobs" in response.text


def test_root_index_includes_memory_review_nav() -> None:
    client = TestClient(app)

    response = client.get("/")

    assert "Memory Review" in response.text


def test_root_index_includes_dedup_review_nav_and_safety_banner() -> None:
    client = TestClient(app)

    response = client.get("/")

    assert "Dedup Review" in response.text
    assert "does not modify your files" in response.text


def test_chat_route_with_path_param_not_shadowed_by_static_mount() -> None:
    """A path-param API route (/chat/{conversation_id}) must still reach
    the real FastAPI handler and return its JSON error shape, not a
    generic HTML 404 from the StaticFiles mount swallowing the path."""
    db = MagicMock()
    db.get.return_value = None
    app.dependency_overrides[get_db] = lambda: db

    try:
        client = TestClient(app)
        response = client.get("/chat/nonexistent-conversation-id")

        assert response.status_code == 404
        assert response.json() == {
            "detail": "Conversation nonexistent-conversation-id not found"
        }

    finally:
        app.dependency_overrides.clear()


def test_dedup_reviews_route_not_shadowed_by_static_mount() -> None:
    db = MagicMock()
    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.dedup_reviews.DedupReviewService") as service_class:
            service_class.return_value.list_reviews.return_value = []

            client = TestClient(app)
            response = client.get("/dedup/reviews")

            assert response.status_code == 200
            assert response.json() == []

    finally:
        app.dependency_overrides.clear()


def test_vendored_markdown_libraries_are_served_locally() -> None:
    """No runtime CDN dependency for markdown rendering - both files are
    committed under frontend/vendor/ and served by the same static mount."""
    client = TestClient(app)

    marked_response = client.get("/vendor/marked.min.js")
    purify_response = client.get("/vendor/purify.min.js")

    assert marked_response.status_code == 200
    assert "javascript" in marked_response.headers["content-type"]

    assert purify_response.status_code == 200
    assert "javascript" in purify_response.headers["content-type"]
