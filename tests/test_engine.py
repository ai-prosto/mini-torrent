"""BEH-01, BEH-02, BEH-05, BEH-17, BEH-18: движок закачки.

Сетевые сценарии гоняются целиком на loopback: сид — отдельная сессия
libtorrent, наш демон качает у него напрямую через ``connect_peer``
(DHT и трекеров в тестах нет).
"""

from __future__ import annotations

import contextlib
import json
import os
import time
import warnings
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from mt import cli, paths
from mt.daemon import engine as engine_mod
from mt.daemon.daemon import Daemon
from mt.ipc.client import Client

with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    import libtorrent as lt

MAGNET = "magnet:?xt=urn:btih:" + "e" * 40 + "&dn=ubuntu.iso"
# ровно один блок libtorrent: кусок либо скачан целиком, либо нет — процент
# перед kill состоит только из проверяемых кусков (важно для теста BEH-11)
PIECE_SIZE = 16384
TIMEOUT = 60.0


# --- вспомогательное -------------------------------------------------------


@dataclass(frozen=True)
class MadeTorrent:
    torrent_path: Path
    content_dir: Path
    payload: Path
    infohash: str
    name: str
    size: int


def make_torrent(root: Path, name: str, size: int = 4 * PIECE_SIZE) -> MadeTorrent:
    """Готовит файл со случайным содержимым и v1-``.torrent`` на него."""
    content_dir = root / f"src-{name}"
    content_dir.mkdir(parents=True, exist_ok=True)
    payload = content_dir / name
    payload.write_bytes(os.urandom(size))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        storage = lt.file_storage()
        lt.add_files(storage, str(payload))
        creator = lt.create_torrent(storage, PIECE_SIZE, flags=lt.create_torrent.v1_only)
        lt.set_piece_hashes(creator, str(content_dir))
        data = lt.bencode(creator.generate())
    torrent_path = root / f"{name}.torrent"
    torrent_path.write_bytes(data)
    infohash = str(lt.torrent_info(str(torrent_path)).info_hash())
    return MadeTorrent(torrent_path, content_dir, payload, infohash, name, size)


def local_session() -> lt.session:
    """Чужой пир (сид или личер) на loopback, без DHT и внешних интерфейсов."""
    return lt.session(
        {
            "listen_interfaces": "127.0.0.1:0",
            "enable_dht": False,
            "enable_lsd": False,
            "enable_upnp": False,
            "enable_natpmp": False,
            "alert_mask": 0,
        }
    )


@contextlib.contextmanager
def seeding(made: MadeTorrent) -> Iterator[int]:
    """Поднимает сид-сессию на loopback и отдаёт её порт."""
    session = local_session()
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


def wait_until(predicate: Callable[[], bool], timeout: float = TIMEOUT) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(f"условие не наступило за {timeout} с")


def list_entries(client: Client) -> list[dict]:
    return client.list()["entries"]


def entry_status(client: Client, infohash: str) -> str | None:
    for entry in list_entries(client):
        if entry["infohash"] == infohash:
            return entry["status"]
    return None


def wait_active(daemon: Daemon, infohash: str) -> None:
    """Ждёт, пока очередь libtorrent распаузит свежедобавленный торрент.

    ``connect_peer`` до этого момента теряется, и качать пришлось бы ждать
    таймера переподключения.
    """

    def active() -> bool:
        stats = daemon.engine.poll().get(infohash)
        return stats is not None and not stats.paused

    wait_until(active)


def download_to_done(daemon: Daemon, client: Client, made: MadeTorrent, seed_port: int) -> None:
    """Добавляет торрент, качает у сида и ждёт перехода записи в ``done``."""
    assert client.add(str(made.torrent_path))["ok"] is True
    wait_active(daemon, made.infohash)
    daemon.engine.connect_peer(made.infohash, "127.0.0.1", seed_port)
    wait_until(lambda: entry_status(client, made.infohash) == "done")


# --- BEH-01: mt add <magnet> ----------------------------------------------


def test_add_magnet_exit_zero_and_listed(
    daemon: Daemon, client: Client, capsys: pytest.CaptureFixture[str]
) -> None:
    """BEH-01: команда печатает имя, выходит с кодом 0, запись видна в списке."""
    code = cli.main(["add", MAGNET])
    captured = capsys.readouterr()
    assert code == 0
    assert captured.out.strip() == "ubuntu.iso"
    assert captured.err == ""

    entries = list_entries(client)
    assert [entry["name"] for entry in entries] == ["ubuntu.iso"]
    assert entries[0]["status"] == "downloading"
    assert entries[0]["infohash"] == "e" * 40
    assert daemon.engine.has("e" * 40)  # торрент действительно заведён в движок


