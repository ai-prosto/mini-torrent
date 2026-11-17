"""state.json: чтение, атомарная запись, устойчивость к мусору."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mt.daemon import state


def make_entry(name: str = "ubuntu.iso") -> state.Entry:
    return state.Entry(
        infohash="a" * 40,
        name=name,
        source="magnet:?xt=urn:btih:" + "a" * 40,
        download_dir="/tmp/dl",
        files=["ubuntu.iso"],
        total_bytes=123,
        added_at="2026-11-17T00:00:00+00:00",
    )


def test_load_missing_file_is_empty(tmp_path: Path) -> None:
    assert state.load(tmp_path / "state.json") == []


def test_save_and_load_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "state.json"
    entries = [make_entry("one"), make_entry("two")]
    state.save(path, entries)
    assert state.load(path) == entries


def test_save_is_atomic_no_temp_leftovers(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    state.save(path, [make_entry()])
    state.save(path, [make_entry(), make_entry("second")])
    names = {child.name for child in tmp_path.iterdir()}
    assert names == {"state.json"}
    data = json.loads(path.read_text(encoding="utf-8"))
    assert len(data["entries"]) == 2


def test_load_rejects_garbage(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(state.StateError):
        state.load(path)


def test_load_rejects_wrong_shape(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"entries": [{"unknown_field": 1}]}), encoding="utf-8")
    with pytest.raises(state.StateError):
        state.load(path)


def test_save_failure_keeps_old_file_and_no_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Сбой записи: прежний state.json цел, временный файл убран."""
    path = tmp_path / "state.json"
    state.save(path, [make_entry("one")])
    original = path.read_text(encoding="utf-8")

    def broken_replace(src: str, dst: object) -> None:
        raise OSError("диск переполнен")

    monkeypatch.setattr(state.os, "replace", broken_replace)
    with pytest.raises(OSError, match="переполнен"):
        state.save(path, [make_entry("one"), make_entry("two")])
    monkeypatch.undo()

    assert path.read_text(encoding="utf-8") == original
    assert {child.name for child in tmp_path.iterdir()} == {"state.json"}
