import json
import logging
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from fractions import Fraction
from hashlib import sha256
from pathlib import Path
from threading import RLock
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from mediaforge.database import OperationStore, VideoFileMetadata
from mediaforge.files import FileService, signature
from mediaforge.metadata import VIDEO_EXTENSIONS

logger = logging.getLogger(__name__)
FORMAT_WHITELIST = "matroska,webm,mov,mp4,m4a,3gp,3g2,mj2,avi,mpegts,mpeg,asf,flv"
INSPECTION_EXTENSIONS = VIDEO_EXTENSIONS | {
    ".webm",
    ".flv",
    ".mpg",
    ".mpeg",
    ".mts",
}
SCAN_INTERVAL_MINUTES = 10
STABLE_SECONDS = 60


class VideoInfoRequest(BaseModel):
    root: int = Field(ge=0)
    path: str = Field(default="", max_length=2000)
    roots_version: str | None = None
    force: bool = False


def locate_ffprobe() -> str | None:
    executable = shutil.which("ffprobe")
    if not executable and os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        local = Path(os.environ["LOCALAPPDATA"])
        for folder in (local / "MediaForge" / "tools", local / "Microsoft" / "WinGet" / "Links"):
            executable = shutil.which("ffprobe", path=str(folder))
            if executable:
                break
    return executable


def number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and result > 0 else None


def frame_rate(value: Any) -> float | None:
    try:
        return number(Fraction(str(value)))
    except (ValueError, ZeroDivisionError):
        return None


