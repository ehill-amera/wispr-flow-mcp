import pytest

from conftest import history
from wispr_flow_mcp import tools


def ids(result):
    return [r["id"] for r in result["results"]]


class TestSearchColumnOrder:
    def test_matches_pasted_and_reports_it_as_source(self, fixture_db):
        result = tools.search_dictations("ALPHA")
        assert ids(result) == ["newest"]
        assert result["results"][0]["text"] == "alpha pasted wins "
        assert result["results"][0]["text_source"] == "pastedText"

    def test_lower_priority_column_is_not_searched_when_pasted_exists(self, fixture_db):
        assert ids(tools.search_dictations("zebra")) == []

    def test_blank_pasted_falls_back_to_edited_not_formatted(self, fixture_db):
        result = tools.search_dictations("bravo")
        assert result["results"][0]["text"] == "bravo edited"
        assert result["results"][0]["text_source"] == "editedText"

    def test_null_pasted_and_edited_fall_back_to_formatted(self, fixture_db):
        assert tools.search_dictations("charlie")["results"][0]["text_source"] == "formattedText"


class TestSearchMatching:
    def test_percent_is_literal_not_a_wildcard(self, fixture_db):
        assert ids(tools.search_dictations("100%")) == ["formatted-fallback"]
        assert ids(tools.search_dictations("%")) == ["formatted-fallback"]

    def test_case_insensitive_beyond_ascii(self, fixture_db):
        assert ids(tools.search_dictations("école")) == ["no-app"]

    def test_empty_query_raises(self, fixture_db):
        with pytest.raises(ValueError):
            tools.search_dictations("  ")

    def test_app_filter_is_case_insensitive_part_match_and_skips_missing_apps(self, fixture_db):
        assert ids(tools.search_dictations("delta", app="CHROME")) == ["no-time"]


class TestNullsAndOrdering:
    def test_recent_is_newest_first_with_null_timestamp_last(self, fixture_db):
        assert ids(tools.recent_dictations()) == [
            "newest", "edited-fallback", "formatted-fallback", "no-app", "blank-app", "late-evening", "no-time",
        ]

    def test_search_puts_null_timestamp_last(self, fixture_db):
        assert ids(tools.search_dictations("delta"))[-1] == "no-time"

    def test_null_row_fields_come_back_as_null(self, fixture_db):
        rows = {r["id"]: r for r in tools.recent_dictations()["results"]}
        assert rows["no-app"]["app"] == tools.UNKNOWN_APP
        assert rows["blank-app"]["app"] == tools.UNKNOWN_APP
        assert rows["blank-app"]["word_count"] == 3  # no stored count, so computed from "delta blank app"
        assert rows["no-time"]["local_time"] is None

    def test_get_dictation_with_null_timestamp(self, fixture_db):
        assert tools.get_dictation("no-time")["local_time"] is None


class TestHistoryWindow:
    EXPECTED = {
        "earliest_local": "2026-11-01T22:00:00-05:00",
        "latest_local": "2026-11-02T11:00:00-05:00",
    }

    def test_zero_result_search_returns_window_and_note(self, fixture_db):
        result = tools.search_dictations("nothing-matches-this")
        assert result["count"] == 0
        assert result["history_window"]["earliest_local"] == self.EXPECTED["earliest_local"]
        assert result["history_window"]["latest_local"] == "2026-11-02T11:00:00-05:00"
        assert result["history_window"]["dictations_without_timestamp"] == 1
        assert "2026-11-01T22:00:00-05:00" in result["note"]

    def test_zero_result_recent_returns_window(self, fixture_db):
        result = tools.recent_dictations(since="2030-01-01")
        assert result["count"] == 0
        assert result["history_window"]["total_dictations"] == 8

    def test_search_with_results_does_not_add_window(self, fixture_db):
        assert "history_window" not in tools.search_dictations("alpha")

    def test_stats_always_returns_window(self, fixture_db):
        assert tools.dictation_stats()["history_window"]["earliest_local"] == self.EXPECTED["earliest_local"]


