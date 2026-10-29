import logging
import os
import shutil
import subprocess
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import httpx
import pytest

from mediaforge.database import operation_actor
from mediaforge.files import FileService
from mediaforge.metadata import (
    MetadataApply,
    MetadataCleanup,
    MetadataMappingInput,
    MetadataPreview,
    MetadataSearch,
    MetadataService,
    TMDBClient,
    folder_query,
    match_candidate,
)
from mediaforge.renaming import RenameApplyRequest, RenamePreviewRequest, RenameService

KEY = "a" * 32
JPEG = b"\xff\xd8\xfftest-image"


@pytest.fixture
def metadata(settings, store, monkeypatch):
    root = settings.media_roots[0]
    root.mkdir()
    series = root / "示例剧 (2020)"
    season = series
    season.mkdir(parents=True)
    (season / "Show.S01E01.mkv").write_bytes(b"video")
    (season / "02.mp4").write_bytes(b"video")
    (season / "Show.S01E03-E04.mkv").write_bytes(b"multi")
    (season / "unknown.mp4").write_bytes(b"unknown")
    (season / "Show.S01E01-trailer.mp4").write_bytes(b"extra")
    calls = []

    def respond(request):
        calls.append(request)
        path = request.url.path
        if request.url.host == "image.tmdb.org":
            return httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"})
        if path.startswith("/3/search/"):
            query = request.url.params["query"]
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "id": 42,
                            "name": query,
                            "title": query,
                            "first_air_date": "2020-01-02",
                            "release_date": "2020-01-02",
                        }
                    ],
                    "total_pages": 1,
                },
            )
        if "/season/" in path:
            return httpx.Response(
                200,
                json={
                    "episodes": [
                        {
                            "id": 100 + number,
                            "name": f"第 {number} 集",
                            "overview": "剧情 < & >",
                            "season_number": 1,
                            "episode_number": number,
                            "air_date": "2020-01-02",
                            "runtime": 45,
                        }
                        for number in (1, 2)
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "id": 42,
                "name": "示例剧",
                "title": "示例电影",
                "overview": "简介 < & >",
                "first_air_date": "2020-01-02",
                "release_date": "2020-01-02",
                "poster_path": "/poster.jpg",
                "backdrop_path": "/backdrop.jpg",
                "genres": [{"name": "剧情"}],
                "external_ids": {"imdb_id": "tt123", "tvdb_id": 456},
                "credits": {"cast": [{"name": "演员", "character": "角色", "order": 0}]},
            },
        )

    service = MetadataService(settings, FileService([root], store), store)
    monkeypatch.setattr(
        service,
        "client",
        lambda key, language, scraper_id="tmdb": TMDBClient(
            KEY, language, httpx.MockTransport(respond)
        ),
    )
    service.calls = calls
    return service


def test_preview_and_write_emby_series_episodes_and_artwork(metadata):
    preview = metadata.preview(MetadataPreview(root=0, path="示例剧 (2020)", api_key=KEY))
    assert preview["file_count"] == 5
    row = preview["rows"][0]
    assert row["tmdb_id"] == 42
    assert any("多集" in warning for warning in row["warnings"])
    assert any("无法识别" in warning for warning in row["warnings"])
    root = metadata.files.roots[0]
    assert not list(root.rglob("*.nfo"))
    assert not any(request.url.host == "image.tmdb.org" for request in metadata.calls)
    search = metadata.calls[0]
    assert search.url.params["query"] == "示例剧"
    assert search.url.params["first_air_date_year"] == "2020"
    result = metadata.apply(MetadataApply(preview_id=preview["preview_id"], api_key=KEY))
    assert result["outcome"] == "partial"  # Unrecognised/multi-episode files are reported.
    series = root / "示例剧 (2020)"
    show = ET.parse(series / "tvshow.nfo").getroot()
    assert show.tag == "tvshow"
    assert show.findtext("title") == "示例剧"
    assert show.findtext("plot") == "简介 < & >"
    assert show.findtext("uniqueid[@type='tmdb']") == "42"
    assert show.findtext("imdbid") == "tt123"
    assert show.findtext("tvdbid") == "456"
    assert show.findtext("actor/name") == "演员"
    for filename, number in (("Show.S01E01.nfo", "1"), ("02.nfo", "2")):
        episode = ET.parse(series / filename).getroot()
        assert episode.tag == "episodedetails"
        assert episode.findtext("season") == "1"
        assert episode.findtext("episode") == number
        assert episode.findtext("aired") == "2020-01-02"
        assert episode.findtext("tmdbid") == str(100 + int(number))
    assert (series / "poster.jpg").read_bytes() == JPEG
    assert (series / "fanart.jpg").read_bytes() == JPEG
    assert not (series / "Show.S01E03-E04.nfo").exists()
    assert not (series / "Show.S01E01-trailer.nfo").exists()
    assert all(KEY not in str(record) for record in metadata.store.recent())
    with pytest.raises(ValueError, match="预览"):
        metadata.apply(MetadataApply(preview_id=preview["preview_id"]))


def test_resolution_after_episode_is_not_a_multi_episode_marker(metadata):
    season = metadata.files.roots[0] / "示例剧 (2020)"
    (season / "Show.S01E01.1080p.mkv").write_bytes(b"video")
    plan = metadata.preview(MetadataPreview(root=0, path="示例剧 (2020)"))
    metadata.apply(MetadataApply(preview_id=plan["preview_id"]))
    assert (season / "Show.S01E01.1080p.nfo").is_file()


def test_selected_root_mapping_rename_and_scrape_share_flat_directory(metadata):
    folder = metadata.files.roots[0] / "示例剧 (2020)"
    nested = folder / "Season 01"
    nested.mkdir()
    video = nested / "Nested.S01E01.mkv"
    video.write_bytes(b"nested video")
    metadata.files.roots = [folder]
    saved = metadata.save_mapping(
        MetadataMappingInput(root=0, path="", media_type="tv", tmdb_id=42)
    )
    assert saved["folder_path"] == ""
    assert saved["folder_name"] == folder.name
    renaming = RenameService(metadata.files, metadata.store)
    rename_plan = renaming.preview(RenamePreviewRequest(root=0))
    renaming.apply(RenameApplyRequest(preview_id=rename_plan["preview_id"]))
    assert (folder / "01.mkv").read_bytes() == b"video"
    assert (folder / "02.mp4").read_bytes() == b"video"
    plan = metadata.preview(MetadataPreview(root=0))
    assert len(plan["rows"]) == 1
    assert plan["rows"][0]["folder_name"] == folder.name
    assert plan["rows"][0]["match_source"] == "manual"
    assert all(Path(path).parent == Path() for path in plan["rows"][0]["files"])
    metadata.apply(MetadataApply(preview_id=plan["preview_id"]))
    assert ET.parse(folder / "01.nfo").findtext("season") == "1"
    assert ET.parse(folder / "02.nfo").findtext("episode") == "2"
    assert (folder / "tvshow.nfo").exists()
    assert video.read_bytes() == b"nested video"
    assert list(nested.iterdir()) == [video]
    assert not any("/search/" in request.url.path for request in metadata.calls)


@pytest.mark.parametrize("filename", ["00.mkv", "1000.mp4", "Show.S01E0000.avi"])
def test_flat_episode_numbers_outside_supported_range_are_skipped(metadata, filename):
    folder = metadata.files.roots[0] / "示例剧 (2020)"
    (folder / filename).write_bytes(b"invalid episode")
    plan = metadata.preview(MetadataPreview(root=0, path=folder.name))
    assert plan["file_count"] == 5
    assert any(filename in warning for warning in plan["rows"][0]["warnings"])
    assert (
        str(Path(folder.name) / Path(filename).with_suffix(".nfo")) not in plan["rows"][0]["files"]
    )


def test_movie_nfo_preservation_and_explicit_backup(metadata):
    root = metadata.files.roots[0]
    movie = root / "示例剧 (2020)"
    original = b"existing metadata"
    (movie / "movie.nfo").write_bytes(original)
    (movie / "poster.jpg").write_bytes(b"old poster")
    preserved = metadata.preview(MetadataPreview(root=0, path="示例剧 (2020)", media_type="movie"))
    assert preserved["file_count"] == 1
    metadata.apply(MetadataApply(preview_id=preserved["preview_id"]))
    assert (movie / "movie.nfo").read_bytes() == original
    assert not list(movie.glob("*.bak.*"))
    replaced = metadata.preview(
        MetadataPreview(root=0, path="示例剧 (2020)", media_type="movie", overwrite=True)
    )
    assert replaced["file_count"] == 3
    result = metadata.apply(MetadataApply(preview_id=replaced["preview_id"]))
    assert result["outcome"] == "success"
    assert ET.parse(movie / "movie.nfo").getroot().tag == "movie"
    assert ET.parse(movie / "movie.nfo").findtext("title") == "示例电影"
    assert [path.read_bytes() for path in movie.glob("movie.nfo.bak.*")] == [original]
    assert [path.read_bytes() for path in movie.glob("poster.jpg.bak.*")] == [b"old poster"]
    assert len([item for item in result["files"] if item.get("backup")]) == 3


@pytest.mark.parametrize("change", ["target", "video"])
def test_changed_files_abort_before_any_write(metadata, change):
    preview = metadata.preview(MetadataPreview(root=0, path="示例剧 (2020)"))
    series = metadata.files.roots[0] / "示例剧 (2020)"
    if change == "target":
        (series / "poster.jpg").write_bytes(b"concurrent write")
    else:
        (series / "Show.S01E01.mkv").unlink()
    with pytest.raises((ValueError, FileNotFoundError)):
        metadata.apply(MetadataApply(preview_id=preview["preview_id"]))
    assert not (series / "tvshow.nfo").exists()
    assert metadata.store.recent()[0]["status"] == "failed"


def test_preview_expiry_and_ownership(metadata, monkeypatch):
    preview = metadata.preview(MetadataPreview(root=0, path="示例剧 (2020)"))
    token = operation_actor.set("other-user")
    try:
        with pytest.raises(ValueError, match="当前用户"):
            metadata.apply(MetadataApply(preview_id=preview["preview_id"]))
    finally:
        operation_actor.reset(token)
    metadata.previews[preview["preview_id"]].created -= 1801
    with pytest.raises(ValueError, match="过期"):
        metadata.apply(MetadataApply(preview_id=preview["preview_id"]))


@pytest.mark.parametrize("change", ["none", "mapping", "roots"])
def test_preview_network_releases_file_lock_and_rejects_stale_configuration(
    metadata, monkeypatch, change
):
    entered, release = Event(), Event()
    original = TMDBClient.details

    def details(client, media_type, media_id):
        entered.set()
        assert release.wait(5)
        return original(client, media_type, media_id)

    monkeypatch.setattr(TMDBClient, "details", details)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(metadata.preview, MetadataPreview(root=0, path="示例剧 (2020)"))
        try:
            assert entered.wait(5)
            assert pool.submit(metadata.files.list, 0, "").result(timeout=3)["entries"]
            if change == "mapping":
                metadata.save_mapping(
                    MetadataMappingInput(root=0, path="示例剧 (2020)", media_type="tv", tmdb_id=42)
                )
            elif change == "roots":
                with metadata.files.lock:
                    metadata.files.roots = []
        finally:
            release.set()
        if change == "none":
            assert pending.result(timeout=5)["file_count"] == 5
        else:
            with pytest.raises(ValueError, match="变化"):
                pending.result(timeout=5)
            assert not metadata.previews


@pytest.mark.parametrize("change", ["source", "target", "configuration"])
def test_image_download_releases_lock_and_revalidates_before_writes(metadata, monkeypatch, change):
    plan = metadata.preview(MetadataPreview(root=0, path="示例剧 (2020)"))
    entered, release = Event(), Event()
    original = TMDBClient.image

    def image(client, path):
        entered.set()
        assert release.wait(5)
        return original(client, path)

    monkeypatch.setattr(TMDBClient, "image", image)
    series = metadata.files.roots[0] / "示例剧 (2020)"
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(metadata.apply, MetadataApply(preview_id=plan["preview_id"]))
        try:
            assert entered.wait(5)
            assert pool.submit(metadata.files.list, 0, "").result(timeout=3)["entries"]
            with metadata.files.lock:
                if change == "source":
                    (series / "Show.S01E01.mkv").write_bytes(b"changed while downloading")
                elif change == "target":
                    (series / "poster.jpg").write_bytes(b"do not overwrite")
                else:
                    metadata.previews.clear()
        finally:
            release.set()
        result = pending.result(timeout=5)
    assert result["outcome"] == "partial"
    if change == "source":
        assert not (series / "Show.S01E01.nfo").exists()
    elif change == "target":
        assert (series / "poster.jpg").read_bytes() == b"do not overwrite"
    else:
        assert not (series / "poster.jpg").exists()
        assert not (series / "Show.S01E01.nfo").exists()
    assert plan["preview_id"] not in metadata.previews
    assert any(item["status"] == "failed" for item in result["files"])


def test_tmdb_network_errors_never_expose_key(caplog):
    def fail(request):
        raise httpx.ConnectError("failed " + str(request.url), request=request)

    caplog.set_level(logging.DEBUG)
    client = TMDBClient(KEY, "zh-CN", httpx.MockTransport(fail))
    with pytest.raises(ValueError, match="网络") as error:
        client.get("search/tv", query="show")
    assert KEY not in str(error.value)
    assert KEY not in caplog.text
    client.close()


def test_tmdb_key_and_bearer_auth_and_invalid_responses(caplog):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"results": []})

    caplog.set_level(logging.INFO)
    client = TMDBClient(KEY, "zh-CN", httpx.MockTransport(respond))
    client.get("search/tv", query="show")
    assert requests[0].url.params["api_key"] == KEY
    assert requests[0].url.params["language"] == "zh-CN"
    client.close()
    bearer = "token.payload.signature"
    client = TMDBClient(bearer, "en-US", httpx.MockTransport(respond))
    client.get("search/movie", query="film")
    assert requests[-1].headers["Authorization"] == "Bearer " + bearer
    assert "api_key" not in requests[-1].url.params
    assert KEY not in caplog.text and bearer not in caplog.text
    client.close()
    for response, message in (
        (httpx.Response(401), "HTTP 401"),
        (httpx.Response(429), "HTTP 429"),
        (httpx.Response(200, content=b"not JSON"), "JSON"),
        (httpx.Response(200, json=[]), "无效数据"),
    ):
        client = TMDBClient(
            KEY, "zh-CN", httpx.MockTransport(lambda request, response=response: response)
        )
        with pytest.raises(ValueError, match=message):
            client.get("search/tv")
        client.close()


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b"<html>bad</html>", headers={"content-type": "text/html"}),
        httpx.Response(200, content=b"not jpeg", headers={"content-type": "image/jpeg"}),
        httpx.Response(302, headers={"location": "https://example.com/image.jpg"}),
    ],
)
def test_invalid_image_does_not_replace_existing_poster(metadata, monkeypatch, response):
    series = metadata.files.roots[0] / "示例剧 (2020)"
    (series / "poster.jpg").write_bytes(b"original")
    preview = metadata.preview(
        MetadataPreview(root=0, path="示例剧 (2020)", media_type="movie", overwrite=True)
    )
    monkeypatch.setattr(
        metadata,
        "client",
        lambda key, language, scraper_id="tmdb": TMDBClient(
            KEY, language, httpx.MockTransport(lambda request: response)
        ),
    )
    result = metadata.apply(MetadataApply(preview_id=preview["preview_id"]))
    assert result["outcome"] == "partial"
    assert (series / "poster.jpg").read_bytes() == b"original"
    assert not list(series.glob("poster.jpg.bak.*"))
    assert not list(series.glob(".mediaforge-*"))
    assert any(item.get("error") for item in result["files"])