def normalize(data: dict[str, Any], size: int) -> dict[str, Any]:
    streams = data.get("streams")
    container = data.get("format")
    if (
        not isinstance(streams, list)
        or not isinstance(container, dict)
        or any(not isinstance(stream, dict) for stream in streams)
    ):
        raise ValueError("ffprobe 返回了无效的视频信息")
    videos, audios, subtitles, risks = [], [], [], []
    for stream in streams:
        if any(not isinstance(stream.get(key, {}), dict) for key in ("tags", "disposition")):
            raise ValueError("ffprobe 返回了无效的轨道属性")
        codec = stream.get("codec_name")
        kind = stream.get("codec_type")
        if kind == "video" and not stream.get("disposition", {}).get("attached_pic"):
            width, height = number(stream.get("width")), number(stream.get("height"))
            if not isinstance(codec, str) or not codec or not width or not height:
                raise ValueError("无法读取有效的视频编码或分辨率")
            width, height = int(width), int(height)
            pixel = stream.get("pix_fmt") or ""
            depth = number(stream.get("bits_per_raw_sample"))
            if depth is None:
                match = re.search(r"(9|10|12|14|16)(?:le|be)$", pixel)
                depth = (
                    int(match[1])
                    if match
                    else (
                        8
                        if pixel
                        in {
                            "yuv420p",
                            "yuv422p",
                            "yuv444p",
                            "yuvj420p",
                            "yuvj422p",
                            "yuvj444p",
                            "nv12",
                            "nv21",
                            "rgb24",
                            "bgr24",
                        }
                        else None
                    )
                )
            side_data = json.dumps(stream.get("side_data_list", []), ensure_ascii=False).lower()
            transfer = stream.get("color_transfer")
            hdr = (
                "Dolby Vision"
                if "dovi" in side_data or "dolby" in side_data
                else "HDR / PQ"
                if transfer == "smpte2084"
                else "HLG"
                if transfer == "arib-std-b67"
                else "未发现 HDR 标记"
            )
            fps = frame_rate(stream.get("avg_frame_rate")) or frame_rate(stream.get("r_frame_rate"))
            video = {
                "codec": codec,
                "profile": stream.get("profile"),
                "level": stream.get("level"),
                "width": width,
                "height": height,
                "fps": fps,
                "bit_rate": number(stream.get("bit_rate")),
                "pixel_format": pixel or None,
                "bit_depth": depth,
                "hdr": hdr,
                "color_transfer": transfer,
                "color_primaries": stream.get("color_primaries"),
            }
            videos.append(video)
            if codec not in {"h264", "hevc"}:
                risks.append(f"视频编码 {codec}：需确认电视具体机型及 Emby 客户端是否支持硬解。")
            if codec == "h264" and depth and depth > 8:
                risks.append("H.264 高于 8-bit，部分电视不支持对应硬解，可能需要服务端转码。")
            if any(marker in pixel for marker in ("422", "444")):
                risks.append("视频采用 4:2:2 / 4:4:4 色度采样，常见电视硬解兼容性可能受限。")
            if width > 3840 or height > 2160:
                risks.append("分辨率超过常规 4K，需确认电视的解码上限。")
            if fps and (fps > 60 or ((width >= 3840 or height >= 2160) and fps > 30)):
                risks.append("高分辨率 / 高帧率视频解码压力较大，需确认设备支持的组合。")
            if hdr != "未发现 HDR 标记":
                risks.append(f"{hdr}：显示设备及客户端需支持对应格式，否则可能触发色调映射或转码。")
        elif kind == "audio":
            audios.append(
                {
                    "codec": codec,
                    "profile": stream.get("profile"),
                    "channels": stream.get("channels"),
                    "layout": stream.get("channel_layout"),
                    "sample_rate": number(stream.get("sample_rate")),
                    "bit_rate": number(stream.get("bit_rate")),
                    "language": stream.get("tags", {}).get("language"),
                }
            )
            if codec in {"truehd", "dts", "eac3", "flac"}:
                risks.append(
                    f"音轨 {codec}：电视或音响链路不支持时可能需音频转码；不代表视频一定卡顿。"
                )
        elif kind == "subtitle":
            subtitles.append({"codec": codec, "language": stream.get("tags", {}).get("language")})
            if codec in {"hdmv_pgs_subtitle", "dvd_subtitle", "ass", "ssa"}:
                risks.append(f"字幕 {codec}：选中后部分客户端需要烧录，可能触发视频转码。")
    if not videos:
        raise ValueError("未发现有效的主视频轨道，文件可能为空、未下载完成或已损坏")
    duration = number(container.get("duration"))
    reported_rate = number(container.get("bit_rate"))
    estimated_rate = size * 8 / duration if duration and not reported_rate else None
    rate = reported_rate or estimated_rate
    if rate and rate >= 80_000_000:
        risks.append(
            "平均总码率达到 80 Mbps 以上，部分电视的百兆有线网络可能成为瓶颈，峰值码率可能更高。"
        )
    if not rate:
        risks.append("缺少有效的总码率或时长，暂时无法评估带宽需求。")
    return {
        "container": container.get("format_name"),
        "size_bytes": size,
        "duration_seconds": duration,
        "bit_rate": reported_rate,
        "estimated_bit_rate": estimated_rate,
        "videos": videos,
        "audios": audios,
        "subtitles": subtitles,
        "risks": list(dict.fromkeys(risks)),
    }


