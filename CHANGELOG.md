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
