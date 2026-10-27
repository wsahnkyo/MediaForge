import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from mediaforge.config import Settings
from mediaforge.database import Base, OperationStore


@pytest.fixture
def sessions():
    if os.environ.get("MEDIAFORGE_RUN_PG_TESTS") == "1":
        settings = Settings()
        schema = "mediaforge_test_" + uuid4().hex
        engine = create_engine(
            settings.database_url.get_secret_value(),
            connect_args={"connect_timeout": 5},
            execution_options={"schema_translate_map": {"mediaforge": schema}},
        )
        with engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            Base.metadata.create_all(connection)
        try:
            yield sessionmaker(engine, expire_on_commit=False)
        finally:
            with engine.begin() as connection:
                # Only this fixture's freshly generated, isolated test schema is removed.
                connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            engine.dispose()
    else:
        engine = create_engine(
            "sqlite://",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
            execution_options={"schema_translate_map": {"mediaforge": None}},
        )
        Base.metadata.create_all(engine)
        try:
            yield sessionmaker(engine, expire_on_commit=False)
        finally:
            engine.dispose()


@pytest.fixture
def store(sessions):
    return OperationStore(sessions)


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        database_url="postgresql+psycopg://test:test@localhost/test",
        media_roots=[tmp_path / "media"],
        hosts_path=tmp_path / "hosts",
        log_path=tmp_path / "logs" / "mediaforge.log",
        hosts_domains=["api.themoviedb.org"],
        hosts_include_github=False,
        dns_providers=["dns"],
        network_timeout=0.1,
    )
