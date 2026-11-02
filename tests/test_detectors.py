"""The live tests rely on these detectors, so prove on a fixture that each can catch the real problem."""

import sqlite3

from helpers import changed_in_every_window, open_modes, record_authorizer, run_every_tool
from wispr_flow_mcp import db


def test_open_mode_detector_sees_read_only_and_writable(fixture_db):
    ro = sqlite3.connect(db.open_uri(str(fixture_db)), uri=True)
    ro.execute("SELECT id FROM Notes").fetchall()
    assert set(open_modes(fixture_db)) == {"r"}
    ro.close()
    rw = sqlite3.connect(fixture_db)
    rw.execute("SELECT id FROM Notes").fetchall()
    assert "u" in open_modes(fixture_db)
    rw.close()


def test_change_detector_fires_on_a_write_to_the_main_file(fixture_db):
    def write_directly():
        with open(fixture_db, "ab") as f:
            f.write(b"\0")

    assert changed_in_every_window(fixture_db, write_directly, windows=2) is True


def test_change_detector_stays_quiet_for_every_tool(fixture_db):
    assert changed_in_every_window(fixture_db, run_every_tool) is False


def test_recorder_sees_a_denied_forbidden_column(fixture_db, monkeypatch):
    reads, denied = record_authorizer(monkeypatch)
    try:
        db.query("SELECT audio FROM History")
    except sqlite3.DatabaseError:
        pass
    assert ("History", "audio") in reads
    assert denied


def test_every_tool_reads_only_allowed_columns_on_the_fixture(fixture_db, monkeypatch):
    reads, denied = record_authorizer(monkeypatch)
    assert run_every_tool() == 10
    assert not denied
    assert ("History", "pastedText") in reads
