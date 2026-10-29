import os
import time
from threading import Event

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from mediaforge.app import create_app
from mediaforge.auth import UserCreate
from mediaforge.metadata import TMDBClient
from mediaforge.scrapers import ScraperDefinition
from mediaforge.video_info import VideoInfoService

HEADERS = {"X-MediaForge-Request": "1"}


def test_regex_rename_api_preview_edit_confirmation_and_unchanged_directory(client):
    assert client.get("/api/renaming/rules").status_code == 401
    assert login(client, "viewer", "viewer-password-123").status_code == 200
    rules = client.get("/api/renaming/rules").json()
    assert (
        client.put("/api/renaming/rules", json={"rules": rules}, headers=HEADERS).status_code == 403
    )
    assert (
        client.post("/api/renaming/preview", json={"root": 0}, headers=HEADERS).status_code == 403
    )
    assert login(client).status_code == 200
    assert client.put("/api/renaming/rules", json={"rules": rules}).status_code == 403
    assert (
        client.put("/api/renaming/rules", json={"rules": rules}, headers=HEADERS).status_code == 200
    )
    root = client.app.state.files.roots[0]
    root.mkdir()
    (root / "Example.01.mkv").write_bytes(b"video")
    (root / "child").mkdir()
    (root / "child" / "02.mkv").write_bytes(b"child")
    assert (
        client.post(
            "/api/renaming/apply", json={"preview_id": "a" * 32}, headers=HEADERS
        ).status_code
        == 400
    )
    preview = client.post("/api/renaming/preview", json={"root": 0}, headers=HEADERS).json()
    assert preview["ready_count"] == 1
    assert (root / "Example.01.mkv").exists()
    revised = client.post(
        "/api/renaming/review",
        headers=HEADERS,
        json={
            "preview_id": preview["preview_id"],
            "edits": [{"source": "Example.01.mkv", "new_name": "03.mkv"}],
        },
    ).json()
    result = client.post(
        "/api/renaming/apply", json={"preview_id": revised["preview_id"]}, headers=HEADERS
    )
    assert result.status_code == 200
    assert result.json()["renamed_count"] == 1
    assert (root / "03.mkv").exists()
    assert (root / "child" / "02.mkv").exists()
    assert (
        client.post("/api/renaming/preview", json={"root": 0}, headers=HEADERS).json()["unchanged"]
        is True
    )
    assert client.get("/api/operations").json()[1]["kind"] == "rename.apply"


def test_multiple_media_roots_rename_only_selected_directory(client, tmp_path):
    assert login(client).status_code == 200
    first, second = tmp_path / "first-library", tmp_path / "second-library"
    first.mkdir()
    second.mkdir()
    (first / "Example.01.mkv").write_bytes(b"first")
    (second / "Example.02.mkv").write_bytes(b"second")
    response = client.put(
        "/api/config/media-roots", json={"roots": [str(first), str(second)]}, headers=HEADERS
    )
    assert response.status_code == 200
    config = client.get("/api/config").json()
    assert [root["path"] for root in config["roots"]] == [str(first), str(second)]
    preview = client.post(
        "/api/renaming/preview",
        json={"root": 1, "roots_version": config["roots_version"]},
        headers=HEADERS,
    ).json()
    assert [row["name"] for row in preview["rows"]] == ["Example.02.mkv"]
    assert preview["rows"][0]["new_name"] == "02.mkv"
    assert (second / "Example.02.mkv").exists()
    result = client.post(
        "/api/renaming/apply", json={"preview_id": preview["preview_id"]}, headers=HEADERS
    )
    assert result.status_code == 200
    assert (second / "02.mkv").read_bytes() == b"second"
    assert (first / "Example.01.mkv").read_bytes() == b"first"
    assert (
        client.post("/api/renaming/preview", json={"root": 0}, headers=HEADERS).json()[
            "ready_count"
        ]
        == 1
    )


