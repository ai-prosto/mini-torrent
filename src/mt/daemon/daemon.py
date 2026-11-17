"""Запуск роли демона: блокировка, сокет, состояние, движок, цикл (D-02, D-15).

``add`` читает конфиг, создаёт каталог загрузок, заводит торрент в движок
и запись в ``state.json``, отвечает клиенту. Конфиг перечитывается на каждый
``add``, поэтому правка ``download_dir`` действует на новые закачки без
перезапуска демона (D-05). Тикер раз в 500 мс собирает срез прогресса для
снапшотов; на 100% запись переходит в ``done``, торрент выбрасывается из
движка, и соединения с пирами закрываются (D-10).
"""

from __future__ import annotations

import errno
import fcntl
import re
import sys
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
from mt.daemon import engine as engine_mod
from mt.daemon import state as state_mod
from mt.ipc import proto
from mt.ipc.server import IPCServer

SNAPSHOT_INTERVAL = 0.5

_BTIH_RE = re.compile(r"^urn:btih:([0-9a-fA-F]{40}|[A-Za-z2-7]{32})$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


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
        name = _clean_name(params.get("dn", [""])[0]) or infohash
        return infohash, name

    path = Path(source).expanduser()
    if not path.is_file():
        raise ValueError(f"файл не найден: {source}")
    return "", _clean_name(path.stem) or "torrent"


def _clean_name(raw: str) -> str:
    """Выкидывает управляющие символы: имя попадает в табличный вывод списка."""
    return _CONTROL_RE.sub(" ", raw).strip()


class Daemon:
    """Держит состояние и обслуживает запросы клиентов через IPCServer."""

    def __init__(self) -> None:
        self._state_path = paths.state_file()
        self._lock = threading.Lock()
        self._entries = state_mod.load(self._state_path)
        self._stats: dict[str, engine_mod.TorrentStats] = {}
        self._stopping = threading.Event()
        self._lock_file: IO[bytes] | None = None
        self._engine: engine_mod.Engine | None = None
        self._ticker: threading.Thread | None = None
        self._server = IPCServer(
            paths.socket_path(),
            handlers={proto.OP_ADD: self._handle_add, proto.OP_LIST: self._handle_list},
            subscribe_handler=self._handle_subscribe,
        )

    @property
    def engine(self) -> engine_mod.Engine:
        if self._engine is None:
            raise DaemonError("демон не запущен")
        return self._engine

    def start(self) -> None:
        """Каталог состояния (0700), flock, движок, сокет и тикер прогресса."""
        paths.ensure_state_dir()
        self._acquire_lock()
        self._engine = engine_mod.Engine(listen_port=self._listen_port_from_config())
        self._server.start()
        self._ticker = threading.Thread(target=self._tick_loop, name="mt-ticker", daemon=True)
        self._ticker.start()

    def stop(self) -> None:
        self._stopping.set()
        ticker, self._ticker = self._ticker, None
        if ticker is not None:
            ticker.join(timeout=5)
        self._server.stop()
        engine, self._engine = self._engine, None
        if engine is not None:
            engine.close()
        if self._lock_file is not None:
            self._lock_file.close()
            self._lock_file = None

    @staticmethod
    def _listen_port_from_config() -> int | None:
        # битый конфиг не должен мешать старту демона: об ошибке сообщит add
        try:
            return config_mod.load().listen_port
        except config_mod.ConfigError:
            return None

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
            if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES):
                raise AlreadyRunning("демон уже запущен (mt.lock занят)") from exc
            raise DaemonError(f"не удалось взять {paths.lock_path()}: {exc}") from exc
        self._lock_file = lock_file

    # --- цикл прогресса ---------------------------------------------------

    def _tick_loop(self) -> None:
        """Раз в 500 мс обновляет срез движка и обрабатывает завершения."""
        while not self._stopping.wait(timeout=SNAPSHOT_INTERVAL):
            try:
                self._refresh()
            except Exception as exc:  # цикл прогресса не должен умирать
                print(f"mt: ошибка цикла прогресса: {exc}", file=sys.stderr)

    def _refresh(self) -> None:
        engine = self._engine
        if engine is None:
            return
        stats = engine.poll()
        completed: list[str] = []
        with self._lock:
            # срез копится, а не замещается: отданные байты завершённых
            # закачек остаются видны и после drop
            self._stats.update(stats)
            changed = False
            for entry in self._entries:
                live = stats.get(entry.infohash)
                if live is None:
                    continue
                if live.has_metadata and not entry.files:
                    # метаданные магнита приехали: узнали имя, размер и файлы
                    entry.name = _clean_name(live.name) or entry.name
                    entry.total_bytes = live.total_bytes
                    entry.files = list(live.files)
                    changed = True
                if live.is_complete and entry.status == state_mod.STATUS_DOWNLOADING:
                    entry.status = state_mod.STATUS_DONE
                    entry.completed_at = datetime.now(UTC).isoformat()
                    completed.append(entry.infohash)
                    changed = True
            if changed:
                state_mod.save(self._state_path, list(self._entries))
        for infohash in completed:
            # BEH-17: на 100% торрент выбрасывается, соединения закрываются
            engine.drop(infohash)

    # --- обработчики запросов -------------------------------------------

    def _handle_add(self, request: dict[str, Any]) -> dict[str, Any]:
        source = request["source"]
        try:
            _, fallback_name = parse_source(source)  # ранняя проверка источника
        except ValueError as exc:
            return proto.error(str(exc))

        cfg = config_mod.load()  # перечитывается на каждый add (D-05)
        cfg.download_dir.mkdir(parents=True, exist_ok=True)

        try:
            added = self.engine.add(source, cfg.download_dir)
        except engine_mod.EngineError as exc:
            return proto.error(str(exc))

        name = _clean_name(added.name) or fallback_name
        entry = state_mod.Entry(
            infohash=added.infohash,
            name=name,
            source=source,
            download_dir=str(cfg.download_dir),
            files=list(added.files),
            total_bytes=added.total_bytes,
            added_at=datetime.now(UTC).isoformat(),
        )
        with self._lock:
            # сперва на диск: при ошибке записи память и state.json не расходятся
            state_mod.save(self._state_path, [*self._entries, entry])
            self._entries.append(entry)
        return proto.ok(name=name, infohash=added.infohash, download_dir=str(cfg.download_dir))

    def _handle_list(self, _request: dict[str, Any]) -> dict[str, Any]:
        return proto.ok(entries=self._snapshot_entries())

    def _handle_subscribe(self, _request: dict[str, Any]) -> Iterator[dict[str, Any]]:
        while not self._stopping.is_set():
            yield {"entries": self._snapshot_entries()}
            time.sleep(SNAPSHOT_INTERVAL)

    def _snapshot_entries(self) -> list[dict[str, Any]]:
        """Записи состояния, обогащённые живым прогрессом из движка."""
        with self._lock:
            snapshot = []
            for entry in self._entries:
                data = asdict(entry)
                live = self._stats.get(entry.infohash)
                if entry.status == state_mod.STATUS_DONE:
                    data.update(
                        progress=100.0,
                        download_rate=0,
                        upload_rate=0,
                        uploaded_bytes=live.uploaded_bytes if live else 0,
                        num_peers=0,
                    )
                elif live is not None:
                    data.update(
                        progress=round(live.progress, 1),
                        download_rate=live.download_rate,
                        upload_rate=live.upload_rate,
                        uploaded_bytes=live.uploaded_bytes,
                        num_peers=live.num_peers,
                    )
                else:
                    data.update(
                        progress=0.0,
                        download_rate=0,
                        upload_rate=0,
                        uploaded_bytes=0,
                        num_peers=0,
                    )
                snapshot.append(data)
            return snapshot


def run() -> int:
    """Точка входа подкоманды ``mt daemon``."""
    try:
        daemon = Daemon()
        daemon.start()
    except AlreadyRunning:
        return 0  # запущенный демон — не ошибка: гонку выиграл другой процесс
    except (DaemonError, engine_mod.EngineError, state_mod.StateError, OSError) as exc:
        print(f"mt: {exc}", file=sys.stderr)
        return 1
    daemon.run_forever()
    return 0
