"""Общие фикстуры: изолированный HOME/XDG и живой демон в потоке."""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from mt import paths
from mt.daemon.daemon import Daemon
from mt.ipc.client import Client


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Подменяет домашний каталог, чтобы не трогать реальные файлы пользователя."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    return home


@pytest.fixture
def state_home(home: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Короткий каталог состояния: путь unix-сокета ограничен ~104 байтами."""
    short = Path(tempfile.mkdtemp(prefix="mt-", dir="/tmp"))
    monkeypatch.setenv("XDG_STATE_HOME", str(short))
    yield short
    shutil.rmtree(short, ignore_errors=True)


@pytest.fixture
def daemon(state_home: Path) -> Iterator[Daemon]:
    """Запущенный демон, слушающий сокет в изолированном состоянии."""
    instance = Daemon()
    instance.start()
    yield instance
    instance.stop()


@pytest.fixture
def client(daemon: Daemon) -> Iterator[Client]:
    with Client(paths.socket_path()) as instance:
        yield instance


def write_config(home: Path, body: str) -> Path:
    config_path = home / ".config" / "mt" / "config.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(body, encoding="utf-8")
    return config_path
