"""Entrypoint inspect-safety tests for the FastMCP Cloud module.

fastmcp_server.py force-enables OAuth 2.1 + stateless at import (by design)
and wires HTTP auth at module level. WORKSPACE_MCP_PREPARE_SERVER=0 must make
the module importable without Google OAuth secrets so `fastmcp inspect` /
`fastmcp list` work; the default path must still raise loudly when the OAuth
config is missing (that is the production contract).
"""

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

PROBE = """
import asyncio
import fastmcp_server  # noqa: F401  (must import without OAuth secrets)
from core.server import server

async def main():
    tools = {t.name: t for t in await server.list_tools()}
    print(len(tools), "sheets_read" in tools)

asyncio.run(main())
"""


def _run_probe(extra_env: dict) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.update(
        {
            "MCP_SINGLE_USER_MODE": "true",
            "USER_GOOGLE_EMAIL": "test@example.com",
            "WORKSPACE_MCP_PERMISSIONS": "",
            "WORKSPACE_MCP_DISABLED_TOOLS": "",
        }
    )
    env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-c", PROBE],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_prepare_server_0_imports_without_oauth_secrets():
    """Inspection mode: import succeeds and the full surface registers."""
    result = _run_probe(
        {
            "WORKSPACE_MCP_PREPARE_SERVER": "0",
            "GOOGLE_OAUTH_CLIENT_ID": "",
            "GOOGLE_OAUTH_CLIENT_SECRET": "",
            "MCP_ENABLE_OAUTH21": "true",  # entrypoint force-enables it regardless
        }
    )
    assert result.returncode == 0, f"import failed: {result.stderr[-2000:]}"
    count, has_read = result.stdout.strip().split()[-2:]
    assert has_read == "True"
    assert int(count) > 50  # the sheets family alone is larger than this


def test_default_path_still_raises_without_oauth_config():
    """Production default: missing OAuth config at import is a loud failure."""
    result = _run_probe(
        {
            "GOOGLE_OAUTH_CLIENT_ID": "",
            "GOOGLE_OAUTH_CLIENT_SECRET": "",
            "MCP_ENABLE_OAUTH21": "true",
        }
    )
    assert result.returncode != 0
    assert "GOOGLE_OAUTH_CLIENT_ID" in result.stderr or "requires" in result.stderr