def test_scraper_settings_permissions_credentials_and_missing_key(client):
    path = "/api/metadata/scrapers"
    assert client.get(path).status_code == 401
    assert login(client, "viewer", "viewer-password-123").status_code == 200
    assert client.get(path).status_code == 200
    body = {"scrapers": [{"id": "tmdb", "api_key": "c" * 32}]}
    assert client.put(path, json=body, headers=HEADERS).status_code == 403
    assert login(client).status_code == 200
    assert client.put(path, json=body).status_code == 403
    response = client.put(path, json=body, headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["scrapers"][0]["key_source"] == "database"
    service = client.app.state.metadata
    definition = service.scrapers.registry["tmdb"]
    calls = []

    def respond(request):
        calls.append(request)
        assert request.url.params["api_key"] == "c" * 32
        return httpx.Response(200, json={"results": [], "total_pages": 1})

    service.scrapers.registry["tmdb"] = ScraperDefinition(
        "tmdb",
        "TMDB",
        lambda key, language: TMDBClient(key, language, httpx.MockTransport(respond)),
        definition.validate_key,
    )
    assert (
        client.post("/api/metadata/search", json={"query": "Example"}, headers=HEADERS).status_code
        == 200
    )
    assert len(calls) == 1
    assert client.get("/api/config").json()["tmdb_key_configured"] is True
    for endpoint in (path, "/api/config", "/api/operations", "/api/logs"):
        assert "c" * 32 not in client.get(endpoint).text
    assert "c" * 32 not in response.text
    body["scrapers"][0] = {"id": "tmdb", "clear_api_key": True}
    assert client.put(path, json=body, headers=HEADERS).status_code == 200
    assert client.get("/api/config").json()["tmdb_key_configured"] is False
    root = service.files.roots[0]
    root.mkdir()
    (root / "Example").mkdir()
    for endpoint, payload in (
        ("/api/metadata/search", {"query": "Example"}),
        ("/api/metadata/preview", {"root": 0}),
    ):
        response = client.post(endpoint, json=payload, headers=HEADERS)
        assert response.status_code == 400
        assert "尚未配置 TMDB Key" in response.json()["detail"]
    assert len(calls) == 1


@pytest.fixture
def client(settings, sessions, monkeypatch):
    engine = sessions.kw["bind"]
    monkeypatch.setattr("mediaforge.app.make_engine", lambda url: engine)
    # The fixture already created isolated test tables; never initialize the real schema here.
    monkeypatch.setattr("mediaforge.app.initialize_database", lambda engine: None)
    app = create_app(settings)
    # Test the scanner independently; automatic background writes would race unrelated assertions.
    monkeypatch.setattr(app.state.video_info, "request_scan", app.state.video_info.status)
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


def test_media_root_configuration_permissions_live_access_and_stale_requests(
    client, tmp_path, monkeypatch
):
    endpoint = "/api/config/media-roots"
    library = tmp_path / "library"
    library.mkdir()
    (library / "Example").mkdir()
    body = {"roots": [str(library)]}
    assert client.put(endpoint, json=body, headers=HEADERS).status_code == 401
    assert login(client, "viewer", "viewer-password-123").status_code == 200
    assert client.put(endpoint, json=body, headers=HEADERS).status_code == 403
    assert login(client).status_code == 200
    assert client.put(endpoint, json=body).status_code == 403
    response = client.put(endpoint, json=body, headers=HEADERS)
    assert response.status_code == 200
    config = client.get("/api/config").json()
    assert config["roots"] == [{"id": 0, "path": str(library.resolve())}]
    assert client.get("/api/files?root=0").json()["entries"][0]["name"] == "Example"
    service = client.app.state.metadata

    def respond(request):
        if "/search/" in request.url.path:
            return httpx.Response(200, json={"results": [{"id": 42, "title": "Example"}]})
        return httpx.Response(200, json={"id": 42, "title": "Example"})

    monkeypatch.setattr(
        service,
        "client",
        lambda key, language, scraper_id="tmdb": TMDBClient(
            "a" * 32, language, httpx.MockTransport(respond)
        ),
    )
    preview = client.post(
        "/api/metadata/preview",
        headers=HEADERS,
        json={
            "root": 0,
            "path": "Example",
            "media_type": "movie",
            "roots_version": config["roots_version"],
        },
    ).json()
    assert preview["file_count"] == 1
    assert client.put(endpoint, json={"roots": []}, headers=HEADERS).status_code == 200
    assert client.get("/api/config").json()["roots"] == []
    assert client.get("/api/files?root=0").status_code == 400
    assert (
        client.post(
            "/api/metadata/apply", json={"preview_id": preview["preview_id"]}, headers=HEADERS
        ).status_code
        == 400
    )
    for path, payload in (
        ("/api/metadata/preview", {"root": 0}),
        (
            "/api/metadata/mappings",
            {
                "root": 0,
                "path": "Example",
                "media_type": "movie",
                "tmdb_id": 42,
            },
        ),
    ):
        response = client.post(
            path, json={**payload, "roots_version": config["roots_version"]}, headers=HEADERS
        )
        assert response.status_code == 400
        assert "配置已变化" in response.json()["detail"]
    assert (library / "Example").is_dir()
    assert not list(library.rglob("*.nfo"))
    assert client.get("/api/operations").json()[0]["kind"] == "media.roots.save"


def test_media_folder_picker_requires_admin_and_selected_folder_can_be_saved(
    client, tmp_path, monkeypatch
):
    endpoint = "/api/config/media-folders"
    monkeypatch.setattr("mediaforge.files.media_drives", lambda: [tmp_path])
    library = tmp_path / "library"
    (library / "shows").mkdir(parents=True)
    (library / "ignore.mkv").write_bytes(b"video")
    assert client.get(endpoint).status_code == 401
    assert login(client, "viewer", "viewer-password-123").status_code == 200
    assert client.get(endpoint).status_code == 403
    assert login(client).status_code == 200
    assert client.get(endpoint).json()["entries"][0]["path"] == str(tmp_path)
    response = client.get(endpoint, params={"path": str(library)})
    assert response.status_code == 200
    listing = response.json()
    assert listing["selectable"] is True
    assert [item["name"] for item in listing["entries"]] == ["shows"]
    saved = client.put(
        "/api/config/media-roots", json={"roots": [listing["path"]]}, headers=HEADERS
    )
    assert saved.status_code == 200
    assert client.get("/api/config").json()["roots"][0]["path"] == listing["path"]
    assert client.get(endpoint, params={"path": "relative"}).status_code == 400


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


def test_subscription_api_selection_and_permissions(client, monkeypatch):
    service = client.app.state.hosts
    monkeypatch.setattr(
        service,
        "scan_subscription",
        lambda: {
            "entries": [
                {
                    "domain": "api.themoviedb.org",
                    "ip": "1.1.1.1",
                    "latency_ms": None,
                    "source": "checktmdb",
                    "retained": False,
                }
            ],
            "failures": [],
            "unresolved": [],
            "partial": False,
            "diagnostics": [],
            "logs": [],
            "source": "checktmdb",
            "exports": {"ipv4": "", "ipv6": ""},
        },
    )
    assert login(client, "viewer", "viewer-password-123").status_code == 200
    preview = client.post(
        "/api/hosts/update", json={"source": "checktmdb", "dry_run": True}, headers=HEADERS
    )
    assert preview.status_code == 200
    assert preview.json()["source"] == "checktmdb"
    assert (
        client.post(
            "/api/hosts/update", json={"source": "checktmdb", "dry_run": False}, headers=HEADERS
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/hosts/update", json={"source": "https://example.com/hosts"}, headers=HEADERS
        ).status_code
        == 422
    )
    assert login(client).status_code == 200
    updated = client.post(
        "/api/hosts/update", json={"source": "checktmdb", "dry_run": False}, headers=HEADERS
    )
    assert updated.status_code == 200
    assert updated.json()["changed"] is True
    assert client.get("/api/config").json()["hosts_source"] == "dns"


def test_metadata_requires_admin_and_does_not_expose_configured_key(client):
    assert (
        client.post("/api/metadata/preview", json={"root": 0}, headers=HEADERS).status_code == 401
    )
    assert login(client, "viewer", "viewer-password-123").status_code == 200
    assert (
        client.post("/api/metadata/preview", json={"root": 0}, headers=HEADERS).status_code == 403
    )
    assert (
        client.post(
            "/api/metadata/apply", json={"preview_id": "a" * 32}, headers=HEADERS
        ).status_code
        == 403
    )
    assert login(client).status_code == 200
    config = client.get("/api/config").json()
    assert config["tmdb_key_configured"] is False
    assert "tmdb_api_key" not in config
    assert (
        client.post(
            "/api/metadata/apply", json={"preview_id": "a" * 32}, headers=HEADERS
        ).status_code
        == 400
    )
    assert (
        next(item for item in client.get("/api/modules").json() if item["id"] == "metadata")[
            "status"
        ]
        == "available"
    )


def test_metadata_preview_apply_api_and_credential_redaction(client, monkeypatch):
    assert login(client).status_code == 200
    service = client.app.state.metadata
    root = service.files.roots[0]
    root.mkdir()
    movie = root / "Example (2020)"
    movie.mkdir()
    key = "f" * 32
    service.settings.tmdb_api_key = SecretStr(key)

    def respond(request):
        if "/search/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"id": 41, "title": "Example", "release_date": "2020-01-01"},
                        {"id": 42, "title": "Example", "release_date": "2020-01-01"},
                    ],
                },
            )
        return httpx.Response(
            200,
            json={
                "id": 42,
                "title": "Example",
                "release_date": "2020-01-01",
                "overview": "Plot",
            },
        )

    monkeypatch.setattr(
        service,
        "client",
        lambda secret, language, scraper_id="tmdb": TMDBClient(
            key, language, httpx.MockTransport(respond)
        ),
    )
    config = client.get("/api/config")
    assert config.json()["tmdb_key_configured"] is True
    assert key not in config.text
    selected_folder = client.get("/api/files?root=0").json()["entries"][0]
    search = client.post(
        "/api/metadata/search",
        json={"query": selected_folder["name"], "media_type": "movie", "parse_folder_name": True},
        headers=HEADERS,
    )
    assert search.status_code == 200
    chosen_id = search.json()["results"][1]["id"]
    associated = client.post(
        "/api/metadata/mappings",
        json={
            "root": 0,
            "path": selected_folder["path"],
            "media_type": "movie",
            "tmdb_id": chosen_id,
        },
        headers=HEADERS,
    )
    assert associated.status_code == 200
    preview = client.post(
        "/api/metadata/preview",
        json={"root": 0, "path": movie.name, "media_type": "movie", "api_key": key},
        headers=HEADERS,
    )
    assert preview.status_code == 200
    assert preview.json()["file_count"] == 1
    assert preview.json()["rows"][0]["match_source"] == "manual"
    assert preview.json()["rows"][0]["tmdb_id"] == chosen_id
    assert not (movie / "movie.nfo").exists()
    applied = client.post(
        "/api/metadata/apply",
        json={"preview_id": preview.json()["preview_id"], "api_key": key},
        headers=HEADERS,
    )
    assert applied.status_code == 200
    assert (movie / "movie.nfo").exists()
    assert applied.json()["files"][0]["status"] == "success"
    records = client.get("/api/operations")
    assert key not in records.text
    assert all(record["actor"] == "admin" for record in records.json())


