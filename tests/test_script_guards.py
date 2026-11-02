"""Runs the real scripts with empty variables and proves they refuse. launchctl is a stand-in; nothing real is touched."""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "bin"
SCRIPTS = sorted(p for p in BIN.iterdir() if p.is_file())
OVERRIDES = {
    "agents": "WISPR_FLOW_AGENTS_DIR",
    "logs": "WISPR_FLOW_LOGS_DIR",
    "sync_label": "WISPR_FLOW_SYNC_LABEL",
    "backup_label": "WISPR_FLOW_BACKUP_LABEL",
}
SYNC_PLIST = "local.wispr-flow-mcp.sync.plist"
BACKUP_PLIST = "local.wispr-flow-mcp.backup.plist"
_REAL_UV = shutil.which("uv") or os.path.expanduser("~/.local/bin/uv")


class Sandbox:
    """A throwaway home holding files a bad delete would destroy, including one named as an empty label would name it."""

    def __init__(self, tmp_path):
        self.home = tmp_path / "home"
        self.agents = self.home / "agents"
        self.logs = self.home / "logs"
        self.agents.mkdir(parents=True)
        self.logs.mkdir()
        self.calls = tmp_path / "launchctl-calls"
        fake = tmp_path / "fake-bin"
        fake.mkdir()
        launchctl = fake / "launchctl"
        launchctl.write_text(f'#!/bin/sh\necho "$@" >> "{self.calls}"\n')
        launchctl.chmod(0o755)
        self.path = f"{fake}:{Path(_REAL_UV).parent}:/usr/bin:/bin"
        for name in (SYNC_PLIST, BACKUP_PLIST, ".plist"):
            (self.agents / name).write_text("keep")
        (self.logs / "sync.log").write_text("keep")
        self.watched = [*self.agents.iterdir(), self.logs / "sync.log"]

    def env(self, **empties):
        env = {
            **os.environ,
            "HOME": str(self.home),
            "PATH": self.path,
            "UV_CACHE_DIR": str(self.home.parent / "uv-cache"),
            "WISPR_FLOW_AGENTS_DIR": str(self.agents),
            "WISPR_FLOW_LOGS_DIR": str(self.logs),
        }
        env.update(empties)
        return env

    def run(self, script, *args, **empties):
        return subprocess.run(
            [str(BIN / script), *args], capture_output=True, text=True, env=self.env(**empties), cwd=self.home, timeout=120
        )

    def nothing_deleted(self):
        return all(path.exists() for path in self.watched)


@pytest.fixture
def sandbox(tmp_path):
    return Sandbox(tmp_path)


class TestEveryDeleteIsGuarded:
    def rm_lines(self):
        return [
            (script.name, line)
            for script in SCRIPTS
            for line in script.read_text().splitlines()
            if re.search(r"(^|[\s;&|(])rm\s", line) and not line.lstrip().startswith("#")
        ]

    def test_the_scripts_that_delete_are_found(self):
        assert {name for name, _ in self.rm_lines()} == {"install-agents", "uninstall"}

    def test_every_variable_on_an_rm_line_has_the_guard(self):
        for name, line in self.rm_lines():
            references = re.findall(r"\$\{[^}]*\}|\$\w+", line)
            assert references, f"{name}: {line}"
            assert all(re.fullmatch(r"\$\{\w+:\?\}", ref) for ref in references), f"{name}: {line}"

    def test_recover_script_deletes_nothing_itself(self):
        assert "rm " not in (BIN / "recover").read_text()


class TestInstallRefuses:
    @pytest.mark.parametrize("variable", ["agents", "logs", "sync_label", "backup_label"])
    def test_empty_variable_stops_it_and_deletes_nothing(self, sandbox, variable):
        result = sandbox.run("install-agents", **{OVERRIDES[variable]: ""})
        assert result.returncode != 0
        assert variable in result.stderr  # The ${var:?} guard fired, not some later failure.
        assert (sandbox.agents / ".plist").read_text() == "keep"
        assert (sandbox.logs / "sync.log").exists()

    def test_empty_home_stops_it_before_anything_runs(self, sandbox):
        result = sandbox.run("install-agents", HOME="")
        assert result.returncode != 0
        assert "HOME" in result.stderr
        assert not sandbox.calls.exists()
        assert sandbox.nothing_deleted()

    def test_with_real_values_it_removes_the_backup_agent_when_the_backup_is_off(self, sandbox):
        # Control: the same delete does happen with good values, so the refusals above mean something.
        result = sandbox.run("install-agents")
        assert result.returncode == 0, result.stderr
        assert not (sandbox.agents / BACKUP_PLIST).exists()
        assert (sandbox.agents / ".plist").read_text() == "keep"


class TestUninstallRefuses:
    @pytest.mark.parametrize("variable", ["agents", "logs", "sync_label", "backup_label"])
    def test_empty_variable_stops_it_and_deletes_nothing(self, sandbox, variable):
        result = sandbox.run("uninstall", **{OVERRIDES[variable]: ""})
        assert result.returncode != 0
        assert variable in result.stderr
        assert sandbox.nothing_deleted()
        assert not sandbox.calls.exists()  # Refused before stopping any agent.

    def test_empty_home_stops_it(self, sandbox):
        result = sandbox.run("uninstall", HOME="")
        assert result.returncode != 0
        assert "HOME" in result.stderr
        assert sandbox.nothing_deleted()

    def test_with_real_values_it_removes_exactly_its_own_files(self, sandbox):
        result = sandbox.run("uninstall")
        assert result.returncode == 0, result.stderr
        assert sorted(p.name for p in sandbox.agents.iterdir()) == [".plist"]
        assert not sandbox.logs.exists()

    def test_it_leaves_the_archive_and_config_alone(self, sandbox):
        keep = [Path(os.environ["WISPR_FLOW_ARCHIVE"]), Path(os.environ["WISPR_FLOW_CONFIG"])]
        keep[0].write_text("archive")
        assert sandbox.run("uninstall").returncode == 0
        assert all(path.exists() for path in keep)


class TestRecoverRefuses:
    def test_empty_home_stops_it(self, sandbox):
        result = sandbox.run("recover", HOME="")
        assert result.returncode != 0
        assert "HOME" in result.stderr

    def test_empty_work_folder_stops_it_and_touches_the_current_folder_not_at_all(self, sandbox):
        (sandbox.home / "flow-copy.sqlite").write_text("keep")
        before = sorted(p.name for p in sandbox.home.iterdir())
        result = sandbox.run("recover", "--work-dir", "")
        assert result.returncode != 0
        assert "empty" in result.stderr
        assert sorted(p.name for p in sandbox.home.iterdir()) == before
        assert (sandbox.home / "flow-copy.sqlite").read_text() == "keep"

    def test_empty_source_stops_it(self, sandbox):
        result = sandbox.run("recover", "--source", "", "--work-dir", str(sandbox.home / "w"))
        assert result.returncode != 0
        assert "empty" in result.stderr
        assert not (sandbox.home / "w").exists()
