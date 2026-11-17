"""BEH-19: пути разрешаются одинаково на macOS и Linux — XDG с запасом."""

from __future__ import annotations

from pathlib import Path

import pytest

from mt import paths


def test_fallback_paths_under_home(home: Path) -> None:
    assert paths.config_file() == home / ".config" / "mt" / "config.toml"
    assert paths.state_dir() == home / ".local" / "state" / "mt"
    assert paths.state_file() == home / ".local" / "state" / "mt" / "state.json"
    assert paths.socket_path() == home / ".local" / "state" / "mt" / "mt.sock"
    assert paths.lock_path() == home / ".local" / "state" / "mt" / "mt.lock"
    assert paths.log_path() == home / ".local" / "state" / "mt" / "mt.log"
    assert paths.meta_dir() == home / ".local" / "state" / "mt" / "meta"


def test_xdg_variables_override_fallback(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "st"))
    assert paths.config_dir() == tmp_path / "cfg" / "mt"
    assert paths.state_dir() == tmp_path / "st" / "mt"


def test_empty_xdg_variable_falls_back(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", "")
    assert paths.config_dir() == home / ".config" / "mt"


def test_default_download_dir(home: Path) -> None:
    assert paths.default_download_dir() == home / "Downloads"


def test_ensure_state_dir_mode_0700(home: Path) -> None:
    created = paths.ensure_state_dir()
    assert created.is_dir()
    assert created.stat().st_mode & 0o777 == 0o700
    # повторный вызов безвреден
    assert paths.ensure_state_dir() == created
