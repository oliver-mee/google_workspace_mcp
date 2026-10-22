"""Cold entrypoint and inspection probes using isolated state and blocked sockets."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
BLOCK_NETWORK = """
import socket

def blocked(*args, **kwargs):
    raise AssertionError('Unexpected network connection in offline contract probe')

socket.socket.connect = blocked
"""


@pytest.mark.parametrize("entrypoint", ["main", "cloud"])
@pytest.mark.parametrize("level", ["readonly", "manage", "full"])
def test_cold_entrypoint_contract(tmp_path, entrypoint, level):
    output = tmp_path / "contract.json"
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "WORKSPACE_MCP_CREDENTIALS_DIR": str(tmp_path / "credentials"),
        "WORKSPACE_MCP_PERMISSIONS": f"sheets:{level} chat:{level} drive:full slides:full",
        "FASTMCP_CHECK_FOR_UPDATES": "off",
    }
    if entrypoint == "main":
        code = (
            BLOCK_NETWORK
            + "\nimport runpy, sys\nsys.argv = ['main.py', '--inspect-contract', sys.argv[1]]\nrunpy.run_path('main.py', run_name='__main__')\n"
        )
    else:
        env.update(
            {
                "GOOGLE_OAUTH_CLIENT_ID": "synthetic-client.apps.googleusercontent.com",
                "GOOGLE_OAUTH_CLIENT_SECRET": "synthetic-secret",
                "FASTMCP_SERVER_AUTH_GOOGLE_JWT_SIGNING_KEY": "synthetic-signing-key",
            }
        )
        code = (
            BLOCK_NETWORK
            + "\nimport asyncio, json, sys\nfrom pathlib import Path\nimport fastmcp_server\nfrom core.contract import inspect_contract\nassert fastmcp_server.server.auth is not None\nPath(sys.argv[1]).write_text(json.dumps(asyncio.run(inspect_contract(fastmcp_server.server))))\n"
        )
    result = subprocess.run(
        [sys.executable, "-c", code, str(output)],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=45,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    contract = json.loads(output.read_text())
    assert contract["initialize"]["serverInfo"]["version"] == "2.1.2"
    tools = {tool["name"]: tool for tool in contract["tools"]}
    assert "sheets_read" in tools
    assert ("sheets_manage" in tools) == (level != "readonly")
    assert ("sheets_delete" in tools) == (level == "full")
    assert "search_gmail_messages" not in tools
    assert contract["input_schema_bytes"] > 0
    if entrypoint == "main":
        # Inspection exits before credential directory creation and HTTP auth setup.
        assert not (tmp_path / "credentials").exists()


def test_inspection_honors_native_disabled_tool_filter(tmp_path):
    output = tmp_path / "contract.json"
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "WORKSPACE_MCP_CREDENTIALS_DIR": str(tmp_path / "creds"),
    }
    result = subprocess.run(
        [
            sys.executable,
            "main.py",
            "--inspect-contract",
            str(output),
            "--permissions",
            "sheets:full",
            "--disabled-tools",
            "sheets_delete",
            "sheets_manage",
        ],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=45,
    )
    assert result.returncode == 0, result.stderr
    names = {t["name"] for t in json.loads(output.read_text())["tools"]}
    assert "sheets_read" in names
    assert not {"sheets_delete", "sheets_manage"} & names
