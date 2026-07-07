from __future__ import annotations

from mcp_steamos_devkit.server import create_server


def test_server_constructs() -> None:
    server = create_server()
    assert server.name == "SteamOS Devkit"

