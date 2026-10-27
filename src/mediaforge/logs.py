import logging
import os
import re
import sys
import time
from datetime import UTC, datetime
from logging.handlers import BaseRotatingHandler
from pathlib import Path
from typing import Any
from uuid import uuid4

from mediaforge.config import Settings
from mediaforge.files import is_link


class RetainedFileHandler(BaseRotatingHandler):
    def __init__(self, path: Path, max_bytes: int, total_bytes: int, days: int):
        self.path = path
        self.max_bytes = max_bytes
        self.total_bytes = total_bytes
        self.max_age = days * 86400
        self.last_error: str | None = None
        self.archive_pattern = re.compile(re.escape(path.name) + r"\.\d{8}T\d{12}Z\.[0-9a-f]{32}\Z")
        self.day = datetime.fromtimestamp(
            path.stat().st_mtime if path.exists() else time.time(), UTC
        ).date()
        super().__init__(path, mode="a", encoding="utf-8")

    def _open(self):
        return open(self.baseFilename, self.mode, encoding="utf-8", newline="\n")

    def shouldRollover(self, record: logging.LogRecord) -> bool:
        size = self.path.stat().st_size
        if not size:
            self.day = datetime.now(UTC).date()
            return False
        message_size = len((self.format(record) + "\n").encode("utf-8"))
        return datetime.now(UTC).date() != self.day or size + message_size > self.max_bytes

    def doRollover(self) -> None:
        if self.stream is not None:
            self.stream.close()
            self.stream = None
        suffix = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ") + "." + uuid4().hex
        self.path.rename(self.path.with_name(self.path.name + "." + suffix))
        self.stream = self._open()
        self.day = datetime.now(UTC).date()

    def cleanup(self) -> None:
        self.acquire()
        try:
            if self.path.stat().st_size and datetime.now(UTC).date() != self.day:
                self.doRollover()
            cutoff = time.time() - self.max_age
            archives = []
            for path in self.path.parent.iterdir():
                if not self.archive_pattern.fullmatch(path.name):
                    continue
                if is_link(path):
                    raise ValueError("轮转日志不能是链接")
                info = path.stat()
                if info.st_mtime < cutoff:
                    path.unlink()
                else:
                    archives.append((info.st_mtime_ns, path, info.st_size))
            total = self.path.stat().st_size + sum(item[2] for item in archives)
            for _, path, size in sorted(archives):
                if total <= self.total_bytes:
                    break
                path.unlink()
                total -= size
        finally:
            self.release()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            super().emit(record)
            self.cleanup()
        except (OSError, ValueError):
            self.handleError(record)

    def handleError(self, record: logging.LogRecord) -> None:
        self.last_error = f"日志写入或清理失败: {sys.exception()}"
        super().handleError(record)


class BoundedFormatter(logging.Formatter):
    def __init__(self, max_bytes: int):
        super().__init__("%(asctime)s %(levelname)-7s %(name)s | %(message)s")
        self.max_bytes = max_bytes - 1

    def format(self, record: logging.LogRecord) -> str:
        message = super().format(record)
        encoded = message.encode("utf-8")
        if len(encoded) > self.max_bytes:
            suffix = " ... [日志过长，已截断]"
            message = (
                encoded[: self.max_bytes - len(suffix.encode())].decode("utf-8", errors="ignore")
                + suffix
            )
        return message


class LogService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.path = settings.log_path
        self.handler: RetainedFileHandler | None = None
        self.logger = logging.getLogger("mediaforge")
        self.previous_level = self.logger.level

    def _check_path(self) -> None:
        for component in [self.path, *self.path.parents]:
            if (component.exists() or component.is_symlink()) and is_link(component):
                raise ValueError("日志路径不能包含符号链接、目录联接或重解析点")

    def start(self) -> None:
        self._check_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handler = RetainedFileHandler(
            self.path,
            max_bytes=self.settings.log_max_file_mb * 1024 * 1024,
            total_bytes=self.settings.log_max_total_mb * 1024 * 1024,
            days=self.settings.log_retention_days,
        )
        handler.setFormatter(BoundedFormatter(handler.max_bytes))
        self.handler = handler
        self.logger.setLevel(logging.INFO)
        self.logger.addHandler(handler)
        self.logger.info("运行日志已启用: %s", self.path)

    def cleanup(self) -> None:
        try:
            self._check_path()
            if self.handler is not None:
                self.handler.cleanup()
        except (OSError, ValueError) as exc:
            if self.handler is not None:
                self.handler.last_error = f"日志清理失败: {exc}"
            raise

    def close(self) -> None:
        if self.handler is not None:
            self.logger.removeHandler(self.handler)
            self.handler.close()
            self.handler = None
            self.logger.setLevel(self.previous_level)

    def tail(self, limit: int = 200) -> dict[str, Any]:
        self._check_path()
        if self.handler is None:
            raise RuntimeError("日志服务尚未启动")
        self.handler.acquire()
        try:
            self.handler.flush()
            with self.path.open("rb") as stream:
                info = os.fstat(stream.fileno())
                start = max(0, info.st_size - 256 * 1024)
                stream.seek(start)
                data = stream.read(256 * 1024)
            if start:
                # The first fragment may start in the middle of a UTF-8 character or log line.
                data = data.partition(b"\n")[2]
            lines = data.decode("utf-8", errors="replace").splitlines()
            return {
                "path": str(self.path),
                "lines": lines[-limit:],
                "truncated": start > 0 or len(lines) > limit,
                "updated_at": datetime.fromtimestamp(info.st_mtime, UTC).isoformat(),
                "error": self.handler.last_error,
            }
        finally:
            self.handler.release()
