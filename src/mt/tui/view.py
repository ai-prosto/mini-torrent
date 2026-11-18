"""Отрисовка списка закачек (D-12): имя, статус, процент, скорость.

Завершённые записи рисуются в той же таблице со статусом «готово»
и 100% (BEH-13). Статус ``downloading`` показывается как есть — так же,
как в протоколе и ``state.json``.
"""

from __future__ import annotations

import unicodedata

from mt.daemon import state as state_mod
from mt.tui.model import Model

STATUS_LABELS = {state_mod.STATUS_DONE: "готово"}

EMPTY = "закачек нет"
FOOTER = "↑/↓ или j/k — навигация · q — выход"

STATUS_W = 11  # len("downloading")
PERCENT_W = 7  # len("100.0%") + запас
RATE_W = 12  # len("1023.9 КиБ/с")
MIN_NAME_W = 8


def format_percent(progress: float) -> str:
    return f"{progress:.1f}%"


def format_rate(bps: int) -> str:
    value = float(bps)
    if value < 1024:
        return f"{int(value)} Б/с"
    for unit in ("КиБ/с", "МиБ/с", "ГиБ/с"):
        value /= 1024
        # 1023.95..1024 печатается как «1024.0» — 13 знаков, шире RATE_W
        if value < 1023.95 or unit == "ГиБ/с":
            return f"{value:.1f} {unit}"
    raise AssertionError("недостижимо")


def status_label(status: str) -> str:
    return STATUS_LABELS.get(status, status)


def render_rows(model: Model, width: int = 80) -> list[str]:
    """Строки списка — по одной на закачку (BEH-06), без заголовка."""
    name_w = _name_width(width)
    lines = []
    for index, row in enumerate(model.rows):
        marker = ">" if index == model.cursor else " "
        lines.append(
            f"{marker} "
            f"{_fit(row.name, name_w):<{name_w}} "
            f"{status_label(row.status):<{STATUS_W}} "
            f"{format_percent(row.progress):>{PERCENT_W}} "
            f"{format_rate(row.download_rate):>{RATE_W}}"
        )
    return lines


def render(model: Model, width: int = 80) -> str:
    """Кадр целиком: заголовок и таблица (или «закачек нет»)."""
    name_w = _name_width(width)
    header = (
        f"  {'ИМЯ':<{name_w}} {'СТАТУС':<{STATUS_W}} "
        f"{'ПРОЦЕНТ':>{PERCENT_W}} {'СКОРОСТЬ':>{RATE_W}}"
    )
    rows = render_rows(model, width) or [f"  {EMPTY}"]
    return "\n".join([header, *rows])


def _name_width(width: int) -> int:
    fixed = 2 + 1 + STATUS_W + 1 + PERCENT_W + 1 + RATE_W  # маркер и колонки с пробелами
    return max(MIN_NAME_W, width - fixed)


def _fit(name: str, limit: int) -> str:
    name = _sanitize(name)
    if len(name) <= limit:
        return name
    return name[: limit - 1] + "…"


def _sanitize(name: str) -> str:
    # имя приходит из чужих торрент-метаданных: ESC-последовательности
    # могли бы переписать заголовок окна или испортить экран
    return "".join(
        "?" if unicodedata.category(char).startswith("C") else char for char in name
    )
