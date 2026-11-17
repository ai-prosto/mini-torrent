"""Обёртка над libtorrent: add, прогресс, drop (D-01, D-09, D-10, D-13, D-14).

Каждый добавленный торрент сразу отдаётся движку, очереди нет (D-09):
лимиты активных торрентов отключены. Раздача выключена глобально (D-10):
``unchoke_slots_limit = 0`` — пиры никогда не анчокаются, блоки не отдаются;
по завершении торрент выбрасывается из движка, и соединения закрываются.
Сеть (D-14): DHT и LSD включены, порт случайный или ``listen_port`` из
конфига, UPnP/NAT-PMP не нужны — для скачивания хватает исходящих соединений.
"""

from __future__ import annotations

import contextlib
import threading
from dataclasses import dataclass, field
from pathlib import Path

import libtorrent as lt

from mt import paths

# Тесты подменяют на True: движок без DHT/LSD, слушает только loopback.
LOCAL_ONLY_DEFAULT = False


class EngineError(Exception):
    """Источник не удалось превратить в торрент движка."""


@dataclass(frozen=True)
class AddedTorrent:
    """Результат добавления: что известно о торренте сразу после ``add``."""

    infohash: str
    name: str
    total_bytes: int
    files: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class TorrentStats:
    """Живой срез одного торрента; считается на лету, на диск не пишется."""

    infohash: str
    name: str
    total_bytes: int
    downloaded_bytes: int
    uploaded_bytes: int
    download_rate: int
    upload_rate: int
    progress: float  # проценты, 0..100
    num_peers: int
    has_metadata: bool
    is_complete: bool
    paused: bool
    files: list[str] = field(default_factory=list)


class Engine:
    """Сессия libtorrent и хэндлы добавленных торрентов."""

    def __init__(self, listen_port: int | None = None, local_only: bool | None = None) -> None:
        if local_only is None:
            local_only = LOCAL_ONLY_DEFAULT
        interface = "127.0.0.1" if local_only else "0.0.0.0"
        self._session: lt.session | None = lt.session(
            {
                "listen_interfaces": f"{interface}:{listen_port or 0}",
                "enable_dht": not local_only,
                "enable_lsd": not local_only,
                "enable_upnp": False,  # D-14: проброс портов не нужен
                "enable_natpmp": False,
                "unchoke_slots_limit": 0,  # D-10: блоки не отдаём никому
                "active_downloads": -1,  # D-09: никакой очереди
                "active_seeds": -1,
                "active_limit": -1,
                "alert_mask": 0,
            }
        )
        self._lock = threading.Lock()
        self._handles: dict[str, lt.torrent_handle] = {}
        self._meta_dir = paths.meta_dir()

    # --- жизненный цикл --------------------------------------------------

    def close(self) -> None:
        """Останавливает сессию; все соединения закрываются."""
        session, self._session = self._session, None
        if session is None:
            return
        with self._lock:
            self._handles.clear()
        session.pause()

    def listen_port(self) -> int:
        return self._require_session().listen_port()

    # --- добавление -------------------------------------------------------

    def add(self, source: str, download_dir: Path) -> AddedTorrent:
        """Заводит magnet-ссылку или ``.torrent``-файл в движок (D-13).

        Повторное добавление того же инфохэша — не ошибка: возвращается
        уже работающий торрент.
        """
        session = self._require_session()
        if source.startswith("magnet:"):
            try:
                params = lt.parse_magnet_uri(source)
            except RuntimeError as exc:
                raise EngineError(f"не удалось разобрать magnet-ссылку: {exc}") from exc
        else:
            params = lt.add_torrent_params()
            params.ti = self._load_metainfo(Path(source))
        params.save_path = str(download_dir)
        params.flags &= ~lt.torrent_flags.duplicate_is_error

        handle = session.add_torrent(params)
        if params.ti is not None:
            info = params.ti
            infohash = str(info.info_hash())
            name = info.name() or infohash
            total_bytes = info.total_size()
            files = _file_list(info)
            self._save_metainfo_copy(infohash, info)
        else:
            infohash = str(params.info_hashes.v1)
            name = params.name or infohash
            total_bytes = 0
            files = []
        with self._lock:
            self._handles[infohash] = handle
        return AddedTorrent(infohash=infohash, name=name, total_bytes=total_bytes, files=files)

    @staticmethod
    def _load_metainfo(path: Path) -> lt.torrent_info:
        if not path.is_file():
            raise EngineError(f"файл не найден: {path}")
        try:
            return lt.torrent_info(str(path))
        except RuntimeError as exc:
            raise EngineError(f"не удалось разобрать {path}: {exc}") from exc

    def _save_metainfo_copy(self, infohash: str, info: lt.torrent_info) -> None:
        """Кладёт копию metainfo в ``meta/`` (D-06): резюме не зависит от источника."""
        target = self._meta_dir / f"{infohash}.torrent"
        if target.exists():
            return
        self._meta_dir.mkdir(parents=True, exist_ok=True)
        params = lt.add_torrent_params()
        params.ti = info
        target.write_bytes(lt.bencode(lt.write_torrent_file(params)))

    # --- прогресс и завершение ---------------------------------------------

    def poll(self) -> dict[str, TorrentStats]:
        """Срез всех активных торрентов; у магнитов по пути сохраняет metainfo."""
        if self._session is None:
            return {}
        with self._lock:
            handles = dict(self._handles)
        stats: dict[str, TorrentStats] = {}
        for infohash, handle in handles.items():
            if not handle.is_valid():
                continue
            status = handle.status()
            files: list[str] = []
            total_bytes = 0
            if status.has_metadata:
                info = handle.torrent_file()
                files = _file_list(info)
                total_bytes = info.total_size()
                # метаданные магнита приехали из роя — сохраняем копию (D-06)
                with contextlib.suppress(OSError, RuntimeError):
                    self._save_metainfo_copy(infohash, info)
            stats[infohash] = TorrentStats(
                infohash=infohash,
                name=status.name or infohash,
                total_bytes=total_bytes,
                downloaded_bytes=status.total_wanted_done,
                uploaded_bytes=status.total_payload_upload,
                download_rate=status.download_payload_rate,
                upload_rate=status.upload_payload_rate,
                progress=status.progress * 100.0,
                num_peers=status.num_peers,
                has_metadata=status.has_metadata,
                is_complete=status.has_metadata
                and status.state in (lt.torrent_status.finished, lt.torrent_status.seeding),
                paused=bool(status.flags & lt.torrent_flags.paused),
                files=files,
            )
        return stats

    def drop(self, infohash: str) -> None:
        """Выбрасывает торрент из движка: соединения с пирами закрываются,
        трекеру уходит ``event=stopped`` (D-10). Незнакомый инфохэш — no-op."""
        session = self._session
        with self._lock:
            handle = self._handles.pop(infohash, None)
        if session is not None and handle is not None and handle.is_valid():
            session.remove_torrent(handle)

    def has(self, infohash: str) -> bool:
        with self._lock:
            return infohash in self._handles

    def connect_peer(self, infohash: str, host: str, port: int) -> None:
        """Явное подключение к пиру — для отладки и тестов без DHT/трекера."""
        with self._lock:
            handle = self._handles.get(infohash)
        if handle is None or not handle.is_valid():
            raise EngineError(f"нет такого торрента: {infohash}")
        handle.connect_peer((host, port))

    def _require_session(self) -> lt.session:
        if self._session is None:
            raise EngineError("движок остановлен")
        return self._session


def _file_list(info: lt.torrent_info) -> list[str]:
    layout = info.layout()
    return [layout.file_path(index) for index in range(layout.num_files())]
