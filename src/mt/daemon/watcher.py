"""Слежение за диском: fsnotify плюс подстраховочный опрос (D-11).

Watcher подписывается на каталоги загрузок (inotify на Linux, FSEvents
на macOS — через watchdog) и дополнительно раз в 5 секунд делает ``stat``
по файлам всех записей. Если ни одного файла торрента на диске не осталось,
запись удаляется через колбэк демона — на живом процессе, без перезапуска
(BEH-15, BEH-16).

Файлы качающегося торрента появляются на диске не сразу (libtorrent создаёт
их с первым записанным куском), поэтому «пропажей» считается только
исчезновение того, что уже было: запись становится кандидатом на удаление,
когда её файлы хоть раз виделись на диске, либо сразу — для записей со
статусом ``done`` (их файлы заведомо существовали).
"""

from __future__ import annotations

import contextlib
import os
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.api import ObservedWatch

# Тесты ускоряют опрос (и у демонов-подпроцессов) через MT_WATCH_INTERVAL.
DEFAULT_POLL_INTERVAL = 5.0


@dataclass(frozen=True)
class WatchedEntry:
    """Срез записи состояния — только то, что нужно для проверки диска."""

    infohash: str
    download_dir: str
    files: list[str] = field(default_factory=list)
    done: bool = False


class _KickHandler(FileSystemEventHandler):
    """Любое исчезновение в каталоге загрузок — повод проверить немедленно."""

    def __init__(self, kick: Callable[[], None]) -> None:
        self._kick = kick

    def on_deleted(self, event: FileSystemEvent) -> None:
        self._kick()

    def on_moved(self, event: FileSystemEvent) -> None:
        self._kick()


class Watcher:
    """Фоновый поток: опрос по таймеру, fs-события лишь будят его раньше.

    ``entries`` отдаёт текущие записи, ``on_all_missing`` вызывается для
    каждой записи, у которой на диске не осталось ни одного файла.
    """

    def __init__(
        self,
        entries: Callable[[], list[WatchedEntry]],
        on_all_missing: Callable[[str], None],
        poll_interval: float | None = None,
    ) -> None:
        if poll_interval is None:
            poll_interval = float(os.environ.get("MT_WATCH_INTERVAL", DEFAULT_POLL_INTERVAL))
        self._entries = entries
        self._on_all_missing = on_all_missing
        self._interval = poll_interval
        self._seen: set[str] = set()
        self._kick = threading.Event()
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None
        self._observer: Observer | None = None
        self._watches: dict[str, ObservedWatch] = {}

    # --- жизненный цикл ----------------------------------------------------

    def start(self) -> None:
        try:
            observer = Observer()
            observer.daemon = True
            observer.start()
            self._observer = observer
        except (OSError, RuntimeError) as exc:
            # без подписки остаётся опрос раз в interval — поведение то же,
            # только реакция медленнее
            print(f"mt: подписка на файловые события не работает: {exc}", file=sys.stderr)
        self._thread = threading.Thread(target=self._loop, name="mt-watcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        self._kick.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=5)
        observer, self._observer = self._observer, None
        if observer is not None:
            observer.stop()
            observer.join(timeout=5)
        self._watches.clear()

    def kick(self) -> None:
        """Будит поток для немедленной проверки (fs-событие или тест)."""
        self._kick.set()

    # --- проверка ----------------------------------------------------------

    def _loop(self) -> None:
        while not self._stopping.is_set():
            try:
                self.check_now()
            except Exception as exc:  # поток слежения не должен умирать
                print(f"mt: ошибка слежения за диском: {exc}", file=sys.stderr)
            self._kick.wait(timeout=self._interval)
            self._kick.clear()

    def check_now(self) -> None:
        """Один проход: обновить подписки и найти записи без файлов на диске."""
        entries = self._entries()
        self._sync_watches({entry.download_dir for entry in entries})
        live = {entry.infohash for entry in entries}
        self._seen &= live  # записи, удалённые другим путём, не копятся
        for entry in entries:
            if not entry.files:
                continue  # magnet без метаданных: проверять нечего
            root = Path(entry.download_dir)
            if any((root / name).exists() for name in entry.files):
                self._seen.add(entry.infohash)
            elif entry.done or entry.infohash in self._seen:
                self._seen.discard(entry.infohash)
                self._on_all_missing(entry.infohash)

    def _sync_watches(self, directories: set[str]) -> None:
        """Подписки следуют за каталогами записей: download_dir меняется (D-05)."""
        observer = self._observer
        if observer is None:
            return
        for gone in set(self._watches) - directories:
            observer.unschedule(self._watches.pop(gone))
        handler = _KickHandler(self.kick)
        for added in directories - set(self._watches):
            # каталога может уже не быть — опрос заметит пропажу файлов и так
            with contextlib.suppress(OSError):
                self._watches[added] = observer.schedule(handler, added, recursive=True)
