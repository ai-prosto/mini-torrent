"""Регистрация автозапуска демона при загрузке ОС (D-08, BEH-10, BEH-20).

macOS — LaunchAgent с ``RunAtLoad``; Linux — user-unit systemd с
``WantedBy=default.target``, ``systemctl --user enable`` и
``loginctl enable-linger`` (демон живёт без входа в систему и переживает
разрыв ssh). Неудача регистрации не мешает демону работать: он пишет
предупреждение и продолжает.
"""

from __future__ import annotations

import os
import sys


class AutostartError(Exception):
    """Автозапуск не зарегистрировался; демон продолжает работать без него."""


def install(daemon_command: list[str]) -> None:
    """Регистрирует автозапуск текущей платформы; повторный вызов — no-op.

    ``MT_DISABLE_AUTOSTART=1`` выключает регистрацию целиком (тесты и
    отладочные запуски не должны трогать launchd/systemd пользователя).
    """
    if os.environ.get("MT_DISABLE_AUTOSTART") == "1":
        return
    if sys.platform == "darwin":
        from mt.autostart import launchd

        launchd.install(daemon_command)
    elif sys.platform.startswith("linux"):
        from mt.autostart import systemd

        systemd.install(daemon_command)
    # прочие платформы: автозапуска нет, демон работает без него
