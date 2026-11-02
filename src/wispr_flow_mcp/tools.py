"""Tool logic: filters, time handling and result caps. Returns plain dicts."""

from __future__ import annotations

import datetime as dt
import re
import sqlite3
from typing import Any, Callable, Iterable

from . import config
from .db import ARCHIVE, LIVE, query

_STORED_FORMAT = "%Y-%m-%d %H:%M:%S.%f %z"

MAX_DICTATION_RESULTS = 100
MAX_DICTIONARY_RESULTS = 200
MAX_NOTE_RESULTS = 50
MAX_STAT_GROUPS = 400
LIST_TEXT_CHARS = 4000
DETAIL_TEXT_CHARS = 20000
NOTE_SNIPPET_CHARS = 400
MAX_RESPONSE_CHARS = 60000
UNKNOWN_APP = "(unknown app)"

# Carved rows can hold garbage or fragments of dictation in any field, so nothing leaves a tool unchecked.
# Corrupted word counts run far above any real dictation. A real count above this limit falls back to counting its
# text, which is accurate anyway.
MAX_PLAUSIBLE_WORDS = 3000
MAX_PLAUSIBLE_DURATION_SECONDS = 86400
_BUNDLE_ID = re.compile(r"[A-Za-z0-9-]{2,}(?:\.[A-Za-z0-9-]+)+")
_HOST_URL = re.compile(
    r"(?:[a-z][a-z0-9+.-]*://)?[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}(?::\d{1,5})?(?:[/?#][!-~]*)?"
)
_CONVERSATION_ID = re.compile(r"[0-9A-Za-z]{8}(?:-[0-9A-Za-z]{4}){3}-[0-9A-Za-z]{12}")
# Wispr Flow's own list of History statuses.
_KNOWN_STATUSES = frozenset(
    {
        "empty",
        "error",
        "raw_transcript",
        "formatted",
        "fallback",
        "verification_failed",
        "no_audio",
        "extension_other",
        "extension_paste",
        "dismissed",
        "processing",
        "timeout",
    }
)
NO_TIMESTAMP = "(no timestamp)"

# Search order approved after Step A, with raw text last so raw-only recovered rows stay findable.
# A blank value counts as missing.
BEST_TEXT_COLUMNS = ("pastedText", "editedText", "formattedText", "asrText")
_BEST_TEXT_SQL = (
    "COALESCE(" + ", ".join(f"NULLIF(TRIM({c}), '')" for c in BEST_TEXT_COLUMNS) + ")"
)
_NOT_ARCHIVED_SQL = "COALESCE(isArchived, 0) != 1"
_NOT_DELETED_SQL = "COALESCE(isDeleted, 0) != 1"
_HAS_TEXT_SQL = f"{_BEST_TEXT_SQL} IS NOT NULL"
# The live file has no provenance columns, so each source supplies them under the same names.
_PROVENANCE = {
    ARCHIVE: "source, possibly_damaged, unreadable_columns",
    LIVE: "'live' AS source, 0 AS possibly_damaged, NULL AS unreadable_columns",
}
_UNAVAILABLE_WARNING = {
    LIVE: "The live Wispr file could not be read, so dictations from the last 15 minutes may be missing.",
    ARCHIVE: "The archive could not be read, so only what the live Wispr file still holds was searched.",
}
_DICTATION_COLUMNS = (
    "transcriptEntityId, timestamp, app, url, numWords, pastedText, editedText, formattedText, asrText"
)


# --- time ---------------------------------------------------------------------------------------

def to_local(stored: str | None) -> str | None:
    """Convert a stored UTC timestamp to the local timezone. Unrecognised shapes raise."""
    if stored is None:
        return None
    try:
        parsed = dt.datetime.strptime(stored, _STORED_FORMAT)
    except (TypeError, ValueError):
        raise ValueError("Database holds a timestamp in an unrecognised format") from None
    return parsed.astimezone(config.timezone()).isoformat(timespec="seconds")


def _to_stored(value: dt.datetime) -> str:
    utc = value.astimezone(dt.timezone.utc)
    return utc.strftime("%Y-%m-%d %H:%M:%S.") + f"{utc.microsecond // 1000:03d} +00:00"


