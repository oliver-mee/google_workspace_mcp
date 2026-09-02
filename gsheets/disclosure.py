"""Progressive disclosure for data-returning Sheets read actions.

Three-layer contract (map -> navigate -> extract) that keeps large returns
inside a client's token budget. Adapted from the backlog_view reference
implementation (Jamie-BitFlight/claude_skills, docs/mcp-progressive-disclosure-contract.md)
and distilled to a single flat artifact: CSV text produced by the ``export``
action.

Token accounting is an *estimate* (chars / 4, i.e. ~4 chars per token) with no
new dependencies. All limits below use that estimator; character-count
approximations are used deliberately and documented as such.

Layer semantics (all optional; absent params = PASSTHROUGH, unchanged output):

- ``map=True``: ordered block index of the artifact, always < 2,000 estimated
  tokens. Block lines read ``{ordinal} {desc} ({est}t) - "{preview}"``.
  Ordinals are blocks ``0..N-1``; block ``b`` row ``r`` is ``b.r``.
- ``navigate="b"`` or ``navigate="b.r"``: full content at that ordinal.
- ``head=N`` (with navigate): first N estimated tokens of that ordinal's
  content; ``skip_tokens=S`` (with head and navigate) continues the window.
  Responses carry a ``next_call`` hint object ready to feed back in.
- Error-on-miss: unknown ordinals raise UserInputError listing the valid
  block ordinals rather than silently falling back.
"""

from __future__ import annotations

import json
import math
from typing import List, Optional, Tuple

from core.utils import UserInputError

#: Rows per map block. Keeps map_text bounded for wide sheets.
BLOCK_SIZE = 25
#: Estimated token budget guarantee for map responses.
MAP_TOKEN_BUDGET = 2000
#: Upper bound for a single extract window.
MAX_HEAD_TOKENS = 25_000


def estimate_tokens(text: str) -> int:
    """Estimated token count: ~4 chars per token (documented approximation)."""
    return max(1, math.ceil(len(text) / 4))


def _rows(csv_text: str) -> List[str]:
    lines = csv_text.split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    return lines


def _block_bounds(csv_text: str) -> Tuple[int, int, int]:
    """Return (total_rows, total_blocks, token estimate) for the artifact."""
    total = len(_rows(csv_text))
    blocks = max(1, math.ceil(total / BLOCK_SIZE)) if total else 0
    return total, blocks, estimate_tokens(csv_text)


def _preview(line: str, cap: int = 60) -> str:
    text = line.strip()
    return text[:cap] + ("..." if len(text) > cap else "")


def valid_ordinals(csv_text: str) -> List[str]:
    """Block ordinals (``0``..``N-1``); rows inside a block address as ``b.r``."""
    total, blocks, _ = _block_bounds(csv_text)
    if not total:
        return []
    return [str(i) for i in range(blocks)]


def _resolve_ordinal(csv_text: str, ordinal: str) -> Tuple[int, Optional[int]]:
    """Resolve ``b`` or ``b.r`` to (block_index, row_index|None)."""
    rows = _rows(csv_text)
    total, blocks, _ = _block_bounds(csv_text)
    if not rows:
        raise UserInputError(
            "The export result is empty; there is nothing to navigate."
        )
    parts = ordinal.split(".")
    if not all(p.isdigit() for p in parts) or len(parts) > 2:
        raise UserInputError(
            f"Ordinal '{ordinal}' is invalid. Format: '<block>' or '<block>.<row>' "
            f"(e.g. '0' or '0.3'). Valid block ordinals: {valid_ordinals(csv_text)}"
        )
    block = int(parts[0])
    if block >= blocks:
        raise UserInputError(
            f"Ordinal '{ordinal}' not found. Valid block ordinals: "
            f"{valid_ordinals(csv_text)}. Use '<block>.<row>' for a specific row "
            f"(e.g. '0.3')."
        )
    start = block * BLOCK_SIZE
    end = min(start + BLOCK_SIZE, total)
    row = int(parts[1]) if len(parts) == 2 else None
    if row is not None and (row < 0 or start + row >= end):
        raise UserInputError(
            f"Row '{row}' not found in block '{parts[0]}' (rows "
            f"{start}-{end - 1}). Use navigate='{parts[0]}' for the whole block."
        )
    return block, row


