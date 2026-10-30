import logging
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar, copy_context
from datetime import UTC, datetime
from hashlib import sha256
from threading import Event, RLock
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import defer

from mediaforge.database import BackgroundTask, OperationFailure, OperationStore, operation_actor

logger = logging.getLogger(__name__)
_progress: ContextVar[Callable[[int, int, str], bool] | None] = ContextVar(
    "task_progress", default=None
)


def task_progress(completed: int, total: int, current: str) -> bool:
    callback = _progress.get()
    return callback(completed, total, current) if callback else True


class TaskService:
    def __init__(self, store: OperationStore):
        self.store = store
        self.lock = RLock()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="media-task")
        self.controls: dict[str, Event] = {}
        self.closing = False

    def recover(self) -> None:
        with self.lock, self.store.sessions() as session:
            for record in session.scalars(
                select(BackgroundTask).where(BackgroundTask.status.in_(["queued", "running"]))
            ):
                record.status = "interrupted"
                record.error = (
                    "服务已重启；任务可能部分生效，请核对文件与操作记录后重新预览，不自动重试。"
                )
                record.finished_at = datetime.now(UTC)
            session.commit()

    @staticmethod
    def _data(record: BackgroundTask, detail: bool = True) -> dict[str, Any]:
        data = {
            "id": record.id,
            "kind": record.kind,
            "actor": record.actor,
            "status": record.status,
            "completed": record.completed,
            "total": record.total,
            "current": record.current,
            "cancel_requested": record.cancel_requested,
            "error": record.error,
            "created_at": record.created_at.isoformat(),
            "finished_at": record.finished_at.isoformat() if record.finished_at else None,
        }
        if detail:
            data.update(request=record.request, result=record.result)
        return data

    def recent(self) -> list[dict[str, Any]]:
        with self.lock, self.store.sessions() as session:
            return [
                self._data(item, False)
                for item in session.scalars(
                    select(BackgroundTask)
                    .options(defer(BackgroundTask.request), defer(BackgroundTask.result))
                    .order_by(BackgroundTask.created_at.desc())
                    .limit(100)
                )
            ]

    def get(self, task_id: str) -> dict[str, Any]:
        with self.lock, self.store.sessions() as session:
            record = session.get(BackgroundTask, task_id)
            if record is None:
                raise ValueError("任务不存在")
            return self._data(record)

    def submit(
        self,
        kind: str,
        payload: dict[str, Any],
        action: Callable[[], dict[str, Any]],
        *,
        inline: bool = False,
    ) -> dict[str, Any]:
        actor = operation_actor.get() or "scheduler"
        preview_id = payload.get("preview_id")
        key = sha256(f"{actor}:{kind}:{preview_id}".encode()).hexdigest() if preview_id else None
        with self.lock, self.store.sessions() as session:
            if self.closing:
                raise ValueError("服务正在停止，无法提交任务")
            if key:
                existing = session.scalar(
                    select(BackgroundTask).where(BackgroundTask.dedupe_key == key)
                )
                if existing:
                    return self._data(existing)
            if len(self.controls) >= 100:
                raise ValueError("等待执行的任务过多，请等待已有任务完成")
            task_id = str(uuid4())
            session.add(
                BackgroundTask(
                    id=task_id,
                    kind=kind,
                    actor=actor,
                    status="queued",
                    request=payload,
                    dedupe_key=key,
                )
            )
            session.commit()
            self.controls[task_id] = Event()
            if not inline:
                self.executor.submit(copy_context().run, self._run, task_id, action)
        if inline:
            return self._run(task_id, action)
        return self.get(task_id)

    def cancel(self, task_id: str) -> dict[str, Any]:
        with self.lock, self.store.sessions() as session:
            record = session.get(BackgroundTask, task_id)
            if record is None:
                raise ValueError("任务不存在")
            if record.status in {"queued", "running"}:
                record.cancel_requested = True
                control = self.controls.get(task_id)
                if control is None:
                    raise ValueError(
                        "任务已停止，但最终状态未保存；请核对操作记录并重启服务恢复状态"
                    )
                control.set()
                session.commit()
            return self._data(record)

    def _run(self, task_id: str, action: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        last_saved = 0.0
        control = self.controls[task_id]

        def progress(completed: int, total: int, current: str) -> bool:
            nonlocal last_saved
            now = time.monotonic()
            if now - last_saved >= 0.25 or completed == total or control.is_set():
                with self.lock, self.store.sessions() as session:
                    record = session.get(BackgroundTask, task_id)
                    record.completed, record.total, record.current = (
                        completed,
                        total,
                        current[:2000],
                    )
                    session.commit()
                last_saved = now
            return not control.is_set()

        token = _progress.set(progress)
        result: dict[str, Any] = {}
        error = None
        status = "failed"
        try:
            with self.lock, self.store.sessions() as session:
                record = session.get(BackgroundTask, task_id)
                record.status = "running"
                record.current = "准备执行，必要时等待当前操作完成"
                session.commit()
            if control.is_set():
                result = {"outcome": "cancelled"}
            else:
                result = action()
            status = result.get("outcome", "success")
            if status not in {"success", "partial", "failed", "cancelled"}:
                raise ValueError("任务返回了无效状态")
        except Exception as exc:
            status = "failed"
            error = (
                "数据库不可用，请核对磁盘与操作记录"
                if isinstance(exc, SQLAlchemyError)
                else (
                    str(exc)[:2000]
                    if isinstance(exc, (ValueError, OSError, RuntimeError))
                    else f"任务发生异常：{type(exc).__name__}，请核对操作记录"
                )
            )
            if isinstance(exc, OperationFailure):
                result = exc.result
            result = {**result, "outcome": "failed", "error": error}
            logger.error("后台任务失败 id=%s type=%s: %s", task_id, type(exc).__name__, error)
        finally:
            _progress.reset(token)
            try:
                with self.lock, self.store.sessions() as session:
                    record = session.get(BackgroundTask, task_id)
                    record.status, record.result, record.error = status, result, error
                    record.finished_at = datetime.now(UTC)
                    if status in {"success", "partial"}:
                        record.completed = record.total
                    record.current = ""
                    session.commit()
            except SQLAlchemyError:
                logger.error("后台任务最终状态保存失败 id=%s，请核对文件和操作记录", task_id)
                raise
            finally:
                with self.lock:
                    self.controls.pop(task_id, None)
        return result

    def close(self) -> None:
        with self.lock:
            self.closing = True
            for control in self.controls.values():
                control.set()
        self.executor.shutdown(wait=True)
