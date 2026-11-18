"""Запуск пакета как ``python -m mt``: так клиент порождает роль демона."""

from mt.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
