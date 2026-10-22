# Carried patches on v2.1.2

This integration candidate starts at upstream tag `v2.1.2`
(`49e5e29bc1202ebf4e7218419cd8ff501de556f3`). Fork release `2.1.2+ome.1` carries the patches below.
Use upstream names and arguments for overlapping operations; retired dispatcher
actions have no compatibility aliases. Future upstream submissions must be split
onto clean upstream branches.

| Patch | Reason and removal condition |
| --- | --- |
| Narrow Drive OAuth scopes | Use `drive.readonly` plus `drive.file`, including comment writers and the Apps Script project-delete path. Remove when upstream offers this policy. Files lacking an app grant remain unwritable. Upstream service-account/DWD scope widening is a separate, unchanged mode. |
| Sheets gap dispatchers | Keep 8 read, 12 manage and 2 delete actions for charts, native table creation/removal, Connected Sheets, formatting inspection, CSV navigation, cross-spreadsheet copies, find/replace, banding, general validation, notes and filters. Remove individual actions when equivalent upstream tools cover them. |
| Upstream table/metadata reads | Extend `list_sheet_tables` with selection and typed-column/dropdown detail; include named ranges and timezone in `get_spreadsheet_info`. These replace duplicate read actions. |
| Action permission guards | `sheets:manage` excludes explicit clears, tab/row/column/table deletion, row moves and value-discarding merges. Mixed upstream tools enforce this at invocation; full-only tools are filtered from registration. Ordinary edits can overwrite values. Metadata removal stays available. There is no raw Sheets batch tool. `chat:manage` excludes reaction deletion. |
| Sheet value fidelity | Preserve numeric/boolean/null inputs and metadata-only reads, including notes on empty cells, while retaining upstream range/cell-count limits and smart chips. |
| Slides envelope/null handling | Advertise exactly one supported request key per item using a compact schema; use upstream request validation and strip nested optional nulls before Google. Avoid a second hand-maintained copy of Google's payload schemas. |
| Basic client ID normalization | Adapt only the supported provider's `/token` route when Basic carries the client ID without a form-body ID. Retain SDK authentication, secret verification, PKCE, refresh validation and FastMCP's public-client DCR policy. No Docker or site-packages monkey patches. Remove after the pinned HTTP path handles both code and refresh grants natively. |
| Version and contract inspection | MCP initialize reports the installed package version. `main.py --inspect-contract PATH` exits after registration and native MCP inspection, before credential checks or HTTP auth setup. `scripts/dump_mcp_contract.py PATH [options]` forwards to that path. Cloud registration honors environment permission profiles and keeps HTTP authentication enabled. |

Gap action parameters are supplied in `params`. Read
`sheets://actions/{action}` to inspect a Sheets action's fields and defaults.
For CSV, `params.map=true` yields a block index, `navigate="block.row"` selects
a logical CSV record, and `head`/`skip_tokens` bound continuation windows. Map
pages, including their JSON metadata, stay below 2,000 estimated tokens; follow
`next_call` (`map=true`, `map_start=block`) for every remaining block, merging it
into the existing export params to retain range_name. Conflicting
native table ID/name selectors are rejected; an unknown ID never falls back to a
name. Chart creation/type changes support basic charts only; PIE is rejected
before mutation because it requires a different Google ChartSpec branch.
Chat named-space creation requires a display name and rejects group-chat import
mode, which this user-OAuth surface does not support.

Token counts are character-based estimates. Each call re-reads the source;
concurrent sheet edits can change ordinal positions.

Representative offline commands (use isolated HOME/OAuth storage):

```sh
uv sync --frozen --extra test --extra dev
uv run --frozen ruff check
uv run --frozen ruff format --check
uv run --frozen pytest
uv run --frozen python scripts/dump_mcp_contract.py /tmp/contract.json --permissions sheets:manage chat:manage drive:full slides:full
```

Tests exercise registered MCP tools over streamable HTTP and synthetic OAuth
routes. Google calls are mocked. Browser/connector acceptance, real Google OAuth,
live API operations and deployment are separate acceptance steps.

Before deployment, build from a clean Git archive and verify container startup,
MCP initialize/tools/list with isolated configuration, and compatibility with a
copy of any existing encrypted OAuth store. Preserve the prior image and store
for rollback. Health alone does not establish OAuth or live-provider acceptance.

## OAuth approval presentation and remembered decisions

The local provider delegates security processing to pinned FastMCP and supplies a
compact escaped consent page. Remembered decisions bind exact client metadata,
redirect, scopes and resource and apply only to same-origin browser navigation.
Cross-site ChatGPT requests still require approval; client authentication does not
prove which user initiated a ChatGPT connection. No trusted-domain bypass, dependency
source patch, new scopes or token-store migration is introduced. Older open consent
forms need restarting. See [OAuth approval and listing metadata](oauth-approval.md)
for the threat model, adapter contract, defaults and client listing checklist.