class TestTimeBounds:
    def test_output_is_in_the_configured_timezone(self, fixture_db):
        row = tools.search_dictations("echo")["results"][0]
        assert row["local_time"] == "2026-11-01T22:00:00-05:00"

    def test_bare_since_date_starts_at_local_midnight(self, fixture_db):
        assert "late-evening" not in ids(tools.recent_dictations(since="2026-11-02", limit=100))

    def test_bare_until_date_covers_the_whole_local_day(self, fixture_db):
        assert ids(tools.search_dictations("echo", until="2026-11-01")) == ["late-evening"]
        assert ids(tools.search_dictations("alpha", until="2026-11-01")) == []

    def test_explicit_offset_is_respected(self, fixture_db):
        assert ids(tools.search_dictations("echo", since="2026-11-02T03:00:00+00:00")) == ["late-evening"]
        assert ids(tools.search_dictations("echo", since="2026-11-02T03:00:01+00:00")) == []

    @pytest.mark.parametrize("bad", ["yesterday", "2026-13-40", ""])
    def test_bad_date_raises(self, fixture_db, bad):
        with pytest.raises(ValueError):
            tools.recent_dictations(since=bad)

    def test_since_after_until_raises(self, fixture_db):
        with pytest.raises(ValueError):
            tools.search_dictations("a", since="2026-11-03", until="2026-11-01")

    def test_unrecognised_stored_timestamp_raises_instead_of_guessing(self, fixture_db):
        import sqlite3
        conn = sqlite3.connect(fixture_db)
        history(conn, "odd", "2099-Nov-02", pasted="odd")
        conn.commit(); conn.close()
        with pytest.raises(ValueError, match="unrecognised"):
            tools.recent_dictations()


class TestArchived:
    def test_excluded_by_default(self, fixture_db):
        assert "archived" not in ids(tools.search_dictations("delta"))

    def test_flag_includes_them(self, fixture_db):
        assert "archived" in ids(tools.search_dictations("delta", include_archived=True))
        assert "archived" in ids(tools.recent_dictations(include_archived=True))

    def test_get_refuses_archived_without_flag_rather_than_saying_missing(self, fixture_db):
        with pytest.raises(ValueError, match="archived"):
            tools.get_dictation("archived")
        assert tools.get_dictation("archived", include_archived=True)["is_archived"] is True

    def test_stats_honours_flag(self, fixture_db):
        without = tools.dictation_stats()["total_dictations"]
        assert tools.dictation_stats(include_archived=True)["total_dictations"] == without + 1


class TestGetDictation:
    def test_all_versions_side_by_side(self, fixture_db):
        versions = tools.get_dictation("newest")["versions"]
        assert set(versions) == {"raw", "cleaned", "edited", "edited_unbounded", "pasted", "server_finalized"}
        assert versions["raw"]["text"] == "raw newest"
        assert versions["cleaned"]["text"] == "zebra only in formatted"
        assert versions["edited"]["text"] == "alpha edited"
        assert versions["pasted"]["text"] == "alpha pasted wins "
        assert versions["server_finalized"]["text"] == "alpha pasted wins "

    def test_missing_id_raises(self, fixture_db):
        with pytest.raises(ValueError, match="No dictation"):
            tools.get_dictation("nope")