def test_ambiguous_search_skips_without_writing(metadata, monkeypatch):
    monkeypatch.setattr(
        metadata,
        "client",
        lambda key, language, scraper_id="tmdb": TMDBClient(
            KEY,
            language,
            httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json={
                        "results": [
                            {"id": 1, "name": "示例剧", "first_air_date": "2020-01-01"},
                            {"id": 2, "name": "示例剧", "first_air_date": "2020-02-01"},
                        ]
                    },
                )
            ),
        ),
    )
    preview = metadata.preview(MetadataPreview(root=0, path="示例剧 (2020)"))
    assert preview["outcome"] == "failed"
    assert preview["file_count"] == 0
    assert preview["rows"][0]["status"] == "skipped"
    assert not metadata.previews


def test_strict_name_year_matching_and_folder_cleanup():
    assert folder_query("Breaking.Bad.(2008).1080p") == ("Breaking Bad", "2008")
    assert folder_query("示例剧 (2020) {tmdb-42}") == ("示例剧", "2020")
    rows = [
        {"id": 1, "name": "Show", "first_air_date": "2000-01-01"},
        {"id": 2, "name": "Show", "first_air_date": "2020-01-01"},
    ]
    assert match_candidate(rows, "Show", None, "tv") is None
    assert match_candidate(rows, "Show", "2020", "tv")["id"] == 2
    assert match_candidate(rows, "Sho", "2020", "tv") is None


