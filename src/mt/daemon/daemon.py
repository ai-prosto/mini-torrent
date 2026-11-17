"""Запуск роли демона: блокировка, сокет, состояние, цикл (D-02, D-15).

Торрент-движка здесь ещё нет (он появляется в DT-02): ``add`` читает конфиг,
создаёт каталог загрузок, заводит запись в ``state.json`` и отвечает клиенту.
Конфиг перечитывается на каждый ``add``, поэтому правка ``download_dir``
действует на новые закачки без перезапуска демона (D-05).
"""

from __future__ import annotations

import fcntl
import re
import threading
import time
from collections.abc import Iterator
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any
from urllib.parse import parse_qs, urlsplit

from mt import config as config_mod
from mt import paths
from mt.daemon import state as state_mod
from mt.ipc import proto
from mt.ipc.server import IPCServer

SNAPSHOT_INTERVAL = 0.5

_BTIH_RE = re.compile(r"^urn:btih:([0-9a-fA-F]{40}|[A-Za-z2-7]{32})$")


class DaemonError(Exception):
    """Демон не смог запуститься."""


class AlreadyRunning(DaemonError):
    """flock занят: демон этого пользователя уже работает."""


def parse_source(source: str) -> tuple[str, str]:
    """Возвращает ``(infohash, name)`` для magnet-ссылки или пути к .torrent.

    Полный разбор metainfo появится вместе с движком (DT-02); здесь только
    различение magnet/файл (D-13) и имя для записи состояния.
    """
    if source.startswith("magnet:"):
        params = parse_qs(urlsplit(source).query)
        infohash = ""
        for xt in params.get("xt", []):
            match = _BTIH_RE.match(xt)
            if match:
                infohash = match.group(1).lower()
                break
        if not infohash:
            raise ValueError("magnet-ссылка без urn:btih")
        name = params.get("dn", [infohash])[0]
        return infohash, name

    path = Path(source).expanduser()
    if not path.is_file():
        raise ValueError(f"файл не найден: {source}")
    return "", path.stem


class Daemon:
    """Держит состояние и обслуживает запросы клиентов через IPCServer."""

    def __init__(self) -> None:
        self._state_path = paths.state_file()
        self._lock = threading.Lock()
        self._entries = state_mod.load(self._state_path)
        self._stopping = threading.Event()
        self._lock_file: IO[bytes] | None = None
        self._server = IPCServer(
            paths.socket_path(),
            handlers={proto.OP_ADD: self._handle_add, proto.OP_LIST: self._handle_list},
            subscribe_handler=self._handle_subscribe,
        )

    def start(self) -> None:
        """Создаёт каталог состояния (0700), берёт flock и открывает сокет."""
        paths.ensure_state_dir()
        self._acquire_lock()
        self._server.start()

    def stop(self) -> None:
        self._stopping.set()
        self._server.stop()
        if self._lock_file is not None:
            self._lock_file.close()
            self._lock_file = None

    def run_forever(self) -> None:
        """Блокирующий цикл роли демона; завершается по stop()."""
        try:
            while not self._stopping.wait(timeout=1.0):
                pass
        finally:
            self.stop()

    def _acquire_lock(self) -> None:
        # файл живёт, пока демон держит flock — контекстный менеджер не подходит
        lock_file = open(paths.lock_path(), "ab")  # noqa: SIM115
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            lock_file.close()
            raise AlreadyRunning("демон уже запущен (mt.lock занят)") from exc
        self._lock_file = lock_file

    # --- обработчики запросов -------------------------------------------

    def _handle_add(self, request: dict[str, Any]) -> dict[str, Any]:
        source = request["source"]
        try:
            infohash, name = parse_source(source)
        except ValueError as exc:
            return proto.error(str(exc))

        cfg = config_mod.load()  # перечитывается на каждый add (D-05)
        cfg.download_dir.mkdir(parents=True, exist_ok=True)

        entry = state_mod.Entry(
            infohash=infohash,
            name=name,
            source=source,
            download_dir=str(cfg.download_dir),
            added_at=datetime.now(UTC).isoformat(),
        )
        with self._lock:
            self._entries.append(entry)
            state_mod.save(self._state_path, self._entries)
        return proto.ok(name=name, infohash=infohash, download_dir=str(cfg.download_dir))

    def _handle_list(self, _request: dict[str, Any]) -> dict[str, Any]:
        return proto.ok(entries=self._snapshot_entries())

    def _handle_subscribe(self, _request: dict[str, Any]) -> Iterator[dict[str, Any]]:
        while not self._stopping.is_set():
            yield {"entries": self._snapshot_entries()}
            time.sleep(SNAPSHOT_INTERVAL)

    def _snapshot_entries(self) -> list[dict[str, Any]]:
        with self._lock:
            return [asdict(entry) for entry in self._entries]


def run() -> int:
    """Точка входа подкоманды ``mt daemon``."""
    daemon = Daemon()
    try:
        daemon.start()
    except AlreadyRunning:
        return 0  # запущенный демон — не ошибка: гонку выиграл другой процесс
    daemon.run_forever()
    return 0
