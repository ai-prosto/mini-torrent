"""CLI: выбор роли, вывод и коды возврата (одинаковые на macOS и Linux)."""

from __future__ import annotations

from pathlib import Path

import pytest

from mt import cli
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


def test_add_without_daemon_fails_with_message(
    state_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
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
