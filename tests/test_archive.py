"""Archive behaviour on invented data, with real SQLite files."""

import hashlib
import os
import sqlite3

import pytest

from conftest import history
from wispr_flow_mcp import archive, db, tools

FORBIDDEN = {"audio", "screenshot", "builtInAudio", "axText", "axHTML", "textboxContents"}


def make_recovered(path, damaged=2, clean=3, empty=1):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE History (transcriptEntityId TEXT PRIMARY KEY, "
        + ",".join(archive.DATA_COLUMNS)
        + ", overflow_chain_ok INT, unreadable_columns TEXT)"
    )
    def add(rid, ok, text, bad=""):
        conn.execute(
            "INSERT INTO History (transcriptEntityId, pastedText, asrText, timestamp, numWords, overflow_chain_ok, "
            "unreadable_columns) VALUES (?,?,?,?,5,?,?)",
            (rid, text, text, "2025-09-05 17:00:00.000 +00:00", ok, bad),
        )
    for i in range(clean):
        add(f"clean-{i}", 1, f"recovered clean {i}")
    for i in range(damaged):
        add(f"damaged-{i}", 0, f"recovered damaged {i}", "pastedText")
    for i in range(empty):
        add(f"empty-{i}", 1, None)
    conn.commit()
    conn.close()
    return str(path)


@pytest.fixture
def seeded(fixture_db, tmp_path):
    conn = archive.open_for_writing()
    counts = archive.seed(conn, make_recovered(tmp_path / "recovered.sqlite"))
    yield conn, counts
    conn.close()


def row(conn, rid):
    conn.row_factory = sqlite3.Row
    found = conn.execute("SELECT * FROM History WHERE transcriptEntityId = ?", (rid,)).fetchone()
    conn.row_factory = None
    return found


def count(conn):
    return conn.execute("SELECT COUNT(*) FROM History").fetchone()[0]


class TestSeed:
    def test_counts_and_source(self, seeded):
        conn, counts = seeded
        assert counts == {"recovered_rows_read": 6, "added": 6}
        assert conn.execute("SELECT COUNT(*) FROM History WHERE source = 'recovered'").fetchone()[0] == 6

    def test_flags_exactly_the_rows_with_broken_chains(self, seeded):
        conn, _ = seeded
        flagged = {r[0] for r in conn.execute("SELECT transcriptEntityId FROM History WHERE possibly_damaged = 1")}
        assert flagged == {"damaged-0", "damaged-1"}
        assert row(conn, "damaged-0")["unreadable_columns"] == "pastedText"

    def test_keeps_rows_with_no_text(self, seeded):
        assert row(seeded[0], "empty-0") is not None

    def test_reseeding_adds_nothing_and_cannot_undo_a_sync(self, seeded, tmp_path):
        conn, _ = seeded
        live = sqlite3.connect(db.db_path())
        history(live, "clean-0", "2025-09-05 17:00:00.000 +00:00", pasted="the live text")
        live.commit(); live.close()
        archive.sync(conn)
        again = archive.seed(conn, str(tmp_path / "recovered.sqlite"))
        assert again["added"] == 0
        assert row(conn, "clean-0")["pastedText"] == "the live text"

    def test_schema_has_no_forbidden_column(self, seeded):
        names = {r[1] for r in seeded[0].execute("PRAGMA table_info(History)")}
        assert not names & FORBIDDEN

    def test_archive_file_is_private(self, seeded):
        assert oct(os.stat(db.archive_path()).st_mode)[-3:] == "600"


class TestMisreadRecoveredRow:
    @pytest.fixture
    def misread(self, seeded, tmp_path):
        conn, _ = seeded
        path = tmp_path / "misread.sqlite"
        make_recovered(path, damaged=0, clean=0, empty=0)
        extra = sqlite3.connect(path)
        extra.execute(
            "INSERT INTO History (transcriptEntityId, pastedText, numWords, duration, isArchived, overflow_chain_ok, "
            "unreadable_columns) VALUES ('misread', 'quokka misread', 'not a count', 'not a time', 'not a flag', 1, '')"
        )
        extra.commit(); extra.close()
        archive.seed(conn, str(path))
        return conn

    def test_is_flagged_even_though_its_chain_looks_fine(self, misread):
        assert row(misread, "misread")["possibly_damaged"] == 1

    def test_does_not_break_stats(self, misread):
        assert isinstance(tools.dictation_stats(group_by="day")["total_words"], int)

    def test_word_count_is_computed_and_duration_is_empty_not_text(self, misread):
        assert tools.search_dictations("quokka")["results"][0]["word_count"] == 2
        opened = tools.get_dictation("misread")
        assert opened["word_count"] == 2
        assert opened["duration_seconds"] is None