def test_paths_and_linked_targets_are_rejected(metadata, tmp_path):
    with pytest.raises(ValueError):
        metadata.preview(MetadataPreview(root=0, path="../outside"))
    series = metadata.files.roots[0] / "示例剧 (2020)"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "sentinel.nfo").write_bytes(b"keep")
    link = series / "linked"
    if os.name == "nt":
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
            check=True,
            capture_output=True,
        )
    else:
        link.symlink_to(outside, target_is_directory=True)
    try:
        with pytest.raises(ValueError, match="链接"):
            metadata.preview(MetadataPreview(root=0, path="示例剧 (2020)"))
        assert (outside / "sentinel.nfo").read_bytes() == b"keep"
        assert not list(series.glob("*.nfo"))
    finally:
        if os.name == "nt":
            link.rmdir()
        else:
            link.unlink()


def test_failed_backup_preserves_original(metadata, monkeypatch):
    series = metadata.files.roots[0] / "示例剧 (2020)"
    original = series / "movie.nfo"
    original.write_bytes(b"original")
    preview = metadata.preview(
        MetadataPreview(root=0, path="示例剧 (2020)", media_type="movie", overwrite=True)
    )
    real_open = Path.open

    def fail_backup(path, *args, **kwargs):
        if ".bak." in path.name:
            raise PermissionError("backup denied")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_backup)
    result = metadata.apply(MetadataApply(preview_id=preview["preview_id"]))
    assert result["outcome"] == "partial"
    assert original.read_bytes() == b"original"
    assert result["files"][0]["error"] == "backup denied"
    assert not list(series.glob(".mediaforge-*"))


