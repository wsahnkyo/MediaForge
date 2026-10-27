import logging
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from mediaforge.logs import BoundedFormatter, LogService, RetainedFileHandler


def archive(path: Path, size: int, age_days: int) -> Path:
    target = path.with_name(path.name + ".20261027T123456000000Z." + uuid4().hex)
    target.write_bytes(b"x" * size)
    timestamp = time.time() - age_days * 86400
    os.utime(target, (timestamp, timestamp))
    return target


def test_expiry_and_total_budget_delete_only_owned_old_archives(tmp_path):
    path = tmp_path / "app.log"
    path.write_bytes(b"current")
    expired = archive(path, 10, 8)
    older = archive(path, 70, 2)
    newer = archive(path, 70, 1)
    unrelated = tmp_path / "app.log.unrelated"
    unrelated.write_text("keep")
    handler = RetainedFileHandler(path, max_bytes=80, total_bytes=100, days=7)
    try:
        handler.cleanup()
        assert not expired.exists()
        assert not older.exists()
        assert newer.exists()
        assert unrelated.read_text() == "keep"
        assert path.read_bytes() == b"current"
        assert sum(file.stat().st_size for file in [path, newer]) <= 100
    finally:
        handler.close()


def test_size_day_rotation_and_oversized_message_stay_bounded(tmp_path):
    path = tmp_path / "app.log"
    handler = RetainedFileHandler(path, max_bytes=160, total_bytes=320, days=7)
    handler.setFormatter(BoundedFormatter(160))
    logger = logging.Logger("rotation-test")
    logger.addHandler(handler)
    try:
        for _ in range(5):
            logger.warning("一个包含中文的消息" * 100)
        files = [
            path,
            *[item for item in tmp_path.iterdir() if handler.archive_pattern.fullmatch(item.name)],
        ]
        assert len(files) >= 2
        assert all(item.stat().st_size <= 160 for item in files)
        assert sum(item.stat().st_size for item in files) <= 320
        assert "已截断" in path.read_text(encoding="utf-8")
        handler.day = datetime.now(UTC).date() - timedelta(days=1)
        logger.info("new day")
        assert "new day" in path.read_text(encoding="utf-8")
        assert "已截断" not in path.read_text(encoding="utf-8")
    finally:
        handler.close()


def test_tail_reads_recent_utf8_and_follows_rotation(settings):
    service = LogService(settings)
    service.start()
    logger = logging.getLogger("mediaforge.tests")
    try:
        logger.info("第一条")
        logger.error("失败原因")
        result = service.tail(1)
        assert len(result["lines"]) == 1
        assert "失败原因" in result["lines"][0]
        assert result["truncated"] is True
        service.handler.doRollover()
        logger.info("最新文件")
        result = service.tail(100)
        assert "最新文件" in "\n".join(result["lines"])
        assert "失败原因" not in "\n".join(result["lines"])
    finally:
        service.close()