def _parse_bound(value: str | None, name: str, end_of_day: bool) -> str | None:
    """ISO date or datetime. No offset means local time. A bare `until` date covers that whole day."""
    if value is None:
        return None
    text = value.strip()
    try:
        if len(text) == 10:
            day = dt.date.fromisoformat(text)
            clock = dt.time(23, 59, 59, 999000) if end_of_day else dt.time(0)
            return _to_stored(dt.datetime.combine(day, clock, config.timezone()))
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"{name} must be an ISO date or datetime, such as 2026-11-02 or 2026-11-02T09:30") from None
    return _to_stored(parsed if parsed.tzinfo else parsed.replace(tzinfo=config.timezone()))


def _time_filters(since: str | None, until: str | None) -> tuple[list[str], list[str]]:
    lower = _parse_bound(since, "since", end_of_day=False)
    upper = _parse_bound(until, "until", end_of_day=True)
    if lower and upper and lower > upper:
        raise ValueError("since is later than until")
    where, params = [], []
    if lower:
        where.append("timestamp >= ?")
        params.append(lower)
    if upper:
        where.append("timestamp <= ?")
        params.append(upper)
    return where, params


def _from_each_source(run: Callable[[str], list[Any]]) -> tuple[dict[str, list[Any]], list[str]]:
    """Run against both sources. One unavailable source becomes a warning; both unavailable raises."""
    found: dict[str, list[Any]] = {}
    warnings: list[str] = []
    failure: Exception | None = None
    for source in (ARCHIVE, LIVE):
        try:
            found[source] = run(source)
        except FileNotFoundError as exc:
            failure = exc
            warnings.append(_UNAVAILABLE_WARNING[source])
        except sqlite3.OperationalError as exc:
            if not any(word in str(exc) for word in ("unable to open", "locked", "busy")):
                raise
            failure = exc
            warnings.append(_UNAVAILABLE_WARNING[source])
    if not found:
        raise failure if failure else RuntimeError("No dictation source could be read")
    return found, warnings


def _merge_by_id(found: dict[str, list[Any]]) -> dict[str, Any]:
    """The live version of a row replaces the archive's."""
    merged: dict[str, Any] = {}
    for source in (ARCHIVE, LIVE):
        for row in found.get(source, []):
            merged[row["transcriptEntityId"]] = row
    return merged


def history_window() -> dict[str, Any]:
    found, warnings = _from_each_source(
        lambda source: query("SELECT transcriptEntityId, timestamp FROM History", source=source)
    )
    merged = _merge_by_id(found)
    dated = [row["timestamp"] for row in merged.values() if row["timestamp"] is not None]
    window = {
        "earliest_local": to_local(min(dated)) if dated else None,
        "latest_local": to_local(max(dated)) if dated else None,
        "total_dictations": len(merged),
        "dictations_without_timestamp": len(merged) - len(dated),
    }
    if warnings:
        window["warnings"] = warnings
    return window


# --- shared helpers -----------------------------------------------------------------------------

def _clean(value: str | None) -> str | None:
    return value if value and value.strip() else None


def _safe_app(value: Any) -> str:
    if isinstance(value, str) and len(value) <= 255 and _BUNDLE_ID.fullmatch(value):
        return value
    return UNKNOWN_APP


def _safe_url(value: Any) -> str | None:
    return value if isinstance(value, str) and len(value) <= 2048 and _HOST_URL.fullmatch(value) else None


def _safe_status(value: Any) -> str | None:
    return value if isinstance(value, str) and value in _KNOWN_STATUSES else None


def _safe_conversation_id(value: Any) -> str | None:
    return value if isinstance(value, str) and _CONVERSATION_ID.fullmatch(value) else None


def _safe_duration(value: Any) -> int | float | None:
    is_number = isinstance(value, (int, float)) and not isinstance(value, bool)
    return value if is_number and 0 <= value <= MAX_PLAUSIBLE_DURATION_SECONDS else None


def _word_count(stored: Any, text: Any) -> int:
    """The stored count if plausible, otherwise a count of the text, otherwise 0."""
    if type(stored) is int and 0 <= stored <= MAX_PLAUSIBLE_WORDS:
        return stored
    return len(text.split()) if isinstance(text, str) else 0


