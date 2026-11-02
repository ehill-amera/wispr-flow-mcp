"""Read-only access to Wispr Flow's SQLite file.

Four independent guards apply to every query:
  0. Anything but a SELECT is refused before it reaches SQLite.
  1. The file is opened with a SQLite URI in mode=ro, so the engine refuses writes.
  2. An authorizer permits only SELECT, and only reads of columns on READABLE_COLUMNS.
     BLOB columns, axText, axHTML and textboxContents are not on it, so a query that
     names one fails at prepare time instead of returning data.
  3. A deadline aborts long queries with an error, so no read holds a snapshot for long.

One connection per call, closed straight away. Nothing is logged here.
"""

from __future__ import annotations

import os
import sqlite3
import time
import urllib.parse
from contextlib import contextmanager
from typing import Any, Iterator, Sequence

DEFAULT_DB_PATH = "~/Library/Application Support/Wispr Flow/flow.sqlite"
DB_PATH_ENV = "WISPR_FLOW_DB"
DEFAULT_ARCHIVE_PATH = "~/Library/Application Support/wispr-flow-mcp/archive.sqlite"
ARCHIVE_PATH_ENV = "WISPR_FLOW_ARCHIVE"
LIVE = "live"
ARCHIVE = "archive"
BUSY_TIMEOUT_SECONDS = 2.0
QUERY_DEADLINE_SECONDS = 20.0
_PROGRESS_INTERVAL_OPS = 1000

READABLE_COLUMNS: dict[str, frozenset[str]] = {
    "History": frozenset(
        {
            "transcriptEntityId",
            "asrText",
            "formattedText",
            "editedText",
            "pastedText",
            "editedTextUnbounded",
            "serverFinalizedText",
            "timestamp",
            "timezoneOffsetMinutes",
            "app",
            "url",
            "numWords",
            "duration",
            "isArchived",
            "status",
            "conversationId",
        }
    ),
    "Dictionary": frozenset(
        {"id", "phrase", "replacement", "frequencyUsed", "lastUsed", "isDeleted", "isSnippet", "isStarred", "manualEntry"}
    ),
    "Notes": frozenset({"id", "title", "content", "searchableContent", "createdAt", "modifiedAt", "isDeleted", "pinned"}),
}

# The archive holds only text and metadata, so nothing forbidden can exist there. The list still applies.
ARCHIVE_READABLE_COLUMNS: dict[str, frozenset[str]] = {
    "History": READABLE_COLUMNS["History"]
    | frozenset(
        {
            "source",
            "possibly_damaged",
            "unreadable_columns",
            "first_archived_at",
            "last_changed_at",
        }
    )
}

_ALLOWED_FUNCTIONS = frozenset({"contains_text", "count", "min", "max", "sum", "coalesce", "nullif", "trim"})


class NotASelect(ValueError):
    """Anything but a SELECT is refused before SQLite sees it, so VACUUM never even starts."""


class QueryDeadlineExceeded(RuntimeError):
    """A query ran past the deadline. Raised rather than returning a partial result."""


def db_path() -> str:
    return os.path.expanduser(os.environ.get(DB_PATH_ENV) or DEFAULT_DB_PATH)


def archive_path() -> str:
    return os.path.expanduser(os.environ.get(ARCHIVE_PATH_ENV) or DEFAULT_ARCHIVE_PATH)


def _decide(
    allowed: dict[str, frozenset[str]], action: int, arg1: str | None, arg2: str | None
) -> int:
    if action == sqlite3.SQLITE_SELECT:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_READ:
        # An empty column name is how SQLite reports a read of no column, as in COUNT(*).
        if arg2 == "" or arg2 in allowed.get(arg1 or "", ()):
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_FUNCTION and arg2 in _ALLOWED_FUNCTIONS:
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def _authorizer(action: int, arg1: str | None, arg2: str | None, _db: str | None, _trigger: str | None) -> int:
    return _decide(READABLE_COLUMNS, action, arg1, arg2)


def _archive_authorizer(action: int, arg1: str | None, arg2: str | None, _db: str | None, _trigger: str | None) -> int:
    return _decide(ARCHIVE_READABLE_COLUMNS, action, arg1, arg2)


def _contains_text(haystack: str | None, needle: str | None) -> int:
    """Literal, Unicode-aware, case-insensitive substring test."""
    if not isinstance(haystack, str) or not isinstance(needle, str):
        return 0
    return int(needle.casefold() in haystack.casefold())


def open_uri(path: str) -> str:
    return "file:" + urllib.parse.quote(path) + "?mode=ro"


@contextmanager
def connect(source: str = LIVE) -> Iterator[sqlite3.Connection]:
    path = db_path() if source == LIVE else archive_path()
    # mode=ro does not create a missing file, but say so plainly rather than "unable to open".
    if not os.path.exists(path):
        name = "Wispr Flow database" if source == LIVE else "Dictation archive"
        raise FileNotFoundError(f"{name} not found at the configured path")
    conn = sqlite3.connect(open_uri(path), uri=True, timeout=BUSY_TIMEOUT_SECONDS, isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        conn.create_function("contains_text", 2, _contains_text, deterministic=True)
        conn.set_authorizer(_authorizer if source == LIVE else _archive_authorizer)
        yield conn
    finally:
        conn.close()


def query(sql: str, params: Sequence[Any] = (), source: str = LIVE) -> list[sqlite3.Row]:
    """Run one SELECT in autocommit, so its read snapshot ends when the rows are fetched."""
    if not sql.lstrip().upper().startswith("SELECT "):
        raise NotASelect("Only SELECT statements are allowed")
    with connect(source) as conn:
        deadline = time.monotonic() + QUERY_DEADLINE_SECONDS
        conn.set_progress_handler(lambda: int(time.monotonic() > deadline), _PROGRESS_INTERVAL_OPS)
        try:
            return conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError as exc:
            if "interrupted" in str(exc):
                raise QueryDeadlineExceeded(
                    f"Query exceeded {QUERY_DEADLINE_SECONDS:g}s and was stopped. Narrow it with since, until or app."
                ) from None
            raise
