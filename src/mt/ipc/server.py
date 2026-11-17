"""Слушатель unix-сокета и роутинг запросов (D-02, D-04, D-15).

Сервер ничего не знает о торрентах: он принимает NDJSON-запросы и зовёт
обработчики, которые ему передал демон. Сокет создаётся с режимом 0600,
слушаем только unix-сокет и никогда TCP.
"""

from __future__ import annotations

import contextlib
import os
import socket
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from mt.ipc import proto

SOCKET_MODE = 0o600
MAX_LINE = 1 << 20  # запрос длиннее 1 МиБ — не наш протокол

# add/list возвращают словарь-ответ; subscribe — итератор снапшотов.
Handler = Callable[[dict[str, Any]], dict[str, Any]]
SubscribeHandler = Callable[[dict[str, Any]], Iterator[dict[str, Any]]]


class IPCServer:
    """Принимает подключения по unix-сокету и раздаёт запросы обработчикам."""

    def __init__(
        self,
        socket_path: Path,
        handlers: dict[str, Handler],
        subscribe_handler: SubscribeHandler | None = None,
    ) -> None:
        self._socket_path = socket_path
        self._handlers = handlers
        self._subscribe_handler = subscribe_handler
        self._sock: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._stopping = threading.Event()

    @property
    def socket_path(self) -> Path:
        return self._socket_path

    def start(self) -> None:
        """Создаёт сокет (0600), начинает принимать подключения в фоне."""
        self._socket_path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(FileNotFoundError):
            self._socket_path.unlink()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(self._socket_path))
        os.chmod(self._socket_path, SOCKET_MODE)
        sock.listen()
        self._sock = sock
        self._accept_thread = threading.Thread(
            target=self._accept_loop, name="mt-ipc-accept", daemon=True
        )
        self._accept_thread.start()

    def stop(self) -> None:
        """Останавливает приём, закрывает сокет и удаляет его файл."""
        self._stopping.set()
        if self._sock is not None:
            with contextlib.suppress(OSError):
                self._sock.shutdown(socket.SHUT_RDWR)
            self._sock.close()
            self._sock = None
        if self._accept_thread is not None:
            self._accept_thread.join(timeout=5)
            self._accept_thread = None
        with contextlib.suppress(FileNotFoundError):
            self._socket_path.unlink()

    def _accept_loop(self) -> None:
        sock = self._sock  # локальная ссылка: stop() обнуляет self._sock
        assert sock is not None
        while not self._stopping.is_set():
            try:
                conn, _ = sock.accept()
            except OSError:
                return
            threading.Thread(
                target=self._serve_connection, args=(conn,), name="mt-ipc-conn", daemon=True
            ).start()

    def _serve_connection(self, conn: socket.socket) -> None:
        with conn, conn.makefile("rb") as reader:
            while True:
                line = reader.readline(MAX_LINE + 1)
                if not line:
                    return
                if len(line) > MAX_LINE:
                    with contextlib.suppress(OSError):
                        conn.sendall(proto.encode(proto.error("запрос слишком длинный")))
                    return
                if not line.strip():
                    continue
                try:
                    self._dispatch(conn, line)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return

    def _dispatch(self, conn: socket.socket, line: bytes) -> None:
        try:
            request = proto.parse_request(line)
        except proto.ProtocolError as exc:
            conn.sendall(proto.encode(proto.error(str(exc))))
            return

        op = request["op"]
        if op == proto.OP_SUBSCRIBE and self._subscribe_handler is not None:
            for snapshot in self._subscribe_handler(request):
                if self._stopping.is_set():
                    return
                conn.sendall(proto.encode(snapshot))
            return

        handler = self._handlers.get(op)
        if handler is None:
            conn.sendall(proto.encode(proto.error(f"операция не поддерживается: {op}")))
            return
        try:
            response = handler(request)
        except Exception as exc:  # обработчик не должен ронять сервер
            response = proto.error(str(exc))
        conn.sendall(proto.encode(response))
