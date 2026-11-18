"""BEH-15, BEH-16: слежение за диском и удаление пропавших записей (D-11).

Сценарии гоняются на настоящих закачках по loopback (сид — отдельная сессия
libtorrent), а логика watcher дополнительно проверяется напрямую, без потоков
и демона — через ``check_now()``.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from mt import paths
from mt.daemon import watcher as watcher_mod
from mt.daemon.daemon import Daemon
from mt.ipc.client import Client
from tests.test_daemon_lifecycle import (
    client_progress,
    daemon_pid,
    lifecycle_home,  # noqa: F401 — фикстура нужна тесту живого демона
    mt_command,
    peer_magnet,
    slow_seed,
)
from tests.test_engine import (
    PIECE_SIZE,
    download_to_done,
    list_entries,
    make_torrent,
    seeding,
    wait_active,
    wait_until,
)


@pytest.fixture(autouse=True)
def _fast_watch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Быстрый подстраховочный опрос: тесты не ждут продакшновые 5 секунд.

    Через окружение интервал доезжает и до демонов-подпроцессов.
    """
    monkeypatch.setenv("MT_WATCH_INTERVAL", "0.2")


def saved_entries() -> list[dict]:
    return json.loads(paths.state_file().read_text(encoding="utf-8"))["entries"]


def wait_progress_settled(infohash: str) -> float:
    """После остановки сида ждёт, пока долетевшие блоки осядут на диск."""
    deadline = time.monotonic() + 30
    prev = client_progress(infohash)
    while time.monotonic() < deadline:
        time.sleep(0.5)
        now = client_progress(infohash)
        if now == prev:
            break
        prev = now
    return prev


# --- BEH-15: удалённый файл пропадает из состояния и снапшота ---------------


def test_deleted_file_removed_from_state_and_snapshot(
    daemon: Daemon, client: Client, tmp_path: Path
) -> None:
    """BEH-15: файлы скачанного торрента стёрты — записи нет ни в state.json,
    ни в снапшоте, который уходит в TUI."""
    made = make_torrent(tmp_path, "season.bin", size=8 * PIECE_SIZE)
    with seeding(made) as seed_port:
        download_to_done(daemon, client, made, seed_port)

    (entry,) = list_entries(client)
    downloaded = Path(entry["download_dir"]) / made.name
    assert downloaded.is_file()
    assert saved_entries()  # запись была и на диске

    downloaded.unlink()

    wait_until(lambda: list_entries(client) == [])
    assert saved_entries() == []  # пропала из state.json, не только из памяти
    # ближайший снапшот подписки уходит уже без записи
    with Client(paths.socket_path()) as subscriber:
        snapshot = next(iter(subscriber.subscribe()))
    assert snapshot["entries"] == []


# --- BEH-16: пропажа записи без перезапуска демона --------------------------


def test_removal_on_live_daemon(lifecycle_home: Path, tmp_path: Path) -> None:  # noqa: F811
    """BEH-16: файлы стёрты при живом демоне — тот же PID отдаёт снапшот
    без записи, процесс демона не перезапускался."""
    made = make_torrent(tmp_path, "livedel.bin", size=512 * PIECE_SIZE)
    paths.ensure_state_dir()
    with open(paths.log_path(), "ab") as log:
        proc = subprocess.Popen(mt_command("daemon"), stdout=log, stderr=log)
    try:
        wait_until(lambda: paths.socket_path().exists())
        pid = daemon_pid()

        with slow_seed(made, upload_limit=200_000) as seed_port:
            with Client(paths.socket_path()) as client:
                assert client.add(peer_magnet(made, seed_port))["ok"] is True
            # застаём закачку на полпути: часть кусков уже на диске
            wait_until(lambda: 0 < client_progress(made.infohash) < 100)
        # сид остановлен; долетевшие блоки должны осесть до удаления файла,
        # иначе поздняя запись куска воскресит файл
        assert 0 < wait_progress_settled(made.infohash) < 100

        payload = paths.default_download_dir() / made.name
        assert payload.is_file()
        payload.unlink()

        def entry_gone() -> bool:
            with Client(paths.socket_path()) as client:
                return client.list()["entries"] == []

        wait_until(entry_gone)
        assert saved_entries() == []
        # процесс демона тот же: никто его не перезапускал
        assert daemon_pid() == pid
        os.kill(pid, 0)
        assert proc.poll() is None
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_active_torrent_dropped_from_engine(
    daemon: Daemon, client: Client, tmp_path: Path
) -> None:
    """BEH-16: ещё качающийся торрент после пропажи файлов выброшен из движка."""
    made = make_torrent(tmp_path, "midflight.bin", size=512 * PIECE_SIZE)
    with slow_seed(made, upload_limit=200_000) as seed_port:
        assert client.add(str(made.torrent_path))["ok"] is True
        wait_active(daemon, made.infohash)
        daemon.engine.connect_peer(made.infohash, "127.0.0.1", seed_port)
        wait_until(lambda: 0 < client_progress(made.infohash) < 100)
    assert 0 < wait_progress_settled(made.infohash) < 100

    (entry,) = list_entries(client)
    assert entry["status"] == "downloading"
    payload = Path(entry["download_dir"]) / made.name
    payload.unlink()

    wait_until(lambda: list_entries(client) == [])
    assert saved_entries() == []
    assert not daemon.engine.has(made.infohash)  # выброшен на живом демоне
    assert made.infohash not in daemon.engine.poll()


