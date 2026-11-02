"""Recovers deleted dictations from a COPY of Wispr Flow's database.

SQLite leaves deleted rows in free space until new data overwrites them, so they can be read back from the raw pages.
The original file is only ever opened for reading as plain bytes: once to check it is worth copying, once to copy it.
Every later step reads the copy. Only text and metadata columns are decoded, so audio, screenshots, axText, axHTML and
textboxContents never reach the output, and there is no intermediate file. Output is counts only.
"""

from __future__ import annotations

import argparse
import mmap
import os
import re
import shutil
import sqlite3
import struct
import sys
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import quote

from . import archive, db
from .local_only import refuse_network

DEFAULT_WORK_DIR = "~/wispr-recovery"
COPY_NAME = "flow-copy.sqlite"
OUTPUT_NAME = "recovered.sqlite"
SQLITE_MAGIC = b"SQLite format 3\x00"
HEADER_BYTES = 100
FREE_PAGES_TO_SAMPLE = 2000
SPACE_MARGIN_BYTES = 1024**3
ID_SERIAL_TYPE = 85  # A 36-character text value, the shape of a dictation id.
MIN_RECORD_COLUMNS = 15
MAX_VARINT_BYTES = 9
_ID_PATTERN = re.compile(rb"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_TIMESTAMP = re.compile(r"^20\d\d-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3} \+00:00$")
_BLOB_COLUMNS = ("audio", "screenshot")


class RecoveryError(RuntimeError):
    """Stops the recovery with a message that is safe to print. Holds no dictation text."""


# --- checks before copying ------------------------------------------------------------------------


def _read_layout(handle: Any) -> dict[str, int]:
    header = handle.read(HEADER_BYTES)
    if len(header) < HEADER_BYTES or not header.startswith(SQLITE_MAGIC):
        raise RecoveryError("That file is not a SQLite database")
    page_size = struct.unpack(">H", header[16:18])[0]
    page_size = 65536 if page_size == 1 else page_size
    if page_size < 512 or page_size & (page_size - 1):
        raise RecoveryError("That database has an unrecognised page size")
    return {
        "page_size": page_size,
        "usable": page_size - header[20],
        "first_trunk": struct.unpack(">I", header[32:36])[0],
        "free_pages": struct.unpack(">I", header[36:40])[0],
    }


def _free_list_trunks(data: Any, layout: dict[str, int]) -> set[int]:
    """Pages now holding the free list. Their bytes are page numbers, whatever they held before."""
    trunks: set[int] = set()
    total_pages = len(data) // layout["page_size"]
    trunk = layout["first_trunk"]
    while trunk and 1 <= trunk <= total_pages and trunk not in trunks:
        trunks.add(trunk)
        offset = (trunk - 1) * layout["page_size"]
        trunk = struct.unpack(">I", data[offset : offset + 4])[0]
    return trunks