class TestSync:
    def test_picks_up_a_new_row(self, seeded):
        conn, _ = seeded
        result = archive.sync(conn)
        assert result["added"] == 8
        assert row(conn, "newest")["source"] == "live"
        assert row(conn, "newest")["pastedText"] == "alpha pasted wins "

    def test_second_sync_changes_nothing(self, seeded):
        conn, _ = seeded
        archive.sync(conn)
        again = archive.sync(conn)
        assert (again["added"], again["changed"]) == (0, 0)

    def test_picks_up_a_changed_row(self, seeded):
        conn, _ = seeded
        archive.sync(conn)
        live = sqlite3.connect(db.db_path())
        live.execute("UPDATE History SET editedText = 'edited later' WHERE transcriptEntityId = 'newest'")
        live.commit(); live.close()
        assert archive.sync(conn)["changed"] == 1
        assert row(conn, "newest")["editedText"] == "edited later"

    def test_never_deletes_after_wispr_deletes(self, seeded):
        conn, _ = seeded
        archive.sync(conn)
        before = count(conn)
        live = sqlite3.connect(db.db_path())
        live.execute("DELETE FROM History")
        live.commit(); live.close()
        archive.sync(conn)
        assert count(conn) == before
        assert row(conn, "newest")["pastedText"] == "alpha pasted wins "

    def test_live_version_wins_over_recovered_and_clears_the_damage_flag(self, seeded):
        conn, _ = seeded
        live = sqlite3.connect(db.db_path())
        history(live, "damaged-0", "2025-09-05 17:00:00.000 +00:00", pasted="the live text")
        live.commit(); live.close()
        assert archive.sync(conn)["changed"] == 1
        found = row(conn, "damaged-0")
        assert (found["pastedText"], found["source"], found["possibly_damaged"]) == ("the live text", "live", 0)
        assert found["unreadable_columns"] is None

    def test_blank_live_value_does_not_erase_held_text(self, seeded):
        conn, _ = seeded
        archive.sync(conn)
        live = sqlite3.connect(db.db_path())
        live.execute("UPDATE History SET pastedText = '  ', editedText = 'new edit' WHERE transcriptEntityId = 'newest'")
        live.commit(); live.close()
        archive.sync(conn)
        found = row(conn, "newest")
        assert found["pastedText"] == "alpha pasted wins "
        assert found["editedText"] == "new edit"

    def test_row_with_no_id_is_counted_not_stored(self, seeded):
        conn, _ = seeded
        live = sqlite3.connect(db.db_path())
        history(live, "", None, pasted="no id")
        live.commit(); live.close()
        assert archive.sync(conn)["live_rows_skipped_no_id"] == 1
        assert row(conn, "") is None

    def test_covers_more_rows_than_one_batch(self, seeded, monkeypatch):
        conn, _ = seeded
        monkeypatch.setattr(archive, "SYNC_BATCH_ROWS", 2)
        assert archive.sync(conn)["added"] == 8

    def test_never_changes_the_live_file_bytes(self, seeded):
        conn, _ = seeded
        path = db.db_path()
        before = hashlib.sha256(open(path, "rb").read()).hexdigest()
        archive.sync(conn)
        archive.sync(conn)
        assert hashlib.sha256(open(path, "rb").read()).hexdigest() == before

    def test_source_has_no_delete_statement(self):
        import inspect
        assert "DELETE" not in inspect.getsource(archive.sync).replace("Contains no DELETE", "")
        assert "DELETE FROM" not in inspect.getsource(archive)


