"""Inspect initialize and tools/list through a native in-memory MCP connection."""

import json

from fastmcp import Client


async def inspect_contract(server) -> dict:
    async with Client(server, mode="legacy") as client:
        tools = [
            tool.model_dump(mode="json", by_alias=True, exclude_none=True)
            for tool in await client.list_tools()
        ]
        initialize = client.initialize_result.model_dump(
            mode="json", by_alias=True, exclude_none=True
        )
    return {
        "initialize": initialize,
        "tool_count": len(tools),
        "tools_list_bytes": len(
            json.dumps({"tools": tools}, separators=(",", ":")).encode()
        ),
        "input_schema_bytes": sum(
            len(json.dumps(t["inputSchema"], separators=(",", ":")).encode())
            for t in tools
        ),
        "tools": tools,
    }