def test_metadata_mapping_permissions_validation_and_management(client):
    path = "/api/metadata/mappings"
    mapping = {"media_type": "tv", "root": 0, "path": "别名 (2020)", "tmdb_id": 42}
    root = client.app.state.metadata.files.roots[0]
    root.mkdir()
    (root / mapping["path"]).mkdir()
    assert client.get(path).status_code == 401
    assert client.post(path, json=mapping, headers=HEADERS).status_code == 401
    assert login(client, "viewer", "viewer-password-123").status_code == 200
    assert client.get(path).json() == []
    assert client.post(path, json=mapping, headers=HEADERS).status_code == 403
    assert client.delete(path + "/missing", headers=HEADERS).status_code == 403
    assert client.post(path + "/cleanup", json={}, headers=HEADERS).status_code == 403
    assert login(client).status_code == 200
    assert client.post(path, json=mapping).status_code == 403
    for fields in (
        {"tmdb_id": 0},
        {"tmdb_id": -1},
        {"tmdb_id": 1.5},
        {"tmdb_id": True},
        {"tmdb_id": "42"},
        {"tmdb_id": 2147483648},
        {"media_type": "episode"},
        {"root": -1},
    ):
        assert client.post(path, json={**mapping, **fields}, headers=HEADERS).status_code == 422
    saved = client.post(path, json=mapping, headers=HEADERS).json()
    updated = client.post(path, json={**mapping, "tmdb_id": 43}, headers=HEADERS).json()
    assert saved["id"] == updated["id"]
    assert client.get(path).json() == [
        {
            "id": saved["id"],
            "media_type": "tv",
            "root": 0,
            "root_path": str(root),
            "folder_path": mapping["path"],
            "folder_name": mapping["path"],
            "tmdb_id": 43,
        }
    ]
    assert client.delete(path + "/" + saved["id"], headers=HEADERS).status_code == 200
    assert client.get(path).json() == []
    records = client.get("/api/operations").json()
    assert [record["kind"] for record in records] == [
        "metadata.mapping.delete",
        "metadata.mapping.save",
        "metadata.mapping.save",
    ]
    assert all(record["actor"] == "admin" for record in records)