def inspect_free_space(path: str | Path) -> dict[str, int]:
    """Counts free pages and how many of a sample are not blank. Plain read-only bytes; nothing is decoded."""
    with open(path, "rb") as handle:
        layout = _read_layout(handle)
        size = os.fstat(handle.fileno()).st_size
        page_size = layout["page_size"]
        total_pages = size // page_size

        def read_page(number: int) -> bytes:
            handle.seek((number - 1) * page_size)
            return handle.read(page_size)

        sampled = with_data = 0
        trunk, seen = layout["first_trunk"], set()
        while trunk and sampled < FREE_PAGES_TO_SAMPLE:
            if not 1 <= trunk <= total_pages or trunk in seen:
                break  # The file may be changing as it is read, so a bad pointer only ends the sample.
            seen.add(trunk)
            page = read_page(trunk)
            next_trunk, count = struct.unpack(">II", page[:8])
            for index in range(min(count, (layout["usable"] - 8) // 4)):
                leaf = struct.unpack(">I", page[8 + index * 4 : 12 + index * 4])[0]
                if sampled >= FREE_PAGES_TO_SAMPLE:
                    break
                if 1 <= leaf <= total_pages:
                    sampled += 1
                    with_data += any(read_page(leaf))
            trunk = next_trunk
    return {"free_pages": layout["free_pages"], "free_pages_sampled": sampled, "sampled_with_data": with_data}


def check_disk_space(folder: Path, needed_for_copy: int) -> dict[str, int]:
    free = shutil.disk_usage(folder).free
    needed = needed_for_copy + SPACE_MARGIN_BYTES
    if free < needed:
        raise RecoveryError(f"Not enough disk space: {needed} bytes needed, {free} free. Nothing was copied.")
    return {"bytes_free": free, "bytes_needed": needed}


def make_copy(source: Path, destination: Path) -> int:
    """Copies the bytes, readable only by this account. A failed copy leaves nothing behind."""
    expected = source.stat().st_size
    previous = os.umask(0o077)
    try:
        shutil.copyfile(source, destination)
        copied = destination.stat().st_size
        if copied < expected:
            raise RecoveryError("The copy came out shorter than the original")
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    finally:
        os.umask(previous)
    destination.chmod(0o400)
    return copied


# --- carving --------------------------------------------------------------------------------------


def _varint(data: bytes, start: int) -> tuple[int, int]:
    value = 0
    for offset in range(MAX_VARINT_BYTES):
        byte = data[start + offset]
        if offset == MAX_VARINT_BYTES - 1:
            return (value << 8) | byte, MAX_VARINT_BYTES
        value = (value << 7) | (byte & 0x7F)
        if byte < 0x80:
            return value, offset + 1
    raise ValueError


def _serial_size(serial: int) -> int | None:
    if serial <= 4:
        return serial
    if serial == 5:
        return 6
    if serial in (6, 7):
        return 8
    if serial in (8, 9):
        return 0
    if serial in (10, 11):
        return None
    return (serial - 12) // 2 if serial % 2 == 0 else (serial - 13) // 2


def _decode(serial: int, raw: bytes) -> Any:
    if serial == 0:
        return None
    if serial in (8, 9):
        return int(serial == 9)
    if 1 <= serial <= 6:
        return int.from_bytes(raw, "big", signed=True)
    if serial == 7:
        return struct.unpack(">d", raw)[0]
    if serial >= 13 and serial % 2 == 1:
        return raw.decode("utf-8")
    return None  # A blob in a kept column is never decoded.


def _read_columns(copy: Path) -> list[str]:
    """The column order, from the copy's own schema, so a different Wispr Flow version still lines up."""
    conn = sqlite3.connect(f"file:{quote(str(copy))}?mode=ro&immutable=1", uri=True)
    try:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(History)")]
    except sqlite3.DatabaseError:
        raise RecoveryError("The copy's schema could not be read") from None
    finally:
        conn.close()
    if not columns or columns[0] != "transcriptEntityId" or "timestamp" not in columns:
        raise RecoveryError("This does not look like a Wispr Flow database this tool understands")
    return columns


class _Carver:
    def __init__(self, data: Any, columns: list[str], layout: dict[str, int]) -> None:
        self.data = data
        self.index = {name: position for position, name in enumerate(columns)}
        self.max_columns = len(columns)
        self.page_size = layout["page_size"]
        self.usable = layout["usable"]
        self.total_pages = len(data) // self.page_size
        self.trunks = _free_list_trunks(data, layout)
        self.max_local = self.usable - 35
        self.min_local = ((self.usable - 12) * 32 // 255) - 23
        self.best: dict[str, dict[str, Any]] = {}
        self.versions: dict[str, int] = {}

    def _types(self, page: bytes, start: int, end: int) -> list[int] | None:
        out: list[int] = []
        position = start
        while position < end:
            serial, used = _varint(page, position)
            if _serial_size(serial) is None or len(out) >= self.max_columns:
                return None
            out.append(serial)
            position += used
        return out if position == end else None

    def _parse_header(self, page: bytes, id_at: int) -> tuple[list[int], int, int, bool] | None:
        """Finds the record header ending where the id begins. A freed cell may have lost its length varint."""
        limit = self.max_columns * MAX_VARINT_BYTES + MAX_VARINT_BYTES
        for length in range(2, limit):
            start = id_at - length
            if start < 0:
                break
            value, used = _varint(page, start)
            if value == length and page[start + used] == ID_SERIAL_TYPE:
                types = self._types(page, start + used, id_at)
                if types:
                    return types, length, start, True
        for types_start in range(id_at - 1, max(id_at - limit, -1), -1):
            if page[types_start] == ID_SERIAL_TYPE:
                types = self._types(page, types_start, id_at)
                if types and len(types) >= MIN_RECORD_COLUMNS:
                    length = id_at - types_start
                    length += 1 if length + 1 < 128 else 2
                    return types, length, id_at - length, False
        return None

    def _chain(self, first: int, needed: int) -> tuple[list[int], bool]:
        pages: list[int] = []
        current, seen = first, set()
        while current:
            if not 1 <= current <= self.total_pages or current in seen or current in self.trunks or len(pages) > needed:
                return pages, False
            seen.add(current)
            pages.append(current)
            offset = (current - 1) * self.page_size
            current = struct.unpack(">I", self.data[offset : offset + 4])[0]
        return pages, len(pages) == needed

    def _read_record_bytes(
        self, page: bytes, start: int, local: int, chain: list[int], offset: int, size: int
    ) -> bytes | None:
        out = bytearray()
        if offset < local:
            out += page[start + offset : start + min(offset + size, local)]
        if offset + size > local:
            per_page = self.usable - 4
            begin = max(offset, local) - local
            end = offset + size - local
            for number in range(begin // per_page, (end - 1) // per_page + 1):
                if number >= len(chain):
                    return None
                base = (chain[number] - 1) * self.page_size
                body = self.data[base + 4 : base + self.usable]
                out += body[max(begin - number * per_page, 0) : min(end - number * per_page, per_page)]
        # A record running off the end of its page was misread. Short text must not pass as the whole value.
        return bytes(out) if len(out) == size else None

    def _carve_record(self, page: bytes, id_at: int, record_id: str) -> None:
        parsed = self._parse_header(page, id_at)
        if not parsed:
            return
        types, header_length, start, header_ok = parsed
        if types[0] != ID_SERIAL_TYPE or len(types) < MIN_RECORD_COLUMNS:
            return
        for name in _BLOB_COLUMNS:
            position = self.index.get(name)
            if position is not None and position < len(types):
                if not (types[position] == 0 or (types[position] >= 12 and types[position] % 2 == 0)):
                    return
        time_type = types[self.index["timestamp"]]
        if not (time_type == 0 or (time_type >= 13 and time_type % 2 == 1)):
            return
        sizes = [_serial_size(serial) or 0 for serial in types]
        offsets, position = [], header_length
        for size in sizes:
            offsets.append(position)
            position += size
        payload = position
        if payload <= self.max_local:
            local, needed, first_overflow = payload, 0, 0
        else:
            spill = self.min_local + ((payload - self.min_local) % (self.usable - 4))
            local = spill if spill <= self.max_local else self.min_local
            if start + local + 4 > self.usable:
                return
            first_overflow = struct.unpack(">I", page[start + local : start + local + 4])[0]
            needed = (payload - local + self.usable - 5) // (self.usable - 4)
        if start + local > self.usable:
            return
        chain, chain_ok = self._chain(first_overflow, needed) if first_overflow else ([], needed == 0)

        row: dict[str, Any] = {}
        unreadable: list[str] = []
        for name in archive.DATA_COLUMNS:
            column = self.index.get(name)
            if column is None or column >= len(types):
                row[name] = None
                continue
            raw = self._read_record_bytes(page, start, local, chain, offsets[column], sizes[column])
            if raw is None:
                row[name] = None
                unreadable.append(name)
                continue
            try:
                row[name] = _decode(types[column], raw)
            except (UnicodeDecodeError, struct.error):
                row[name] = None
                unreadable.append(name)
        stamp = row["timestamp"]
        if stamp is not None and not (isinstance(stamp, str) and _TIMESTAMP.match(stamp)):
            row["timestamp"] = None
            unreadable.append("timestamp")
            if not header_ok:
                return

        self.versions[record_id] = self.versions.get(record_id, 0) + 1
        text_values = [row[name] for name in archive.TEXT_COLUMNS if isinstance(row[name], str)]
        score = (chain_ok, not unreadable, sum(1 for t in text_values if t.strip()), sum(map(len, text_values)))
        current = self.best.get(record_id)
        if current is None or score > current["score"]:
            self.best[record_id] = {
                "score": score,
                "row": row,
                "chain_ok": chain_ok,
                "header_ok": header_ok,
                "columns": len(types),
                "unreadable": ",".join(unreadable),
            }

    def run(self) -> None:
        for number in range(self.total_pages):
            page = self.data[number * self.page_size : (number + 1) * self.page_size]
            for match in _ID_PATTERN.finditer(page):
                try:
                    self._carve_record(page, match.start(), match.group().decode())
                except (IndexError, ValueError, struct.error):
                    continue  # Free space holds fragments that only resemble records.


def carve(copy: Path, output: Path) -> dict[str, Any]:
    """Writes every record it can read to `output`, keeping the fullest version of each id."""
    if output.exists():
        raise RecoveryError(f"{output.name} already exists in that folder. Move it or pick another folder.")
    columns = _read_columns(copy)
    with open(copy, "rb") as handle:
        layout = _read_layout(handle)
        with mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as data:
            carver = _Carver(data, columns, layout)
            carver.run()
    if not carver.best:
        return {"rows": 0}

    previous = os.umask(0o077)
    try:
        conn = sqlite3.connect(output, isolation_level=None)
    finally:
        os.umask(previous)
    try:
        conn.execute("PRAGMA journal_mode=MEMORY")  # No journal file holding text beside the output.
        names = ",".join(archive.DATA_COLUMNS)
        conn.execute(
            f"CREATE TABLE History (transcriptEntityId TEXT PRIMARY KEY, {names}, versions_seen INT, "
            "overflow_chain_ok INT, header_intact INT, record_columns INT, unreadable_columns TEXT)"
        )
        marks = ",".join("?" * (len(archive.DATA_COLUMNS) + 6))
        conn.execute("BEGIN")
        conn.executemany(
            f"INSERT INTO History VALUES ({marks})",
            [
                (
                    record_id,
                    *[item["row"][name] for name in archive.DATA_COLUMNS],
                    carver.versions[record_id],
                    int(item["chain_ok"]),
                    int(item["header_ok"]),
                    item["columns"],
                    item["unreadable"],
                )
                for record_id, item in carver.best.items()
            ],
        )
        conn.execute("COMMIT")
        has_text = " OR ".join(f"(typeof({c}) = 'text' AND length(trim({c})) > 0)" for c in archive.TEXT_COLUMNS)
        rows, stamped, earliest, latest = conn.execute(
            "SELECT COUNT(*), COUNT(timestamp), substr(MIN(timestamp), 1, 10), substr(MAX(timestamp), 1, 10) FROM History"
        ).fetchone()
        with_text = conn.execute(f"SELECT COUNT(*) FROM History WHERE {has_text}").fetchone()[0]
        broken = conn.execute("SELECT COUNT(*) FROM History WHERE overflow_chain_ok = 0").fetchone()[0]
    finally:
        conn.close()
    return {
        "rows": rows,
        "with_text": with_text,
        "with_timestamp": stamped,
        "incomplete": broken,
        "earliest": earliest,
        "latest": latest,
    }


# --- the command ----------------------------------------------------------------------------------


def _ask(question: str) -> bool:
    """Anything but an explicit yes means no. So does having nobody there to answer."""
    try:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        print("(no answer, so no)", flush=True)
        return False


def _show(label: str, values: dict[str, Any]) -> None:
    print(label, " ".join(f"{k}={v}" for k, v in values.items()), flush=True)


def run(
    source: Path,
    work_dir: Path,
    import_answer: bool | None = None,
    delete_answer: bool | None = None,
    ask: Callable[[str], bool] = _ask,
) -> int:
    print(
        "Before recovering: switch off Wispr Flow's 24-hour auto-delete, and run this soon. "
        "New dictations overwrite old free space.",
        flush=True,
    )
    if not source.is_file():
        raise RecoveryError("The Wispr Flow database was not found")
    work_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    copy = work_dir / COPY_NAME
    output = work_dir / OUTPUT_NAME
    # Everything that can stop the run is checked before the slow copy starts.
    if output.exists():
        raise RecoveryError(f"{OUTPUT_NAME} already exists in that folder. Move it or pick another folder.")
    if copy.exists():
        raise RecoveryError(f"{COPY_NAME} already exists in that folder. Delete it or pick another folder.")

    free_space = inspect_free_space(source)
    _show("Free space:", free_space)
    if not free_space["sampled_with_data"]:
        print("Nothing to recover: the free pages hold no data. Nothing was copied.", flush=True)
        return 0
    _show("Disk space:", check_disk_space(work_dir, source.stat().st_size))

    print("Copying the database. The original is only read.", flush=True)
    make_copy(source, copy)
    print("Reading deleted records from the copy. This can take several minutes.", flush=True)
    try:
        result = carve(copy, output)
    except BaseException:
        output.unlink(missing_ok=True)
        raise
    _show("Recovered:", result)

    if result["rows"]:
        print(f"Saved to {output}", flush=True)
        wanted = ask("Import these into your archive?") if import_answer is None else import_answer
        if wanted:
            conn = archive.open_for_writing()
            try:
                _show("Imported:", archive.seed(conn, str(output)))
            finally:
                conn.close()
        else:
            print("Not imported. To do it later: bin/wispr-flow-archive seed --recovered <that file>", flush=True)
    else:
        print("No records could be read from the free space.", flush=True)

    remove = ask("Delete the temporary copy of your database?") if delete_answer is None else delete_answer
    if remove:
        copy.unlink()
        print("Temporary copy deleted.", flush=True)
    else:
        print(f"Temporary copy kept at {copy}. It holds your full database, including audio.", flush=True)
    return 0


def main(argv: Iterable[str] | None = None) -> int:
    refuse_network()
    parser = argparse.ArgumentParser(prog="recover", description="Recover deleted dictations from a copy of the database.")
    parser.add_argument("--work-dir", default=DEFAULT_WORK_DIR, help="where the copy and the result go")
    parser.add_argument("--source", help="database to read; defaults to Wispr Flow's own")
    importing = parser.add_mutually_exclusive_group()
    importing.add_argument("--import", dest="import_answer", action="store_const", const=True)
    importing.add_argument("--no-import", dest="import_answer", action="store_const", const=False)
    deleting = parser.add_mutually_exclusive_group()
    deleting.add_argument("--delete-copy", dest="delete_answer", action="store_const", const=True)
    deleting.add_argument("--keep-copy", dest="delete_answer", action="store_const", const=False)
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        # An empty path would otherwise mean "the current folder", which is where the copy would land and be deleted.
        if not args.work_dir.strip() or (args.source is not None and not args.source.strip()):
            raise RecoveryError("An empty folder or file path was given")
        source = Path(os.path.expanduser(args.source)) if args.source else Path(db.db_path())
        return run(source, Path(os.path.expanduser(args.work_dir)), args.import_answer, args.delete_answer)
    except RecoveryError as exc:
        print(f"Recovery stopped: {exc}", file=sys.stderr, flush=True)
    except Exception as exc:  # A traceback could quote values, so report the type only.
        print(f"Recovery failed: {type(exc).__name__}", file=sys.stderr, flush=True)
    return 1
