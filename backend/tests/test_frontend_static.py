from fastapi.testclient import TestClient

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