def test_metadata_mapping_can_target_selected_root_itself(client):
    assert login(client).status_code == 200
    root = client.app.state.metadata.files.roots[0]
    root.mkdir()
    response = client.post(
        "/api/metadata/mappings",
        json={"root": 0, "path": "", "media_type": "tv", "tmdb_id": 42},
        headers=HEADERS,
    )
    assert response.status_code == 200
    mapping = response.json()
    assert mapping["folder_path"] == ""
    assert mapping["folder_name"] == root.name
    assert client.get("/api/metadata/mappings").json() == [
        {key: value for key, value in mapping.items() if key != "operation_id"}
    ]


def test_video_info_permissions_readonly_scan_and_media_root_trigger(client, monkeypatch):
    root = client.app.state.files.roots[0]
    root.mkdir()
    path = root / "01.mp4"
    original = b"video metadata fixture"
    path.write_bytes(original)
    old = time.time() - 120
    os.utime(path, (old, old))
    monkeypatch.setattr("mediaforge.video_info.locate_ffprobe", lambda: "fixture")
    monkeypatch.setattr(
        client.app.state.video_info,
        "probe",
        lambda path, timeout: {
            "format": {"duration": "10", "bit_rate": "10000000", "format_name": "mp4"},
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": 1920,
                    "height": 1080,
                    "pix_fmt": "yuv420p",
                    "avg_frame_rate": "24/1",
                }
            ],
        },
    )
    assert client.get("/api/video-info/status").status_code == 401
    assert client.post("/api/video-info/scan-all", json={}, headers=HEADERS).status_code == 401
    assert login(client, "viewer", "viewer-password-123").status_code == 200
    assert client.get("/api/video-info?root=0").json()["rows"][0]["status"] == "missing"
    assert client.post("/api/video-info/scan", json={"root": 0}, headers=HEADERS).status_code == 403
    assert login(client).status_code == 200
    assert client.post("/api/video-info/scan", json={"root": 0}).status_code == 403
    result = client.post("/api/video-info/scan", json={"root": 0}, headers=HEADERS)
    assert result.status_code == 200 and result.json()["analyzed"] == 1
    row = client.get("/api/video-info?root=0").json()["rows"][0]
    assert row["details"]["videos"][0]["codec"] == "h264"
    assert row["status"] == "saved" and path.read_bytes() == original
    assert (
        client.post(
            "/api/video-info/scan", json={"root": 0, "path": "../outside"}, headers=HEADERS
        ).status_code
        == 400
    )
    calls = []
    monkeypatch.setattr(client.app.state.video_info, "request_scan", lambda: calls.append(True))
    assert (
        client.put(
            "/api/config/media-roots", json={"roots": [str(root)]}, headers=HEADERS
        ).status_code
        == 200
    )
    assert calls == [True]


