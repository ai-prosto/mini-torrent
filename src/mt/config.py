"""Чтение необязательного TOML-конфига со значениями по умолчанию (D-05).

Файла нет или ключа нет — берётся ``~/Downloads``. Конфиг перечитывается
на каждый ``add``, поэтому правка пути действует на новые закачки без
перезапуска демона.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from mt import paths


class ConfigError(Exception):
    """Конфиг существует, но прочитать его не удалось."""


@dataclass(frozen=True)
class Config:
    download_dir: Path
    listen_port: int | None = None


def load(path: Path | None = None) -> Config:
    """Читает конфиг из ``path`` (по умолчанию ``~/.config/mt/config.toml``).

    Отсутствующий файл — не ошибка: возвращаются значения по умолчанию.
    """
    if path is None:
        path = paths.config_file()

    data: dict = {}
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError:
        pass
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"не удалось разобрать {path}: {exc}") from exc

    download_dir = _read_download_dir(data, path)
    listen_port = _read_listen_port(data, path)
    return Config(download_dir=download_dir, listen_port=listen_port)


def _read_download_dir(data: dict, path: Path) -> Path:
    raw = data.get("download_dir")
    if raw is None:
        return paths.default_download_dir()
    if not isinstance(raw, str) or not raw.strip():
        raise ConfigError(f"{path}: download_dir должен быть непустой строкой")
    return Path(raw).expanduser()


def _read_listen_port(data: dict, path: Path) -> int | None:
    raw = data.get("listen_port")
    if raw is None:
        return None
    if not isinstance(raw, int) or isinstance(raw, bool) or not (1 <= raw <= 65535):
        raise ConfigError(f"{path}: listen_port должен быть числом порта 1..65535")
    return raw
