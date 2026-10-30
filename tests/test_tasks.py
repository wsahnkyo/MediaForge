from threading import Event

import pytest
from sqlalchemy import event
from sqlalchemy.exc import OperationalError

from mediaforge.database import BackgroundTask, operation_actor
from mediaforge.tasks import TaskService, task_progress


@pytest.fixture
def tasks(store):
    service = TaskService(store)
    try:
        yield service
    finally:
        service.close()


def test_async_preserves_actor_and_deduplicates_preview(tasks):
    entered, release = Event(), Event()
    actors = []

    def action():
        actors.append(operation_actor.get())
        entered.set()
        assert release.wait(5)
        assert task_progress(1, 2, "first")
        return {"outcome": "partial", "files": ["first"]}

    actor = operation_actor.set("alice")
    try:
        first = tasks.submit("file.migrate", {"preview_id": "preview"}, action)
        assert entered.wait(5)
        duplicate = tasks.submit("file.migrate", {"preview_id": "preview"}, action)
        assert duplicate["id"] == first["id"]
        assert "result" not in tasks.recent()[0]
    finally:
        release.set()
        operation_actor.reset(actor)
    tasks.executor.submit(lambda: None).result(timeout=5)
    detail = tasks.get(first["id"])
    assert actors == ["alice"]
    assert detail["actor"] == "alice" and detail["status"] == "partial"
    assert detail["completed"] == detail["total"] == 2
    assert detail["result"]["files"] == ["first"]


def test_queued_cancel_never_runs_action(tasks):
    entered, release = Event(), Event()
    called = []

    def blocking():
        entered.set()
        assert release.wait(5)
        return {}

    try:
        tasks.submit("video.info.scan", {}, blocking)
        assert entered.wait(5)
        queued = tasks.submit("rename.apply", {}, lambda: called.append(True))
        assert tasks.cancel(queued["id"])["cancel_requested"]
    finally:
        release.set()
    tasks.executor.submit(lambda: None).result(timeout=5)
    assert not called
    assert tasks.get(queued["id"])["status"] == "cancelled"


def test_running_cancel_preserves_progress_and_partial_results(tasks):
    def action():
        assert task_progress(0, 3, "first")
        tasks.cancel(tasks.recent()[0]["id"])
        assert not task_progress(1, 3, "second")
        return {"outcome": "cancelled", "moved_count": 1}

    result = tasks.submit("file.migrate", {}, action, inline=True)
    record = tasks.get(tasks.recent()[0]["id"])
    assert result["moved_count"] == 1
    assert record["status"] == "cancelled" and record["completed"] == 1
    assert record["total"] == 3 and record["finished_at"]


def test_failure_sanitizes_database_error(tasks, caplog):
    def action():
        raise OperationalError("private query", {}, Exception("private credentials"))

    result = tasks.submit("video.info.scan", {}, action, inline=True)
    assert result["outcome"] == "failed"
    assert "数据库" in result["error"]
    assert "private" not in caplog.text
    assert tasks.recent()[0]["status"] == "failed"


def test_recovery_marks_abandoned_tasks_without_replaying(tasks, sessions):
    with sessions() as session:
        for status in ("queued", "running", "success"):
            session.add(
                BackgroundTask(
                    id=status, kind="file.migrate", actor="alice", status=status, request={}
                )
            )
        session.commit()
    tasks.recover()
    tasks.recover()
    assert tasks.get("queued")["status"] == tasks.get("running")["status"] == "interrupted"
    assert tasks.get("success")["status"] == "success"
    assert "不自动重试" in tasks.get("running")["error"]
    assert not tasks.controls


def test_final_persistence_failure_is_logged_and_cannot_cancel_dead_worker(tasks, sessions, caplog):
    def fail_final(session, context, instances):
        if any(
            isinstance(row, BackgroundTask) and row.status == "success" for row in session.dirty
        ):
            raise OperationalError("private query", {}, Exception("private credentials"))

    event.listen(sessions.class_, "before_flush", fail_final)
    try:
        with pytest.raises(OperationalError):
            tasks.submit("video.info.scan", {}, lambda: {}, inline=True)
    finally:
        event.remove(sessions.class_, "before_flush", fail_final)
    assert "最终状态保存失败" in caplog.text and "private" not in caplog.text
    record = tasks.recent()[0]
    with pytest.raises(ValueError, match="最终状态未保存"):
        tasks.cancel(record["id"])
    tasks.recover()
    assert tasks.get(record["id"])["status"] == "interrupted"
