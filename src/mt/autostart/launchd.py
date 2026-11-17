"""Установка LaunchAgent (macOS): ``RunAtLoad`` поднимает демона после загрузки.

Демон на старте читает ``state.json`` и сам возобновляет незавершённые
закачки (D-07), поэтому plist с ``RunAtLoad`` — всё, что нужно для
«после перезагрузки закачки возобновляются без команды» (BEH-10).
"""

from __future__ import annotations

import plistlib
from pathlib import Path

from mt import paths
from mt.autostart import AutostartError

LABEL = "dev.mt.daemon"


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def render(daemon_command: list[str]) -> bytes:
    log = str(paths.log_path())
    return plistlib.dumps(
        {
            "Label": LABEL,
            "ProgramArguments": list(daemon_command),
            "RunAtLoad": True,
            "StandardOutPath": log,
            "StandardErrorPath": log,
        }
    )


def install(daemon_command: list[str]) -> None:
    """Пишет plist в ``~/Library/LaunchAgents``; совпадающий не перезаписывает."""
    target = plist_path()
    payload = render(daemon_command)
    try:
        if target.exists() and target.read_bytes() == payload:
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    except OSError as exc:
        raise AutostartError(f"не удалось записать {target}: {exc}") from exc
