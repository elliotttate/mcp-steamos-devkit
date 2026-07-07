from __future__ import annotations

from pathlib import Path

from mcp_steamos_devkit.models import SafetyLevel
from mcp_steamos_devkit.safety import ConfirmationManager
from mcp_steamos_devkit.state import JsonStore


def test_confirmation_requires_same_action_and_params(tmp_path: Path) -> None:
    manager = ConfirmationManager(JsonStore(tmp_path), ttl_seconds=60)
    first = manager.require(
        "delete_title",
        {"target": "deck", "gameid": "test"},
        SafetyLevel.DESTRUCTIVE,
        "delete test",
        token=None,
    )
    assert first is not None
    token = first["confirmation_token"]

    assert not manager.verify(token, "delete_title", {"target": "deck", "gameid": "other"})
    assert manager.verify(token, "delete_title", {"target": "deck", "gameid": "test"})
    assert not manager.verify(token, "delete_title", {"target": "deck", "gameid": "test"})


def test_write_operation_does_not_require_confirmation(tmp_path: Path) -> None:
    manager = ConfirmationManager(JsonStore(tmp_path), ttl_seconds=60)
    assert manager.require("sync", {}, SafetyLevel.WRITE, "sync", token=None) is None

