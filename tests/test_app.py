import pytest
from fastapi.testclient import TestClient

from mediaforge.app import create_app
from mediaforge.auth import UserCreate

HEADERS = {"X-MediaForge-Request": "1"}


@pytest.fixture
def client(settings, sessions, monkeypatch):
    engine = sessions.kw["bind"]
    monkeypatch.setattr("mediaforge.app.make_engine", lambda url: engine)
    # The fixture already created isolated test tables; never initialize the real schema here.
    monkeypatch.setattr("mediaforge.app.initialize_database", lambda engine: None)
    app = create_app(settings)
    app.state.auth.create_user(
        UserCreate(username="admin", password="test-password-123", role="admin")
    )
    app.state.auth.create_user(
        UserCreate(username="viewer", password="viewer-password-123", role="viewer")
    )
    with TestClient(app) as client:
        yield client


def login(client, username="admin", password="test-password-123"):
    return client.post(
        "/api/auth/login", json={"username": username, "password": password}, headers=HEADERS
    )


def test_page_auth_csrf_cookie_and_role_enforcement(client):
    page = client.get("/")
    assert page.status_code == 200 and '<div id="app">' in page.text
    assert client.get("/api/config").status_code == 401
    assert (
        client.post(
            "/api/auth/login", json={"username": "admin", "password": "test-password-123"}
        ).status_code
        == 403
    )
    response = login(client, "viewer", "viewer-password-123")
    assert response.status_code == 200
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=strict" in response.headers["set-cookie"]
    assert client.get("/api/config").status_code == 200
    assert client.get("/api/users").status_code == 403
    assert (
        client.post(
            "/api/files/action",
            json={"action": "delete", "root": 0, "path": "movie.mkv", "dry_run": False},
            headers=HEADERS,
        ).status_code
        == 403
    )
    assert (
        client.post("/api/hosts/update", json={"dry_run": False}, headers=HEADERS).status_code
        == 403
    )
    client.post("/api/auth/logout", json={}, headers=HEADERS)
    assert client.get("/api/auth/me").status_code == 401


def test_admin_user_management_logs_actor_without_passwords(client):
    assert login(client).status_code == 200
    response = client.post(
        "/api/users",
        json={"username": "new-viewer", "password": "private-password-123", "role": "viewer"},
        headers=HEADERS,
    )
    assert response.status_code == 200
    logs = client.get("/api/operations")
    assert logs.status_code == 200
    assert logs.json()[0]["actor"] == "admin"
    assert "private-password-123" not in logs.text
    user_id = response.json()["id"]
    response = client.put(
        f"/api/users/{user_id}", json={"role": "viewer", "active": False}, headers=HEADERS
    )
    assert response.status_code == 200
    users = client.get("/api/users").json()
    assert next(user for user in users if user["id"] == user_id)["active"] is False


def test_runtime_logs_require_login_and_are_bounded(client):
    assert client.get("/api/logs").status_code == 401
    assert login(client, "viewer", "viewer-password-123").status_code == 200
    response = client.get("/api/logs?limit=1")
    assert response.status_code == 200
    assert len(response.json()["lines"]) == 1
    assert "初始化完成" in response.json()["lines"][0]
    assert client.get("/api/logs?limit=1001").status_code == 422
    assert client.get("/api/logs?path=.env").json()["path"] == response.json()["path"]


def test_hosts_error_response_includes_execution_logs(client, monkeypatch):
    assert login(client).status_code == 200
    service = client.app.state.hosts
    monkeypatch.setattr(
        service,
        "scan",
        lambda: {
            "entries": [],
            "failures": ["api.themoviedb.org"],
            "unresolved": [{"domain": "api.themoviedb.org", "record_type": "A"}],
            "partial": True,
            "diagnostics": [],
            "logs": [],
            "exports": {"ipv4": "", "ipv6": ""},
        },
    )
    response = client.post("/api/hosts/update", json={"dry_run": False}, headers=HEADERS)
    assert response.status_code == 400
    assert response.json()["report"]["outcome"] == "failed"
    assert response.json()["report"]["logs"][-1]["level"] == "error"
    assert client.get("/api/operations").json()[0]["status"] == "failed"