def build_map(csv_text: str) -> str:
    """Layer 1 - MAP: ordinal block structure, guaranteed < 2,000 est. tokens."""
    rows = _rows(csv_text)
    total, blocks, total_est = _block_bounds(csv_text)
    if not total:
        return json.dumps(
            {
                "action": "export",
                "mode": "map",
                "total_blocks": 0,
                "total_rows": 0,
                "total_est_tokens": 0,
                "over_budget": False,
                "map_text": "The export result is empty.",
            }
        )
    lines = []
    for b in range(blocks):
        start = b * BLOCK_SIZE
        end = min(start + BLOCK_SIZE, total)
        block_text = "\n".join(rows[start:end])
        preview = _preview(rows[start])
        lines.append(
            f"{b} rows {start + 1}-{end} ({estimate_tokens(block_text)}t)"
            f"{' — ' + chr(34) + preview + chr(34) if preview else ''}"
        )
    return json.dumps(
        {
            "action": "export",
            "mode": "map",
            "total_blocks": blocks,
            "total_rows": total,
            "total_est_tokens": total_est,
            "over_budget": total_est > MAP_TOKEN_BUDGET,
            "map_text": "\n".join(lines),
            "usage": "Call navigate='<block>' (or '<block>.<row>') to fetch content; "
            "add head=<tokens> to bound the window.",
        }
    )


def _content_at(csv_text: str, ordinal: str) -> str:
    rows = _rows(csv_text)
    block, row = _resolve_ordinal(csv_text, ordinal)
    start = block * BLOCK_SIZE
    if row is None:
        return "\n".join(rows[start : start + BLOCK_SIZE])
    return rows[start + row]


def navigate_or_extract(
    csv_text: str, ordinal: str, head: Optional[int] = None, skip_tokens: int = 0
) -> str:
    """Layer 2/3 - NAVIGATE or EXTRACT at an ordinal."""
    content = _content_at(csv_text, ordinal)
    total_tokens = estimate_tokens(content)

    if head is None:  # NAVIGATE
        if skip_tokens:
            raise UserInputError("skip_tokens requires head (extract mode).")
        return json.dumps(
            {
                "action": "export",
                "mode": "navigate",
                "ordinal": ordinal,
                "content": content,
                "total_tokens": total_tokens,
                "truncated": False,
            }
        )

    if head < 1 or head > MAX_HEAD_TOKENS:
        raise UserInputError(
            f"head must be between 1 and {MAX_HEAD_TOKENS} tokens (got {head})."
        )
    if skip_tokens < 0:
        raise UserInputError(f"skip_tokens must be >= 0 (got {skip_tokens}).")

    skip_chars = skip_tokens * 4
    window = content[skip_chars : skip_chars + head * 4]
    truncated = len(content) > skip_chars + head * 4
    return json.dumps(
        {
            "action": "export",
            "mode": "extract",
            "ordinal": ordinal,
            "content": window,
            "total_tokens": total_tokens,
            "returned_tokens": estimate_tokens(window),
            "truncated": truncated,
            "next_call": (
                None
                if not truncated
                else json.dumps(
                    {
                        "navigate": ordinal,
                        "head": head,
                        "skip_tokens": skip_tokens + head,
                    }
                )
            ),
        }
    )


def export_response(
    csv_text: str,
    map_flag: bool = False,
    navigate: Optional[str] = None,
    head: Optional[int] = None,
    skip_tokens: int = 0,
) -> str:
    """Route an export result through the disclosure layers.

    Raises UserInputError on invalid parameter combinations or ordinal misses.
    With no disclosure parameters supplied this is a PASSTHROUGH (returns the
    CSV unchanged, preserving the legacy behavior).
    """
    if not (map_flag or navigate is not None or head is not None or skip_tokens):
        return csv_text

    if map_flag and navigate is not None:
        raise UserInputError("map and navigate are mutually exclusive.")
    if map_flag and head is not None:
        raise UserInputError("map is incompatible with head.")
    if head is not None and navigate is None:
        raise UserInputError("head requires navigate.")
    if skip_tokens and head is None:
        raise UserInputError("skip_tokens requires head and navigate.")

    if map_flag:
        return build_map(csv_text)
    if navigate is None:
        raise UserInputError(
            "navigate is required for disclosure modes (or use map=true)."
        )

    return navigate_or_extract(csv_text, navigate, head=head, skip_tokens=skip_tokens)
