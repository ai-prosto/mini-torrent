"""BEH-07, BEH-08, BEH-09, BEH-11: жизненный цикл демона.

Демон здесь — настоящий подпроцесс ``python -m mt daemon`` (его можно убить
SIGKILL и пережить закрытие «терминала» клиента); сид — отдельная сессия
libtorrent на loopback, до которой торрент добирается через ``x.pe`` в
magnet-ссылке (DHT и трекеров в тестах нет). BEH-10 и BEH-20 проверяются
вручную: docs/manual/reboot-resume.md и docs/manual/ssh-detach.md.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import signal
import subprocess
import sys
import time
import warnings
from collections.abc import Iterator
from pathlib import Path

import pytest

from mt import paths
from mt.daemon.daemon import Daemon
from mt.ipc.client import Client
from tests.test_engine import (
    PIECE_SIZE,
    MadeTorrent,
    local_session,
    make_torrent,
    seeding,
    wait_until,
)

with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    import libtorrent as lt

CLIENT_TIMEOUT = 60


@pytest.fixture
def lifecycle_home(state_home: Path) -> Iterator[Path]:
    """Изолированное состояние + добивание осиротевшего демона-подпроцесса."""
    yield state_home
    with contextlib.suppress(OSError, ValueError):
        pid = int(paths.lock_path().read_text(encoding="ascii"))
        if pid != os.getpid():  # в лок мог записаться демон внутри самого pytest
            os.kill(pid, signal.SIGKILL)


def mt_command(*args: str) -> list[str]:
    return [sys.executable, "-m", "mt", *args]


def daemon_pid() -> int:
    return int(paths.lock_path().read_text(encoding="ascii"))


def peer_magnet(made: MadeTorrent, seed_port: int) -> str:
    """Magnet с адресом сида: пир находится без DHT и трекеров."""
    return f"magnet:?xt=urn:btih:{made.infohash}&dn={made.name}&x.pe=127.0.0.1:{seed_port}"


@contextlib.contextmanager
def slow_seed(made: MadeTorrent, upload_limit: int) -> Iterator[int]:
    """Сид с ограниченной отдачей: закачку можно застать на полпути."""
    session = local_session()
    session.apply_settings(
        {
            "upload_rate_limit": upload_limit,
            # иначе libtorrent игнорирует лимиты для loopback и отдаёт всё мгновенно
            "ignore_limits_on_local_network": False,
        }
    )
    params = lt.add_torrent_params()
    params.ti = lt.torrent_info(str(made.torrent_path))
    params.save_path = str(made.content_dir)
    params.flags |= lt.torrent_flags.seed_mode
    handle = session.add_torrent(params)
    wait_until(lambda: handle.status().is_seeding)
    try:
        yield session.listen_port()
    finally:
        session.pause()


def client_progress(infohash: str) -> float:
    with Client(paths.socket_path()) as client:
        for entry in client.list()["entries"]:
            if entry["infohash"] == infohash:
                return entry["progress"]
    raise AssertionError(f"записи {infohash} нет в списке демона")


def _partial_download_then_kill(made: MadeTorrent) -> float:
    """Качает часть торрента демоном-подпроцессом и убивает его SIGKILL.

    Возвращает последний процент, который демон показал перед смертью.
    """
    paths.ensure_state_dir()
    with open(paths.log_path(), "ab") as log:
        proc = subprocess.Popen(mt_command("daemon"), stdout=log, stderr=log)
    try:
        wait_until(lambda: paths.socket_path().exists())
        with slow_seed(made, upload_limit=256_000) as seed_port:
            with Client(paths.socket_path()) as client:
                assert client.add(peer_magnet(made, seed_port))["ok"] is True
            # застаём закачку на полпути: часть кусков уже на диске
            wait_until(lambda: 0 < client_progress(made.infohash) < 100)
        # сид остановлен; ждём, пока долетевшие блоки осядут и процент замрёт
        meta = paths.meta_dir() / f"{made.infohash}.torrent"
        wait_until(meta.is_file)  # копия metainfo нужна для возобновления (D-06)
        deadline = time.monotonic() + 30
        prev = client_progress(made.infohash)
        while time.monotonic() < deadline:
            time.sleep(1.0)
            now = client_progress(made.infohash)
            if now == prev:
                break
            prev = now
        assert 0 < prev < 100, f"нужна недокачанная закачка, а прогресс {prev}%"
        return prev
    finally:
        proc.kill()  # именно SIGKILL: демон не успевает ничего сохранить
        proc.wait()


# --- BEH-07: демон живёт после закрытия терминала клиента -------------------


def test_daemon_survives_client_terminal_close(lifecycle_home: Path, tmp_path: Path) -> None:
    """BEH-07: клиент и его сессия умерли — демон жив, байты продолжают расти."""
    made = make_torrent(tmp_path, "survive.bin", size=512 * PIECE_SIZE)
    with slow_seed(made, upload_limit=200_000) as seed_port:
        # клиент в собственной сессии — её закрытие и есть «закрытие терминала»
        client_proc = subprocess.Popen(
            mt_command("add", peer_magnet(made, seed_port)),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        out, err = client_proc.communicate(timeout=CLIENT_TIMEOUT)
        assert client_proc.returncode == 0, err
        assert out.strip() == made.name

        # клиент завершён, его сессии больше нет — демон жив
        pid = daemon_pid()
        os.kill(pid, 0)
        # демон — лидер собственной сессии: SIGHUP от терминала клиента не дойдёт
        assert os.getsid(pid) == pid

        wait_until(lambda: client_progress(made.infohash) > 0)
        first = client_progress(made.infohash)
        # скачанные байты в следующих снапшотах продолжают расти
        wait_until(lambda: client_progress(made.infohash) > first)


# --- BEH-08: демон поднимается сам при первой команде mt add ----------------


def test_daemon_autospawn_on_first_add(lifecycle_home: Path) -> None:
    """BEH-08: сокета нет — два параллельных ``mt add`` поднимают одного демона."""
    assert not paths.socket_path().exists()

    magnets = {
        "one.iso": "magnet:?xt=urn:btih:" + "a" * 40 + "&dn=one.iso",
        "two.iso": "magnet:?xt=urn:btih:" + "b" * 40 + "&dn=two.iso",
    }
    procs = [
        subprocess.Popen(
            mt_command("add", magnet),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for magnet in magnets.values()
    ]
    for proc in procs:
        out, err = proc.communicate(timeout=CLIENT_TIMEOUT)
        assert proc.returncode == 0, err
        assert out.strip() in magnets

    # демон жив и ровно один: flock занят, второй претендент вышел с кодом 0
    pid = daemon_pid()
    os.kill(pid, 0)
    with open(paths.lock_path(), "ab") as fh, pytest.raises(OSError):
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    # обе записи попали в одного демона, без дублей
    with Client(paths.socket_path()) as client:
        names = sorted(entry["name"] for entry in client.list()["entries"])
    assert names == sorted(magnets)


# --- BEH-09: после kill закачки возобновляются сами -------------------------


def test_resume_after_kill_without_user_command(lifecycle_home: Path, tmp_path: Path) -> None:
    """BEH-09: SIGKILL демона; новый старт роли сам поднимает закачку и докачивает."""
    made = make_torrent(tmp_path, "resume.bin", size=512 * PIECE_SIZE)
    _partial_download_then_kill(made)

    daemon = Daemon()
    daemon.start()  # никакая команда пользователя не выполняется
    try:
        # запись downloading поднялась в движок сама
        wait_until(lambda: daemon.engine.has(made.infohash))
        with Client(paths.socket_path()) as client:
            (entry,) = client.list()["entries"]
            assert entry["status"] == "downloading"

        # и действительно качается дальше: с новым сидом доходит до done
        def ready() -> bool:
            stats = daemon.engine.poll().get(made.infohash)
            return stats is not None and not stats.is_checking and not stats.paused

        with seeding(made) as seed_port:
            wait_until(ready)
            daemon.engine.connect_peer(made.infohash, "127.0.0.1", seed_port)

            def done() -> bool:
                with Client(paths.socket_path()) as client:
                    (entry,) = client.list()["entries"]
                    return entry["status"] == "done"

            wait_until(done)

        downloaded = Path(paths.default_download_dir()) / made.name
        assert downloaded.read_bytes() == made.payload.read_bytes()
    finally:
        daemon.stop()


# --- BEH-11: процент после возобновления не ниже прежнего -------------------


def test_progress_not_lower_after_resume(lifecycle_home: Path, tmp_path: Path) -> None:
    """BEH-11: после рестарта процент ≥ прежнего — куски берутся с диска.

    Сид при возобновлении выключен, поэтому добрать проценты из сети
    невозможно: дотянуться до прежнего значения можно только проверенными
    кусками с диска.
    """
    made = make_torrent(tmp_path, "percent.bin", size=512 * PIECE_SIZE)
    before = _partial_download_then_kill(made)

    daemon = Daemon()
    daemon.start()
    try:
        wait_until(lambda: daemon.engine.has(made.infohash))
        # первый же снапшот после окончания проверки показывает процент не ниже
        wait_until(lambda: client_progress(made.infohash) >= before)
        with Client(paths.socket_path()) as client:
            (entry,) = client.list()["entries"]
            assert entry["progress"] >= before
            assert entry["status"] == "downloading"
    finally:
        daemon.stop()


# --- возобновление: краевые случаи ------------------------------------------


def test_resume_skips_done_and_survives_missing_source(
    state_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Готовые записи не поднимаются; пропавший источник — предупреждение, не отказ."""
    from mt.daemon import state as state_mod

    paths.ensure_state_dir()
    entries = [
        state_mod.Entry(
            infohash="a" * 40,
            name="ready.bin",
            source="magnet:?xt=urn:btih:" + "a" * 40,
            download_dir=str(state_home / "dl"),
            status=state_mod.STATUS_DONE,
        ),
        state_mod.Entry(
            infohash="b" * 40,
            name="lost.bin",
            source=str(state_home / "nope.torrent"),  # файла нет, meta-копии нет
            download_dir=str(state_home / "dl"),
            status=state_mod.STATUS_DOWNLOADING,
        ),
    ]
    state_mod.save(paths.state_file(), entries)

    daemon = Daemon()
    daemon.start()
    try:
        assert not daemon.engine.has("a" * 40)  # done не возобновляется
        assert not daemon.engine.has("b" * 40)  # источник пропал — запись ждёт
        err = capsys.readouterr().err
        assert "не удалось возобновить" in err
        assert "lost.bin" in err
    finally:
        daemon.stop()


def test_resume_magnet_without_meta_uses_source(state_home: Path) -> None:
    """Magnet без meta-копии возобновляется по исходной ссылке из state.json."""
    from mt.daemon import state as state_mod

    paths.ensure_state_dir()
    infohash = "c" * 40
    state_mod.save(
        paths.state_file(),
        [
            state_mod.Entry(
                infohash=infohash,
                name="pending.iso",
                source=f"magnet:?xt=urn:btih:{infohash}&dn=pending.iso",
                download_dir=str(state_home / "dl"),
            )
        ],
    )

    daemon = Daemon()
    daemon.start()
    try:
        assert daemon.engine.has(infohash)
    finally:
        daemon.stop()


# --- самозапуск: частности ---------------------------------------------------


def test_daemon_command_uses_same_interpreter() -> None:
    from mt.daemon import spawn

    command = spawn.daemon_command()
    assert command[0] == sys.executable
    assert command[-3:] == ["-m", "mt", "daemon"]
