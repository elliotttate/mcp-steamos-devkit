from __future__ import annotations

from pathlib import Path

from mcp_steamos_devkit.state import JsonStore


def test_store_roundtrip_and_mapping_update(tmp_path: Path) -> None:
    store = JsonStore(tmp_path)
    assert store.load("devices", {}) == {}

    store.update_mapping("devices", "abc", {"name": "deck"})

    assert store.load("devices", {}) == {"abc": {"name": "deck"}}


def test_store_quarantines_corrupt_json(tmp_path: Path) -> None:
    store = JsonStore(tmp_path)
    (tmp_path / "bad.json").write_text("{", encoding="utf-8")

    assert store.load("bad", {"ok": True}) == {"ok": True}
    assert (tmp_path / "bad.json.corrupt").exists()