def test_partial_backup_is_removed_without_touching_original(metadata, monkeypatch):
    series = metadata.files.roots[0] / "示例剧 (2020)"
    original = series / "movie.nfo"
    original.write_bytes(b"original")
    preview = metadata.preview(
        MetadataPreview(root=0, path="示例剧 (2020)", media_type="movie", overwrite=True)
    )

    def fail_copy(source, destination):
        destination.write(b"partial")
        raise OSError("copy failed")

    monkeypatch.setattr(shutil, "copyfileobj", fail_copy)
    result = metadata.apply(MetadataApply(preview_id=preview["preview_id"]))
    assert result["files"][0]["error"] == "copy failed"
    assert original.read_bytes() == b"original"
    assert not list(series.glob("movie.nfo.bak.*"))


def test_same_stem_versions_do_not_duplicate_episode_nfo(metadata):
    season = metadata.files.roots[0] / "示例剧 (2020)"
    (season / "Show.S01E01.mp4").write_bytes(b"another version")
    preview = metadata.preview(MetadataPreview(root=0, path="示例剧 (2020)"))
    planned = preview["rows"][0]["files"]
    assert len(planned) == len(set(planned)) == 5


def test_missing_season_does_not_discard_series_metadata(metadata):
    season = metadata.files.roots[0] / "示例剧 (2020)"
    (season / "Show.S02E01.mkv").write_bytes(b"unknown season")
    preview = metadata.preview(MetadataPreview(root=0, path="示例剧 (2020)"))
    assert preview["rows"][0]["status"] == "matched"
    assert preview["file_count"] == 5
    assert any("第 2 季分集刮削失败" in warning for warning in preview["rows"][0]["warnings"])


