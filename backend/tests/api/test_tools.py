from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from app.db.session import get_db
from app.main import app


def test_list_tools_returns_registered_tools() -> None:
    db = MagicMock()

    app.dependency_overrides[get_db] = lambda: db

    try:
        client = TestClient(app)

        response = client.get("/tools")

        assert response.status_code == 200

        names = {tool["name"] for tool in response.json()}
        assert names == {
            "search_knowledge_base",
            "get_current_datetime",
            "list_recent_documents",
        }

        for tool in response.json():
            assert isinstance(tool["description"], str)
            assert tool["description"]

    finally:
        app.dependency_overrides.clear()
