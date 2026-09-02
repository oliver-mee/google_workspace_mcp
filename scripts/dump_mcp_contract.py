"""Dump the registered MCP contract of this server.

Replicates fastmcp_server.py's registration order (all tool modules + registry
filters) WITHOUT the HTTP/auth wiring, then prints the registered surface as
JSON. This is the measurement probe for "how big is the MCP?" — the decorated
grep under-counts because registration is import-driven.

Usage:
    MCP_ENABLE_OAUTH21=false MCP_SINGLE_USER_MODE=true
    WORKSPACE_MCP_STATELESS_MODE=false USER_GOOGLE_EMAIL=test@example.com \
    WORKSPACE_MCP_PERMISSIONS= WORKSPACE_MCP_DISABLED_TOOLS= \
    .venv/bin/python scripts/dump_mcp_contract.py [--json]

Default output is a compact summary; --json prints the full registered tool
list (names, required properties, descriptions).
"""

from __future__ import annotations

import argparse
import asyncio
import json

# Mirror fastmcp_server.py registration order
import gmail.gmail_tools  # noqa: F401
import gdrive.drive_tools  # noqa: F401
import gcalendar.calendar_tools  # noqa: F401
import gdocs.docs_tools  # noqa: F401
import gsheets.sheets_tools  # noqa: F401
import gchat.chat_tools  # noqa: F401
import gforms.forms_tools  # noqa: F401
import gslides.slides_tools  # noqa: F401
import gtasks.tasks_tools  # noqa: F401
import gsearch.search_tools  # noqa: F401

from auth.scopes import set_enabled_tools
from core.server import server
from core.tool_registry import (
    filter_server_tools,
    resolve_disabled_tools,
    set_disabled_tools,
    set_enabled_tools as set_enabled_tool_names,
    wrap_server_tool_method,
)

wrap_server_tool_method(server)
set_enabled_tools(
    [
        "gmail",
        "drive",
        "calendar",
        "docs",
        "sheets",
        "chat",
        "forms",
        "slides",
        "tasks",
        "search",
    ]
)
set_enabled_tool_names(None)
set_disabled_tools(resolve_disabled_tools())
filter_server_tools(server)


async def main(full: bool) -> None:
    tools = {t.name: t for t in await server.list_tools()}
    if full:
        print(
            json.dumps(
                {
                    "server_name": server.name,
                    "server_version": server.version,
                    "tool_count": len(tools),
                    "tools": [
                        {
                            "name": name,
                            "required": tool.parameters.get("required", []),
                            "description": (tool.description or "")[:2000],
                        }
                        for name, tool in sorted(tools.items())
                    ],
                },
                indent=2,
                default=str,
            )
        )
        return
    print(
        json.dumps(
            {
                "server_name": server.name,
                "server_version": server.version,
                "tool_count": len(tools),
                "services": sorted(
                    {
                        name.split("_")[0]
                        for name in tools
                        if name not in ("list_spreadsheets",)
                    }
                ),
                "sheets_dispatchers": [
                    name
                    for name in ("sheets_read", "sheets_manage", "sheets_delete")
                    if name in tools
                ],
            },
            indent=2,
            default=str,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="full tool list as JSON")
    args = parser.parse_args()
    asyncio.run(main(args.json))