class TestStats:
    def test_by_day_uses_local_days_and_puts_no_timestamp_last(self, fixture_db):
        result = tools.dictation_stats(group_by="day")
        assert [g["group"] for g in result["groups"]] == ["2026-11-02", "2026-11-01", tools.NO_TIMESTAMP]
        assert result["groups"][1] == {"group": "2026-11-01", "dictations": 1, "words": 10}
        assert result["total_dictations"] == 7

    def test_by_app_groups_blank_and_missing_together(self, fixture_db):
        groups = {g["group"]: g for g in tools.dictation_stats(group_by="app")["groups"]}
        assert groups[tools.UNKNOWN_APP]["dictations"] == 2
        assert groups[tools.UNKNOWN_APP]["words"] == 6  # 3 stored, plus 3 computed for the row with no stored count

    def test_totals_match_groups(self, fixture_db):
        result = tools.dictation_stats(group_by="app")
        assert result["total_words"] == sum(g["words"] for g in result["groups"])

    def test_date_filter_drops_undated_rows(self, fixture_db):
        result = tools.dictation_stats(since="2026-11-02")
        assert tools.NO_TIMESTAMP not in [g["group"] for g in result["groups"]]

    def test_bad_group_raises(self, fixture_db):
        with pytest.raises(ValueError):
            tools.dictation_stats(group_by="week")


class TestDictionary:
    def test_search_skips_deleted(self, fixture_db):
        result = tools.search_dictionary("wispr")
        assert [r["phrase"] for r in result["results"]] == ["Wispr"]
        assert result["results"][0]["times_used"] == 9
        assert result["results"][0]["last_used_local"] == "2026-11-02T07:00:00-05:00"

    def test_list_skips_deleted_and_tolerates_nulls(self, fixture_db):
        assert [r["phrase"] for r in tools.list_dictionary()["results"]] == ["Wispr", "Other"]

    def test_starred_only(self, fixture_db):
        assert [r["phrase"] for r in tools.list_dictionary(starred_only=True)["results"]] == ["Wispr"]


class TestNotes:
    def test_search_skips_deleted(self, fixture_db):
        result = tools.search_notes("foxtrot")
        assert [r["id"] for r in result["results"]] == ["n1"]
        assert result["results"][0]["snippet"] == "Foxtrot body"

    def test_get_note(self, fixture_db):
        note = tools.get_note("n1")
        assert note["content"]["text"] == "<p>Foxtrot body</p>"
        assert note["pinned"] is True

    def test_get_deleted_note_raises(self, fixture_db):
        with pytest.raises(ValueError, match="No note"):
            tools.get_note("n2")


class TestCaps:
    def test_limit_applies_and_reports_more(self, fixture_db):
        result = tools.recent_dictations(limit=2)
        assert result["count"] == 2
        assert result["has_more"] is True

    def test_no_more_means_has_more_false(self, fixture_db):
        assert tools.recent_dictations(limit=100)["has_more"] is False

    def test_oversized_limit_is_capped_and_says_so(self, fixture_db, monkeypatch):
        monkeypatch.setattr(tools, "MAX_DICTATION_RESULTS", 3)
        result = tools.recent_dictations(limit=50)
        assert result["count"] == 3
        assert result["limit_capped_to"] == 3
        assert result["has_more"] is True

    @pytest.mark.parametrize("limit", [0, -1])
    def test_bad_limit_raises(self, fixture_db, limit):
        with pytest.raises(ValueError):
            tools.recent_dictations(limit=limit)

    def test_long_text_is_cut_and_flagged(self, fixture_db):
        import sqlite3
        conn = sqlite3.connect(fixture_db)
        history(conn, "long", "2026-11-02 17:00:00.000 +00:00", pasted="x" * 5000)
        conn.commit(); conn.close()
        row = tools.recent_dictations(limit=1)["results"][0]
        assert len(row["text"]) == tools.LIST_TEXT_CHARS
        assert row["text_truncated"] is True
        assert row["text_total_chars"] == 5000

    def test_size_budget_stops_early_and_flags_it(self, fixture_db, monkeypatch):
        monkeypatch.setattr(tools, "MAX_RESPONSE_CHARS", 60)
        result = tools.recent_dictations(limit=100)
        assert 0 < result["count"] < 7
        assert result["has_more"] is True
        assert result["stopped_at_size_cap_chars"] == 60
