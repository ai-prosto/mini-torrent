"""Пути конфига, состояния и сокета — одинаково на macOS и Linux (D-16).

Все пути разрешаются через XDG-переменные с запасным вариантом
``~/.config`` и ``~/.local/state``. Платформенных веток здесь нет.
"""

from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "mt"

STATE_DIR_MODE = 0o700


def _xdg_dir(env_var: str, fallback: str) -> Path:
    value = os.environ.get(env_var, "").strip()
    base = Path(value) if value else Path.home() / fallback
    return base / APP_NAME


def config_dir() -> Path:
    """Каталог конфига: ``$XDG_CONFIG_HOME/mt`` или ``~/.config/mt``."""
    return _xdg_dir("XDG_CONFIG_HOME", ".config")


def config_file() -> Path:
    """TOML-конфиг; файла может не быть — это нормально."""
    return config_dir() / "config.toml"


def state_dir() -> Path:
    """Каталог состояния: ``$XDG_STATE_HOME/mt`` или ``~/.local/state/mt``."""
    return _xdg_dir("XDG_STATE_HOME", os.path.join(".local", "state"))


def state_file() -> Path:
    """Список закачек, пишется атомарно."""
    return state_dir() / "state.json"


def socket_path() -> Path:
    """Unix-сокет демона, режим 0600."""
    return state_dir() / "mt.sock"


def lock_path() -> Path:
    """flock: один демон на пользователя."""
    return state_dir() / "mt.lock"


def log_path() -> Path:
    """Лог демона."""
    return state_dir() / "mt.log"


def meta_dir() -> Path:
    """Копии метаданных торрентов (``<infohash>.torrent``)."""
    return state_dir() / "meta"


def default_download_dir() -> Path:
    """Папка загрузок по умолчанию, когда конфига нет или ключ не задан."""
    return Path.home() / "Downloads"


def ensure_state_dir() -> Path:
    """Создаёт каталог состояния с режимом 0700 и возвращает его."""
    path = state_dir()
    path.mkdir(mode=STATE_DIR_MODE, parents=True, exist_ok=True)
    os.chmod(path, STATE_DIR_MODE)
    return path
