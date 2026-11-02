"""Invented corrupted rows, as carving can produce. Nothing from a corrupted field may reach any tool's output."""

import sqlite3

import pytest

from wispr_flow_mcp import archive, tools

UNKNOWN = "(unknown app)"
TS = "2025-10-01 12:00:00.000 +00:00"
FRAGMENT = "SECRETFRAGMENT"
GOOD_TEXT = "quokka one two three"  # four words


def add(conn, rid, **fields):
    values = {"pastedText": GOOD_TEXT, "timestamp": TS, "numWords": 4, "app": "com.google.Chrome", **fields}
    names = list(values)
    conn.execute(
        f"INSERT INTO History (transcriptEntityId, {', '.join(names)}, source, possibly_damaged, first_archived_at, "
        f"last_changed_at) VALUES (?, {','.join('?' * len(names))}, 'recovered', 1, 'x', 'x')",
        [rid, *[values[n] for n in names]],
    )


@pytest.fixture
def conn(fixture_db):
    c = archive.open_for_writing()
    yield c
    c.close()


BAD_APPS = {
    "words": f"the quick {FRAGMENT} went",
    "no-dot": FRAGMENT,
    "control": f"com.\x01\x02{FRAGMENT}",
    "binary": f"�\x00{FRAGMENT}.x",
    "newline": f"com.{FRAGMENT}\n",
    "leading-dot": f".{FRAGMENT}",
    "trailing-dot": f"{FRAGMENT}.",
    "one-letter-first": f"a.{FRAGMENT}",
    "underscore": f"com.{FRAGMENT}_x",
}


def in_recent(rid):
    return next(r for r in tools.recent_dictations(limit=100)["results"] if r["id"] == rid)


def every_output(rids):
    outputs = [
        tools.recent_dictations(limit=100),
        tools.search_dictations("quokka", limit=100),
        tools.search_dictations(FRAGMENT[:6], limit=100),
        tools.dictation_stats(group_by="app"),
        tools.dictation_stats(group_by="day"),
    ]
    outputs += [tools.get_dictation(r) for r in rids]
    return outputs


class TestApp:
    @pytest.mark.parametrize("name", BAD_APPS)
    def test_fragment_never_reaches_any_output(self, conn, name):
        add(conn, "r1", app=BAD_APPS[name])
        assert FRAGMENT not in repr(every_output(["r1"]))

    @pytest.mark.parametrize("name", BAD_APPS)
    def test_reported_as_unknown_in_every_tool(self, conn, name):
        add(conn, "r1", app=BAD_APPS[name])
        assert tools.search_dictations("quokka")["results"][0]["app"] == UNKNOWN
        assert in_recent("r1")["app"] == UNKNOWN
        assert tools.get_dictation("r1")["app"] == UNKNOWN
        assert UNKNOWN in [g["group"] for g in tools.dictation_stats(group_by="app")["groups"]]

    @pytest.mark.parametrize("value", [None, "", "   "])
    def test_blank_and_missing_are_unknown(self, conn, value):
        add(conn, "r1", app=value)
        assert tools.get_dictation("r1")["app"] == UNKNOWN

    @pytest.mark.parametrize("value", ["com.google.Chrome", "com.anthropic.claudefordesktop", "notion.id", "us.zoom.xos"])
    def test_real_bundle_ids_survive(self, conn, value):
        add(conn, "r1", app=value)
        assert tools.get_dictation("r1")["app"] == value
        assert value in [g["group"] for g in tools.dictation_stats(group_by="app")["groups"]]

    def test_archive_keeps_the_raw_value(self, conn):
        add(conn, "r1", app=BAD_APPS["words"])
        assert conn.execute("SELECT app FROM History WHERE transcriptEntityId = 'r1'").fetchone()[0] == BAD_APPS["words"]

    def test_all_corrupt_apps_share_one_group(self, conn):
        for i, value in enumerate(BAD_APPS.values()):
            add(conn, f"r{i}", app=value)
        groups = tools.dictation_stats(group_by="app")["groups"]
        unknown = [g for g in groups if g["group"] == UNKNOWN]
        assert len(unknown) == 1
        assert unknown[0]["dictations"] >= len(BAD_APPS)


