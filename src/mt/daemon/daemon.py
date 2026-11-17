"""Запуск роли демона: блокировка, сокет, состояние, движок, цикл (D-02, D-15).

``add`` читает конфиг, создаёт каталог загрузок, заводит торрент в движок
и запись в ``state.json``, отвечает клиенту. Конфиг перечитывается на каждый
``add``, поэтому правка ``download_dir`` действует на новые закачки без
перезапуска демона (D-05). Тикер раз в 500 мс собирает срез прогресса для
снапшотов; на 100% запись переходит в ``done``, торрент выбрасывается из
движка, и соединения с пирами закрываются (D-10).
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import os
import re
import signal
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any
from urllib.parse import parse_qs, urlsplit

from mt import autostart, paths
from mt import config as config_mod
from mt.daemon import engine as engine_mod
from mt.daemon import spawn as spawn_mod
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
        """Каталог состояния (0700), flock, движок, возобновление, сокет, тикер."""
        paths.ensure_state_dir()
        self._acquire_lock()
        self._engine = engine_mod.Engine(listen_port=self._listen_port_from_config())
        self._resume_pending()
        self._server.start()
        # после сокета: клиенты не ждут systemctl/loginctl (у _run есть таймаут,
        # но и он съел бы 5-секундный бюджет spawn.connect)
        self._register_autostart()
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
        """Блокирующий цикл роли демона; завершается по stop()/request_stop()."""
        try:
            while not self._stopping.wait(timeout=1.0):
                pass
        finally:
            self.stop()

    def request_stop(self) -> None:
        """Просит демона остановиться; run_forever() завершится сам."""
        self._stopping.set()

    def _acquire_lock(self) -> None:
        # файл живёт, пока демон держит flock — контекстный менеджер не подходит
        fd = os.open(paths.lock_path(), os.O_RDWR | os.O_CREAT, 0o600)
        lock_file = os.fdopen(fd, "r+b")
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            lock_file.close()
            if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES):
                raise AlreadyRunning("демон уже запущен (mt.lock занят)") from exc
            raise DaemonError(f"не удалось взять {paths.lock_path()}: {exc}") from exc
        # pid в lock-файле: тестам и пользователю видно, кого именно убивать
        lock_file.truncate(0)
        lock_file.write(str(os.getpid()).encode("ascii"))
        lock_file.flush()
        self._lock_file = lock_file

    def _resume_pending(self) -> None:
        """D-07: записи ``downloading`` поднимаются сами на каждом старте.

        Торрент заводится из сохранённой копии metainfo (D-06), а когда её
        нет (magnet, не успевший получить метаданные) — из исходной ссылки.
        Движок перепроверяет куски на диске, поэтому докачка начинается
        с прежнего процента, не ниже (BEH-09, BEH-11).
        """
        for entry in self._entries:
            if entry.status != state_mod.STATUS_DOWNLOADING:
                continue
            meta = paths.meta_dir() / f"{entry.infohash}.torrent"
            # битая копия metainfo (SIGKILL мог оборвать её запись) не должна
            # блокировать возобновление навсегда — откат к исходной ссылке
            sources = [str(meta)] if meta.is_file() else []
            if entry.source not in sources:
                sources.append(entry.source)
            failure: Exception | None = None
            for source in sources:
                # RuntimeError приходит из libtorrent; без перехвата одна битая
                # запись валила бы старт демона (с Restart=on-failure — циклично)
                try:
                    self.engine.add(source, Path(entry.download_dir))
                    failure = None
                    break
                except (engine_mod.EngineError, RuntimeError) as exc:
                    failure = exc
            if failure is not None:
                print(f"mt: не удалось возобновить «{entry.name}»: {failure}", file=sys.stderr)

    @staticmethod
    def _register_autostart() -> None:
        """D-08: регистрация автозапуска ОС; неудача — предупреждение, не отказ."""
        try:
            autostart.install(spawn_mod.daemon_command())
        except autostart.AutostartError as exc:
            print(f"mt: автозапуск не настроен: {exc}", file=sys.stderr)

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
    except (state_mod.StateError, OSError) as exc:
        print(f"mt: {exc}", file=sys.stderr)
        return 1
    # launchd/systemd останавливают демона SIGTERM — завершаемся аккуратно;
    # обработчик ставится до start(): проверка кусков при возобновлении может
    # длиться долго, и SIGTERM в этом окне не должен ронять процесс жёстко
    signal.signal(signal.SIGTERM, lambda *_: daemon.request_stop())
    try:
        daemon.start()
    except AlreadyRunning:
        return 0  # запущенный демон — не ошибка: гонку выиграл другой процесс
    except (DaemonError, engine_mod.EngineError, state_mod.StateError, OSError) as exc:
        print(f"mt: {exc}", file=sys.stderr)
        return 1
    # Ctrl-C в форграунде: stop() уже отработал в finally run_forever()
    with contextlib.suppress(KeyboardInterrupt):
        daemon.run_forever()
    return 0