def test_persisted_mapping_bypasses_name_year_search_and_survives_service_restart(
    metadata, monkeypatch
):
    root = metadata.files.roots[0]
    alias = "手动指定的别名 (1999)"
    (root / "示例剧 (2020)").rename(root / alias)
    metadata.save_mapping(MetadataMappingInput(media_type="tv", root=0, path=alias, tmdb_id=42))
    restarted = MetadataService(metadata.settings, metadata.files, metadata.store)
    monkeypatch.setattr(restarted, "client", metadata.client)
    preview = restarted.preview(MetadataPreview(root=0, path=alias))
    row = preview["rows"][0]
    assert row["status"] == "matched"
    assert row["match_source"] == "manual"
    assert row["folder_name"] == alias
    assert row["title"] == "示例剧"
    assert row["year"] == "2020"
    assert row["tmdb_id"] == 42
    assert not any("/search/" in request.url.path for request in metadata.calls)
    applied = restarted.apply(MetadataApply(preview_id=preview["preview_id"]))
    assert applied["files"][0]["status"] == "success"
    assert ET.parse(root / alias / "tvshow.nfo").findtext("tmdbid") == "42"


def test_mapping_update_delete_and_media_type_isolation(metadata):
    tv_preview = metadata.preview(MetadataPreview(root=0, path="示例剧 (2020)"))
    movie_preview = metadata.preview(
        MetadataPreview(root=0, path="示例剧 (2020)", media_type="movie")
    )
    request = MetadataMappingInput(media_type="tv", root=0, path="示例剧 (2020)", tmdb_id=42)
    saved = metadata.save_mapping(request)
    assert tv_preview["preview_id"] not in metadata.previews
    assert movie_preview["preview_id"] in metadata.previews
    updated = metadata.save_mapping(request.model_copy(update={"tmdb_id": 43}))
    assert updated["id"] == saved["id"]
    assert len(metadata.mappings()) == 1
    assert metadata.mappings()[0]["tmdb_id"] == 43
    metadata.save_mapping(
        MetadataMappingInput(
            media_type="movie",
            root=0,
            path=request.path,
            tmdb_id=44,
        )
    )
    assert len(metadata.mappings()) == 2
    metadata.delete_mapping(updated["id"])
    assert [(item["media_type"], item["tmdb_id"]) for item in metadata.mappings()] == [
        ("movie", 44)
    ]
    automatic = metadata.preview(MetadataPreview(root=0, path="示例剧 (2020)"))
    assert automatic["rows"][0]["match_source"] == "automatic"
    assert any("/search/tv" in request.url.path for request in metadata.calls)
    with pytest.raises(ValueError, match="不存在"):
        metadata.delete_mapping(updated["id"])


