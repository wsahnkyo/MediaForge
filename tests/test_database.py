import pytest
from sqlalchemy import event, inspect, text

from mediaforge.database import OPERATION_TIME_INDEX, Operation, initialize_database


def test_audit_commits_intent_and_releases_connection_before_action(store, sessions):
    engine = sessions.kw["bind"]
    checked_out = 0

    def checkout(*args):
        nonlocal checked_out
        checked_out += 1

    def checkin(*args):
        nonlocal checked_out
        checked_out -= 1

    event.listen(engine, "checkout", checkout)
    event.listen(engine, "checkin", checkin)
    try:

        def action():
            assert checked_out == 0
            assert store.recent()[0]["status"] == "running"
            return {"outcome": "success"}

        result = store.execute("test.action", {"value": 1}, action)
        assert store.get(result["operation_id"])["status"] == "success"
    finally:
        event.remove(engine, "checkout", checkout)
        event.remove(engine, "checkin", checkin)


def test_summary_query_does_not_load_json_but_detail_keeps_full_result(store, sessions):
    result = store.execute("test.large", {"input": "request"}, lambda: {"rows": ["x" * 10000]})
    statements = []
    engine = sessions.kw["bind"]

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        records = store.recent(summary=True)
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert len(records) == 1
    assert "request" not in records[0] and "result" not in records[0]
    query = next(sql for sql in statements if sql.lstrip().upper().startswith("SELECT"))
    assert ".request" not in query and ".result" not in query
    detail = store.get(result["operation_id"])
    assert detail["request"] == {"input": "request"}
    assert detail["result"]["rows"] == ["x" * 10000]
    assert store.get("missing") is None
    with sessions() as session:
        assert session.get(Operation, result["operation_id"]).status == "success"


def test_startup_adds_time_index_to_existing_table_idempotently(sessions):
    engine = sessions.kw["bind"]
    if engine.dialect.name != "sqlite":
        pytest.skip("Do not initialize the application's schema from an isolated PG fixture")
    OPERATION_TIME_INDEX.drop(engine)
    assert OPERATION_TIME_INDEX.name not in {
        index["name"] for index in inspect(engine).get_indexes("operations")
    }

    def skip_postgres_bootstrap(conn, statement, multiparams, params, execution_options):
        if str(statement).startswith(("SELECT pg_advisory_xact_lock", "CREATE SCHEMA")):
            statement = text("SELECT 1")
        return statement, multiparams, params

    event.listen(engine, "before_execute", skip_postgres_bootstrap, retval=True)
    try:
        initialize_database(engine)
        initialize_database(engine)
    finally:
        event.remove(engine, "before_execute", skip_postgres_bootstrap)
    assert OPERATION_TIME_INDEX.name in {
        index["name"] for index in inspect(engine).get_indexes("operations")
    }
