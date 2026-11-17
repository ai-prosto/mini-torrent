"""Структуры запросов и ответов: построчный JSON (NDJSON, D-04).

Запросы: ``{"op": "add", "source": ...}``, ``{"op": "list"}``,
``{"op": "subscribe"}``. Ответы: ``{"ok": true, ...}`` либо
``{"ok": false, "error": "..."}``; на ``subscribe`` демон шлёт по
строке-снапшоту, пока клиент не отключится.
"""

from __future__ import annotations

import json
from typing import Any

OP_ADD = "add"
OP_LIST = "list"
OP_SUBSCRIBE = "subscribe"

OPS = frozenset({OP_ADD, OP_LIST, OP_SUBSCRIBE})


class ProtocolError(Exception):
    """Строка не является корректным запросом или ответом протокола."""


def encode(message: dict[str, Any]) -> bytes:
    """Кодирует сообщение в одну NDJSON-строку (с завершающим ``\\n``)."""
    return (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")


def decode(line: bytes | str) -> dict[str, Any]:
    """Разбирает одну NDJSON-строку в словарь."""
    if isinstance(line, bytes):
        line = line.decode("utf-8", errors="replace")
    try:
        message = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"не JSON: {line!r}") from exc
    if not isinstance(message, dict):
        raise ProtocolError(f"ожидался объект, пришло: {line!r}")
    return message


def parse_request(line: bytes | str) -> dict[str, Any]:
    """Разбирает и проверяет запрос клиента."""
    message = decode(line)
    op = message.get("op")
    if op not in OPS:
        raise ProtocolError(f"неизвестная операция: {op!r}")
    if op == OP_ADD:
        source = message.get("source")
        if not isinstance(source, str) or not source.strip():
            raise ProtocolError("add: нужен непустой source")
    return message


def ok(**fields: Any) -> dict[str, Any]:
    return {"ok": True, **fields}


def error(message: str) -> dict[str, Any]:
    return {"ok": False, "error": message}
