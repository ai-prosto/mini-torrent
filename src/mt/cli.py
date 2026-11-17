"""Разбор argv и выбор роли: клиент ``add``/``tui`` или ``daemon``.

Вывод и коды возврата идентичны на macOS и Linux (D-16): 0 — успех,
1 — ошибка выполнения, 2 — неверное использование.
"""

from __future__ import annotations

import os
import sys

from mt import paths
from mt.ipc.client import Client, DaemonUnavailable, IPCError

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
        with Client(paths.socket_path()) as client:
            response = client.add(normalize_source(source))
    except DaemonUnavailable:
        # самозапуск демона появится в DT-03
        print("mt: демон не запущен", file=sys.stderr)
        return 1
    except IPCError as exc:
        print(f"mt: {exc}", file=sys.stderr)
        return 1
    print(response["name"])
    return 0


def _cmd_tui() -> int:
    # полноценный TUI появится в DT-05; пока — одноразовый список
    try:
        with Client(paths.socket_path()) as client:
            response = client.list()
    except DaemonUnavailable:
        print("mt: демон не запущен, закачек нет", file=sys.stderr)
        return 1
    except IPCError as exc:
        print(f"mt: {exc}", file=sys.stderr)
        return 1
    for entry in response["entries"]:
        print(f"{entry['name']}\t{entry['status']}\t{entry['download_dir']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
