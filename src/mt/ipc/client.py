"""Подключение к сокету демона: вызовы ``add``/``list``/``subscribe`` (D-02)."""

from __future__ import annotations

import socket
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from mt.ipc import proto


class IPCError(Exception):
    """Не удалось поговорить с демоном."""


class DaemonUnavailable(IPCError):
    """Сокета нет или подключение отвергнуто — демон не запущен."""


class Client:
    """Короткоживущий клиент: одно подключение к unix-сокету демона."""

    def __init__(self, socket_path: Path, timeout: float | None = 10.0) -> None:
        self._socket_path = socket_path
        try:
            self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._sock.settimeout(timeout)
            self._sock.connect(str(socket_path))
        except (FileNotFoundError, ConnectionRefusedError) as exc:
            raise DaemonUnavailable(f"демон не отвечает на {socket_path}") from exc
        self._reader = self._sock.makefile("rb")

    def close(self) -> None:
        self._reader.close()
        self._sock.close()

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def add(self, source: str) -> dict[str, Any]:
        """Просит демона добавить закачку; возвращает ответ с именем."""
        return self._call({"op": proto.OP_ADD, "source": source})

    def list(self) -> dict[str, Any]:
        """Возвращает список закачек демона."""
        return self._call({"op": proto.OP_LIST})

    def subscribe(self) -> Iterator[dict[str, Any]]:
        """Открывает подписку: по снапшоту на строку, пока демон жив."""
        self._send({"op": proto.OP_SUBSCRIBE})
        while True:
            line = self._readline()
            if not line:
                return
            yield proto.decode(line)

    def _call(self, request: dict[str, Any]) -> dict[str, Any]:
        self._send(request)
        line = self._readline()
        if not line:
            raise IPCError("демон закрыл соединение без ответа")
        response = proto.decode(line)
        if not response.get("ok", False):
            raise IPCError(response.get("error", "неизвестная ошибка демона"))
        return response

    def _send(self, request: dict[str, Any]) -> None:
        try:
            self._sock.sendall(proto.encode(request))
        except OSError as exc:
            raise IPCError(f"не удалось отправить запрос: {exc}") from exc

    def _readline(self) -> bytes:
        try:
            return self._reader.readline()
        except OSError as exc:
            raise IPCError(f"не удалось прочитать ответ: {exc}") from exc