def _capped_text(value: str | None, cap: int) -> dict[str, Any]:
    if value is None:
        return {"text": None, "truncated": False, "total_chars": 0}
    return {"text": value[:cap], "truncated": len(value) > cap, "total_chars": len(value)}


def _effective_limit(limit: int, maximum: int) -> int:
    if limit < 1:
        raise ValueError("limit must be at least 1")
    return min(limit, maximum)


def _required_text(value: str, name: str) -> str:
    if not value or not value.strip():
        raise ValueError(f"{name} must not be empty")
    return value


def _envelope(rows: Iterable[dict[str, Any]], requested: int, applied: int, more_in_db: bool) -> dict[str, Any]:
    """Stop adding rows at the size budget, and always say when anything was left out."""
    results: list[dict[str, Any]] = []
    used = 0
    stopped_for_size = False
    for row in rows:
        size = sum(len(v) for v in row.values() if isinstance(v, str))
        if results and used + size > MAX_RESPONSE_CHARS:
            stopped_for_size = True
            break
        results.append(row)
        used += size
    out: dict[str, Any] = {
        "count": len(results),
        "has_more": more_in_db or stopped_for_size,
        "results": results,
    }
    if requested > applied:
        out["limit_capped_to"] = applied
    if stopped_for_size:
        out["stopped_at_size_cap_chars"] = MAX_RESPONSE_CHARS
    return out


def _dictation_row(row: Any) -> dict[str, Any]:
    source = next((c for c in BEST_TEXT_COLUMNS if _clean(row[c])), None)
    capped = _capped_text(row[source] if source else None, LIST_TEXT_CHARS)
    return {
        "id": row["transcriptEntityId"],
        "local_time": to_local(row["timestamp"]),
        "app": _safe_app(row["app"]),
        "url": _safe_url(row["url"]),
        "word_count": _word_count(row["numWords"], row[source] if source else None),
        "source": row["source"],
        "possibly_damaged": bool(row["possibly_damaged"]),
        "text": capped["text"],
        "text_source": source,
        "text_truncated": capped["truncated"],
        "text_total_chars": capped["total_chars"],
    }


def _dictation_list(
    where: list[str], params: list[Any], limit: int, include_archived: bool
) -> dict[str, Any]:
    applied = _effective_limit(limit, MAX_DICTATION_RESULTS)
    clauses = [*where, _HAS_TEXT_SQL]
    if not include_archived:
        clauses.append(_NOT_ARCHIVED_SQL)
    where_sql = " AND ".join(clauses)
    found, warnings = _from_each_source(
        lambda source: query(
            f"SELECT {_DICTATION_COLUMNS}, {_PROVENANCE[source]} FROM History WHERE {where_sql} "
            "ORDER BY timestamp DESC NULLS LAST LIMIT ?",
            [*params, applied + 1],
            source,
        )
    )
    rows = sorted(_merge_by_id(found).values(), key=lambda row: row["timestamp"] or "", reverse=True)
    out = _envelope((_dictation_row(r) for r in rows[:applied]), limit, applied, len(rows) > applied)
    if warnings:
        out["warnings"] = warnings
    if not rows:
        window = history_window()
        out["history_window"] = window
        out["note"] = (
            f"No matches. The history only covers {window['earliest_local']} to {window['latest_local']} "
            f"({config.timezone().key}), so older dictations cannot be found here."
        )
    return out


# --- dictation tools ----------------------------------------------------------------------------

def search_dictations(
    query_text: str,
    since: str | None = None,
    until: str | None = None,
    app: str | None = None,
    limit: int = 25,
    include_archived: bool = False,
) -> dict[str, Any]:
    _required_text(query_text, "query")
    where, params = _time_filters(since, until)
    if app is not None:
        _required_text(app, "app")
        where.append("contains_text(app, ?)")
        params.append(app)
    where.append(f"contains_text({_BEST_TEXT_SQL}, ?)")
    params.append(query_text)
    return _dictation_list(where, params, limit, include_archived)


