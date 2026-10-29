import os
import struct
import subprocess
import time
from copy import deepcopy
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from mediaforge import video_info
from mediaforge.database import VideoFileMetadata
from mediaforge.files import FileService
from mediaforge.video_info import VideoInfoRequest, VideoInfoService, normalize


def sample():
    return {
        "format": {"format_name": "matroska,webm", "duration": "100", "bit_rate": "90000000"},
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "hevc",
                "profile": "Main 10",
                "level": 153,
                "width": 3840,
                "height": 2160,
                "pix_fmt": "yuv420p10le",
                "avg_frame_rate": "60000/1001",
                "color_transfer": "smpte2084",
            },
            {"codec_type": "audio", "codec_name": "truehd", "channels": 8, "sample_rate": "48000"},
            {"codec_type": "subtitle", "codec_name": "hdmv_pgs_subtitle"},
        ],
    }


def add_video(root, name="01.mkv"):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fixture video")
    old = time.time() - 120
    os.utime(path, (old, old))
    return path


@pytest.fixture
def info(settings, store, monkeypatch):
    root = settings.media_roots[0]
    root.mkdir()
    service = VideoInfoService(FileService([root], store), store)
    monkeypatch.setattr(video_info, "locate_ffprobe", lambda: "fixture-ffprobe")
    service.calls = []

    def probe(path, timeout=20):
        service.calls.append(path)
        return deepcopy(sample())

    monkeypatch.setattr(service, "probe", probe)
    try:
        yield service
    finally:
        service.close()


def test_automatic_all_roots_direct_only_cache_persists_across_restart(info):
    first = info.files.roots[0]
    add_video(first)
    nested = add_video(first, "child/02.mkv")
    second = first.with_name("second")
    second.mkdir()
    add_video(second, "03.mp4")
    info.files.roots.append(second)
    info.request_scan()
    info.executor.submit(lambda: None).result(timeout=10)
    assert info.status()["running"] is False
    assert info.status()["summary"]["analyzed"] == 2
    assert len(info.calls) == 2 and nested not in info.calls
    assert (
        info.listing(VideoInfoRequest(root=0))["rows"][0]["details"]["videos"][0]["codec"] == "hevc"
    )
    restarted = VideoInfoService(info.files, info.store)
    try:
        assert restarted.scan(VideoInfoRequest(root=1))["cached"] == 1
        assert restarted.listing(VideoInfoRequest(root=1))["rows"][0]["status"] == "saved"
    finally:
        restarted.close()
    assert (first / "01.mkv").read_bytes() == b"fixture video"


def test_changed_files_reinspect_but_recent_files_wait(info):
    path = add_video(info.files.roots[0])
    assert info.scan(VideoInfoRequest(root=0))["analyzed"] == 1
    assert info.scan(VideoInfoRequest(root=0))["cached"] == 1
    path.write_bytes(b"modified fixture video")
    assert info.listing(VideoInfoRequest(root=0))["rows"][0]["status"] == "changed"
    assert info.scan(VideoInfoRequest(root=0))["deferred"] == 1
    old = time.time() - 120
    os.utime(path, (old, old))
    assert info.scan(VideoInfoRequest(root=0))["analyzed"] == 1
    assert len(info.calls) == 2
    assert info.scan(VideoInfoRequest(root=0, force=True))["analyzed"] == 1


def test_only_common_video_extensions_are_listed_probed_and_saved(info):
    root = info.files.roots[0]
    videos = {
        f"video{extension}"
        for extension in (
            ".mkv",
            ".MP4",
            ".avi",
            ".mov",
            ".m4v",
            ".ts",
            ".wmv",
            ".m2ts",
            ".webm",
            ".flv",
            ".mpg",
            ".mpeg",
            ".mts",
        )
    }
    ignored = {
        "tvshow.nfo",
        "poster.jpg",
        "background.png",
        "01.srt",
        "01.ass",
        "notes.txt",
        "metadata.json",
        "music.mp3",
        "audio.flac",
        "archive.zip",
        "program.exe",
        "video.mp4.part",
        "video.mkv.txt",
        "no-extension",
        "legacy.rm",
        "legacy.rmvb",
        "legacy.asf",
        "disc.vob",
    }
    for name in videos | ignored:
        add_video(root, name)
    (root / "folder.mp4").mkdir()
    add_video(root, "nested/video.mkv")

    assert {row["name"] for row in info.listing(VideoInfoRequest(root=0))["rows"]} == videos
    info.request_scan()
    info.executor.submit(lambda: None).result(timeout=10)
    assert info.status()["summary"]["analyzed"] == len(videos)
    assert {path.name for path in info.calls} == videos
    with info.store.sessions() as session:
        assert set(session.scalars(select(VideoFileMetadata.file_path))) == videos

    info.calls.clear()
    assert info.scan(VideoInfoRequest(root=0, force=True))["analyzed"] == len(videos)
    assert {path.name for path in info.calls} == videos
    assert all((root / name).read_bytes() == b"fixture video" for name in ignored)