def test_invalid_manual_id_fails_explicitly_without_automatic_fallback(metadata, monkeypatch):
    metadata.save_mapping(
        MetadataMappingInput(
            media_type="tv",
            root=0,
            path="示例剧 (2020)",
            tmdb_id=999,
        )
    )
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(404)

    monkeypatch.setattr(
        metadata,
        "client",
        lambda key, language, scraper_id="tmdb": TMDBClient(
            KEY, language, httpx.MockTransport(respond)
        ),
    )
    preview = metadata.preview(MetadataPreview(root=0, path="示例剧 (2020)"))
    assert preview["file_count"] == 0
    assert preview["rows"][0]["status"] == "failed"
    assert preview["rows"][0]["match_source"] == "manual"
    assert preview["rows"][0]["tmdb_id"] == 999
    assert "HTTP 404" in preview["rows"][0]["reason"]
    assert [request.url.path for request in calls] == ["/3/tv/999"]


@pytest.mark.parametrize("media_type", ["tv", "movie"])
def test_search_returns_all_candidates_and_paginates_without_saving_or_writing(
    metadata, monkeypatch, media_type
):
    results = [
        {"id": 1, "name": "另一个译名", "original_name": "Alias", "overview": "第一部"},
        {"id": 2, "name": "不同的候选", "original_name": "Another", "overview": "第二部"},
    ]
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "page": 2,
                "total_pages": 3,
                "total_results": 45,
                "results": results,
            },
        )

    monkeypatch.setattr(
        metadata,
        "client",
        lambda key, language, scraper_id="tmdb": TMDBClient(
            KEY, language, httpx.MockTransport(respond)
        ),
    )
    report = metadata.search(
        MetadataSearch(
            query=" Alias ",
            media_type=media_type,
            language="zh-TW",
            page=2,
            api_key=KEY,
        )
    )
    assert report["query"] == "Alias"
    assert report["media_type"] == media_type
    assert report["results"] == [{**item, "scraper_id": "tmdb"} for item in results]
    assert report["page"] == 2 and report["total_pages"] == 3 and report["total_results"] == 45
    assert len(calls) == 1
    assert calls[0].url.path == "/3/search/" + media_type
    assert calls[0].url.params["query"] == "Alias"
    assert calls[0].url.params["page"] == "2"
    assert calls[0].url.params["language"] == "zh-TW"
    assert metadata.mappings() == []
    assert not list(metadata.files.roots[0].rglob("*.nfo"))
    assert not list(metadata.files.roots[0].rglob("*.jpg"))
    assert KEY not in str(metadata.store.recent())


