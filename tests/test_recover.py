"""Recovery against an invented database with deleted rows. No test touches a real Wispr Flow file."""

import hashlib
import os
import sqlite3
import uuid
from collections import namedtuple

import pytest

from wispr_flow_mcp import archive, recover

SENTINEL_BLOB = b"SENTINEL-BLOB-" * 40
SENTINEL_AX = "SENTINEL-AXTEXT-VALUE"
SENTINEL_HTML = "SENTINEL-AXHTML-VALUE"
SENTINEL_BOX = "SENTINEL-TEXTBOX-VALUE"
FORBIDDEN = [b"SENTINEL-BLOB", SENTINEL_AX.encode(), SENTINEL_HTML.encode(), SENTINEL_BOX.encode()]

# Columns in a different order from any real version, plus one this tool has never heard of, to prove the order is
# read from the file's own schema.
SCHEMA = """CREATE TABLE History (
  transcriptEntityId VARCHAR(36) PRIMARY KEY, timestamp DATETIME, audio BLOB, asrText TEXT, screenshot BLOB,
  formattedText TEXT, editedText TEXT, textboxContents TEXT, pastedText TEXT, axText TEXT, axHTML TEXT, app VARCHAR(255),
  url VARCHAR(255), numWords INTEGER, duration FLOAT, isArchived TINYINT(1), status VARCHAR(255),
  conversationId VARCHAR(255), timezoneOffsetMinutes INTEGER, serverFinalizedText TEXT, editedTextUnbounded TEXT,
  someFutureColumn TEXT)"""

Rows = namedtuple("Rows", "path deleted kept")