# --- BEH-02: mt add <файл.torrent> ----------------------------------------


def test_add_torrent_file(
    daemon: Daemon, client: Client, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """BEH-02: закачка заводится из metainfo файла; плохой путь — код 1 и stderr."""
    made = make_torrent(tmp_path, "arch.iso")

    code = cli.main(["add", str(made.torrent_path)])
    captured = capsys.readouterr()
    assert code == 0
    assert captured.out.strip() == made.name

    (entry,) = list_entries(client)
    assert entry["infohash"] == made.infohash
    assert entry["total_bytes"] == made.size
    assert entry["files"] == [made.name]
    assert entry["status"] == "downloading"
    # копия metainfo сохранена для будущего возобновления (D-06)
    assert (paths.meta_dir() / f"{made.infohash}.torrent").is_file()

    code = cli.main(["add", str(tmp_path / "nope.torrent")])
    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    assert captured.err.startswith("mt: ")
    assert "не найден" in captured.err


# --- BEH-05: параллельность без очереди ------------------------------------


def test_three_adds_all_downloading(daemon: Daemon, client: Client, tmp_path: Path) -> None:
    """BEH-05: три закачки подряд активны одновременно, ни одна не ждёт слота."""
    infohashes = []
    for index in range(3):
        made = make_torrent(tmp_path, f"t{index}.bin")
        assert client.add(str(made.torrent_path))["ok"] is True
        infohashes.append(made.infohash)

    # очередь libtorrent распаузивает торренты на своём тике — дожидаемся всех
    wait_until(
        lambda: all(
            infohash in daemon.engine.poll() and not daemon.engine.poll()[infohash].paused
            for infohash in infohashes
        )
    )

    statuses = [entry["status"] for entry in list_entries(client)]
    assert statuses == ["downloading"] * 3
    stats = daemon.engine.poll()
    assert all(not stats[infohash].paused for infohash in infohashes)
    assert all(not stats[infohash].is_complete for infohash in infohashes)


def test_many_adds_none_queued(daemon: Daemon, client: Client, tmp_path: Path) -> None:
    """D-09: лимит активных отключён — и пять закачек идут без очереди."""
    infohashes = []
    for index in range(5):
        made = make_torrent(tmp_path, f"q{index}.bin", size=PIECE_SIZE)
        assert client.add(str(made.torrent_path))["ok"] is True
        infohashes.append(made.infohash)

    def all_active() -> bool:
        stats = daemon.engine.poll()
        return all(infohash in stats and not stats[infohash].paused for infohash in infohashes)

    wait_until(all_active)


# --- BEH-17: 100% -> done, торрент выброшен --------------------------------


def test_complete_sets_done_and_drops(daemon: Daemon, client: Client, tmp_path: Path) -> None:
    """BEH-17: на 100% статус ``done``, Drop, соединений с пирами нет."""
    made = make_torrent(tmp_path, "movie.bin", size=16 * PIECE_SIZE)
    with seeding(made) as seed_port:
        download_to_done(daemon, client, made, seed_port)

    # файл действительно скачан и совпадает с исходным
    downloaded = Path(list_entries(client)[0]["download_dir"]) / made.name
    assert downloaded.read_bytes() == made.payload.read_bytes()

    # торрент выброшен из движка — соединения по нему закрыты
    assert not daemon.engine.has(made.infohash)
    assert made.infohash not in daemon.engine.poll()

    (entry,) = list_entries(client)
    assert entry["status"] == "done"
    assert entry["completed_at"] is not None
    assert entry["progress"] == 100.0
    assert entry["num_peers"] == 0

    # done записан и в state.json, не только в память
    saved = json.loads(paths.state_file().read_text(encoding="utf-8"))
    assert saved["entries"][0]["status"] == "done"


# --- BEH-18: после завершения раздачи нет ----------------------------------


def test_no_upload_after_done(daemon: Daemon, client: Client, tmp_path: Path) -> None:
    """BEH-18: сторонний личер после завершения не получает ни байта."""
    made = make_torrent(tmp_path, "album.bin", size=8 * PIECE_SIZE)
    with seeding(made) as seed_port:
        download_to_done(daemon, client, made, seed_port)

    leecher = local_session()
    params = lt.add_torrent_params()
    params.ti = lt.torrent_info(str(made.torrent_path))
    params.save_path = str(tmp_path / "leech")
    handle = leecher.add_torrent(params)
    handle.connect_peer(("127.0.0.1", daemon.engine.listen_port()))
    time.sleep(2.0)

    assert handle.status().total_payload_download == 0
    assert handle.status().progress == 0.0
    (entry,) = list_entries(client)
    assert entry["uploaded_bytes"] == 0
    leecher.pause()


def test_magnet_with_peer_fetches_metadata_and_completes(
    daemon: Daemon, client: Client, tmp_path: Path
) -> None:
    """BEH-01 целиком: magnet без метаданных добирает их у пира и докачивается."""
    made = make_torrent(tmp_path, "distro.bin", size=4 * PIECE_SIZE)
    magnet = f"magnet:?xt=urn:btih:{made.infohash}&dn={made.name}"
    with seeding(made) as seed_port:
        assert client.add(magnet)["ok"] is True
        wait_active(daemon, made.infohash)
        daemon.engine.connect_peer(made.infohash, "127.0.0.1", seed_port)
        wait_until(lambda: entry_status(client, made.infohash) == "done")

    (entry,) = list_entries(client)
    assert entry["total_bytes"] == made.size  # метаданные приехали из роя
    assert entry["files"] == [made.name]
    # копия metainfo сохранена, хотя исходного .torrent-файла не было (D-06)
    assert (paths.meta_dir() / f"{made.infohash}.torrent").is_file()


# --- движок напрямую -------------------------------------------------------


@pytest.fixture
def engine(state_home: Path) -> Iterator[engine_mod.Engine]:
    instance = engine_mod.Engine()
    yield instance
    instance.close()


def test_engine_add_missing_file_raises(engine: engine_mod.Engine, tmp_path: Path) -> None:
    with pytest.raises(engine_mod.EngineError, match="не найден"):
        engine.add(str(tmp_path / "nope.torrent"), tmp_path)


def test_engine_add_invalid_torrent_raises(engine: engine_mod.Engine, tmp_path: Path) -> None:
    bad = tmp_path / "bad.torrent"
    bad.write_bytes(b"d4:infod4:name4:archeee")
    with pytest.raises(engine_mod.EngineError, match="не удалось разобрать"):
        engine.add(str(bad), tmp_path)


def test_engine_add_invalid_magnet_raises(engine: engine_mod.Engine, tmp_path: Path) -> None:
    with pytest.raises(engine_mod.EngineError, match="magnet"):
        engine.add("magnet:?xt=urn:btih:zzzz", tmp_path)


def test_engine_duplicate_add_is_not_an_error(
    engine: engine_mod.Engine, tmp_path: Path
) -> None:
    made = make_torrent(tmp_path, "dup.bin")
    first = engine.add(str(made.torrent_path), tmp_path)
    second = engine.add(str(made.torrent_path), tmp_path)
    assert first.infohash == second.infohash == made.infohash
    assert len(engine.poll()) == 1


def test_engine_drop_unknown_is_noop(engine: engine_mod.Engine) -> None:
    engine.drop("f" * 40)
    assert not engine.has("f" * 40)


def test_engine_connect_peer_unknown_raises(engine: engine_mod.Engine) -> None:
    with pytest.raises(engine_mod.EngineError, match="нет такого"):
        engine.connect_peer("f" * 40, "127.0.0.1", 6881)


def test_engine_closed_rejects_add(engine: engine_mod.Engine, tmp_path: Path) -> None:
    engine.close()
    engine.close()  # повторный close — no-op
    assert engine.poll() == {}
    with pytest.raises(engine_mod.EngineError, match="остановлен"):
        engine.add(MAGNET, tmp_path)


def test_engine_stats_shape_for_fresh_torrent(
    engine: engine_mod.Engine, tmp_path: Path
) -> None:
    made = make_torrent(tmp_path, "shape.bin")
    added = engine.add(str(made.torrent_path), tmp_path / "dl")
    assert added.name == made.name
    assert added.total_bytes == made.size
    assert added.files == [made.name]

    stats = engine.poll()[made.infohash]
    assert stats.name == made.name
    assert stats.total_bytes == made.size
    assert stats.uploaded_bytes == 0
    assert stats.has_metadata is True
    assert stats.is_complete is False
    assert stats.files == [made.name]