class TestMergedTools:
    def ids(self, result):
        return [r["id"] for r in result["results"]]

    def test_search_reaches_recovered_rows_and_says_so(self, seeded):
        found = tools.search_dictations("recovered clean 1")
        assert found["results"][0]["source"] == "recovered"
        assert found["results"][0]["possibly_damaged"] is False

    def test_damaged_rows_are_marked(self, seeded):
        assert tools.search_dictations("recovered damaged 0")["results"][0]["possibly_damaged"] is True

    def test_row_not_yet_synced_is_still_searchable(self, seeded):
        assert self.ids(tools.search_dictations("alpha pasted")) == ["newest"]

    def test_row_in_both_appears_once_with_the_live_version(self, seeded):
        conn, _ = seeded
        archive.sync(conn)
        live = sqlite3.connect(db.db_path())
        live.execute("UPDATE History SET pastedText = 'alpha changed' WHERE transcriptEntityId = 'newest'")
        live.commit(); live.close()
        results = tools.search_dictations("alpha")["results"]
        assert [r["id"] for r in results] == ["newest"]
        assert results[0]["text"] == "alpha changed"

    def test_rows_with_no_text_stay_out_of_search_and_recent(self, seeded):
        assert "empty-0" not in self.ids(tools.recent_dictations(limit=100))
        assert "empty-0" not in self.ids(tools.search_dictations("e", limit=100))

    def test_rows_with_no_text_can_still_be_opened_and_are_counted(self, seeded):
        assert tools.get_dictation("empty-0")["source"] == "recovered"
        assert tools.dictation_stats()["dictations_without_text"] == 1

    def test_raw_only_row_is_found(self, seeded):
        conn, _ = seeded
        conn.execute(
            "INSERT INTO History (transcriptEntityId, asrText, source, first_archived_at, last_changed_at) "
            "VALUES ('raw-only', 'quokka raw', 'recovered', 'x', 'x')"
        )
        assert tools.search_dictations("quokka")["results"][0]["text_source"] == "asrText"

    def test_rows_deleted_by_wispr_remain_searchable(self, seeded):
        conn, _ = seeded
        archive.sync(conn)
        live = sqlite3.connect(db.db_path())
        live.execute("DELETE FROM History")
        live.commit(); live.close()
        assert self.ids(tools.search_dictations("alpha")) == ["newest"]

    def test_stats_show_the_full_range_without_double_counting(self, seeded):
        conn, _ = seeded
        archive.sync(conn)
        stats = tools.dictation_stats(group_by="app")
        window = stats["history_window"]
        assert window["earliest_local"] == "2025-09-05T13:00:00-04:00"
        assert window["total_dictations"] == 14
        assert stats["recovered_dictations"] == 6
        assert stats["possibly_damaged_dictations"] == 2

    def test_search_past_the_limit_reports_more(self, seeded):
        result = tools.recent_dictations(limit=3)
        assert result["count"] == 3
        assert result["has_more"] is True

    def test_missing_live_file_warns_and_still_searches_the_archive(self, seeded, monkeypatch, tmp_path):
        monkeypatch.setenv("WISPR_FLOW_DB", str(tmp_path / "gone.sqlite"))
        result = tools.search_dictations("recovered clean 1")
        assert result["count"] == 1
        assert "live Wispr file" in result["warnings"][0]

    def test_missing_archive_warns_and_still_searches_live(self, fixture_db):
        result = tools.search_dictations("alpha")
        assert result["count"] == 1
        assert "archive could not be read" in result["warnings"][0]

    def test_archive_reader_cannot_write(self, seeded):
        with pytest.raises(sqlite3.DatabaseError, match="prohibited|not authorized"):
            with db.connect(db.ARCHIVE) as conn:
                conn.execute("DELETE FROM History")
        with pytest.raises(db.NotASelect):
            db.query("DELETE FROM History", source=db.ARCHIVE)