def test_search_uses_selected_folder_name_without_restricting_candidate_year(metadata):
    report = metadata.search(
        MetadataSearch(
            query="示例剧 (2020) 1080p",
            parse_folder_name=True,
            api_key=KEY,
        )
    )
    assert report["query"] == "示例剧"
    assert metadata.calls[0].url.params["query"] == "示例剧"
    assert "first_air_date_year" not in metadata.calls[0].url.params
    assert metadata.mappings() == []


def test_associations_are_scoped_to_actual_folder_and_survive_root_reordering(metadata):
    root = metadata.files.roots[0]
    for parent in ("A", "B"):
        (root / parent / "同名剧").mkdir(parents=True)
    metadata.save_mapping(
        MetadataMappingInput(
            root=0,
            path=str(Path("A") / "同名剧"),
            media_type="tv",
            tmdb_id=42,
        )
    )
    unmapped = metadata.preview(MetadataPreview(root=0, path=str(Path("B") / "同名剧")))
    assert unmapped["rows"][0]["match_source"] == "automatic"
    metadata.save_mapping(
        MetadataMappingInput(
            root=0,
            path=str(Path("B") / "同名剧"),
            media_type="tv",
            tmdb_id=43,
        )
    )
    assert len(metadata.mappings()) == 2
    other_root = root.parent / "other-media"
    other_root.mkdir()
    metadata.files.roots.insert(0, other_root)
    assert all(item["root"] == 1 for item in metadata.mappings())
    mapped = metadata.preview(MetadataPreview(root=1, path=str(Path("A") / "同名剧")))
    assert mapped["rows"][0]["match_source"] == "manual"
    assert mapped["rows"][0]["tmdb_id"] == 42


def test_association_requires_an_existing_safe_directory(metadata):
    for path in ("../outside", "missing", str(Path("示例剧 (2020)") / "02.mp4")):
        with pytest.raises((ValueError, FileNotFoundError)):
            metadata.save_mapping(
                MetadataMappingInput(
                    root=0,
                    path=path,
                    media_type="tv",
                    tmdb_id=42,
                )
            )
    assert metadata.mappings() == []


