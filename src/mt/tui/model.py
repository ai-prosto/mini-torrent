"""Состояние TUI и обработка снапшотов подписки (D-12).

Модель меняется только двумя способами: очередным снапшотом демона
(BEH-14 — без нажатий) и навигационными клавишами. Никаких действий
над закачками у TUI нет — он только смотрит.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Row:
    """Одна строка списка: ровно то, что нужно для отрисовки."""

    name: str
    status: str
    progress: float  # проценты, 0..100
    download_rate: int  # байт/с


@dataclass
class Model:
    """Список строк и курсор; `quitting` поднимается клавишей ``q``."""

    rows: list[Row] = field(default_factory=list)
    cursor: int = 0
    quitting: bool = False

    def apply_snapshot(self, snapshot: dict[str, Any]) -> None:
        """Замещает строки очередным снапшотом подписки (BEH-06, BEH-14)."""
        self.rows = [
            Row(
                name=str(entry.get("name", "")),
                status=str(entry.get("status", "")),
                progress=float(entry.get("progress", 0.0)),
                download_rate=int(entry.get("download_rate", 0)),
            )
            for entry in snapshot.get("entries", [])
        ]
        self.cursor = min(self.cursor, max(len(self.rows) - 1, 0))

    def handle_key(self, key: str) -> None:
        """Клавиши только навигационные и ``q`` (D-12)."""
        if key in ("q", "Q"):
            self.quitting = True
        elif key in ("up", "k"):
            self.cursor = max(self.cursor - 1, 0)
        elif key in ("down", "j"):
            self.cursor = min(self.cursor + 1, max(len(self.rows) - 1, 0))
