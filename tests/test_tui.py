"""TUI: строки списка, колонки, «готово» и самообновление (BEH-06, BEH-12, BEH-13, BEH-14)."""

from __future__ import annotations

import queue
from typing import Any

import pytest

from mt import cli
from mt.daemon.daemon import Daemon
from mt.tui import app, view
from mt.tui.model import Model

MAGNET = "magnet:?xt=urn:btih:" + "e" * 40 + "&dn=disk.iso"


def entry(
    name: str,
    *,
    status: str = "downloading",
    progress: float = 0.0,
    download_rate: int = 0,
) -> dict[str, Any]:
    return {
        "infohash": "a" * 40,
        "name": name,
        "status": status,
        "progress": progress,
        "download_rate": download_rate,
    }


def snapshot(*entries: dict[str, Any]) -> dict[str, Any]:
    return {"entries": list(entries)}


def model_with(*entries: dict[str, Any]) -> Model:
    model = Model()
    model.apply_snapshot(snapshot(*entries))
    return model


def test_three_rows() -> None:
    """BEH-06: три активные закачки — ровно три строки списка."""
    model = model_with(
        entry("a.iso", progress=10.0),
        entry("b.iso", progress=20.0),
        entry("c.iso", progress=30.0),
    )
    rows = view.render_rows(model, width=80)
    assert len(rows) == 3
    for name, row in zip(["a.iso", "b.iso", "c.iso"], rows, strict=True):
        assert name in row


def test_list_columns(daemon: Daemon, capsys: pytest.CaptureFixture[str]) -> None:
    """BEH-12: `mt` и `mt tui` показывают одно окно с именем, процентом и скоростью."""
    assert cli.main(["add", MAGNET]) == 0
    capsys.readouterr()

    frames = []
    for args in ([], ["tui"]):
        assert cli.main(args) == 0
        out = capsys.readouterr().out
        assert "disk.iso" in out  # имя
        assert "0.0%" in out  # процент
        assert "Б/с" in out  # скорость
        frames.append(out)
    assert frames[0] == frames[1]  # одно и то же окно


def test_done_entries_listed() -> None:
    """BEH-13: записи ``done`` — в той же таблице, со статусом «готово» и 100%."""
    model = model_with(
        entry("movie.mkv", progress=42.0, download_rate=2048),
        entry("album.flac", status="done", progress=100.0),
    )
    rows = view.render_rows(model, width=80)
    assert len(rows) == 2
    assert "movie.mkv" in rows[0]
    assert "42.0%" in rows[0]
    done_row = rows[1]
    assert "album.flac" in done_row
    assert "готово" in done_row
    assert "100.0%" in done_row


def test_snapshot_refresh_without_input() -> None:
    """BEH-14: кадр на каждый снапшот подписки, клавиатура не нужна."""
    snapshots = iter(
        [
            snapshot(entry("a.iso", progress=10.0, download_rate=1024)),
            snapshot(entry("a.iso", progress=55.5, download_rate=2048)),
            snapshot(entry("a.iso", progress=99.9, download_rate=512)),
        ]
    )
    frames: list[str] = []
    app.run_loop(snapshots, queue.Queue(), frames.append, width=lambda: 80)
    assert len(frames) == 3
    assert "10.0%" in frames[0]
    assert "55.5%" in frames[1]
    assert "99.9%" in frames[2]
    assert len(set(frames)) == 3  # каждый снапшот менял отрисовку


def test_quit_key_stops_loop() -> None:
    """``q`` завершает цикл даже на бесконечной подписке."""
    keys: queue.Queue[str] = queue.Queue()
    keys.put("q")
    frames: list[str] = []
    endless = iter(lambda: snapshot(entry("a.iso")), None)  # подписка не кончается
    app.run_loop(endless, keys, frames.append, width=lambda: 80)
    assert frames == []  # вышли до первого кадра


def test_navigation_keys_move_and_clamp() -> None:
    model = model_with(entry("a"), entry("b"))
    assert model.cursor == 0
    model.handle_key("up")
    assert model.cursor == 0  # упор в начало
    model.handle_key("down")
    assert model.cursor == 1
    model.handle_key("j")
    assert model.cursor == 1  # упор в конец
    model.handle_key("k")
    assert model.cursor == 0


def test_cursor_clamped_when_rows_shrink() -> None:
    """Запись пропала из снапшота (watcher удалил) — курсор не вылетает за список."""
    model = model_with(entry("a"), entry("b"), entry("c"))
    model.handle_key("down")
    model.handle_key("down")
    assert model.cursor == 2
    model.apply_snapshot(snapshot(entry("a")))
    assert model.cursor == 0


def test_format_rate_units() -> None:
    assert view.format_rate(0) == "0 Б/с"
    assert view.format_rate(512) == "512 Б/с"
    assert view.format_rate(2048) == "2.0 КиБ/с"
    assert view.format_rate(3 * 1024 * 1024) == "3.0 МиБ/с"
    assert view.format_rate(5 * 1024**3) == "5.0 ГиБ/с"


def test_empty_list_renders_placeholder() -> None:
    assert view.EMPTY in view.render(Model(), width=80)


def test_print_once_empty_subscription_fails(capsys: pytest.CaptureFixture[str]) -> None:
    """Подписка оборвалась до первого снапшота — код 1 и сообщение."""
    assert app._print_once(iter([])) == 1
    assert "mt: " in capsys.readouterr().err


def test_run_reports_ipc_error(
    daemon: Daemon, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ошибка IPC по живому сокету — код 1 и сообщение, не трейсбек."""
    from mt.ipc.client import Client, IPCError

    def broken_subscribe(self: Client) -> Any:
        raise IPCError("обрыв")

    monkeypatch.setattr(Client, "subscribe", broken_subscribe)
    assert app.run() == 1
    assert "обрыв" in capsys.readouterr().err


def test_long_name_truncated_to_width() -> None:
    model = model_with(entry("x" * 200))
    row = view.render_rows(model, width=60)[0]
    assert "…" in row
    assert len(row) == 60