class VideoInfoService:
    def __init__(self, files: FileService, store: OperationStore):
        self.files = files
        self.store = store
        self.lock = RLock()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="video-info")
        self.pending = False
        self.closing = False
        self.state: dict[str, Any] = {
            "running": False,
            "current": "",
            "finished_at": None,
            "error": "",
            "summary": {},
        }

    def close(self) -> None:
        with self.lock:
            self.closing = True
            self.pending = False
        self.executor.shutdown(wait=True)

    def status(self) -> dict[str, Any]:
        with self.lock:
            return {
                **self.state,
                "available": bool(locate_ffprobe()),
                "interval_minutes": SCAN_INTERVAL_MINUTES,
                "recursive": False,
                "extensions": sorted(INSPECTION_EXTENSIONS),
            }

    def request_scan(self) -> dict[str, Any]:
        with self.lock:
            if self.closing:
                raise RuntimeError("服务正在停止，无法启动视频识别")
            self.pending = True
            if not self.state["running"]:
                self.state.update(running=True, error="", current="等待扫描")
                self.executor.submit(self._worker)
            return self.status()

    def _worker(self) -> None:
        while True:
            with self.lock:
                if self.closing or not self.pending:
                    self.state.update(running=False, current="")
                    return
                self.pending = False
            try:
                if not locate_ffprobe():
                    raise ValueError("未找到 ffprobe，请安装 FFmpeg 工具或 ffprobe 后重启服务")
                with self.files.lock:
                    roots_version = self.files.roots_version
                    roots = list(self.files.roots)
                summary = {"analyzed": 0, "cached": 0, "deferred": 0, "failed": 0}
                errors = []
                for index, root in enumerate(roots):
                    with self.lock:
                        if self.closing:
                            break
                        self.state["current"] = str(root)
                    try:
                        result = self.scan(
                            VideoInfoRequest(root=index, roots_version=roots_version)
                        )
                    except (OSError, ValueError) as exc:
                        logger.warning("视频识别目录不可用 root=%s: %s", root, exc)
                        errors.append(f"{root}：{exc}")
                        summary["failed"] += 1
                        continue
                    for key in summary:
                        summary[key] += result[key]
                with self.lock:
                    self.state.update(
                        summary=summary,
                        error="；".join(errors)[:2000],
                        finished_at=datetime.now(UTC).isoformat(),
                    )
            except (OSError, ValueError, RuntimeError, SQLAlchemyError) as exc:
                logger.exception("自动视频参数识别失败")
                with self.lock:
                    self.state.update(
                        error="数据库不可用，视频未被修改"
                        if isinstance(exc, SQLAlchemyError)
                        else str(exc),
                        finished_at=datetime.now(UTC).isoformat(),
                    )

    @staticmethod
    def key(path: Path) -> str:
        return sha256(os.path.normcase(str(path)).encode("utf-8")).hexdigest()

    def probe(self, path: Path, timeout: float = 20) -> dict[str, Any]:
        executable = locate_ffprobe()
        if not executable:
            raise ValueError("未找到 ffprobe，无法识别视频参数")
        entries = (
            "format=format_name,duration,bit_rate:"
            "stream=index,codec_type,codec_name,profile,level,width,height,pix_fmt,"
            "bits_per_raw_sample,avg_frame_rate,r_frame_rate,bit_rate,color_transfer,"
            "color_primaries,sample_rate,channels,channel_layout:"
            "stream_tags=language:stream_disposition=attached_pic:stream_side_data"
        )
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
            try:
                result = subprocess.run(
                    [
                        executable,
                        "-v",
                        "error",
                        "-protocol_whitelist",
                        "file",
                        "-format_whitelist",
                        FORMAT_WHITELIST,
                        "-show_entries",
                        entries,
                        "-of",
                        "json",
                        str(path),
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=errors,
                    timeout=timeout,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                raise ValueError("ffprobe 识别超时，稍后可重试") from None
            if result.returncode:
                errors.seek(0)
                message = errors.read(1500).decode("utf-8", errors="replace")
                raise ValueError(f"ffprobe 无法识别视频：{message}")
            output.seek(0)
            content = output.read(1024 * 1024 + 1)
            if len(content) > 1024 * 1024:
                raise ValueError("ffprobe 返回信息超过大小限制")
        try:
            data = json.loads(content)
        except (ValueError, UnicodeError):
            raise ValueError("ffprobe 返回了无效 JSON") from None
        if not isinstance(data, dict):
            raise ValueError("ffprobe 返回了无效数据")
        return data

    def _entries(self, request: VideoInfoRequest) -> tuple[Path, list[dict[str, Any]]]:
        self.files.check_roots_version(request.roots_version)
        folder = self.files.resolve(request.root, request.path)
        entries = [
            item
            for item in self.files.list(request.root, request.path)["entries"]
            if not item["directory"] and Path(item["name"]).suffix.lower() in INSPECTION_EXTENSIONS
        ]
        return folder, entries

    @staticmethod
    def _row(item: dict[str, Any], record: VideoFileMetadata | None) -> dict[str, Any]:
        return {
            "name": item["name"],
            "path": item["path"],
            "details": record.details if record else None,
            "error": record.error if record else None,
            "scanned_at": record.scanned_at.isoformat() if record else None,
            "status": "failed" if record and record.error else "saved" if record else "missing",
        }

    def listing(self, request: VideoInfoRequest) -> dict[str, Any]:
        with self.files.lock:
            folder, entries = self._entries(request)
            with self.store.sessions() as session:
                saved = {
                    record.location_key: record
                    for record in session.scalars(
                        select(VideoFileMetadata).where(
                            VideoFileMetadata.directory_key == self.key(folder)
                        )
                    )
                }
            rows = []
            for item in entries:
                if item["blocked"]:
                    rows.append(
                        {**self._row(item, None), "status": "blocked", "error": "链接禁止读取"}
                    )
                    continue
                path = self.files.resolve(request.root, item["path"])
                record = saved.get(self.key(path))
                row = self._row(item, record)
                if record and list(signature(path) or ()) != record.fingerprint:
                    row["status"] = "changed"
                rows.append(row)
            return {"rows": rows}

    def scan(self, request: VideoInfoRequest) -> dict[str, Any]:
        with self.files.lock:
            if not locate_ffprobe():
                raise ValueError("未找到 ffprobe，无法识别视频参数")
            self.files.check_roots_version(request.roots_version)
            self.files.resolve(request.root, request.path)
            return self.store.execute(
                "video.info.scan", request.model_dump(), lambda: self._scan(request)
            )

    def _scan(self, request: VideoInfoRequest) -> dict[str, Any]:
        folder, entries = self._entries(request)
        summary = {"analyzed": 0, "cached": 0, "deferred": 0, "failed": 0}
        started = time.monotonic()
        with self.store.sessions() as session:
            for item in entries:
                if item["blocked"]:
                    summary["deferred"] += 1
                    logger.warning("视频参数识别跳过链接：%s", item["name"])
                    continue
                try:
                    path = self.files.resolve(request.root, item["path"])
                    before = signature(path)
                    if before is None:
                        raise ValueError("视频文件在识别前已变化")
                except (OSError, ValueError) as exc:
                    summary["deferred"] += 1
                    logger.warning("视频识别暂缓 file=%s: %s", item["path"], exc)
                    continue
                record = session.get(VideoFileMetadata, self.key(path))
                if (
                    not request.force
                    and record
                    and record.details is not None
                    and not record.error
                    and record.fingerprint == list(before)
                ):
                    summary["cached"] += 1
                    continue
                remaining = 120 - (time.monotonic() - started)
                if time.time() - path.stat().st_mtime < STABLE_SECONDS or remaining < 1:
                    summary["deferred"] += 1
                    continue
                details, error = None, None
                try:
                    details = normalize(self.probe(path, min(20, remaining)), before[0])
                except (OSError, ValueError) as exc:
                    error = str(exc)[:2000]
                    logger.warning("视频参数识别失败 file=%s: %s", path, exc)
                if signature(path) != before:
                    summary["deferred"] += 1
                    logger.info("视频仍在变化，暂不保存识别结果：%s", path)
                    continue
                if record is None:
                    record = VideoFileMetadata(location_key=self.key(path))
                    session.add(record)
                record.directory_key = self.key(folder)
                record.root_path = str(self.files.root(request.root))
                record.file_path = item["path"]
                record.fingerprint = list(before)
                record.details = details
                record.error = error
                record.scanned_at = datetime.now(UTC)
                session.commit()
                summary["failed" if error else "analyzed"] += 1
        return {
            **summary,
            "outcome": "partial" if summary["failed"] or summary["deferred"] else "success",
        }
