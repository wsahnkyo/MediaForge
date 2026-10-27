import pytest
from pydantic import ValidationError

from mediaforge.config import Settings, system_hosts_path


def test_system_hosts_requires_explicit_opt_in():
    with pytest.raises(ValidationError, match="HOSTS_ALLOW_SYSTEM"):
        Settings(
            _env_file=None,
            database_url="postgresql+psycopg://test:test@localhost/test",
            hosts_path=system_hosts_path(),
        )


def test_postgres_only_and_hosts_outside_media_root(tmp_path):
    with pytest.raises(ValidationError, match="postgresql"):
        Settings(_env_file=None, database_url="sqlite:///test.db")
    with pytest.raises(ValidationError, match="媒体目录"):
        Settings(
            _env_file=None,
            database_url="postgresql+psycopg://test:test@localhost/test",
            media_roots=[tmp_path],
            hosts_path=tmp_path / "hosts",
        )


def test_log_budget_and_media_boundary(settings, tmp_path):
    config = settings.model_dump()
    with pytest.raises(ValidationError, match="总大小"):
        Settings(_env_file=None, **{**config, "log_max_total_mb": 10, "log_max_file_mb": 50})
    with pytest.raises(ValidationError, match="日志文件"):
        Settings(_env_file=None, **{**config, "log_path": settings.media_roots[0] / "app.log"})
