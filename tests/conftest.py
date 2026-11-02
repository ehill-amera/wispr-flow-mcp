"""Builds a real WAL-mode SQLite fixture. Only invented text; the live file is never touched here."""

import sqlite3

import pytest

SENTINEL_BLOB = b"SENTINEL-BLOB"
SENTINEL_AX = "SENTINEL-AX"

HISTORY_SQL = """CREATE TABLE History (
  transcriptEntityId VARCHAR(36) PRIMARY KEY, asrText TEXT, formattedText TEXT, editedText TEXT,
  timestamp DATETIME, audio BLOB, screenshot BLOB, status VARCHAR(255), app VARCHAR(255), url VARCHAR(255),
  duration FLOAT, numWords INTEGER, textboxContents TEXT, isArchived TINYINT(1), conversationId VARCHAR(255),
  builtInAudio BLOB, pastedText TEXT, axText TEXT, axHTML TEXT, timezoneOffsetMinutes INTEGER,
  serverFinalizedText TEXT, editedTextUnbounded TEXT)"""


def history(conn, id, ts, app="com.google.Chrome", pasted=None, edited=None, formatted=None, archived=0, words=3):
    conn.execute(
        "INSERT INTO History (transcriptEntityId, asrText, formattedText, editedText, timestamp, audio, screenshot, "
        "status, app, url, duration, numWords, textboxContents, isArchived, conversationId, builtInAudio, pastedText, "
        "axText, axHTML, timezoneOffsetMinutes, serverFinalizedText, editedTextUnbounded) "
        "VALUES (?,?,?,?,?,?,?,'formatted',?,'https://example.test',1.5,?,?,?,'conv',?,?,?,?,300,?,NULL)",
        (id, f"raw {id}", formatted, edited, ts, SENTINEL_BLOB, SENTINEL_BLOB, app, words, SENTINEL_AX, archived,
         SENTINEL_BLOB, pasted, SENTINEL_AX, SENTINEL_AX, pasted),
    )


@pytest.fixture(autouse=True)
def no_real_archive(tmp_path, monkeypatch):
    """Tests get their own archive path, which does not exist unless a test creates it."""
    monkeypatch.setenv("WISPR_FLOW_ARCHIVE", str(tmp_path / "test-archive.sqlite"))


@pytest.fixture(autouse=True)
def fixed_settings(tmp_path, monkeypatch):
    """Expected times are written for this zone. The backup stays off, whatever the real Mac is set to."""
    path = tmp_path / "test-config.toml"
    path.write_text('timezone = "America/New_York"\n')
    monkeypatch.setenv("WISPR_FLOW_CONFIG", str(path))
    return path


@pytest.fixture
def fixture_db(tmp_path, monkeypatch):
    path = tmp_path / "flow with space.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(HISTORY_SQL)
    conn.execute(
        "CREATE TABLE Dictionary (id TEXT PRIMARY KEY, phrase TEXT, replacement TEXT, lastUsed DATETIME, "
        "frequencyUsed INTEGER, manualEntry INT, isDeleted INT, isSnippet INT, isStarred INT, source TEXT)"
    )
    conn.execute(
        "CREATE TABLE Notes (id TEXT PRIMARY KEY, title TEXT, content TEXT, createdAt DATETIME, modifiedAt DATETIME, "
        "isDeleted INT, pinned INT, searchableContent TEXT, synced INT)"
    )
    history(conn, "newest", "2026-11-02 16:00:00.000 +00:00", pasted="alpha pasted wins ", edited="alpha edited",
            formatted="zebra only in formatted")
    history(conn, "edited-fallback", "2026-11-02 15:00:00.000 +00:00", pasted="   ", edited="bravo edited",
            formatted="bravo formatted")
    history(conn, "formatted-fallback", "2026-11-02 14:00:00.000 +00:00", pasted=None, edited=None,
            formatted="charlie 100% formatted")
    history(conn, "no-app", "2026-11-02 13:00:00.000 +00:00", app=None, pasted="delta École text")
    history(conn, "blank-app", "2026-11-02 12:00:00.000 +00:00", app="", pasted="delta blank app", words=None)
    history(conn, "no-time", None, pasted="delta no timestamp")
    history(conn, "archived", "2026-11-02 11:00:00.000 +00:00", pasted="delta archived", archived=1)
    # 03:00 UTC on Nov 2 is 22:00 on Nov 1 in the test timezone, and the file's earliest row.
    history(conn, "late-evening", "2026-11-02 03:00:00.000 +00:00", pasted="echo evening", words=10)
    conn.executemany(
        "INSERT INTO Dictionary (id, phrase, replacement, lastUsed, frequencyUsed, isDeleted, isSnippet, isStarred) "
        "VALUES (?,?,?,?,?,?,?,?)",
        [
            ("d1", "Wispr", "Wispr Flow", "2026-11-02 12:00:00.000 +00:00", 9, 0, 0, 1),
            ("d2", "gone wispr", None, None, 99, 1, 0, 1),
            ("d3", "Other", None, None, None, None, 1, 0),
        ],
    )
    conn.executemany(
        "INSERT INTO Notes (id, title, content, createdAt, modifiedAt, isDeleted, pinned, searchableContent) "
        "VALUES (?,?,?,?,?,?,?,?)",
        [
            ("n1", "Plan", "<p>Foxtrot body</p>", "2026-11-01 12:00:00.000 +00:00", "2026-11-02 12:00:00.000 +00:00",
             0, 1, "Foxtrot body"),
            ("n2", "Gone foxtrot", "x", "2026-11-01 12:00:00.000 +00:00", "2026-11-02 13:00:00.000 +00:00", 1, 0, "x"),
        ],
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("WISPR_FLOW_DB", str(path))
    return path
