"""The timezone default and the optional backup. Nothing here depends on the real Mac's settings."""

import os
import sqlite3

import pytest

from test_archive import make_recovered
from wispr_flow_mcp import archive, config, tools


@pytest.fixture
def seeded(fixture_db, tmp_path):
    conn = archive.open_for_writing()
    yield conn, archive.seed(conn, make_recovered(tmp_path / "recovered.sqlite"))
    conn.close()


class TestTimezone:
    def point_localtime_at(self, tmp_path, monkeypatch, name):
        link = tmp_path / "localtime"
        link.symlink_to(f"/var/db/timezone/zoneinfo/{name}")
        monkeypatch.setattr(config, "LOCALTIME_PATH", str(link))

    def test_with_no_setting_uses_the_macs_timezone(self, tmp_path, monkeypatch, fixed_settings):
        fixed_settings.unlink()
        self.point_localtime_at(tmp_path, monkeypatch, "Asia/Tokyo")
        assert config.timezone().key == "Asia/Tokyo"

    def test_tool_output_follows_the_macs_timezone(self, fixture_db, tmp_path, monkeypatch, fixed_settings):
        fixed_settings.unlink()
        self.point_localtime_at(tmp_path, monkeypatch, "Asia/Tokyo")
        row = tools.search_dictations("echo")["results"][0]
        assert row["local_time"] == "2026-11-02T12:00:00+09:00"

    def test_bare_dates_follow_the_macs_timezone(self, fixture_db, tmp_path, monkeypatch, fixed_settings):
        fixed_settings.unlink()
        self.point_localtime_at(tmp_path, monkeypatch, "Asia/Tokyo")
        assert tools.search_dictations("echo", until="2026-11-01")["results"] == []
        assert len(tools.search_dictations("echo", until="2026-11-02")["results"]) == 1

    def test_the_no_results_note_names_the_timezone_in_use(self, fixture_db, fixed_settings):
        fixed_settings.write_text('timezone = "Europe/Paris"\n')
        assert "(Europe/Paris)" in tools.search_dictations("no-such-text-qzxv")["note"]

    def test_config_overrides_the_mac(self, tmp_path, monkeypatch, fixed_settings):
        fixed_settings.write_text('timezone = "Europe/Paris"\n')
        self.point_localtime_at(tmp_path, monkeypatch, "Asia/Tokyo")
        assert config.timezone().key == "Europe/Paris"

    def test_unreadable_mac_timezone_raises_rather_than_guessing(self, tmp_path, monkeypatch, fixed_settings):
        fixed_settings.unlink()
        plain = tmp_path / "localtime"
        plain.write_text("x")
        monkeypatch.setattr(config, "LOCALTIME_PATH", str(plain))
        with pytest.raises(config.ConfigError, match="timezone"):
            config.timezone()

    def test_unknown_timezone_name_raises(self, fixed_settings):
        fixed_settings.write_text('timezone = "Not/AZone"\n')
        with pytest.raises(config.ConfigError, match="recognised"):
            config.timezone()


class TestBackupIsOptional:
    def test_off_by_default_and_touches_nothing(self, seeded, capsys, tmp_path):
        before = sorted(p.name for p in tmp_path.iterdir())
        assert archive.main(["backup"]) == 0
        assert "backup=off" in capsys.readouterr().out
        assert sorted(p.name for p in tmp_path.iterdir()) == before

    def test_runs_once_a_folder_is_configured(self, seeded, tmp_path):
        folder = tmp_path / "chosen"
        folder.mkdir()
        assert archive.main(["set-backup-folder", str(folder)]) == 0
        assert archive.main(["backup"]) == 0
        copies = list((folder / archive.BACKUP_FOLDER).iterdir())
        assert len(copies) == 1
        check = sqlite3.connect(copies[0])
        assert check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        check.close()

    def test_setting_a_folder_keeps_other_settings(self, tmp_path):
        folder = tmp_path / 'with "quote" and space'
        folder.mkdir()
        config.set_backup_folder(str(folder))
        assert config.backup_folder() == str(folder)
        assert config.timezone().key == "America/New_York"

    def test_setting_a_missing_folder_fails_and_changes_nothing(self, tmp_path):
        assert archive.main(["set-backup-folder", str(tmp_path / "gone")]) == 1
        assert config.backup_folder() is None

    def test_configured_but_missing_folder_is_an_error_not_a_silent_skip(self, seeded, fixed_settings, tmp_path, capsys):
        fixed_settings.write_text(f'backup_folder = "{tmp_path}/unmounted"\n')
        assert archive.main(["backup"]) == 1
        assert "backup folder" in capsys.readouterr().err
        assert not (tmp_path / "unmounted").exists()

    def test_show_prints_nothing_when_off(self, capsys):
        assert archive.main(["show-backup-folder"]) == 0
        assert capsys.readouterr().out.strip() == ""

    def test_config_file_is_private(self, tmp_path, fixed_settings):
        fixed_settings.unlink()
        folder = tmp_path / "chosen"
        folder.mkdir()
        config.set_backup_folder(str(folder))
        assert os.stat(fixed_settings).st_mode & 0o077 == 0
