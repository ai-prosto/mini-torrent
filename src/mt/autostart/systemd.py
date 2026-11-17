"""Установка systemd user-unit и linger (Linux).

``WantedBy=default.target`` поднимает демона при загрузке машины (BEH-10),
``loginctl enable-linger`` держит user-юниты живыми без активной сессии —
демон переживает разрыв ssh и работает до входа пользователя (BEH-20).
"""

from __future__ import annotations

import contextlib
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

    Совпадающий unit на диске — маркер завершённой установки: он остаётся
    лежать только после успешных ``enable`` и linger, поэтому повторные
    вызовы systemctl не нужны. Неудача systemctl убирает unit, и следующий
    старт демона повторяет установку целиком (ssh-вход без DBus — обычная
    среда для первой неудачи, BEH-20).
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
    try:
        _run(["systemctl", "--user", "daemon-reload"])
        _run(["systemctl", "--user", "enable", UNIT_NAME])
        _run(["loginctl", "enable-linger", getpass.getuser()])
    except AutostartError:
        with contextlib.suppress(OSError):
            target.unlink()
        raise


_RUN_TIMEOUT = 15.0


def _run(command: list[str]) -> None:
    try:
        result = subprocess.run(  # noqa: S603 — команды собираются из констант
            command, capture_output=True, text=True, check=False, timeout=_RUN_TIMEOUT
        )
    except subprocess.TimeoutExpired as exc:
        raise AutostartError(f"{shlex.join(command)}: не ответила за {_RUN_TIMEOUT:g} с") from exc
    except OSError as exc:
        raise AutostartError(f"не удалось выполнить {shlex.join(command)}: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"код {result.returncode}"
        raise AutostartError(f"{shlex.join(command)}: {detail}")
