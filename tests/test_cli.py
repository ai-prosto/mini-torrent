"""CLI: выбор роли, вывод и коды возврата (одинаковые на macOS и Linux)."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from mt import cli, paths
from mt.daemon import spawn
from mt.daemon.daemon import Daemon

MAGNET = "magnet:?xt=urn:btih:" + "d" * 40 + "&dn=mint.iso"


def test_add_prints_name_and_exits_zero(
    daemon: Daemon, capsys: pytest.CaptureFixture[str]
) -> None:
    code = cli.main(["add", MAGNET])
    captured = capsys.readouterr()
    assert code == 0
    assert captured.out.strip() == "mint.iso"
    assert captured.err == ""


def test_add_without_daemon_and_failed_spawn_fails(
    state_home: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Демон не поднялся за отведённое время — код 1 и сообщение, не зависание."""
    monkeypatch.setattr(spawn, "spawn_daemon", lambda: None)  # демон «не стартует»
    monkeypatch.setattr(spawn, "SPAWN_TIMEOUT", 0.2)
    code = cli.main(["add", MAGNET])
    captured = capsys.readouterr()
    assert code == 1
    assert "демон" in captured.err
    assert captured.out == ""


def test_add_bad_source_nonzero_and_stderr(
    daemon: Daemon, capsys: pytest.CaptureFixture[str]
) -> None:
    code = cli.main(["add", "/no/such/file.torrent"])
    captured = capsys.readouterr()
    assert code == 1
    assert captured.err.startswith("mt: ")


def test_tui_lists_entries(daemon: Daemon, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["add", MAGNET]) == 0
    capsys.readouterr()

    for args in ([], ["tui"]):
        code = cli.main(args)
        captured = capsys.readouterr()
        assert code == 0
        assert "mint.iso" in captured.out
        assert "downloading" in captured.out


def test_tui_without_daemon_fails(state_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["tui"])
    assert code == 1
    assert "демон" in capsys.readouterr().err


@pytest.mark.parametrize(
    "args",
    [["add"], ["add", "a", "b"], ["frobnicate"], ["tui", "extra"], ["daemon", "extra"]],
)
def test_usage_errors_exit_two(
    home: Path, args: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    code = cli.main(args)
    captured = capsys.readouterr()
    assert code == 2
    assert "использование" in captured.err


def test_daemon_role_runs_and_stops(state_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``mt daemon`` доходит до цикла и останавливается; повторный — выходит с 0."""
    monkeypatch.setattr(Daemon, "run_forever", Daemon.stop)
    assert cli.main(["daemon"]) == 0

    running = Daemon()
    running.start()
    try:
        assert cli.main(["daemon"]) == 0  # lock занят — гонку выиграл первый
    finally:
        running.stop()


def test_run_forever_stops_and_cleans_up(state_home: Path) -> None:
    """Настоящий цикл: stop() завершает run_forever, сокет убран, lock отпущен."""
    instance = Daemon()
    instance.start()
    loop = threading.Thread(target=instance.run_forever)
    loop.start()
    instance.stop()
    loop.join(timeout=5)

    assert not loop.is_alive()
    assert not paths.socket_path().exists()

    successor = Daemon()  # lock свободен — новый демон стартует
    successor.start()
    successor.stop()


def test_daemon_with_corrupt_state_exits_one(
    state_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Битый state.json: чистое сообщение и код 1, а не трейсбек."""
    paths.ensure_state_dir()
    paths.state_file().write_text("{broken", encoding="utf-8")

    code = cli.main(["daemon"])
    captured = capsys.readouterr()
    assert code == 1
    assert captured.err.startswith("mt: ")
    assert "state.json" in captured.err


def test_normalize_source_absolutizes_relative_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Путь к .torrent абсолютизируется клиентом: cwd демона — другой."""
    monkeypatch.chdir(tmp_path)
    assert cli.normalize_source("file.torrent") == str(tmp_path / "file.torrent")
    assert cli.normalize_source(MAGNET) == MAGNET


def test_add_empty_source_exits_one(
    daemon: Daemon, capsys: pytest.CaptureFixture[str]
) -> None:
    code = cli.main(["add", ""])
    captured = capsys.readouterr()
    assert code == 1
    assert captured.err.startswith("mt: ")
