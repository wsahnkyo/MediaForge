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
    MetaData,
    String,
    create_engine,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

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


def make_engine(url: str):
    return create_engine(url, pool_pre_ping=True, connect_args={"connect_timeout": 5})


def initialize_database(engine) -> None:
    with engine.begin() as connection:
        connection.execute(text("SELECT pg_advisory_xact_lock(704019319)"))
        connection.execute(text("CREATE SCHEMA IF NOT EXISTS mediaforge"))
        Base.metadata.create_all(connection)


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
        with self.sessions() as session:
            if session.bind is not None and session.bind.dialect.name == "postgresql":
                session.execute(text("SELECT pg_advisory_xact_lock(704019320)"))
            record = session.get(Operation, operation_id)
            assert record is not None
            try:
                result = action()
            except (OSError, ValueError, RuntimeError) as exc:
                record.status = "failed"
                record.error = str(exc)[:2000]
                if isinstance(exc, OperationFailure):
                    record.result = exc.result
                record.finished_at = datetime.now(UTC)
                session.commit()
                logger.error("操作失败 id=%s type=%s: %s", operation_id, kind, exc)
                raise
            record.status = result.get("outcome", "success")
            record.result = result
            record.finished_at = datetime.now(UTC)
            session.commit()
            logger.info("操作结束 id=%s type=%s status=%s", operation_id, kind, record.status)
            return {"operation_id": operation_id, **result}

    def recent(self) -> list[dict[str, Any]]:
        with self.sessions() as session:
            records = session.scalars(
                select(Operation).order_by(Operation.created_at.desc()).limit(100)
            )
            return [
                {
                    "id": item.id,
                    "kind": item.kind,
                    "actor": item.actor,
                    "status": item.status,
                    "request": item.request,
                    "result": item.result,
                    "error": item.error,
                    "created_at": item.created_at.isoformat(),
                }
                for item in records
            ]
