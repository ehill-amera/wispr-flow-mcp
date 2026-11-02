"""Hard rules: read-only, forbidden columns, local only. Fixture file, real SQLite engine."""

import hashlib
import sqlite3

import pytest

from wispr_flow_mcp import db, tools

FORBIDDEN = ["audio", "screenshot", "builtInAudio", "axText", "axHTML", "textboxContents"]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("column", FORBIDDEN)
def test_authorizer_refuses_each_forbidden_column(fixture_db, column):
    with pytest.raises(sqlite3.DatabaseError, match="prohibited|not authorized"):
        db.query(f"SELECT {column} FROM History")


@pytest.mark.parametrize("column", FORBIDDEN)
def test_authorizer_refuses_forbidden_column_inside_where(fixture_db, column):
    with pytest.raises(sqlite3.DatabaseError, match="prohibited|not authorized"):
        db.query(f"SELECT app FROM History WHERE {column} IS NOT NULL")


def test_authorizer_refuses_select_star(fixture_db):
    with pytest.raises(sqlite3.DatabaseError, match="prohibited|not authorized"):
        db.query("SELECT * FROM History")


def test_every_allowed_history_column_excludes_forbidden_ones():
    assert not set(FORBIDDEN) & db.READABLE_COLUMNS["History"]


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO Notes (id) VALUES ('x')",
        "UPDATE Notes SET title = 'x'",
        "DELETE FROM Notes",
        "DROP TABLE Notes",
        "VACUUM",
        "  vacuum",
        "PRAGMA wal_checkpoint(TRUNCATE)",
        "PRAGMA journal_mode=DELETE",
    ],
)
def test_query_refuses_every_non_select_before_it_reaches_sqlite(fixture_db, monkeypatch, statement):
    def must_not_connect():
        raise AssertionError("reached the database")

    monkeypatch.setattr(db, "connect", must_not_connect)
    with pytest.raises(db.NotASelect):
        db.query(statement)


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO Notes (id) VALUES ('x')",
        "UPDATE Notes SET title = 'x'",
        "DELETE FROM Notes",
        "DROP TABLE Notes",
        "PRAGMA wal_checkpoint(TRUNCATE)",
        "PRAGMA journal_mode=DELETE",
    ],
)
def test_authorizer_refuses_writes_when_the_select_check_is_bypassed(fixture_db, statement):
    with db.connect() as conn:
        with pytest.raises(sqlite3.DatabaseError, match="prohibited|not authorized"):
            conn.execute(statement)
    assert len(db.query("SELECT id FROM Notes")) == 2


def test_uri_alone_refuses_writes_without_the_authorizer(fixture_db):
    conn = sqlite3.connect(db.open_uri(str(fixture_db)), uri=True)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("UPDATE Notes SET title = 'x'")
    finally:
        conn.close()


def test_missing_file_raises_and_is_not_created(tmp_path, monkeypatch):
    missing = tmp_path / "nope.sqlite"
    monkeypatch.setenv("WISPR_FLOW_DB", str(missing))
    with pytest.raises(FileNotFoundError):
        tools.recent_dictations()
    assert not missing.exists()


def test_all_tools_leave_the_main_file_bytes_unchanged(fixture_db):
    before = sha(fixture_db)
    tools.search_dictations("delta")
    tools.search_dictations("nothing-matches")
    tools.recent_dictations()
    tools.get_dictation("newest")
    tools.dictation_stats(group_by="day")
    tools.dictation_stats(group_by="app")
    tools.search_dictionary("wispr")
    tools.list_dictionary()
    tools.search_notes("foxtrot")
    tools.get_note("n1")
    assert sha(fixture_db) == before


def test_tool_output_never_contains_forbidden_values(fixture_db):
    outputs = [
        tools.recent_dictations(limit=100, include_archived=True),
        tools.get_dictation("newest"),
        tools.search_dictations("delta"),
    ]
    assert "SENTINEL" not in repr(outputs)


def test_deadline_raises_instead_of_returning_a_partial_result(fixture_db, monkeypatch):
    monkeypatch.setattr(db, "QUERY_DEADLINE_SECONDS", -1)
    with pytest.raises(db.QueryDeadlineExceeded):
        db.query("SELECT COUNT(*) FROM History a, History b, History c, History d, History e")
