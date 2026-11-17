"""``state.json``: чтение, атомарная запись, снапшот (D-06).

Запись ведётся через временный файл в том же каталоге и ``os.replace``,
поэтому читатель никогда не видит полуразрушенный JSON.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

STATUS_DOWNLOADING = "downloading"
STATUS_DONE = "done"


class StateError(Exception):
    """Файл состояния существует, но прочитать его не удалось."""


@dataclass
class Entry:
    infohash: str
    name: str
    source: str
    download_dir: str
    files: list[str] = field(default_factory=list)
    total_bytes: int = 0
    status: str = STATUS_DOWNLOADING
    added_at: str = ""
    completed_at: str | None = None


def load(path: Path) -> list[Entry]:
    """Читает список закачек; отсутствующий файл — пустое состояние."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    try:
        data = json.loads(raw)
        return [Entry(**item) for item in data["entries"]]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise StateError(f"не удалось разобрать {path}: {exc}") from exc


def save(path: Path, entries: list[Entry]) -> None:
    """Атомарно пишет состояние: temp-файл рядом + ``os.replace``."""
    payload = json.dumps(
        {"entries": [asdict(entry) for entry in entries]},
        ensure_ascii=False,
        indent=2,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".state-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
        _fsync_dir(path.parent)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)
        raise


def _fsync_dir(directory: Path) -> None:
    """Сбрасывает каталог на диск: без этого rename может не пережить сбой питания."""
    dir_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