def recent_dictations(
    limit: int = 25, since: str | None = None, app: str | None = None, include_archived: bool = False
) -> dict[str, Any]:
    where, params = _time_filters(since, None)
    if app is not None:
        _required_text(app, "app")
        where.append("contains_text(app, ?)")
        params.append(app)
    return _dictation_list(where, params, limit, include_archived)


def get_dictation(dictation_id: str, include_archived: bool = False) -> dict[str, Any]:
    _required_text(dictation_id, "id")
    columns = (
        "transcriptEntityId, timestamp, timezoneOffsetMinutes, app, url, numWords, duration, status, "
        "conversationId, isArchived, asrText, formattedText, editedText, editedTextUnbounded, pastedText, "
        "serverFinalizedText"
    )
    found, _warnings = _from_each_source(
        lambda source: query(
            f"SELECT {columns}, {_PROVENANCE[source]} FROM History WHERE transcriptEntityId = ?",
            [dictation_id],
            source,
        )
    )
    rows = found.get(LIVE) or found.get(ARCHIVE)
    if not rows:
        raise ValueError("No dictation with that id")
    row = rows[0]
    if row["isArchived"] == 1 and not include_archived:
        raise ValueError("That dictation is archived. Pass include_archived to read it.")
    best_text = next((row[c] for c in BEST_TEXT_COLUMNS if _clean(row[c])), None)
    versions = {
        "raw": row["asrText"],
        "cleaned": row["formattedText"],
        "edited": row["editedText"],
        "edited_unbounded": row["editedTextUnbounded"],
        "pasted": row["pastedText"],
        "server_finalized": row["serverFinalizedText"],
    }
    return {
        "id": row["transcriptEntityId"],
        "local_time": to_local(row["timestamp"]),
        "app": _safe_app(row["app"]),
        "url": _safe_url(row["url"]),
        "word_count": _word_count(row["numWords"], best_text),
        "duration_seconds": _safe_duration(row["duration"]),
        "status": _safe_status(row["status"]),
        "conversation_id": _safe_conversation_id(row["conversationId"]),
        "is_archived": row["isArchived"] == 1,
        "source": row["source"],
        "possibly_damaged": bool(row["possibly_damaged"]),
        "unreadable_columns": row["unreadable_columns"] or None,
        "versions": {k: _capped_text(v, DETAIL_TEXT_CHARS) for k, v in versions.items()},
    }


def dictation_stats(
    since: str | None = None, until: str | None = None, group_by: str = "day", include_archived: bool = False
) -> dict[str, Any]:
    if group_by not in ("day", "app"):
        raise ValueError("group_by must be day or app")
    where, params = _time_filters(since, until)
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    found, warnings = _from_each_source(
        lambda source: query(
            f"SELECT transcriptEntityId, timestamp, app, numWords, isArchived, {_BEST_TEXT_SQL} AS best_text, "
            f"{_PROVENANCE[source]} FROM History {where_sql}",
            params,
            source,
        )
    )
    # Filter after merging, so the live version decides whether a row counts as archived.
    rows = [row for row in _merge_by_id(found).values() if include_archived or row["isArchived"] != 1]

    groups: dict[str, dict[str, int]] = {}
    for row in rows:
        if group_by == "app":
            key = _safe_app(row["app"])
        else:
            local = to_local(row["timestamp"])
            key = local[:10] if local else NO_TIMESTAMP
        group = groups.setdefault(key, {"dictations": 0, "words": 0})
        group["dictations"] += 1
        group["words"] += _word_count(row["numWords"], row["best_text"])

    if group_by == "app":
        ordered = sorted(groups.items(), key=lambda kv: (-kv[1]["dictations"], kv[0]))
    else:
        dated = sorted((kv for kv in groups.items() if kv[0] != NO_TIMESTAMP), key=lambda kv: kv[0], reverse=True)
        ordered = dated + [kv for kv in groups.items() if kv[0] == NO_TIMESTAMP]

    return {
        "group_by": group_by,
        "total_dictations": sum(g["dictations"] for g in groups.values()),
        "total_words": sum(g["words"] for g in groups.values()),
        "recovered_dictations": sum(row["source"] == "recovered" for row in rows),
        "possibly_damaged_dictations": sum(bool(row["possibly_damaged"]) for row in rows),
        "dictations_without_text": sum(row["best_text"] is None for row in rows),
        "group_count": len(ordered),
        "has_more": len(ordered) > MAX_STAT_GROUPS,
        "groups": [{"group": k, **v} for k, v in ordered[:MAX_STAT_GROUPS]],
        "history_window": history_window(),
        **({"warnings": warnings} if warnings else {}),
    }


