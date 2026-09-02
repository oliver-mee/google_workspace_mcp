# Changelog

This fork uses semantic versioning for the MCP server package and tool
contract:

- **PATCH** (`1.26.1`): bug fixes, security fixes, and documentation-only
  changes that do not alter the published tool/action contract.
- **MINOR** (`1.27.0`): backwards-compatible tools, dispatcher actions,
  parameters, or client-visible contract improvements.
- **MAJOR** (`2.0.0`): removal/rename of tools or actions, incompatible
  parameter/response changes, or permission/approval semantics that require
  connector review.

The version is the single package version in `pyproject.toml` (mirrored in
`uv.lock`). The server exposes it through the MCP `initialize` handshake as
`serverInfo.version` and through `/health` as `version`. Clients should refresh
`tools/list` after reconnecting or when the server version changes; a GPT's own
"Version name" (for example, `dev mode`) is separate connector metadata.

## 1.28.0 — 2026-09-02

- Shipped the Chat dispatcher pair (`chat_read` + `chat_manage`, 4 actions
  each) — the original 2026-08-28 dispatcher prototype that proved the
  dispatcher + per-tool scope-gate pattern, previously uncommitted. Read
  loads at read-only scopes (chat_read: find_space, list_spaces,
  list_reactions, list_threads); write loads at chat_write + chat_spaces
  (chat_manage: create_space, dm_space, create_reaction, delete_reaction).
- Tier-wired: `chat_read` at chat extended, `chat_manage` at chat complete;
  legacy Chat tools stay live during migration.
- Published explicit client-visible descriptions for both dispatchers
  (per-action required fields) — the same bug the sheets descriptions fixed.
- 30 tests (tests/gchat), including schema-enum, scope-separation, and
  dispatcher routing coverage.

## 1.27.0 — 2026-09-02

- Added progressive disclosure to `sheets_read` `export` (reference implementation
  of the map/navigate/extract contract): `map=true` returns a token-bounded
  block index; `navigate='<block>'`/`'<block>.<row>'` fetches content at an
  ordinal; `head=<tokens>` (+ `navigate`) bounds a window and returns a
  `next_call` hint; `skip_tokens=<tokens>` continues it. PASSTHROUGH when all
  four are omitted; token counts are estimates (~4 chars/token).
- Made the FastMCP Cloud entrypoint inspectable: `WORKSPACE_MCP_PREPARE_SERVER=0`
  skips import-time credentials/auth wiring so `fastmcp inspect`/`list` work
  without OAuth secrets; default behavior unchanged.
- Added `scripts/dump_mcp_contract.py` (registered-surface probe) and
  version-lockstep contract tests.

## 1.26.0 — 2026-09-02

- Added the Sheets dispatcher surface:
  - `sheets_read`
  - `sheets_manage`
  - `sheets_delete`
- Added native Sheets table creation and typed-column support.
- Added the destructive Sheets safety split and MCP annotations.
- Added explicit client-visible action contracts listing required fields for
  every Sheets dispatcher action.
- Kept legacy Sheets tools live during migration for compatibility.
