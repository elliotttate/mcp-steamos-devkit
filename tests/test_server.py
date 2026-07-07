from __future__ import annotations

from pathlib import Path

from mcp_steamos_devkit.models import SafetyLevel
from mcp_steamos_devkit.operations import OperationManager
from mcp_steamos_devkit.server import _run_tool, create_server
from mcp_steamos_devkit.state import JsonStore


def test_server_constructs() -> None:
    server = create_server()
    assert server.name == "SteamOS Devkit"


def test_run_tool_persists_compact_operation_summary(tmp_path: Path) -> None:
    operations = OperationManager(JsonStore(tmp_path))
    large_log = "x" * 2000

    response = _run_tool(
        operations,
        "journalctl_tail",
        SafetyLevel.READ_ONLY,
        lambda: {"lines": [large_log, "small"], "device": {"name": "frame"}},
    )

    assert response["result"]["lines"][0] == large_log
    stored = operations.get(response["operation_id"])
    assert stored is not None
    assert stored["result"]["summary"]["lines"]["count"] == 2
    assert stored["result"]["summary"]["lines"]["sample"][0]["truncated"]