class TestBackup:
    def test_produces_a_valid_self_contained_copy(self, seeded, tmp_path):
        conn, _ = seeded
        root = tmp_path / "synced"; root.mkdir()
        result = archive.backup(str(root))
        copy = root / archive.BACKUP_FOLDER / result["name"]
        check = sqlite3.connect(copy)
        assert check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert check.execute("SELECT COUNT(*) FROM History").fetchone()[0] == count(conn)
        assert check.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        check.close()
        assert sorted(p.name for p in copy.parent.iterdir()) == [result["name"]]

    def test_captures_committed_rows_during_an_open_write_transaction(self, seeded, tmp_path):
        conn, _ = seeded
        root = tmp_path / "synced"; root.mkdir()
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE History SET status = 'uncommitted'")
        result = archive.backup(str(root))
        conn.execute("ROLLBACK")
        check = sqlite3.connect(root / archive.BACKUP_FOLDER / result["name"])
        assert check.execute("SELECT COUNT(*) FROM History WHERE status = 'uncommitted'").fetchone()[0] == 0
        check.close()

    def test_keeps_the_newest_seven_and_only_prunes_its_own_files(self, seeded, tmp_path):
        import datetime as dt
        root = tmp_path / "synced"; root.mkdir()
        folder = root / archive.BACKUP_FOLDER; folder.mkdir()
        (folder / "my-notes.txt").write_text("keep me")
        for day in range(1, 11):
            archive.backup(str(root), today=dt.date(2026, 11, day))
        names = sorted(p.name for p in folder.iterdir() if p.name.startswith("wispr-archive"))
        assert names == [f"wispr-archive-2026-11-{d:02d}.sqlite" for d in range(4, 11)]
        assert (folder / "my-notes.txt").exists()

    def test_same_day_rerun_replaces_the_copy(self, seeded, tmp_path):
        import datetime as dt
        root = tmp_path / "synced"; root.mkdir()
        archive.backup(str(root), today=dt.date(2026, 11, 2))
        archive.backup(str(root), today=dt.date(2026, 11, 2))
        assert len(list((root / archive.BACKUP_FOLDER).iterdir())) == 1

    def test_missing_backup_folder_raises_and_creates_nothing(self, seeded, tmp_path):
        root = tmp_path / "not-signed-in"
        with pytest.raises(FileNotFoundError, match="backup folder"):
            archive.backup(str(root))
        assert not root.exists()

    def test_failed_verification_keeps_older_copies_and_leaves_no_partial(self, seeded, tmp_path, monkeypatch):
        import datetime as dt
        root = tmp_path / "synced"; root.mkdir()
        archive.backup(str(root), today=dt.date(2026, 11, 1))
        def fail(*_):
            raise RuntimeError("Backup copy failed its integrity check")
        monkeypatch.setattr(archive, "_verify_copy", fail)
        with pytest.raises(RuntimeError):
            archive.backup(str(root), today=dt.date(2026, 11, 2))
        assert [p.name for p in (root / archive.BACKUP_FOLDER).iterdir()] == ["wispr-archive-2026-11-01.sqlite"]


class TestOutput:
    def test_command_line_reports_counts_and_no_text(self, seeded, capsys, monkeypatch, tmp_path):
        assert archive.main(["sync"]) == 0
        printed = capsys.readouterr()
        assert "added=8" in printed.out
        assert "alpha" not in printed.out + printed.err

    def test_failure_prints_no_row_values(self, fixture_db, capsys, monkeypatch):
        def boom(_conn):
            raise sqlite3.IntegrityError("alpha pasted wins")
        monkeypatch.setattr(archive, "sync", boom)
        assert archive.main(["sync"]) == 1
        assert "alpha" not in capsys.readouterr().err


def test_archive_command_refuses_network_before_doing_anything(tmp_path):
    import subprocess
    import sys
    code = (
        "import socket\n"
        "from wispr_flow_mcp import archive\n"
        "archive.main(['sync'])\n"
        "try:\n"
        "    socket.socket().connect(('127.0.0.1', 9))\n"
        "except OSError as e:\n"
        "    print('refused' if 'local only' in str(e) else 'other')\n"
    )
    env = {**os.environ, "WISPR_FLOW_DB": str(tmp_path / "gone"), "WISPR_FLOW_ARCHIVE": str(tmp_path / "a.sqlite")}
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env).stdout
    assert out.strip() == "refused"