# --- dictionary tools ---------------------------------------------------------------------------

_DICTIONARY_COLUMNS = "phrase, replacement, frequencyUsed, lastUsed, isStarred, isSnippet"


def _dictionary_row(row: Any) -> dict[str, Any]:
    return {
        "phrase": row["phrase"],
        "replacement": row["replacement"],
        "times_used": row["frequencyUsed"],
        "last_used_local": to_local(row["lastUsed"]),
        "starred": bool(row["isStarred"]),
        "snippet": bool(row["isSnippet"]),
    }


def _dictionary_list(where: list[str], params: list[Any], limit: int) -> dict[str, Any]:
    applied = _effective_limit(limit, MAX_DICTIONARY_RESULTS)
    clauses = [_NOT_DELETED_SQL, *where]
    rows = query(
        f"SELECT {_DICTIONARY_COLUMNS} FROM Dictionary WHERE {' AND '.join(clauses)} "
        "ORDER BY COALESCE(frequencyUsed, 0) DESC, phrase LIMIT ?",
        [*params, applied + 1],
    )
    return _envelope((_dictionary_row(r) for r in rows[:applied]), limit, applied, len(rows) > applied)


def search_dictionary(query_text: str, limit: int = 50) -> dict[str, Any]:
    _required_text(query_text, "query")
    return _dictionary_list(
        ["(contains_text(phrase, ?) OR contains_text(replacement, ?))"], [query_text, query_text], limit
    )


def list_dictionary(limit: int = 50, starred_only: bool = False) -> dict[str, Any]:
    return _dictionary_list(["isStarred = 1"] if starred_only else [], [], limit)


# --- notes tools --------------------------------------------------------------------------------

_NOTE_TEXT_SQL = "COALESCE(NULLIF(TRIM(searchableContent), ''), NULLIF(TRIM(content), ''))"


def search_notes(query_text: str, limit: int = 25) -> dict[str, Any]:
    _required_text(query_text, "query")
    applied = _effective_limit(limit, MAX_NOTE_RESULTS)
    rows = query(
        "SELECT id, title, searchableContent, content, createdAt, modifiedAt, pinned FROM Notes "
        f"WHERE {_NOT_DELETED_SQL} AND (contains_text(title, ?) OR contains_text({_NOTE_TEXT_SQL}, ?)) "
        "ORDER BY modifiedAt DESC NULLS LAST LIMIT ?",
        [query_text, query_text, applied + 1],
    )

    def shape(row: Any) -> dict[str, Any]:
        capped = _capped_text(_clean(row["searchableContent"]) or _clean(row["content"]), NOTE_SNIPPET_CHARS)
        return {
            "id": row["id"],
            "title": row["title"],
            "snippet": capped["text"],
            "snippet_truncated": capped["truncated"],
            "created_local": to_local(row["createdAt"]),
            "modified_local": to_local(row["modifiedAt"]),
            "pinned": bool(row["pinned"]),
        }

    return _envelope((shape(r) for r in rows[:applied]), limit, applied, len(rows) > applied)


def get_note(note_id: str) -> dict[str, Any]:
    _required_text(note_id, "id")
    rows = query(
        "SELECT id, title, content, searchableContent, createdAt, modifiedAt, pinned FROM Notes "
        f"WHERE id = ? AND {_NOT_DELETED_SQL}",
        [note_id],
    )
    if not rows:
        raise ValueError("No note with that id")
    row = rows[0]
    return {
        "id": row["id"],
        "title": row["title"],
        "content": _capped_text(row["content"], DETAIL_TEXT_CHARS),
        "searchable_content": _capped_text(row["searchableContent"], DETAIL_TEXT_CHARS),
        "created_local": to_local(row["createdAt"]),
        "modified_local": to_local(row["modifiedAt"]),
        "pinned": bool(row["pinned"]),
    }
