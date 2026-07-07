from __future__ import annotations

import argparse
import json
from typing import Sequence

from .adapter import SteamOSDevkitAdapter
from .config import find_layout
from .server import run
from .state import JsonStore


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mcp-steamos-devkit")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="Run the MCP server")
    serve.add_argument(
        "--transport",
        choices=["stdio", "sse", "streamable-http"],
        default="stdio",
        help="MCP transport to use",
    )

    sub.add_parser("doctor", help="Inspect local configuration and dependencies")

    args = parser.parse_args(argv)

    if args.command in (None, "serve"):
        run(args.transport if args.command == "serve" else "stdio")
        return 0

    if args.command == "doctor":
        layout = find_layout()
        adapter = SteamOSDevkitAdapter(layout=layout, store=JsonStore(layout.data_dir))
        print(json.dumps(adapter.doctor(), indent=2, sort_keys=True))
        return 0

    parser.print_help()
    return 2

