"""Разбор argv и выбор роли: клиент ``add``/``tui`` или ``daemon``.

Вывод и коды возврата идентичны на macOS и Linux (D-16): 0 — успех,
1 — ошибка выполнения, 2 — неверное использование.
"""

from __future__ import annotations

import os
import sys

from mt import paths
from mt.daemon import spawn
from mt.ipc.client import DaemonUnavailable, IPCError

USAGE = """\
использование:
  mt add <magnet | файл.torrent>   добавить закачку
  mt [tui]                         открыть список закачек
  mt daemon                        служебная роль (запускается сама)
"""


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv

    if not args or args[0] == "tui":
        if len(args) > 1:
            return _usage_error()
        return _cmd_tui()
    if args[0] == "add":
        if len(args) != 2:
            return _usage_error()
        return _cmd_add(args[1])
    if args[0] == "daemon":
        if len(args) != 1:
            return _usage_error()
        from mt.daemon import daemon as daemon_mod

        return daemon_mod.run()
    return _usage_error()


def _usage_error() -> int:
    print(USAGE, end="", file=sys.stderr)
    return 2


def normalize_source(source: str) -> str:
    """Абсолютизирует путь к .torrent: демон работает с другим cwd."""
    if source.startswith("magnet:"):
        return source
    return os.path.abspath(os.path.expanduser(source))


def _cmd_add(source: str) -> int:
    try:
        # BEH-08: демона нет — клиент сам поднимает его и ждёт сокет
        client = spawn.connect(paths.socket_path())
    except DaemonUnavailable:
        print("mt: демон не запустился", file=sys.stderr)
        return 1
    try:
        with client:
            response = client.add(normalize_source(source))
    except IPCError as exc:
        print(f"mt: {exc}", file=sys.stderr)
        return 1
    print(response["name"])
    return 0


def _cmd_tui() -> int:
    # BEH-12: `mt` и `mt tui` открывают одно и то же окно со списком
    from mt.tui import app

    return app.run()


if __name__ == "__main__":
    raise SystemExit(main())