# --- логика watcher напрямую, без потоков -----------------------------------


class Recorder:
    """Подменный демон: отдаёт записи и копит, что watcher велел удалить."""

    def __init__(self, entries: list[watcher_mod.WatchedEntry]) -> None:
        self.entries = entries
        self.removed: list[str] = []

    def list_entries(self) -> list[watcher_mod.WatchedEntry]:
        return list(self.entries)

    def remove(self, infohash: str) -> None:
        self.removed.append(infohash)
        self.entries = [entry for entry in self.entries if entry.infohash != infohash]


def make_watcher(recorder: Recorder, poll_interval: float = 60.0) -> watcher_mod.Watcher:
    return watcher_mod.Watcher(recorder.list_entries, recorder.remove, poll_interval)


def test_watcher_ignores_entry_without_files(tmp_path: Path) -> None:
    """Magnet без метаданных: файлов ещё нет, удалять нечего."""
    recorder = Recorder([watcher_mod.WatchedEntry("a" * 40, str(tmp_path), files=[])])
    watcher = make_watcher(recorder)
    watcher.check_now()
    assert recorder.removed == []


def test_watcher_waits_until_files_seen(tmp_path: Path) -> None:
    """Файлы качающегося торрента ещё не созданы движком — это не пропажа;
    пропажа — когда то, что виделось на диске, исчезло."""
    entry = watcher_mod.WatchedEntry("b" * 40, str(tmp_path), files=["movie.bin"])
    recorder = Recorder([entry])
    watcher = make_watcher(recorder)

    watcher.check_now()  # файла ещё нет: свежая закачка не удаляется
    assert recorder.removed == []

    payload = tmp_path / "movie.bin"
    payload.write_bytes(b"x")
    watcher.check_now()  # файл появился и замечен
    assert recorder.removed == []

    payload.unlink()
    watcher.check_now()
    assert recorder.removed == ["b" * 40]


def test_watcher_done_entry_removed_immediately(tmp_path: Path) -> None:
    """У записи ``done`` файлы заведомо существовали: пропажу видно и после
    перезапуска демона, без предварительного «виделся на диске»."""
    entry = watcher_mod.WatchedEntry("c" * 40, str(tmp_path), files=["iso.img"], done=True)
    recorder = Recorder([entry])
    watcher = make_watcher(recorder)
    watcher.check_now()
    assert recorder.removed == ["c" * 40]


def test_watcher_keeps_entry_while_any_file_remains(tmp_path: Path) -> None:
    """Многофайловый торрент живёт, пока на диске есть хоть один его файл."""
    (tmp_path / "album").mkdir()
    first = tmp_path / "album" / "01.mp3"
    second = tmp_path / "album" / "02.mp3"
    first.write_bytes(b"x")
    second.write_bytes(b"x")
    entry = watcher_mod.WatchedEntry(
        "d" * 40, str(tmp_path), files=["album/01.mp3", "album/02.mp3"], done=True
    )
    recorder = Recorder([entry])
    watcher = make_watcher(recorder)

    first.unlink()
    watcher.check_now()
    assert recorder.removed == []

    second.unlink()
    watcher.check_now()
    assert recorder.removed == ["d" * 40]


def test_watcher_forgets_entries_removed_elsewhere(tmp_path: Path) -> None:
    """Запись ушла другим путём — её след в «виденных» не копится вечно."""
    payload = tmp_path / "gone.bin"
    payload.write_bytes(b"x")
    entry = watcher_mod.WatchedEntry("e" * 40, str(tmp_path), files=["gone.bin"])
    recorder = Recorder([entry])
    watcher = make_watcher(recorder)
    watcher.check_now()
    assert "e" * 40 in watcher._seen

    recorder.entries = []  # запись удалили не через watcher
    watcher.check_now()
    assert "e" * 40 not in watcher._seen


def test_watcher_thread_reacts_to_deletion(tmp_path: Path) -> None:
    """Поток watcher с fsnotify-подпиской замечает удаление сам, без опроса
    (опрос стоит заведомо реже, чем таймаут ожидания)."""
    payload = tmp_path / "quick.bin"
    payload.write_bytes(b"x")
    entry = watcher_mod.WatchedEntry("f" * 40, str(tmp_path), files=["quick.bin"], done=True)
    recorder = Recorder([entry])
    watcher = make_watcher(recorder, poll_interval=30.0)
    watcher.start()
    try:
        wait_until(lambda: watcher._watches != {}, timeout=5)
        payload.unlink()
        wait_until(lambda: recorder.removed == ["f" * 40], timeout=10)
    finally:
        watcher.stop()


def test_watcher_missing_download_dir(tmp_path: Path) -> None:
    """Снесли весь каталог загрузок: подписаться не на что, опрос всё равно
    замечает пропажу."""
    gone_dir = tmp_path / "nope"
    entry = watcher_mod.WatchedEntry("1" * 40, str(gone_dir), files=["x.bin"], done=True)
    recorder = Recorder([entry])
    watcher = make_watcher(recorder)
    watcher.start()  # подписка на несуществующий каталог делу не мешает
    try:
        wait_until(lambda: recorder.removed == ["1" * 40], timeout=10)
    finally:
        watcher.stop()