def test_video_info_startup_requests_automatic_scan(settings, sessions, monkeypatch):
    engine = sessions.kw["bind"]
    monkeypatch.setattr("mediaforge.app.make_engine", lambda url: engine)
    monkeypatch.setattr("mediaforge.app.initialize_database", lambda engine: None)
    requested = Event()
    monkeypatch.setattr(VideoInfoService, "request_scan", lambda self: requested.set())
    with TestClient(create_app(settings)):
        assert requested.wait(3)


def test_cleanup_api_removes_only_association_records(client):
    assert login(client).status_code == 200
    root = client.app.state.metadata.files.roots[0]
    root.mkdir()
    folder = root / "gone"
    folder.mkdir()
    mapping = client.post(
        "/api/metadata/mappings",
        json={"root": 0, "path": folder.name, "media_type": "tv", "tmdb_id": 42},
        headers=HEADERS,
    )
    assert mapping.status_code == 200
    folder.rmdir()
    checked = client.post("/api/metadata/mappings/cleanup", json={}, headers=HEADERS)
    assert checked.status_code == 200
    assert checked.json()["invalid_count"] == 1
    assert checked.json()["removed_count"] == 0
    cleaned = client.post(
        "/api/metadata/mappings/cleanup",
        json={"dry_run": False},
        headers=HEADERS,
    )
    assert cleaned.status_code == 200
    assert cleaned.json()["removed_count"] == 1
    assert client.get("/api/metadata/mappings").json() == []


