"""Contract tests for MCP versioning and Sheets dispatcher metadata."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))


REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("path", [REPO_ROOT / "pyproject.toml", REPO_ROOT / "uv.lock"])
def test_release_version_is_1_26_0(path):
    text = path.read_text()
    assert 'version = "1.26.0"' in text


def test_changelog_documents_client_refresh_contract():
    text = (REPO_ROOT / "CHANGELOG.md").read_text()
    assert "serverInfo.version" in text
    assert "tools/list" in text
    assert "1.26.0" in text


def test_sheets_dispatchers_publish_action_contracts():
    """The action-specific minimum fields must reach MCP clients."""
    probe = """
import asyncio
import json
import gsheets.sheets_tools  # noqa: F401
from core.server import server

async def main():
    tools = {tool.name: tool for tool in await server.list_tools()}
    names = ["sheets_read", "sheets_manage", "sheets_delete"]
    print(json.dumps({
        name: {
            "description": tools[name].description,
            "required": tools[name].parameters["required"],
            "properties": list(tools[name].parameters["properties"]),
        }
        for name in names
    }))

asyncio.run(main())
"""
    env = os.environ.copy()
    env.update(
        {
            "MCP_ENABLE_OAUTH21": "false",
            "MCP_SINGLE_USER_MODE": "true",
            "WORKSPACE_MCP_STATELESS_MODE": "false",
            "USER_GOOGLE_EMAIL": "test@example.com",
            "WORKSPACE_MCP_PERMISSIONS": "",
            "WORKSPACE_MCP_DISABLED_TOOLS": "",
        }
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    payload = __import__("json").loads(result.stdout.strip().splitlines()[-1])

    read = payload["sheets_read"]
    manage = payload["sheets_manage"]
    delete = payload["sheets_delete"]

    assert "table_get requires table_id or table_name" in (read["description"] or "")
    assert "export accepts optional range_name" in (read["description"] or "")
    assert "table_create requires range_name and table_name" in (
        manage["description"] or ""
    )
    assert "batch_update requires params.requests" in (manage["description"] or "")
    assert "requires sheets:full" in (delete["description"] or "")
    assert "delete_dimension requires sheet_name" in (delete["description"] or "")

    for tool in (read, manage, delete):
        assert "spreadsheet_id" in tool["required"]
        assert "action" in tool["required"]
        assert "params" in tool["properties"]
