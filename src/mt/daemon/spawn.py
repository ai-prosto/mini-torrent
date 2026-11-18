"""Самозапуск демона из клиента (D-03, BEH-07, BEH-08).

Клиент пробует подключиться к сокету; при отказе запускает ``mt daemon``
отдельным процессом в новой сессии (``start_new_session`` — демон отвязан
от управляющего терминала клиента и не получит его SIGHUP, stdio уходит
в лог) и ждёт появления сокета до 5 секунд. Ручного ``mt start`` не
существует; flock в демоне гарантирует, что гонка двух ``mt add`` не
породит двух демонов: проигравший выходит, клиенты подключаются к победителю.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

from mt import paths
from mt.ipc.client import Client, DaemonUnavailable

SPAWN_TIMEOUT = 5.0
_POLL_INTERVAL = 0.05


def daemon_command() -> list[str]:
    """Команда запуска роли демона: тот же интерпретатор, модуль ``mt``.

    Одна и та же команда используется для самозапуска, LaunchAgent и
    systemd-unit — демон всегда стартует одинаково.
    """
    return [sys.executable, "-m", "mt", "daemon"]


def spawn_daemon() -> None:
    """Запускает ``mt daemon`` отдельной сессией, stdio — в лог демона."""
    paths.ensure_state_dir()
    with open(paths.log_path(), "ab") as log:
        subprocess.Popen(  # noqa: S603 — команда собирается из констант
            daemon_command(),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )


def connect(socket_path: Path, timeout: float | None = None) -> Client:
    """Подключается к демону, при необходимости подняв его (BEH-08).

    Демон не ответил за ``timeout`` секунд — ``DaemonUnavailable``.
    """
    if timeout is None:
        timeout = SPAWN_TIMEOUT
    try:
        return Client(socket_path)
    except DaemonUnavailable:
        spawn_daemon()
    deadline = time.monotonic() + timeout
    while True:
        try:
            return Client(socket_path)
        except DaemonUnavailable:
            if time.monotonic() >= deadline:
                raise
            time.sleep(_POLL_INTERVAL)
