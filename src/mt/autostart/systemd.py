"""Установка systemd user-unit и linger (Linux).

``WantedBy=default.target`` поднимает демона при загрузке машины (BEH-10),
``loginctl enable-linger`` держит user-юниты живыми без активной сессии —
демон переживает разрыв ssh и работает до входа пользователя (BEH-20).
"""

from __future__ import annotations

import getpass
import os
import shlex
import subprocess
from pathlib import Path

from mt.autostart import AutostartError

UNIT_NAME = "mt.service"

UNIT_TEMPLATE = """\
[Unit]
Description=mt torrent daemon

[Service]
ExecStart={exec_start}
Restart=on-failure

[Install]
WantedBy=default.target
"""


def unit_path() -> Path:
    value = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = Path(value) if value else Path.home() / ".config"
    return base / "systemd" / "user" / UNIT_NAME


def render(daemon_command: list[str]) -> str:
    return UNIT_TEMPLATE.format(exec_start=shlex.join(daemon_command))


def install(daemon_command: list[str]) -> None:
    """Пишет unit, делает ``enable`` и включает linger; идемпотентна.

    Совпадающий unit на диске означает, что ``enable`` и linger уже
    делались раньше — повторные вызовы systemctl не нужны.
    """
    target = unit_path()
    payload = render(daemon_command)
    try:
        if target.exists() and target.read_text(encoding="utf-8") == payload:
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(payload, encoding="utf-8")
    except OSError as exc:
        raise AutostartError(f"не удалось записать {target}: {exc}") from exc
    _run(["systemctl", "--user", "daemon-reload"])
    _run(["systemctl", "--user", "enable", UNIT_NAME])
    _run(["loginctl", "enable-linger", getpass.getuser()])


def _run(command: list[str]) -> None:
    try:
        result = subprocess.run(  # noqa: S603 — команды собираются из констант
            command, capture_output=True, text=True, check=False
        )
    except OSError as exc:
        raise AutostartError(f"не удалось выполнить {shlex.join(command)}: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"код {result.returncode}"
        raise AutostartError(f"{shlex.join(command)}: {detail}")
