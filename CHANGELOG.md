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

## 1.29.2 — 2026-09-02

MINOR release: `sheets_read action="get"` now returns cell values for a range
(legacy semantics) instead of the spreadsheet summary. The summary is
still available via `action="metadata"`. **Migration:** callers that used
`get` for metadata should switch to `metadata`; callers that used `get`
expecting values now get them directly.

- Default `range_name` falls back to the first sheet's title when omitted.
- `SHEETS_READ_DESCRIPTION` published to clients: `metadata` (summary) and
  `get` (cell values) are now distinct, both with their required fields
  spelled out.
- The action's output shape changed (metadata → cell values), so per the
  semantic-versioning policy this is a MINOR bump.

Three regression tests (tests/gsheets/test_sheets_1_29_fixes.py).

## 1.29.1 — 2026-09-02

Retest-driven bugfixes (3 of the 8 failures from the GPT connector E2E
on 1.29.0). The other five fixes from 1.29.0 (banding atomicity, values
schema, chart_create rectangular ranges, datasource masks, notes early-exit
on empty cells) remain — these patches sharpen the cases that still slipped
through under realistic client patterns.

- **`datasource_table_describe` mask** — Google 400s on
  `sheets.dataSourceTables.dataSourceId` because `dataSourceTables` is a
  conditional array that does not exist on sheets without a Connected Sheets
  data source. Removed the explicit leaf fields; the parent array still
  appears in responses when present.
- **`read_sheet_values` note-only cells** — `_fetch_grid_metadata` returned
  `("", "")` for empty ranges because `_a1_range_for_values` could not
  compute tight bounds. Falls back to the user's original range when values
  is empty, so a note on an empty cell is now returned instead of being
  swallowed by the "No data found" early exit.
- **`banding_set` top-level colors** — the dispatcher was reading colors
  only from the `params` dict, so callers passing them as top-level kwargs
  (the natural shape for an MCP-aware client) saw them silently dropped and
  were rejected with "Provide at least one of: ...". Now reads top-level
  kwargs first with `params` as fallback; the error message explains the
  fallback; the `banding_set` docstring line documents both shapes.

Five regression tests in `tests/gsheets/test_sheets_1_29_fixes.py`.

## 1.29.0 — 2026-09-02

Bugfix release driven by the ChatGPT connector E2E test (95 invocations,
87 passed). Fixes:

- **banding_set atomicity** — a successful `addBanding` mutation can never be
  surfaced as an error: reply parsing is defensive and a missing
  `bandedRangeId` is recovered by re-reading the sheet. Errors remain
  pre-flight only.
- **datasource actions unblocked** — `datasource_describe` and
  `datasource_table_describe` sent invalid Sheets fields masks
  (`dataSources.type`, `dataSourceTables.syncState` — neither exists in the
  v4 discovery schema). Masks now use only valid fields
  (`dataSources(dataSourceId,spec,sheetId)` and
  `dataSourceTables(dataSourceId,columnSelectionType,columns,dataExecutionStatus)`),
  with sync state derived from `dataExecutionStatus.state`, and a discovery-
  aligned lockstep test.
- **modify_sheet_values cell schema** — `values` accepted only strings in the
  generated schema; nested cells now allow strings, numbers, booleans, and
  null (runtime already handled them).
- **chart_create rectangular ranges** — a multi-column range is translated the
  way the Sheets UI does: first column = domain (labels), remaining columns =
  series. Single-column ranges keep the legacy shape that already worked.
- **read_sheet_values note/hyperlink-only cells** — the "No data found" early
  exit no longer discards fetched notes/hyperlinks when the range has values
  only in metadata.

Eleven regression tests (tests/gsheets/test_sheets_1_29_fixes.py).

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
