"""BEH-03 и BEH-04: папка загрузок по умолчанию и из конфига."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mt import config, paths
from mt.daemon.daemon import Daemon
from mt.ipc.client import Client
from tests.conftest import write_config

MAGNET = "magnet:?xt=urn:btih:" + "a" * 40 + "&dn=debian.iso"


def test_default_download_dir(home: Path, daemon: Daemon, client: Client) -> None:
    """BEH-03: при пустом конфиге файлы кладутся в ``~/Downloads``.

    Конфига нет вовсе — add отрабатывает без ошибок и без предварительной
    настройки, каталог по умолчанию создаётся, запись указывает на него.
    """
    assert not (home / ".config" / "mt" / "config.toml").exists()

    response = client.add(MAGNET)

    default_dir = home / "Downloads"
    assert response["ok"] is True
    assert response["download_dir"] == str(default_dir)
    assert default_dir.is_dir()

    listed = client.list()["entries"]
    assert [entry["download_dir"] for entry in listed] == [str(default_dir)]


def test_default_download_dir_when_key_missing(
    home: Path, daemon: Daemon, client: Client
) -> None:
    """BEH-03: конфиг есть, но без ключа ``download_dir`` — тот же умолчательный путь."""
    write_config(home, 'listen_port = 6881\n')

    response = client.add(MAGNET)

    assert response["download_dir"] == str(home / "Downloads")


def test_config_download_dir_applies(home: Path, daemon: Daemon, client: Client) -> None:
    """BEH-04: путь из конфига применяется к новым закачкам без перезапуска демона.

    Демон уже работает; правим конфиг, добавляем закачку — файлы новой закачки
    назначены в заданный каталог, и запись в состоянии содержит этот же путь.
    """
    custom_dir = home / "torrents"
    write_config(home, f'download_dir = "{custom_dir}"\n')

    response = client.add(MAGNET)

    assert response["ok"] is True
    assert response["download_dir"] == str(custom_dir)
    assert custom_dir.is_dir()

    saved = json.loads(paths.state_file().read_text(encoding="utf-8"))
    assert [entry["download_dir"] for entry in saved["entries"]] == [str(custom_dir)]


def test_config_change_affects_only_new_downloads(
    home: Path, daemon: Daemon, client: Client
) -> None:
    """BEH-04: уже добавленные записи остаются на старом пути, новые — на новом."""
    first = client.add(MAGNET)
    assert first["download_dir"] == str(home / "Downloads")

    custom_dir = home / "torrents"
    write_config(home, f'download_dir = "{custom_dir}"\n')
    second = client.add(MAGNET)
    assert second["download_dir"] == str(custom_dir)

    dirs = [entry["download_dir"] for entry in client.list()["entries"]]
    assert dirs == [str(home / "Downloads"), str(custom_dir)]


def test_load_defaults_without_file(home: Path) -> None:
    cfg = config.load()
    assert cfg.download_dir == home / "Downloads"
    assert cfg.listen_port is None


def test_load_reads_values(home: Path) -> None:
    write_config(home, 'download_dir = "~/stash"\nlisten_port = 7001\n')
    cfg = config.load()
    assert cfg.download_dir == home / "stash"
    assert cfg.listen_port == 7001


def test_load_rejects_broken_toml(home: Path) -> None:
    path = write_config(home, "download_dir = [unclosed\n")
    with pytest.raises(config.ConfigError, match=str(path)):
        config.load()


def test_load_rejects_empty_download_dir(home: Path) -> None:
    write_config(home, 'download_dir = ""\n')
    with pytest.raises(config.ConfigError, match="download_dir"):
        config.load()


@pytest.mark.parametrize("value", ["0", "65536", "true", '"7000"'])
def test_load_rejects_bad_listen_port(home: Path, value: str) -> None:
    write_config(home, f"listen_port = {value}\n")
    with pytest.raises(config.ConfigError, match="listen_port"):
        config.load()
