"""Use the production CLI registration path to write an MCP contract without serving.

Example: uv run python scripts/dump_mcp_contract.py /tmp/contract.json --permissions sheets:manage chat:manage
All options after PATH are forwarded to main.py. Isolate HOME and credential
storage when comparing test profiles. This command never configures HTTP auth.
"""

import subprocess
import sys
from pathlib import Path

if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    root = Path(__file__).resolve().parents[1]
    raise SystemExit(
        subprocess.call(
            [
                sys.executable,
                str(root / "main.py"),
                "--inspect-contract",
                *sys.argv[1:],
            ],
            cwd=root,
        )
    )
