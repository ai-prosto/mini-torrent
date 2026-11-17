"""D-08: регистрация автозапуска ОС (LaunchAgent / systemd user-unit).

Сами launchd/systemd в тестах не трогаются: platform-ветки проверяются
через диспетчер, системные команды — через подменённый ``subprocess.run``.
BEH-10 и BEH-20 целиком проверяются вручную (docs/manual/).
"""

from __future__ import annotations

import plistlib
import subprocess
import sys
from pathlib import Path

import pytest

from mt import autostart, paths
from mt.autostart import launchd, systemd

COMMAND = [sys.executable, "-m", "mt", "daemon"]


# --- диспетчер ---------------------------------------------------------------


def test_install_disabled_by_env(home: Path) -> None:
    """MT_DISABLE_AUTOSTART=1 (ставится conftest) — никаких файлов и команд."""
    autostart.install(COMMAND)
    assert not launchd.plist_path().exists()
    assert not systemd.unit_path().exists()


@pytest.mark.parametrize(
    ("platform", "expected"),
    [("darwin", "launchd"), ("linux", "systemd"), ("win32", None)],
)
def test_install_dispatches_by_platform(
    home: Path, monkeypatch: pytest.MonkeyPatch, platform: str, expected: str | None
) -> None:
    monkeypatch.delenv("MT_DISABLE_AUTOSTART", raising=False)
    calls: list[str] = []
    monkeypatch.setattr(launchd, "install", lambda cmd: calls.append("launchd"))
    monkeypatch.setattr(systemd, "install", lambda cmd: calls.append("systemd"))
    monkeypatch.setattr(sys, "platform", platform)

    autostart.install(COMMAND)
    assert calls == ([expected] if expected else [])


# --- launchd (macOS) ---------------------------------------------------------


def test_launchd_install_writes_runatload_plist(home: Path) -> None:
    launchd.install(COMMAND)
    target = launchd.plist_path()
    assert target == home / "Library" / "LaunchAgents" / "dev.mt.daemon.plist"

    data = plistlib.loads(target.read_bytes())
    assert data["Label"] == "dev.mt.daemon"
    assert data["ProgramArguments"] == COMMAND
    assert data["RunAtLoad"] is True
    assert data["StandardOutPath"] == str(paths.log_path())

    before = target.stat().st_mtime_ns
    launchd.install(COMMAND)  # повторная установка ничего не переписывает
    assert target.stat().st_mtime_ns == before


def test_launchd_install_failure_raises(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    blocker = home / "blocker"
    blocker.write_text("файл на месте каталога", encoding="utf-8")
    monkeypatch.setattr(launchd, "plist_path", lambda: blocker / "x.plist")
    with pytest.raises(autostart.AutostartError, match="не удалось записать"):
        launchd.install(COMMAND)


# --- systemd (Linux) ---------------------------------------------------------


def test_systemd_install_writes_unit_and_enables(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(systemd.subprocess, "run", fake_run)
    monkeypatch.setattr(systemd.getpass, "getuser", lambda: "andrei")

    systemd.install(COMMAND)
    unit = systemd.unit_path()
    assert unit == home / ".config" / "systemd" / "user" / "mt.service"
    body = unit.read_text(encoding="utf-8")
    assert "WantedBy=default.target" in body
    assert "Restart=on-failure" in body
    assert "ExecStart=" in body
    assert calls == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "mt.service"],
        ["loginctl", "enable-linger", "andrei"],  # BEH-20: жизнь без ssh-сессии
    ]

    calls.clear()
    systemd.install(COMMAND)  # unit совпадает — systemctl не дёргается
    assert calls == []


def test_systemd_failed_enable_is_retried_next_start(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Неудача systemctl не оставляет «успешный» unit: следующий вызов повторяет всё.

    Первый старт демона по ssh без DBus — типичная среда, где enable/linger
    падают; совпадающий unit на диске не должен навсегда закэшировать неудачу.
    """

    def failing_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 1, "", "нет dbus")

    monkeypatch.setattr(systemd.subprocess, "run", failing_run)
    with pytest.raises(autostart.AutostartError, match="нет dbus"):
        systemd.install(COMMAND)
    assert not systemd.unit_path().exists()  # маркер успеха не остался

    calls: list[list[str]] = []

    def ok_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(systemd.subprocess, "run", ok_run)
    systemd.install(COMMAND)
    assert systemd.unit_path().read_text(encoding="utf-8") == systemd.render(COMMAND)
    assert len(calls) == 3  # daemon-reload, enable и enable-linger повторились


def test_systemd_command_failure_raises(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 1, "", "boom")

    monkeypatch.setattr(systemd.subprocess, "run", fake_run)
    with pytest.raises(autostart.AutostartError, match="boom"):
        systemd.install(COMMAND)


def test_systemd_missing_systemctl_raises(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("systemctl")

    monkeypatch.setattr(systemd.subprocess, "run", fake_run)
    with pytest.raises(autostart.AutostartError, match="не удалось выполнить"):
        systemd.install(COMMAND)


# --- демон: неудача автозапуска не мешает работе -----------------------------


def test_daemon_warns_but_starts_when_autostart_fails(
    state_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from mt.daemon.daemon import Daemon

    def broken_install(_command: list[str]) -> None:
        raise autostart.AutostartError("нет прав")

    monkeypatch.setattr(autostart, "install", broken_install)
    daemon = Daemon()
    daemon.start()
    try:
        err = capsys.readouterr().err
        assert "автозапуск не настроен" in err
        assert paths.socket_path().exists()  # демон всё равно работает
    finally:
        daemon.stop()
