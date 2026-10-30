import json

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy.exc import OperationalError

from mediaforge.database import EmbySetting
from mediaforge.emby import EmbyRefresh, EmbyService, EmbyUpdate

TOKEN = "fixture-emby-token"
URL = "http://emby.invalid/emby"


@pytest.fixture
def emby(store):
    service = EmbyService(store)
    service.save(EmbyUpdate(server_url=URL, api_token=SecretStr(TOKEN)))
    return service


def test_settings_preserve_token_without_exposing_it_and_clear_immediately(emby, sessions):
    assert emby.configuration() == {"server_url": URL, "configured": True, "auto_refresh": False}
    emby.save(EmbyUpdate(server_url=URL, auto_refresh=True))
    with sessions() as session:
        assert session.get(EmbySetting, 1).api_token == TOKEN
    with pytest.raises(ValueError, match="重新填写"):
        emby.save(EmbyUpdate(server_url="http://other.invalid"))
    assert emby.configuration()["server_url"] == URL
    assert TOKEN not in json.dumps(emby.store.recent())
    assert emby.clear_token()["configured"] is False
    assert not emby.configuration()["auto_refresh"]
    with pytest.raises(ValueError, match="Token"):
        emby.test_connection()


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/emby",
        "http://user:password@emby.invalid",
        "http://emby.invalid?api_key=bad",
        "http://emby.invalid/#fragment",
        "http://emby.invalid:99999",
        "http://emby.invalid/\npath",
    ],
)
def test_rejects_unsafe_urls(emby, url):
    with pytest.raises(ValueError, match="地址"):
        emby.save(EmbyUpdate(server_url=url))
    assert emby.configuration()["server_url"] == URL


def test_refresh_endpoints_header_and_non_destructive_options(emby):
    calls = []

    def respond(request):
        calls.append(request)
        assert request.headers["X-Emby-Token"] == TOKEN
        assert TOKEN not in str(request.url)
        if request.url.path.endswith("System/Info"):
            return httpx.Response(200, json={"ServerName": "Fixture", "Version": "4"})
        if request.url.path.endswith("VirtualFolders/Query"):
            assert request.url.params["StartIndex"] == "0"
            return httpx.Response(
                200, json={"Items": [{"ItemId": "123", "Name": "TV"}], "TotalRecordCount": 1}
            )
        assert request.method == "POST" and json.loads(request.content) == {}
        return httpx.Response(204)

    emby.transport = httpx.MockTransport(respond)
    assert emby.test_connection()["server_name"] == "Fixture"
    assert emby.refresh(EmbyRefresh())["accepted"]
    assert calls[-1].url.path == "/emby/Library/Refresh"
    assert emby.refresh(EmbyRefresh(library_id="123"))["accepted"]
    request = calls[-1]
    assert request.url.path == "/emby/Items/123/Refresh"
    assert request.url.params["Recursive"] == "true"
    assert request.url.params["ReplaceAllMetadata"] == "false"
    assert request.url.params["ReplaceAllImages"] == "false"
    count = len(calls)
    with pytest.raises(ValueError, match="已不存在"):
        emby.refresh(EmbyRefresh(library_id="missing"))
    assert len(calls) == count + 1 and calls[-1].method == "GET"
    assert TOKEN not in json.dumps(emby.store.recent())


def test_library_pagination(emby):
    def respond(request):
        start = int(request.url.params["StartIndex"])
        count = 200 if start == 0 else 1
        return httpx.Response(
            200,
            json={
                "Items": [
                    {"Id": str(index), "Name": str(index)} for index in range(start, start + count)
                ],
                "TotalRecordCount": 201,
            },
        )

    emby.transport = httpx.MockTransport(respond)
    assert len(emby.libraries()) == 201


@pytest.mark.parametrize("status", [302, 401, 500])
def test_remote_errors_never_echo_body_or_follow_redirects(emby, status, caplog):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(status, content=TOKEN, headers={"Location": "http://other.invalid"})

    emby.transport = httpx.MockTransport(respond)
    with pytest.raises(ValueError, match=f"HTTP {status}") as error:
        emby.refresh(EmbyRefresh())
    assert len(calls) == 1
    assert TOKEN not in str(error.value) + caplog.text + json.dumps(emby.store.recent())


def test_automatic_refresh_only_after_changes_and_failure_keeps_media_result(emby, monkeypatch):
    calls = []
    emby.transport = httpx.MockTransport(
        lambda request: calls.append(request) or httpx.Response(204)
    )
    original = {"outcome": "success", "moved_count": 2}
    assert emby.after_change(dict(original), True) == original
    emby.save(EmbyUpdate(server_url=URL, auto_refresh=True))
    assert emby.after_change(dict(original), False) == original and not calls
    assert emby.after_change(dict(original), True)["emby_refresh"]["accepted"]
    assert len(calls) == 1
    emby.transport = httpx.MockTransport(lambda request: httpx.Response(503))
    result = emby.after_change(dict(original), True)
    assert result["moved_count"] == 2 and result["outcome"] == "partial"
    assert result["emby_refresh"]["status"] == "failed"

    def unavailable():
        raise OperationalError("private query", {}, Exception("private credentials"))

    monkeypatch.setattr(emby, "_setting", unavailable)
    result = emby.after_change(dict(original), True)
    assert result["moved_count"] == 2 and result["outcome"] == "partial"
    assert "private" not in json.dumps(result)