def test_metadata_search_api_permissions_candidates_and_errors(client, monkeypatch):
    path = "/api/metadata/search"
    body = {"query": "示例剧", "media_type": "tv", "api_key": "a" * 32}
    assert client.post(path, json=body, headers=HEADERS).status_code == 401
    assert login(client, "viewer", "viewer-password-123").status_code == 200
    assert client.post(path, json=body, headers=HEADERS).status_code == 403
    assert login(client).status_code == 200
    assert client.post(path, json=body).status_code == 403
    for fields in ({"query": " "}, {"query": ""}, {"page": 0}, {"page": 501}):
        assert client.post(path, json={**body, **fields}, headers=HEADERS).status_code == 422
    service = client.app.state.metadata
    results = [
        {"id": 1, "name": "示例剧", "first_air_date": "2020-01-01"},
        {"id": 2, "name": "示例剧", "first_air_date": "2025-01-01"},
    ]
    monkeypatch.setattr(
        service,
        "client",
        lambda key, language, scraper_id="tmdb": TMDBClient(
            "a" * 32,
            language,
            httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json={"page": 1, "results": results, "total_pages": 1, "total_results": 2},
                )
            ),
        ),
    )
    response = client.post(path, json=body, headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["results"] == [{**item, "scraper_id": "tmdb"} for item in results]
    assert client.get("/api/metadata/mappings").json() == []
    assert "a" * 32 not in client.get("/api/operations").text
    monkeypatch.setattr(
        service,
        "client",
        lambda key, language, scraper_id="tmdb": TMDBClient(
            "a" * 32,
            language,
            httpx.MockTransport(lambda request: httpx.Response(429)),
        ),
    )
    failure = client.post(path, json=body, headers=HEADERS)
    assert failure.status_code == 400
    assert "HTTP 429" in failure.json()["detail"]
    assert "a" * 32 not in failure.text
    assert client.get("/api/operations").json()[0]["status"] == "failed"