def test_failed_probe_is_recorded_and_retried_without_changing_video(info, monkeypatch):
    path = add_video(info.files.roots[0])
    probe = info.probe

    def fail(path, timeout):
        raise ValueError("Invalid data found when processing input")

    monkeypatch.setattr(info, "probe", fail)
    assert info.scan(VideoInfoRequest(root=0))["failed"] == 1
    row = info.listing(VideoInfoRequest(root=0))["rows"][0]
    assert row["status"] == "failed" and row["details"] is None
    assert "Invalid data" in row["error"]
    assert path.read_bytes() == b"fixture video"
    monkeypatch.setattr(info, "probe", probe)
    assert info.scan(VideoInfoRequest(root=0))["analyzed"] == 1


def test_change_during_probe_and_missing_tool_do_not_save_false_results(info, monkeypatch):
    path = add_video(info.files.roots[0])

    def changing_probe(path, timeout):
        path.write_bytes(b"changed while reading")
        return sample()

    monkeypatch.setattr(info, "probe", changing_probe)
    assert info.scan(VideoInfoRequest(root=0))["deferred"] == 1
    assert info.listing(VideoInfoRequest(root=0))["rows"][0]["status"] == "missing"
    monkeypatch.setattr(video_info, "locate_ffprobe", lambda: None)
    with pytest.raises(ValueError, match="未找到 ffprobe"):
        info.scan(VideoInfoRequest(root=0))
    assert path.exists()


def test_unavailable_first_root_does_not_prevent_later_roots(info):
    first = info.files.roots[0]
    second = first.with_name("second")
    second.mkdir()
    add_video(second)
    info.files.roots = [first / "offline", second]
    info.request_scan()
    info.executor.submit(lambda: None).result(timeout=10)
    assert info.status()["summary"]["analyzed"] == 1
    assert info.status()["summary"]["failed"] == 1
    assert "offline" in info.status()["error"]


def test_normalized_values_and_conditional_playback_risks():
    details = normalize(sample(), 2000)
    video = details["videos"][0]
    assert video["fps"] == pytest.approx(59.94006, rel=1e-5)
    assert video["bit_depth"] == 10 and video["hdr"] == "HDR / PQ"
    assert details["bit_rate"] == 90_000_000 and details["estimated_bit_rate"] is None
    risks = " ".join(details["risks"])
    assert all(marker in risks for marker in ("高帧率", "色调映射", "字幕", "百兆", "音频转码"))
    data = sample()
    data["format"].pop("bit_rate")
    data["streams"][0].update(codec_name="h264", avg_frame_rate="0/0")
    data["streams"][0].pop("color_transfer")
    details = normalize(data, 10_000_000)
    assert details["estimated_bit_rate"] == 800_000
    assert details["videos"][0]["fps"] is None
    assert details["videos"][0]["hdr"] == "未发现 HDR 标记"
    assert any("H.264" in risk for risk in details["risks"])


def test_probe_uses_read_only_bounded_local_command(settings, store, monkeypatch):
    service = VideoInfoService(FileService([], store), store)
    monkeypatch.setattr(video_info, "locate_ffprobe", lambda: "ffprobe")

    def fake_run(arguments, **kwargs):
        assert "-protocol_whitelist" in arguments and "file" in arguments
        assert "-format_whitelist" in arguments
        assert "-c" not in arguments and kwargs["timeout"] == 20
        kwargs["stdout"].write(b'{"streams": [], "format": {}}')
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    try:
        assert service.probe(settings.media_roots[0] / "video.mkv") == {"streams": [], "format": {}}
    finally:
        service.close()


def test_real_ffprobe_reads_avi_without_modifying_input(settings, store):
    if not video_info.locate_ffprobe():
        pytest.skip("Requires ffprobe")
    root = settings.media_roots[0]
    root.mkdir()

    def chunk(tag, content):
        return (
            tag + struct.pack("<I", len(content)) + content + (b"\0" if len(content) % 2 else b"")
        )

    avih = struct.pack("<14I", 500000, 32, 0, 0x10, 2, 0, 1, 16, 2, 2, 0, 0, 0, 0)
    strh = struct.pack(
        "<4s4sIHH8I4h", b"vids", b"DIB ", 0, 0, 0, 0, 1, 2, 0, 2, 16, 0xFFFFFFFF, 0, 0, 0, 2, 2
    )
    strf = struct.pack("<IiiHHIIiiII", 40, 2, 2, 1, 24, 0, 16, 0, 0, 0, 0)
    headers = chunk(b"avih", avih) + chunk(
        b"LIST", b"strl" + chunk(b"strh", strh) + chunk(b"strf", strf)
    )
    content = (
        b"AVI "
        + chunk(b"LIST", b"hdrl" + headers)
        + chunk(b"LIST", b"movi" + chunk(b"00db", bytes(16)) * 2)
    )
    path = root / "tiny.avi"
    original = chunk(b"RIFF", content)
    path.write_bytes(original)
    old = time.time() - 120
    os.utime(path, (old, old))
    service = VideoInfoService(FileService([root], store), store)
    try:
        result = service.scan(VideoInfoRequest(root=0))
        assert result["analyzed"] == 1, result
        details = service.listing(VideoInfoRequest(root=0))["rows"][0]["details"]
        assert details["videos"][0]["width"] == 2
        assert details["videos"][0]["height"] == 2
        assert details["videos"][0]["codec"] == "rawvideo"
        assert path.read_bytes() == original
    finally:
        service.close()
