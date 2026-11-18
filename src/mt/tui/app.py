"""Роль клиента ``mt``/``mt tui``: подписка и самообновляющийся список (D-12).

``mt`` без аргументов эквивалентна ``mt tui``: обе открывают ``subscribe``
и перерисовывают таблицу на каждый снапшот (~2 раза в секунду) без нажатий
(BEH-12, BEH-14). Когда stdout — не терминал (пайп, тесты), печатается один
кадр списка и клиент выходит с кодом 0: вывод можно грепать.
"""

from __future__ import annotations

import contextlib
import queue
import shutil
import sys
import threading
from collections.abc import Callable, Iterator
from typing import Any

from mt import paths
from mt.ipc.client import Client, DaemonUnavailable, IPCError
from mt.tui import view
from mt.tui.model import Model

ENTER_ALT = "\x1b[?1049h\x1b[?25l"  # альтернативный экран, курсор спрятан
LEAVE_ALT = "\x1b[?25h\x1b[?1049l"
CLEAR = "\x1b[H\x1b[2J"


def run() -> int:
    """Точка входа ``mt`` и ``mt tui`` — одно и то же окно (BEH-12)."""
    try:
        client = Client(paths.socket_path())
    except DaemonUnavailable:
        print("mt: демон не запущен, закачек нет", file=sys.stderr)
        return 1
    try:
        with client:
            snapshots = client.subscribe()
            if sys.stdout.isatty() and sys.stdin.isatty():  # pragma: no cover
                _run_interactive(snapshots)
                return 0
            return _print_once(snapshots)
    except IPCError as exc:
        print(f"mt: {exc}", file=sys.stderr)
        return 1


def run_loop(
    snapshots: Iterator[dict[str, Any]],
    keys: queue.Queue[str],
    write: Callable[[str], None],
    width: Callable[[], int] | None = None,
) -> None:
    """Сердце TUI: кадр на каждый снапшот подписки, ввод не требуется.

    Вынесено из ``_run_interactive``, чтобы тесты гоняли цикл на готовых
    снапшотах без терминала (BEH-14). Цикл завершается концом подписки
    (демон остановился) или клавишей ``q``.
    """
    model = Model()
    width_of = width or _width
    for snapshot in snapshots:
        model.apply_snapshot(snapshot)
        while True:
            try:
                model.handle_key(keys.get_nowait())
            except queue.Empty:
                break
        if model.quitting:
            return
        write(view.render(model, width_of()))


def _print_once(snapshots: Iterator[dict[str, Any]]) -> int:
    """Не-tty режим: один кадр первого снапшота и выход."""
    snapshot = next(snapshots, None)
    if snapshot is None:
        print("mt: демон закрыл подписку без снапшота", file=sys.stderr)
        return 1
    model = Model()
    model.apply_snapshot(snapshot)
    print(view.render(model, _width()))
    return 0


def _width() -> int:
    return shutil.get_terminal_size((80, 24)).columns


# --- интерактивный терминал: не покрывается юнит-тестами ------------------


def _run_interactive(snapshots: Iterator[dict[str, Any]]) -> None:  # pragma: no cover
    keys: queue.Queue[str] = queue.Queue()
    stop = threading.Event()
    reader = threading.Thread(
        target=_read_keys, args=(keys, stop), name="mt-tui-keys", daemon=True
    )
    sys.stdout.write(ENTER_ALT)
    try:
        with _cbreak():
            reader.start()
            run_loop(snapshots, keys, _draw)
    finally:
        stop.set()
        sys.stdout.write(LEAVE_ALT)
        sys.stdout.flush()


def _draw(frame: str) -> None:  # pragma: no cover
    sys.stdout.write(f"{CLEAR}{frame}\n\n{view.FOOTER}\n")
    sys.stdout.flush()


@contextlib.contextmanager
def _cbreak() -> Iterator[None]:  # pragma: no cover
    """Клавиши отдаются сразу, без Enter; настройки терминала возвращаются."""
    import termios
    import tty

    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


def _read_keys(keys: queue.Queue[str], stop: threading.Event) -> None:  # pragma: no cover
    while not stop.is_set():
        char = sys.stdin.read(1)
        if not char:
            return
        if char == "\x1b":  # стрелки приходят как ESC [ A/B
            seq = sys.stdin.read(2)
            if seq == "[A":
                keys.put("up")
            elif seq == "[B":
                keys.put("down")
        else:
            keys.put(char)
