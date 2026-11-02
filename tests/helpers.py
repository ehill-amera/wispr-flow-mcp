"""Shared by the fixture and live tests, so the fixture tests prove these detectors can fire."""

import os
import subprocess

from wispr_flow_mcp import db, tools


def run_every_tool():
    """Calls all eight tools. Returns how many results came back, never their contents."""
    recent = tools.recent_dictations(limit=5)
    calls = 1
    tools.search_dictations("a")
    tools.search_dictations("no-such-text-qzxv")
    tools.dictation_stats(group_by="day")
    tools.dictation_stats(group_by="app")
    tools.search_dictionary("a")
    tools.list_dictionary()
    calls += 6
    if recent["results"]:
        tools.get_dictation(recent["results"][0]["id"])
        calls += 1
    notes = tools.search_notes("a")
    calls += 1
    if notes["results"]:
        tools.get_note(notes["results"][0]["id"])
        calls += 1
    return calls


def record_authorizer(monkeypatch):
    """Wraps the real authorizer. Returns the (table, column) reads seen and any it denied."""
    import sqlite3

    real = db._authorizer
    reads, denied = set(), []

    def recording(action, arg1, arg2, dbname, trigger):
        verdict = real(action, arg1, arg2, dbname, trigger)
        if action == sqlite3.SQLITE_READ:
            reads.add((arg1, arg2))
        if verdict != sqlite3.SQLITE_OK:
            denied.append((action, arg1, arg2))
        return verdict

    monkeypatch.setattr(db, "_authorizer", recording)
    return reads, denied


def open_modes(path):
    """How this process holds each open copy of a file, per lsof: r read, w write, u both."""
    out = subprocess.run(
        ["/usr/sbin/lsof", "-nP", "-a", "-p", str(os.getpid()), "-Fan"], capture_output=True, text=True
    ).stdout
    modes, access = [], None
    for line in out.splitlines():
        if line.startswith("a"):
            access = line[1:].strip()
        elif line.startswith("n") and line[1:] == os.path.realpath(path):
            modes.append(access)
    return modes


def file_state(path):
    stat = os.stat(path)
    return stat.st_mtime_ns, stat.st_size


def changed_in_every_window(path, action, windows=5):
    """True only if the file changed during all windows, so one write by Wispr cannot cause a false alarm."""
    for _ in range(windows):
        before = file_state(path)
        action()
        if file_state(path) == before:
            return False
    return True
