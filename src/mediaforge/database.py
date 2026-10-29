import logging
from collections.abc import Callable
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    MetaData,
    String,
    UniqueConstraint,
    create_engine,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, defer, mapped_column, sessionmaker

operation_actor: ContextVar[str | None] = ContextVar("operation_actor", default=None)
logger = logging.getLogger(__name__)


class OperationFailure(ValueError):
    def __init__(self, message: str, result: dict[str, Any]):
        super().__init__(message)
        self.result = result


class Base(DeclarativeBase):
    metadata = MetaData(schema="mediaforge")


class Operation(Base):
    __tablename__ = "operations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    kind: Mapped[str] = mapped_column(String(40), index=True)
    status: Mapped[str] = mapped_column(String(16), default="running", index=True)
    request: Mapped[dict[str, Any]] = mapped_column(JSON().with_variant(JSONB(), "postgresql"))
    result: Mapped[dict[str, Any] | None] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=True
    )
    error: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    actor: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


OPERATION_TIME_INDEX = Index("ix_mediaforge_operations_created_at", Operation.created_at)


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    role: Mapped[str] = mapped_column(String(16), default="viewer")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class LoginSession(Base):
    __tablename__ = "login_sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("mediaforge.users.id", ondelete="CASCADE"), index=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class MetadataMapping(Base):
    __tablename__ = "metadata_folder_mappings"
    __table_args__ = (UniqueConstraint("media_type", "location_key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    media_type: Mapped[str] = mapped_column(String(8))
    root_path: Mapped[str] = mapped_column(String(2000))
    folder_path: Mapped[str] = mapped_column(String(2000))
    location_key: Mapped[str] = mapped_column(String(64))
    tmdb_id: Mapped[int]


class ScraperSetting(Base):
    __tablename__ = "metadata_scraper_settings"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    api_key: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    priority: Mapped[int] = mapped_column(default=0)


class MediaRootSetting(Base):
    __tablename__ = "media_root_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    roots: Mapped[list[str]] = mapped_column(JSON().with_variant(JSONB(), "postgresql"))


class RenameRule(Base):
    __tablename__ = "filename_rename_rules"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    pattern: Mapped[str] = mapped_column(String(2000))
    template: Mapped[str] = mapped_column(String(180))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    priority: Mapped[int] = mapped_column(default=0)
    default_season: Mapped[int] = mapped_column(default=1)


class RenameDirectoryState(Base):
    __tablename__ = "filename_rename_directories"

    location_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    root_path: Mapped[str] = mapped_column(String(2000))
    folder_path: Mapped[str] = mapped_column(String(2000))
    fingerprint: Mapped[str] = mapped_column(String(64))
    rules_version: Mapped[str] = mapped_column(String(64))
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class VideoFileMetadata(Base):
    __tablename__ = "video_file_metadata"

    location_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    directory_key: Mapped[str] = mapped_column(String(64), index=True)
    root_path: Mapped[str] = mapped_column(String(2000))
    file_path: Mapped[str] = mapped_column(String(2000))
    fingerprint: Mapped[list[int]] = mapped_column(JSON().with_variant(JSONB(), "postgresql"))
    details: Mapped[dict[str, Any] | None] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=True
    )
    error: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    scanned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


def make_engine(url: str):
    return create_engine(url, pool_pre_ping=True, connect_args={"connect_timeout": 5})


def initialize_database(engine) -> None:
    with engine.begin() as connection:
        connection.execute(text("SELECT pg_advisory_xact_lock(704019319)"))
        connection.execute(text("CREATE SCHEMA IF NOT EXISTS mediaforge"))
        Base.metadata.create_all(connection)
        # create_all does not add indexes to tables that already exist.
        OPERATION_TIME_INDEX.create(connection, checkfirst=True)


class OperationStore:
    def __init__(self, sessions: sessionmaker[Session]):
        self.sessions = sessions

    def execute(
        self, kind: str, payload: dict[str, Any], action: Callable[[], dict[str, Any]]
    ) -> dict[str, Any]:
        # A committed intent survives a process crash between filesystem and database writes.
        with self.sessions() as session:
            record = Operation(
                kind=kind, request=payload, actor=operation_actor.get() or "scheduler"
            )
            session.add(record)
            session.commit()
            operation_id = record.id
        logger.info("操作开始 id=%s type=%s actor=%s", operation_id, kind, record.actor)
        try:
            result = action()
        except (OSError, ValueError, RuntimeError) as exc:
            with self.sessions() as session:
                record = session.get(Operation, operation_id)
                assert record is not None
                record.status = "failed"
                record.error = str(exc)[:2000]
                if isinstance(exc, OperationFailure):
                    record.result = exc.result
                record.finished_at = datetime.now(UTC)
                session.commit()
            logger.error("操作失败 id=%s type=%s: %s", operation_id, kind, exc)
            raise
        with self.sessions() as session:
            record = session.get(Operation, operation_id)
            assert record is not None
            record.status = result.get("outcome", "success")
            record.result = result
            record.finished_at = datetime.now(UTC)
            session.commit()
            logger.info("操作结束 id=%s type=%s status=%s", operation_id, kind, record.status)
            return {"operation_id": operation_id, **result}

    @staticmethod
    def _data(item: Operation, *, summary: bool = False) -> dict[str, Any]:
        data = {
            "id": item.id,
            "kind": item.kind,
            "actor": item.actor,
            "status": item.status,
            "error": item.error,
            "created_at": item.created_at.isoformat(),
        }
        if not summary:
            data.update(request=item.request, result=item.result)
        return data

    def recent(self, *, summary: bool = False) -> list[dict[str, Any]]:
        with self.sessions() as session:
            query = select(Operation).order_by(Operation.created_at.desc()).limit(100)
            if summary:
                query = query.options(defer(Operation.request), defer(Operation.result))
            return [self._data(item, summary=summary) for item in session.scalars(query)]

    def get(self, operation_id: str) -> dict[str, Any] | None:
        with self.sessions() as session:
            record = session.get(Operation, operation_id)
            return self._data(record) if record is not None else None
