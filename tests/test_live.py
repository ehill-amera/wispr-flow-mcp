"""Runs against the real Wispr Flow file, read-only. Run with: uv run pytest -m live --tb=no

Assertions compare counts and booleans, so a failure cannot print dictation text.
"""

import os
import sqlite3
import statistics
import time
from pathlib import Path

import anyio
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from helpers import changed_in_every_window, open_modes, record_authorizer, run_every_tool
from wispr_flow_mcp import archive, db, tools

pytestmark = pytest.mark.live

FORBIDDEN = {"audio", "screenshot", "builtInAudio", "axText", "axHTML", "textboxContents"}
LAUNCHER = Path(__file__).parent.parent / "bin" / "wispr-flow-mcp"


@pytest.fixture(autouse=True)
def real_file(monkeypatch):
    monkeypatch.delenv(db.DB_PATH_ENV, raising=False)
    monkeypatch.delenv(db.ARCHIVE_PATH_ENV, raising=False)
    assert os.path.exists(db.DEFAULT_DB_PATH.replace("~", os.path.expanduser("~"), 1))


def test_main_file_does_not_change_while_every_tool_runs():
    assert changed_in_every_window(db.db_path(), run_every_tool) is False


def test_main_file_is_held_read_only_while_querying():
    conn = sqlite3.connect(db.open_uri(db.db_path()), uri=True)
    try:
        conn.execute("SELECT COUNT(*) FROM Notes").fetchone()
        modes = open_modes(db.db_path())
    finally:
        conn.close()
    assert modes and set(modes) == {"r"}


def test_engine_refuses_a_write_with_no_authorizer_in_place():
    # The statement changes nothing even if it ran, so this cannot harm the file.
    conn = sqlite3.connect(db.open_uri(db.db_path()), uri=True, timeout=1)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("UPDATE Notes SET title = title WHERE 0")
    finally:
        conn.close()


def test_server_refuses_a_write_before_it_reaches_sqlite():
    with pytest.raises(db.NotASelect):
        db.query("UPDATE Notes SET title = title WHERE 0")


def test_authorizer_refuses_a_write_on_the_real_file():
    # Denied while preparing the statement, and it changes nothing even if it ran.
    with db.connect() as conn:
        with pytest.raises(sqlite3.DatabaseError, match="prohibited|not authorized"):
            conn.execute("UPDATE Notes SET title = title WHERE 0")


@pytest.mark.parametrize("column", sorted(FORBIDDEN))
def test_forbidden_columns_are_refused(column):
    with pytest.raises(sqlite3.DatabaseError, match="prohibited|not authorized"):
        db.query(f"SELECT {column} FROM History LIMIT 1")


def test_every_tool_reads_only_allowed_columns(monkeypatch):
    reads, denied = record_authorizer(monkeypatch)
    run_every_tool()
    assert not denied
    assert reads
    assert not {column for _table, column in reads} & FORBIDDEN
    for table, column in reads:
        assert column == "" or column in db.READABLE_COLUMNS[table]


def test_results_are_newest_first_with_undated_rows_last():
    results = tools.recent_dictations(limit=100)["results"]
    flags = [r["local_time"] is None for r in results]
    assert flags == sorted(flags)
    dated = [r["local_time"] for r in results if r["local_time"]]
    assert dated == sorted(dated, reverse=True)


def test_search_finds_a_word_from_the_newest_dictation_that_has_text():
    # The very newest row can have no text yet, while Wispr Flow is still processing it.
    for row in tools.recent_dictations(limit=25)["results"]:
        words = [w for w in (row["text"] or "").split() if w.isalpha() and len(w) >= 4]
        if words:
            found = tools.search_dictations(words[0].upper(), limit=100)
            assert (row["id"] in [r["id"] for r in found["results"]]) is True
            return
    pytest.fail("No recent dictation has a searchable word")


def test_zero_results_say_how_far_back_the_history_goes():
    result = tools.search_dictations("no-such-text-qzxv")
    assert result["count"] == 0
    assert result["history_window"]["earliest_local"] is not None
    assert result["history_window"]["latest_local"] is not None


def test_results_stay_within_the_size_cap():
    result = tools.recent_dictations(limit=100)
    size = sum(len(v) for r in result["results"] for v in r.values() if isinstance(v, str))
    assert size <= tools.MAX_RESPONSE_CHARS


def test_typical_search_is_fast():
    durations = []
    for _ in range(5):
        start = time.perf_counter()
        tools.search_dictations("no-such-text-qzxv")
        durations.append(time.perf_counter() - start)
    assert statistics.median(durations) < 5


def test_launcher_serves_tools_with_a_bare_environment_and_logs_no_text(tmp_path):
    errlog_path = tmp_path / "err.log"

    async def main():
        params = StdioServerParameters(
            command=str(LAUNCHER), env={"HOME": os.path.expanduser("~"), "PATH": "/usr/bin:/bin"}
        )
        with open(errlog_path, "w") as errlog:
            async with stdio_client(params, errlog=errlog) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    called = await session.call_tool("recent_dictations", {"limit": 5})
                    return len(listed.tools), called.content[0].text

    tool_count, body = anyio.run(main)
    assert tool_count == 8
    assert '"count"' in body
    words = {w for w in body.split() if len(w) > 12 and w.isalpha()}
    logged = errlog_path.read_text()
    assert (any(w in logged for w in words)) is False
    assert (len(logged) == 0) is True


def test_sync_reads_the_real_file_read_only_into_a_throwaway_memory_archive():
    conn = sqlite3.connect(":memory:", isolation_level=None)
    archive.create_schema(conn)
    assert changed_in_every_window(db.db_path(), lambda: archive.sync(conn)) is False
    live_ids = db.query("SELECT COUNT(*) FROM History WHERE transcriptEntityId > ''")[0][0]
    assert conn.execute("SELECT COUNT(*) FROM History").fetchone()[0] >= live_ids - 5
    conn.close()


def require_real_archive():
    if not os.path.exists(db.archive_path()):
        pytest.skip("No archive on this Mac yet")


def test_real_archive_has_rows_and_only_allowed_columns():
    require_real_archive()
    assert db.query("SELECT COUNT(*) FROM History", source=db.ARCHIVE)[0][0] > 0
    conn = sqlite3.connect(db.open_uri(db.archive_path()), uri=True)
    try:
        names = {r[1] for r in conn.execute("PRAGMA table_info(History)")}
    finally:
        conn.close()
    assert not names & FORBIDDEN


def test_tools_reach_a_recovered_row_in_the_real_archive():
    require_real_archive()
    rows = db.query(
        "SELECT transcriptEntityId FROM History WHERE source = 'recovered' AND transcriptEntityId > '' LIMIT 1",
        source=db.ARCHIVE,
    )
    if not rows:
        pytest.skip("Nothing recovered on this Mac")
    assert tools.get_dictation(rows[0][0])["source"] == "recovered"


def test_real_stats_have_no_junk_groups_or_negative_words():
    groups = tools.dictation_stats(group_by="app")["groups"]
    assert all(g["words"] >= 0 for g in groups)
    assert all(g["group"] == tools.UNKNOWN_APP or tools._BUNDLE_ID.fullmatch(g["group"]) for g in groups)
