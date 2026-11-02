"""Optional settings. With no config file, the timezone is the Mac's own and the backup is off."""

from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_CONFIG_PATH = "~/Library/Application Support/wispr-flow-mcp/config.toml"
CONFIG_PATH_ENV = "WISPR_FLOW_CONFIG"
LOCALTIME_PATH = "/etc/localtime"
_ZONEINFO_MARKER = "zoneinfo/"


class ConfigError(RuntimeError):
    """The config file or the Mac's timezone could not be understood. Never guessed around."""


def config_path() -> Path:
    return Path(os.path.expanduser(os.environ.get(CONFIG_PATH_ENV) or DEFAULT_CONFIG_PATH))


def load() -> dict[str, object]:
    try:
        with config_path().open("rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError:
        return {}
    except tomllib.TOMLDecodeError:
        raise ConfigError("The config file is not valid TOML") from None


def _text_setting(name: str) -> str | None:
    value = load().get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{name} in the config file must be non-empty text")
    return value


def backup_folder() -> str | None:
    """The folder backups go in, or None when the backup is off."""
    value = _text_setting("backup_folder")
    return os.path.expanduser(value) if value else None


def set_backup_folder(folder: str) -> None:
    """Writes the setting, keeping any other settings in the file."""
    resolved = Path(os.path.expanduser(folder))
    if not folder.strip() or not resolved.is_dir():
        raise FileNotFoundError("The backup folder was not found")
    settings = {**load(), "backup_folder": str(resolved)}
    path = config_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    previous = os.umask(0o077)
    try:
        path.write_text("".join(f"{key} = {json.dumps(value, ensure_ascii=False)}\n" for key, value in settings.items()))
    finally:
        os.umask(previous)


def _system_timezone_name() -> str:
    target = os.path.realpath(LOCALTIME_PATH)
    if _ZONEINFO_MARKER not in target:
        raise ConfigError("Could not read this Mac's timezone. Set timezone in the config file.")
    return target.split(_ZONEINFO_MARKER, 1)[1]


def timezone() -> ZoneInfo:
    """The config's timezone if set, otherwise the Mac's. Read each time, so a change on the Mac is picked up."""
    name = _text_setting("timezone") or _system_timezone_name()
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise ConfigError("The timezone is not a recognised name, such as Europe/Paris") from None
