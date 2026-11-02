"""The permanent archive: seeding, sync from the live file, and the daily backup.

Only this module writes dictation text, and only to the archive and its backup copies. The live Wispr file is
reached solely through db.query, which is read-only. Output is counts only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any, Iterable, Sequence

from . import config, db
from .local_only import refuse_network

TEXT_COLUMNS = ("asrText", "formattedText", "editedText", "pastedText", "editedTextUnbounded", "serverFinalizedText")
META_COLUMNS = (
    "timestamp",
    "timezoneOffsetMinutes",
    "app",
    "url",
    "numWords",
    "duration",
    "isArchived",
    "status",
    "conversationId",
)
DATA_COLUMNS = TEXT_COLUMNS + META_COLUMNS
DEFAULT_RECOVERED_PATH = "~/wispr-recovery/recovered.sqlite"
SYNC_BATCH_ROWS = 200
BUSY_TIMEOUT_SECONDS = 30
BACKUP_FOLDER = "wispr-flow-archive"
BACKUP_NAME = re.compile(r"^wispr-archive-\d{4}-\d{2}-\d{2}\.sqlite$")
BACKUPS_TO_KEEP = 7

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS History (
  transcriptEntityId TEXT PRIMARY KEY NOT NULL,
  asrText TEXT, formattedText TEXT, editedText TEXT, pastedText TEXT, editedTextUnbounded TEXT,
  serverFinalizedText TEXT,
  timestamp TEXT, timezoneOffsetMinutes INTEGER, app TEXT, url TEXT, numWords INTEGER, duration REAL,
  isArchived INTEGER, status TEXT, conversationId TEXT,
  source TEXT NOT NULL CHECK (source IN ('recovered', 'live')),
  possibly_damaged INTEGER NOT NULL DEFAULT 0,
  unreadable_columns TEXT,
  first_archived_at TEXT NOT NULL,
  last_changed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_archive_timestamp ON History (timestamp);
"""


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)


def open_for_writing(path: str | None = None) -> sqlite3.Connection:
    """Creates the archive readable only by this account. WAL lets the server read during a sync."""
    target = Path(path or db.archive_path())
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    previous = os.umask(0o077)
    try:
        conn = sqlite3.connect(target, timeout=BUSY_TIMEOUT_SECONDS, isolation_level=None)
    finally:
        os.umask(previous)
    conn.execute("PRAGMA journal_mode=WAL")
    create_schema(conn)
    return conn


# --- seeding --------------------------------------------------------------------------------------


def _looks_damaged(row: Sequence[Any]) -> bool:
    """A broken overflow chain, an unreadable column, or text in a numeric field: the record was misread."""
    numeric = [row[1 + DATA_COLUMNS.index(c)] for c in ("numWords", "duration", "isArchived")]
    return not row[-2] or bool(row[-1]) or any(isinstance(value, str) for value in numeric)


def seed(conn: sqlite3.Connection, recovered_path: str) -> dict[str, int]:
    """Adds recovered rows. Never overwrites a row already held, so reseeding cannot undo a sync."""
    if not os.path.exists(recovered_path):
        raise FileNotFoundError("Recovered database not found at the given path")
    source = sqlite3.connect(db.open_uri(recovered_path), uri=True)
    columns = ", ".join(("transcriptEntityId", *DATA_COLUMNS))
    try:
        rows = source.execute(
            f"SELECT {columns}, overflow_chain_ok, unreadable_columns FROM History ORDER BY transcriptEntityId"
        ).fetchall()
    finally:
        source.close()
    now = _now()
    before = conn.total_changes
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.executemany(
            f"INSERT OR IGNORE INTO History ({columns}, source, possibly_damaged, unreadable_columns, "
            f"first_archived_at, last_changed_at) VALUES ({','.join('?' * (len(DATA_COLUMNS) + 1))}, "
            "'recovered', ?, ?, ?, ?)",
            [(*row[:-2], int(_looks_damaged(row)), row[-1] or None, now, now) for row in rows],
        )
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
    return {"recovered_rows_read": len(rows), "added": conn.total_changes - before}


# --- sync -----------------------------------------------------------------------------------------


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def merged_values(live: Any, held: Any | None) -> tuple[Any, ...]:
    """The live version wins, except that a blank live value never erases something the archive holds."""
    return tuple(
        held[c] if held is not None and _is_blank(live[c]) else live[c] for c in DATA_COLUMNS
    )


def _upsert_batch(conn: sqlite3.Connection, live_rows: Sequence[Any]) -> tuple[int, int]:
    ids = [row["transcriptEntityId"] for row in live_rows]
    previous_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        marks = ",".join("?" * len(ids))
        held = {
            row["transcriptEntityId"]: row
            for row in conn.execute(f"SELECT * FROM History WHERE transcriptEntityId IN ({marks})", ids)
        }
    finally:
        conn.row_factory = previous_factory
    now = _now()
    added = changed = 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        for live in live_rows:
            rid = live["transcriptEntityId"]
            current = held.get(rid)
            values = merged_values(live, current)
            if current is None:
                conn.execute(
                    f"INSERT INTO History (transcriptEntityId, {', '.join(DATA_COLUMNS)}, source, possibly_damaged, "
                    f"first_archived_at, last_changed_at) VALUES ({','.join('?' * (len(DATA_COLUMNS) + 1))}, "
                    "'live', 0, ?, ?)",
                    (rid, *values, now, now),
                )
                added += 1
            elif (
                current["source"] != "live"
                or current["possibly_damaged"]
                or values != tuple(current[c] for c in DATA_COLUMNS)
            ):
                assignments = ", ".join(f"{c} = ?" for c in DATA_COLUMNS)
                conn.execute(
                    f"UPDATE History SET {assignments}, source = 'live', possibly_damaged = 0, "
                    "unreadable_columns = NULL, last_changed_at = ? WHERE transcriptEntityId = ?",
                    (*values, now, rid),
                )
                changed += 1
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
    return added, changed