def test_cleanup_only_removes_confirmed_missing_folders_and_404_ids(metadata, monkeypatch):
    root = metadata.files.roots[0]
    gone = root / "gone"
    gone.mkdir()
    invalid_id = root / "invalid-id"
    invalid_id.mkdir()
    sentinel = invalid_id / "movie.nfo"
    sentinel.write_bytes(b"keep metadata")
    for folder, media_type, tmdb_id in (
        ("gone", "movie", 42),
        ("invalid-id", "movie", 999),
        ("示例剧 (2020)", "tv", 42),
    ):
        metadata.save_mapping(
            MetadataMappingInput(
                root=0,
                path=folder,
                media_type=media_type,
                tmdb_id=tmdb_id,
            )
        )
    gone.rmdir()

    def respond(request):
        if request.url.path.endswith("/999"):
            return httpx.Response(404)
        return httpx.Response(200, json={"id": 42})

    monkeypatch.setattr(
        metadata,
        "client",
        lambda key, language, scraper_id="tmdb": TMDBClient(
            KEY, language, httpx.MockTransport(respond)
        ),
    )
    checked = metadata.cleanup_mappings(MetadataCleanup(api_key=KEY))
    assert checked["invalid_count"] == 2
    assert checked["removed_count"] == 0
    assert len(metadata.mappings()) == 3
    cleaned = metadata.cleanup_mappings(MetadataCleanup(dry_run=False, api_key=KEY))
    assert cleaned["removed_count"] == 2
    assert {row["status"] for row in cleaned["rows"]} == {"valid", "missing_folder", "missing_id"}
    assert len(metadata.mappings()) == 1
    assert metadata.mappings()[0]["folder_name"] == "示例剧 (2020)"
    assert sentinel.read_bytes() == b"keep metadata"
    assert KEY not in str(metadata.store.recent())


@pytest.mark.parametrize("failure", [401, 403, 429, 500, "network", "invalid_payload"])
def test_cleanup_preserves_associations_on_unverifiable_tmdb_response(
    metadata, monkeypatch, failure
):
    metadata.save_mapping(
        MetadataMappingInput(
            root=0,
            path="示例剧 (2020)",
            media_type="tv",
            tmdb_id=42,
        )
    )

    def respond(request):
        if failure == "network":
            raise httpx.ConnectError("unavailable", request=request)
        if failure == "invalid_payload":
            return httpx.Response(200, json={})
        return httpx.Response(failure)

    monkeypatch.setattr(
        metadata,
        "client",
        lambda key, language, scraper_id="tmdb": TMDBClient(
            KEY, language, httpx.MockTransport(respond)
        ),
    )
    result = metadata.cleanup_mappings(MetadataCleanup(dry_run=False))
    assert result["removed_count"] == 0
    assert result["rows"][0]["status"] == "unverified"
    assert result["rows"][0]["reason"]
    assert result["outcome"] == "partial"
    assert len(metadata.mappings()) == 1


@pytest.mark.parametrize("unavailable", ["offline", "unconfigured", "permission"])
def test_cleanup_preserves_associations_when_directory_cannot_be_verified(
    metadata, monkeypatch, unavailable
):
    root = metadata.files.roots[0]
    folder = root / "示例剧 (2020)"
    metadata.save_mapping(
        MetadataMappingInput(
            root=0,
            path=folder.name,
            media_type="tv",
            tmdb_id=42,
        )
    )
    if unavailable == "offline":
        root.rename(root.with_name("offline-media"))
    elif unavailable == "unconfigured":
        metadata.files.roots = []
    else:
        real_stat = Path.stat

        def denied(path, *args, **kwargs):
            if path == folder:
                raise PermissionError("access denied")
            return real_stat(path, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", denied)
    result = metadata.cleanup_mappings(MetadataCleanup(dry_run=False))
    assert result["removed_count"] == 0
    assert result["rows"][0]["status"] == "unverified"
    assert len(metadata.mappings()) == 1


def test_cleanup_without_key_can_remove_missing_folder_but_preserves_existing_ids(
    metadata, monkeypatch
):
    root = metadata.files.roots[0]
    gone = root / "gone"
    gone.mkdir()
    for name in (gone.name, "示例剧 (2020)"):
        metadata.save_mapping(MetadataMappingInput(root=0, path=name, media_type="tv", tmdb_id=42))
    gone.rmdir()
    monkeypatch.setattr(
        metadata,
        "client",
        lambda key, language: MetadataService.client(metadata, key, language),
    )
    result = metadata.cleanup_mappings(MetadataCleanup(dry_run=False))
    assert result["removed_count"] == 1
    assert result["outcome"] == "partial"
    assert metadata.mappings()[0]["folder_name"] == "示例剧 (2020)"
