from fastapi.testclient import TestClient


def create_link(client: TestClient, **body) -> dict:
    """Create a link through the API and return its JSON (asserts success)."""
    body.setdefault("url", "https://example.com/page")
    response = client.post("/api/v1/urls", json=body)
    assert response.status_code == 201, response.text
    return response.json()