class TestWordCount:
    @pytest.mark.parametrize("value", [-107819, -1, 29000, 3001, None, "not a count", 4.5])
    def test_invalid_count_is_computed_from_the_text(self, conn, value):
        add(conn, "r1", numWords=value, status="formatted")
        assert tools.search_dictations("quokka")["results"][0]["word_count"] == 4
        assert in_recent("r1")["word_count"] == 4
        assert tools.get_dictation("r1")["word_count"] == 4

    @pytest.mark.parametrize("value", [0, 1, 3000])
    def test_valid_count_is_kept_even_if_the_text_differs(self, conn, value):
        add(conn, "r1", numWords=value)
        assert tools.get_dictation("r1")["word_count"] == value

    def test_invalid_count_with_no_text_counts_as_zero(self, conn):
        add(conn, "r1", numWords=-5000, pastedText=None)
        assert tools.get_dictation("r1")["word_count"] == 0
        assert tools.dictation_stats(since="2025-10-01", until="2025-10-01")["total_words"] == 0

    def test_totals_use_the_corrected_counts(self, conn):
        add(conn, "a", numWords=-107819)
        add(conn, "b", numWords=29000)
        add(conn, "c", numWords=10)
        stats = tools.dictation_stats(since="2025-10-01", until="2025-10-01")
        assert stats["total_words"] == 4 + 4 + 10
        assert [g["words"] for g in stats["groups"]] == [18]

    def test_no_group_is_ever_negative(self, conn):
        add(conn, "a", numWords=-107819)
        assert all(g["words"] >= 0 for g in tools.dictation_stats(group_by="app")["groups"])


class TestOtherFields:
    @pytest.mark.parametrize(
        "value",
        [f"some {FRAGMENT} words", FRAGMENT, f"\x01{FRAGMENT}.com", f"{FRAGMENT}.com\n", f"www.{FRAGMENT}.com/a b"],
    )
    def test_bad_url_is_dropped_everywhere(self, conn, value):
        add(conn, "r1", url=value)
        assert tools.get_dictation("r1")["url"] is None
        assert tools.search_dictations("quokka")["results"][0]["url"] is None

    @pytest.mark.parametrize(
        "value", ["mail.google.com", "mail.google.com/mail/u/0", "localhost.dev:3000/x", "https://example.test/a?b=1"]
    )
    def test_real_urls_survive(self, conn, value):
        add(conn, "r1", url=value)
        assert tools.get_dictation("r1")["url"] == value

    @pytest.mark.parametrize("value", [FRAGMENT, "words", "formatted extra", f"{FRAGMENT}_x"])
    def test_status_outside_wisprs_list_is_dropped(self, conn, value):
        add(conn, "r1", status=value)
        assert tools.get_dictation("r1")["status"] is None

    @pytest.mark.parametrize("value", ["formatted", "dismissed", "extension_paste", "raw_transcript", "no_audio"])
    def test_real_status_survives(self, conn, value):
        add(conn, "r1", status=value)
        assert tools.get_dictation("r1")["status"] == value

    @pytest.mark.parametrize("value", [FRAGMENT[:6], f"two {FRAGMENT}", f"\x01{FRAGMENT}"])
    def test_bad_conversation_id_is_dropped(self, conn, value):
        add(conn, "r1", conversationId=value)
        assert tools.get_dictation("r1")["conversation_id"] is None

    def test_real_conversation_id_survives(self, conn):
        value = "0f8fad5b-d9cb-469f-a165-70867728950e"
        add(conn, "r1", conversationId=value)
        assert tools.get_dictation("r1")["conversation_id"] == value

    @pytest.mark.parametrize("value", [-1.0, 4.4e289, 86401.0, "not a time"])
    def test_impossible_duration_is_dropped(self, conn, value):
        add(conn, "r1", duration=value)
        assert tools.get_dictation("r1")["duration_seconds"] is None

    def test_real_duration_survives(self, conn):
        add(conn, "r1", duration=12.5)
        assert tools.get_dictation("r1")["duration_seconds"] == 12.5

    @pytest.mark.parametrize("value,expected", [(FRAGMENT, False), (0, False), (None, False), (1, True), (7, False)])
    def test_archived_flag_is_true_only_for_one(self, conn, value, expected):
        add(conn, "r1", isArchived=value)
        assert tools.get_dictation("r1", include_archived=True)["is_archived"] is expected

    def test_a_fully_corrupt_row_leaks_nothing_and_breaks_nothing(self, conn):
        add(conn, "r1", app=BAD_APPS["words"], url=f"some {FRAGMENT}", status=FRAGMENT, conversationId=f"x {FRAGMENT}",
            numWords=f"{FRAGMENT} count", duration=f"{FRAGMENT}", isArchived=f"{FRAGMENT}")
        assert FRAGMENT not in repr(every_output(["r1"]))