def build(path, page_size=4096, secure_delete="OFF", rows=300):
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA page_size={page_size}")
    conn.execute(f"PRAGMA secure_delete={secure_delete}")
    conn.execute(SCHEMA)
    deleted, kept = {}, []
    for number in range(rows):
        rid = str(uuid.UUID(int=number + 1))
        # Every tenth row is long enough to spill onto overflow pages.
        text = f"invented dictation {number} " + ("padding words here " * 900 if number % 10 == 0 else "short")
        conn.execute(
            "INSERT INTO History VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rid, f"2026-03-{number % 28 + 1:02d} 12:00:00.000 +00:00", SENTINEL_BLOB, text, SENTINEL_BLOB, None, None,
             SENTINEL_BOX, text, SENTINEL_AX, SENTINEL_HTML, "com.example.App", "https://example.test", 5, 1.5, 0,
             "formatted", "conv", 300, None, None, "future value"),
        )
        # One unbroken run, as an auto-delete of the oldest rows would be. Scattered deletes make SQLite rebalance,
        # which reuses freed pages and really does overwrite some of them.
        if number >= rows // 2:
            kept.append(rid)
        else:
            deleted[rid] = text
    conn.commit()
    conn.executemany("DELETE FROM History WHERE transcriptEntityId = ?", [(r,) for r in deleted])
    conn.commit()
    conn.close()
    return Rows(path, deleted, kept)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_output(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    rows = {r["transcriptEntityId"]: r for r in conn.execute("SELECT * FROM History")}
    columns = {r[1] for r in conn.execute("PRAGMA table_info(History)")}
    conn.close()
    return rows, columns


@pytest.fixture
def invented(tmp_path):
    return build(tmp_path / "flow.sqlite")


@pytest.fixture
def carved(invented, tmp_path):
    copy = tmp_path / "copy.sqlite"
    copy.write_bytes(invented.path.read_bytes())
    output = tmp_path / "out.sqlite"
    return recover.carve(copy, output), output, invented


def answers(*values):
    remaining = list(values)
    return lambda _question: remaining.pop(0)


class TestCarving:
    def test_finds_deleted_rows(self, carved):
        _, output, invented = carved
        rows, _ = read_output(output)
        found = set(rows) & set(invented.deleted)
        assert len(found) >= 0.95 * len(invented.deleted)

    def test_deleted_short_text_comes_back_exact(self, carved):
        _, output, invented = carved
        rows, _ = read_output(output)
        short = [r for r in invented.deleted if r in rows and len(invented.deleted[r]) < 200]
        assert len(short) > 100
        assert all(rows[r]["pastedText"] == invented.deleted[r] for r in short)

    def test_deleted_long_text_is_exact_whenever_its_chain_is_intact(self, carved):
        _, output, invented = carved
        rows, _ = read_output(output)
        long_rows = [r for r in invented.deleted if r in rows and len(invented.deleted[r]) >= 200]
        intact = [r for r in long_rows if rows[r]["overflow_chain_ok"]]
        assert intact, "the fixture must exercise overflow pages"
        assert all(rows[r]["pastedText"] == invented.deleted[r] for r in intact)

    def make_carver(self, data, usable=4096):
        layout = {"page_size": 4096, "usable": usable, "first_trunk": 0, "free_pages": 0}
        return recover._Carver(data, ["transcriptEntityId", "timestamp"], layout)

    def test_a_value_running_off_its_page_is_unreadable_not_short(self):
        carver = self.make_carver(bytes(4096))
        page = b"x" * 4096
        assert carver._read_record_bytes(page, 4000, 500, [], 0, 200) is None  # Only 96 bytes remain on the page.
        assert carver._read_record_bytes(page, 3000, 500, [], 0, 200) == b"x" * 200  # Control.

    def test_an_overflow_chain_landing_on_a_free_list_trunk_is_not_intact(self):
        # Two pages whose "next page" pointers are both 0, so both end a one-page chain.
        carver = self.make_carver(bytes(8192))
        assert carver._chain(2, 1) == ([2], True)  # Control: the same page, before it is a trunk.
        carver.trunks = {2}
        assert carver._chain(2, 1) == ([], False)

    def test_free_list_trunks_are_found_in_a_real_file(self, invented):
        with open(invented.path, "rb") as handle:
            layout = recover._read_layout(handle)
            handle.seek(0)
            data = handle.read()
        assert layout["first_trunk"] in recover._free_list_trunks(data, layout)

    def test_a_record_with_unreadable_columns_imports_as_possibly_damaged(self, tmp_path, monkeypatch):
        invented = build(tmp_path / "flow.sqlite", page_size=65536)
        output = tmp_path / "out.sqlite"
        recover.carve(invented.path, output)
        rows, _ = read_output(output)
        marked = [r for r, v in rows.items() if v["unreadable_columns"] and v["overflow_chain_ok"]]
        assert marked, "the fixture must produce one"
        conn = archive.open_for_writing()
        archive.seed(conn, str(output))
        flags = {r[0] for r in conn.execute("SELECT transcriptEntityId FROM History WHERE possibly_damaged = 1")}
        conn.close()
        assert set(marked) <= flags

    def test_metadata_lines_up_with_the_files_own_column_order(self, carved):
        _, output, invented = carved
        rows, _ = read_output(output)
        row = rows[next(r for r in invented.deleted if r in rows and len(invented.deleted[r]) < 200)]
        assert row["app"] == "com.example.App"
        assert row["status"] == "formatted"
        assert row["numWords"] == 5
        assert row["duration"] == 1.5
        assert row["timestamp"].startswith("2026-03-")

    def test_forbidden_columns_are_not_in_the_output_schema(self, carved):
        _, output, _ = carved
        _, columns = read_output(output)
        assert not columns & {"audio", "screenshot", "builtInAudio", "axText", "axHTML", "textboxContents"}

    def test_forbidden_values_are_nowhere_in_the_output_bytes(self, carved):
        _, output, _ = carved
        raw = output.read_bytes()
        assert not [s for s in FORBIDDEN if s in raw]

    def test_the_fixture_really_holds_those_values_in_free_space(self, invented):
        raw = invented.path.read_bytes()
        assert all(s in raw for s in FORBIDDEN)

    def test_reports_counts(self, carved):
        result, output, _ = carved
        rows, _ = read_output(output)
        assert result["rows"] == len(rows)
        assert result["with_text"] == sum(1 for r in rows.values() if r["pastedText"] or r["asrText"])
        assert result["earliest"] == "2026-03-01"

    @pytest.mark.parametrize("page_size", [1024, 8192, 65536])
    def test_works_at_other_page_sizes(self, tmp_path, page_size):
        invented = build(tmp_path / "flow.sqlite", page_size=page_size)
        copy = tmp_path / "copy.sqlite"
        copy.write_bytes(invented.path.read_bytes())
        output = tmp_path / "out.sqlite"
        recover.carve(copy, output)
        rows, _ = read_output(output)
        short = [r for r in invented.deleted if r in rows and len(invented.deleted[r]) < 200]
        assert len(short) > 100
        assert all(rows[r]["pastedText"] == invented.deleted[r] for r in short)

    def test_will_not_overwrite_an_earlier_result(self, carved, tmp_path):
        _, output, _ = carved
        before = digest(output)
        with pytest.raises(recover.RecoveryError, match="already exists"):
            recover.carve(tmp_path / "copy.sqlite", output)
        assert digest(output) == before

    def test_unrecognised_table_shape_raises(self, tmp_path):
        path = tmp_path / "other.sqlite"
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE History (something TEXT)")
        conn.commit()
        conn.close()
        with pytest.raises(recover.RecoveryError, match="understands"):
            recover.carve(path, tmp_path / "out.sqlite")


class TestChecksBeforeCopying:
    def test_counts_free_pages_that_still_hold_data(self, invented):
        report = recover.inspect_free_space(invented.path)
        assert report["free_pages"] > 0
        assert report["sampled_with_data"] > 0

    def test_zeroed_free_pages_report_no_data(self, tmp_path):
        wiped = build(tmp_path / "flow.sqlite", secure_delete="ON")
        report = recover.inspect_free_space(wiped.path)
        assert report["free_pages"] > 0
        assert report["free_pages_sampled"] > 0
        assert report["sampled_with_data"] == 0

    def test_zeroed_free_pages_stop_before_any_copy(self, tmp_path, capsys):
        wiped = build(tmp_path / "flow.sqlite", secure_delete="ON")
        work = tmp_path / "work"
        assert recover.run(wiped.path, work, ask=answers()) == 0
        assert "Nothing to recover" in capsys.readouterr().out
        assert list(work.iterdir()) == []

    def test_not_a_database_is_refused(self, tmp_path):
        path = tmp_path / "x.sqlite"
        path.write_bytes(b"not sqlite" * 50)
        with pytest.raises(recover.RecoveryError, match="not a SQLite"):
            recover.inspect_free_space(path)

    def test_low_disk_space_stops_before_any_copy(self, invented, tmp_path, monkeypatch):
        usage = namedtuple("usage", "total used free")
        monkeypatch.setattr(recover.shutil, "disk_usage", lambda _p: usage(10, 9, 1))
        work = tmp_path / "work"
        with pytest.raises(recover.RecoveryError, match="Not enough disk space"):
            recover.run(invented.path, work, ask=answers())
        assert list(work.iterdir()) == []

    def test_space_check_counts_the_database_size_and_a_margin(self, tmp_path, monkeypatch):
        usage = namedtuple("usage", "total used free")
        margin = recover.SPACE_MARGIN_BYTES
        monkeypatch.setattr(recover.shutil, "disk_usage", lambda _p: usage(0, 0, 100 + margin))
        recover.check_disk_space(tmp_path, 100)
        with pytest.raises(recover.RecoveryError):
            recover.check_disk_space(tmp_path, 101)

    def test_earlier_result_stops_before_any_copy(self, invented, tmp_path):
        work = tmp_path / "work"
        work.mkdir()
        (work / recover.OUTPUT_NAME).write_text("earlier")
        with pytest.raises(recover.RecoveryError, match="already exists"):
            recover.run(invented.path, work, ask=answers())
        assert sorted(p.name for p in work.iterdir()) == [recover.OUTPUT_NAME]

    def test_leftover_copy_stops_the_run(self, invented, tmp_path):
        work = tmp_path / "work"
        work.mkdir()
        (work / recover.COPY_NAME).write_text("leftover")
        with pytest.raises(recover.RecoveryError, match="already exists"):
            recover.run(invented.path, work, ask=answers())
        assert (work / recover.COPY_NAME).read_text() == "leftover"


class TestTheWholeRun:
    def test_the_original_is_never_changed(self, invented, tmp_path):
        before, stat = digest(invented.path), os.stat(invented.path)
        recover.run(invented.path, tmp_path / "work", ask=answers(True, True))
        assert digest(invented.path) == before
        assert os.stat(invented.path).st_mtime_ns == stat.st_mtime_ns

    def test_no_sidecar_files_appear_beside_the_original(self, invented, tmp_path):
        recover.run(invented.path, tmp_path / "work", ask=answers(False, True))
        assert [p.name for p in invented.path.parent.iterdir() if p.name.startswith("flow.sqlite")] == ["flow.sqlite"]

    def test_carving_reads_the_copy_not_the_original(self, invented, tmp_path, monkeypatch):
        seen = []
        real = recover.carve
        monkeypatch.setattr(recover, "carve", lambda copy, output: (seen.append(copy), real(copy, output))[1])
        work = tmp_path / "work"
        recover.run(invented.path, work, ask=answers(False, False))
        assert seen == [work / recover.COPY_NAME]

    def test_yes_to_both_imports_then_deletes_the_copy(self, invented, tmp_path, capsys):
        work = tmp_path / "work"
        assert recover.run(invented.path, work, ask=answers(True, True)) == 0
        assert sorted(p.name for p in work.iterdir()) == [recover.OUTPUT_NAME]
        conn = sqlite3.connect(os.environ["WISPR_FLOW_ARCHIVE"])
        held = conn.execute("SELECT COUNT(*), MIN(source), MAX(source) FROM History").fetchone()
        conn.close()
        rows, _ = read_output(work / recover.OUTPUT_NAME)
        assert held == (len(rows), "recovered", "recovered")
        assert f"added={len(rows)}" in capsys.readouterr().out

    def test_no_to_both_leaves_the_archive_alone_and_keeps_the_copy(self, invented, tmp_path, capsys):
        work = tmp_path / "work"
        assert recover.run(invented.path, work, ask=answers(False, False)) == 0
        assert not os.path.exists(os.environ["WISPR_FLOW_ARCHIVE"])
        assert sorted(p.name for p in work.iterdir()) == [recover.COPY_NAME, recover.OUTPUT_NAME]
        assert "kept" in capsys.readouterr().out

    def test_the_questions_are_asked_only_when_no_flag_answers_them(self, invented, tmp_path):
        work = tmp_path / "work"
        assert recover.run(invented.path, work, True, True, ask=answers()) == 0
        assert sorted(p.name for p in work.iterdir()) == [recover.OUTPUT_NAME]

    def test_only_a_plain_yes_counts(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda _q: "sure")
        assert recover._ask("?") is False
        monkeypatch.setattr("builtins.input", lambda _q: "YES ")
        assert recover._ask("?") is True

    def test_nobody_there_to_answer_means_no(self, monkeypatch):
        def closed(_q):
            raise EOFError
        monkeypatch.setattr("builtins.input", closed)
        assert recover._ask("?") is False

    def test_no_stray_files_are_left_in_the_work_folder(self, invented, tmp_path):
        work = tmp_path / "work"
        recover.run(invented.path, work, ask=answers(False, True))
        assert sorted(p.name for p in work.iterdir()) == [recover.OUTPUT_NAME]

    def test_output_holds_no_forbidden_bytes_after_a_full_run(self, invented, tmp_path):
        work = tmp_path / "work"
        recover.run(invented.path, work, ask=answers(False, True))
        raw = (work / recover.OUTPUT_NAME).read_bytes()
        assert not [s for s in FORBIDDEN if s in raw]

    def test_results_are_private_to_this_account(self, invented, tmp_path):
        work = tmp_path / "work"
        recover.run(invented.path, work, ask=answers(False, False))
        assert os.stat(work).st_mode & 0o077 == 0
        for path in work.iterdir():
            assert os.stat(path).st_mode & 0o077 == 0

    def test_prints_no_dictation_text(self, invented, tmp_path, capsys):
        recover.run(invented.path, tmp_path / "work", ask=answers(True, True))
        printed = capsys.readouterr()
        assert "invented dictation" not in printed.out + printed.err

    def test_warns_about_auto_delete_first(self, invented, tmp_path, capsys):
        recover.run(invented.path, tmp_path / "work", ask=answers(False, True))
        assert capsys.readouterr().out.splitlines()[0].startswith("Before recovering: switch off")

    def test_a_failed_copy_leaves_nothing_behind(self, invented, tmp_path, monkeypatch):
        def broken(_src, dst):
            dst.write_bytes(b"partial")
            raise OSError("disk full")
        monkeypatch.setattr(recover.shutil, "copyfile", broken)
        target = tmp_path / "copy.sqlite"
        with pytest.raises(OSError):
            recover.make_copy(invented.path, target)
        assert not target.exists()

    def test_command_line_failure_prints_no_row_values(self, invented, tmp_path, monkeypatch, capsys):
        def boom(_copy, _output):
            raise sqlite3.IntegrityError("invented dictation")
        monkeypatch.setattr(recover, "carve", boom)
        code = recover.main(["--source", str(invented.path), "--work-dir", str(tmp_path / "w"), "--no-import", "--delete-copy"])
        assert code == 1
        assert "invented dictation" not in capsys.readouterr().err

    def test_command_line_runs_end_to_end(self, invented, tmp_path):
        work = tmp_path / "w"
        code = recover.main(["--source", str(invented.path), "--work-dir", str(work), "--import", "--delete-copy"])
        assert code == 0
        assert sorted(p.name for p in work.iterdir()) == [recover.OUTPUT_NAME]


def test_recover_command_refuses_network(tmp_path):
    import subprocess
    import sys
    code = (
        "import socket\n"
        "from wispr_flow_mcp import recover\n"
        "recover.main(['--source', 'gone', '--work-dir', %r])\n"
        "try:\n"
        "    socket.socket().connect(('127.0.0.1', 9))\n"
        "except OSError as e:\n"
        "    print('refused' if 'local only' in str(e) else 'other')\n"
    ) % str(tmp_path / "w")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True).stdout
    assert out.strip().splitlines()[-1] == "refused"