def sync(conn: sqlite3.Connection) -> dict[str, int]:
    """Reads the live file in short batches, each on its own read-only connection. Contains no DELETE."""
    columns = ", ".join(("transcriptEntityId", *DATA_COLUMNS))
    totals = {"live_rows_read": 0, "added": 0, "changed": 0}
    last_id = ""
    while True:
        batch = db.query(
            f"SELECT {columns} FROM History WHERE transcriptEntityId > ? ORDER BY transcriptEntityId LIMIT ?",
            [last_id, SYNC_BATCH_ROWS],
        )
        if not batch:
            break
        added, changed = _upsert_batch(conn, batch)
        totals["live_rows_read"] += len(batch)
        totals["added"] += added
        totals["changed"] += changed
        last_id = batch[-1]["transcriptEntityId"]
    # A row with no id cannot be matched on later syncs, so it is counted and left out.
    totals["live_rows_skipped_no_id"] = db.query(
        "SELECT COUNT(*) FROM History WHERE transcriptEntityId IS NULL OR transcriptEntityId = ''"
    )[0][0]
    return totals


# --- backup ---------------------------------------------------------------------------------------


def _verify_copy(path: Path, expected_rows: int) -> None:
    check = sqlite3.connect(db.open_uri(str(path)), uri=True)
    try:
        verdict = check.execute("PRAGMA integrity_check").fetchone()[0]
        rows = check.execute("SELECT COUNT(*) FROM History").fetchone()[0]
    finally:
        check.close()
    if verdict != "ok":
        raise RuntimeError("Backup copy failed its integrity check")
    if rows != expected_rows:
        raise RuntimeError(f"Backup copy has {rows} rows, expected {expected_rows}")


def backup(backup_root: str, today: dt.date | None = None, keep: int = BACKUPS_TO_KEEP) -> dict[str, Any]:
    """Writes a verified snapshot with SQLite's backup API, publishes it, then prunes. Old copies survive a failure."""
    root = Path(backup_root)
    if not root.is_dir():
        raise FileNotFoundError(
            "The backup folder was not found. If it is a synced folder, check its app is running and signed in."
        )
    folder = root / BACKUP_FOLDER
    folder.mkdir(exist_ok=True)
    day = today or dt.datetime.now().date()
    final = folder / f"wispr-archive-{day.isoformat()}.sqlite"
    temp = folder / f".{final.name}.tmp"
    temp.unlink(missing_ok=True)
    source = sqlite3.connect(db.open_uri(db.archive_path()), uri=True, timeout=BUSY_TIMEOUT_SECONDS)
    try:
        previous = os.umask(0o077)
        try:
            target = sqlite3.connect(temp)
        finally:
            os.umask(previous)
        try:
            source.backup(target)
            expected = target.execute("SELECT COUNT(*) FROM History").fetchone()[0]
            # One self-contained file, with no WAL files beside it for a sync app to upload.
            target.execute("PRAGMA journal_mode=DELETE")
        finally:
            target.close()
    finally:
        source.close()
    try:
        _verify_copy(temp, expected)
        os.replace(temp, final)
    finally:
        temp.unlink(missing_ok=True)
    return {"rows": expected, "bytes": final.stat().st_size, "name": final.name, "pruned": _prune(folder, keep)}


def _prune(folder: Path, keep: int) -> int:
    """Removes only this job's own dated files, oldest first."""
    dated = sorted((p for p in folder.iterdir() if BACKUP_NAME.match(p.name)), key=lambda p: p.name, reverse=True)
    for old in dated[keep:]:
        old.unlink()
    return len(dated[keep:])


# --- command line ---------------------------------------------------------------------------------


def _report(values: dict[str, Any]) -> None:
    stamp = dt.datetime.now().isoformat(timespec="seconds")
    print(stamp, " ".join(f"{k}={v}" for k, v in values.items()), flush=True)


def main(argv: Iterable[str] | None = None) -> int:
    refuse_network()
    parser = argparse.ArgumentParser(prog="wispr-flow-archive")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("sync")
    seeding = commands.add_parser("seed")
    seeding.add_argument("--recovered", default=DEFAULT_RECOVERED_PATH)
    commands.add_parser("backup")
    commands.add_parser("show-backup-folder")
    setting = commands.add_parser("set-backup-folder")
    setting.add_argument("folder")
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        if args.command == "backup":
            root = config.backup_folder()
            _report(backup(root) if root else {"backup": "off"})
            return 0
        if args.command == "show-backup-folder":
            print(config.backup_folder() or "")
            return 0
        if args.command == "set-backup-folder":
            config.set_backup_folder(args.folder)
            print("backup folder set", flush=True)
            return 0
        conn = open_for_writing()
        try:
            if args.command == "seed":
                _report(seed(conn, os.path.expanduser(args.recovered)))
            else:
                _report(sync(conn))
        finally:
            conn.close()
        return 0
    except Exception as exc:  # A traceback could quote values, so report the type and a fixed message only.
        message = str(exc) if isinstance(exc, (FileNotFoundError, db.QueryDeadlineExceeded, RuntimeError)) else ""
        print(f"{args.command} failed: {type(exc).__name__} {message}".strip(), file=sys.stderr, flush=True)
        return 1
