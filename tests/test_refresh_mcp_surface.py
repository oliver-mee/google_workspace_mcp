"""Native streamable HTTP/registered-tool regressions with synthetic identities."""

import json
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastmcp import FastMCP, Client
from fastmcp.server.auth import AccessToken, TokenVerifier
from starlette.testclient import TestClient

import auth.permissions as permissions
import auth.service_decorator as decorators
import core.tool_registry as registry
from auth.auth_info_middleware import AuthInfoMiddleware
from auth.permissions import get_all_permission_scopes
from auth.scopes import PROTOCOL_AUTH_SCOPES, DRIVE_SCOPE
from core.portable_schema_middleware import PortableSchemaMiddleware
from core.server import server
from gsheets import sheets_tools, sheets_dispatch_helpers
from gchat import chat_tools  # noqa: F401
from gslides import slides_tools  # noqa: F401


class SyntheticVerifier(TokenVerifier):
    async def verify_token(self, token):
        if token not in ("alice-token", "bob-token", "limited-token"):
            return None
        email = "bob@example.test" if token == "bob-token" else "alice@example.test"
        scopes = (
            PROTOCOL_AUTH_SCOPES
            if token == "limited-token"
            else [*PROTOCOL_AUTH_SCOPES, *get_all_permission_scopes()]
        )
        return AccessToken(
            token=token,
            client_id="synthetic-client",
            scopes=scopes,
            claims={"email": email},
        )


def google_service():
    service = Mock()
    service.spreadsheets().get().execute.return_value = {
        "properties": {"title": "Synthetic", "locale": "en", "timeZone": "UTC"},
        "namedRanges": [
            {
                "name": "Totals",
                "namedRangeId": "nr1",
                "range": {
                    "sheetId": 0,
                    "startRowIndex": 0,
                    "endRowIndex": 5,
                    "startColumnIndex": 0,
                    "endColumnIndex": 2,
                },
            }
        ],
        "dataSources": [
            {"dataSourceId": "ds1", "spec": {"bigQuery": {"projectId": "synthetic"}}}
        ],
        "sheets": [
            {
                "properties": {
                    "sheetId": 0,
                    "title": "Sheet1",
                    "gridProperties": {"rowCount": 100, "columnCount": 10},
                },
                "tables": [
                    {
                        "tableId": "tbl1",
                        "name": "Table1",
                        "range": {
                            "sheetId": 0,
                            "startRowIndex": 0,
                            "endRowIndex": 5,
                            "startColumnIndex": 0,
                            "endColumnIndex": 2,
                        },
                        "columnProperties": [
                            {"columnName": "Name", "columnType": "TEXT"}
                        ],
                    }
                ],
                "charts": [
                    {
                        "chartId": 42,
                        "spec": {"title": "Old", "basicChart": {"chartType": "COLUMN"}},
                    }
                ],
                "bandedRanges": [{"bandedRangeId": 7, "range": {"sheetId": 0}}],
                "data": [
                    {
                        "rowData": [
                            {
                                "values": [
                                    {
                                        "formattedValue": "test",
                                        "hyperlink": "https://example.test",
                                        "userEnteredFormat": {
                                            "numberFormat": {"type": "TEXT"}
                                        },
                                        "dataValidation": {
                                            "condition": {
                                                "type": "ONE_OF_LIST",
                                                "values": [{"userEnteredValue": "A"}],
                                            }
                                        },
                                        "dataSourceTable": {
                                            "dataSourceId": "ds1",
                                            "dataExecutionStatus": {
                                                "state": "SUCCEEDED"
                                            },
                                            "columns": [],
                                        },
                                    }
                                ]
                            }
                        ]
                    }
                ],
            }
        ],
    }
    service.spreadsheets().values().get().execute.return_value = {
        "values": [["Name", "Count"], ["alpha", 2]],
        "range": "Sheet1!A1:B2",
    }
    service.spreadsheets().values().clear().execute.return_value = {
        "clearedRange": "Sheet1!A2:B5"
    }
    service.spreadsheets().values().update().execute.return_value = {"updatedCells": 1}
    service.spreadsheets().sheets().copyTo().execute.return_value = {"title": "Copied"}
    service.spreadsheets().batchUpdate().execute.return_value = {
        "replies": [
            {
                "addTable": {"table": {"tableId": "created"}},
                "addChart": {"chart": {"chartId": 43}},
                "addBanding": {"bandedRange": {"bandedRangeId": 8}},
            }
        ]
    }
    service.spaces().list().execute.return_value = {
        "spaces": [{"name": "spaces/s1", "displayName": "Synthetic"}]
    }
    service.spaces().create().execute.return_value = {
        "name": "spaces/new",
        "displayName": "New",
    }
    service.spaces().findDirectMessage().execute.return_value = {"name": "spaces/dm"}
    service.spaces().messages().list().execute.return_value = {
        "messages": [{"thread": {"name": "spaces/s1/threads/t1"}}]
    }
    service.spaces().messages().reactions().list().execute.return_value = {
        "reactions": [
            {"name": "spaces/s1/messages/m1/reactions/r1", "emoji": {"unicode": "x"}}
        ]
    }
    service.comments().create().execute.return_value = {
        "id": "c1",
        "author": {"displayName": "Synthetic"},
        "createdTime": "2026-10-22T00:00:00Z",
    }
    service.presentations().batchUpdate().execute.return_value = {"replies": [{}]}
    service.reset_mock()
    return service


@pytest.fixture
def native(monkeypatch):
    service = google_service()
    built = []
    monkeypatch.setattr(decorators, "is_oauth21_enabled", lambda: True)
    monkeypatch.setattr(decorators, "is_service_account_enabled", lambda: False)
    monkeypatch.setattr(decorators, "is_trust_gateway_identity", lambda: False)
    monkeypatch.setattr(decorators, "get_auth_provider", lambda: object())

    async def credentials(token, email, session):
        return SimpleNamespace(scopes=token.scopes, synthetic_email=email)

    monkeypatch.setattr(decorators, "ensure_session_from_access_token", credentials)
    monkeypatch.setattr(decorators, "recycling", nullcontext)

    def build(name, version, credentials):
        built.append((name, credentials.synthetic_email))
        return service

    monkeypatch.setattr(decorators, "build_google_service", build)
    monkeypatch.setattr(registry, "_enabled_tools", None)
    monkeypatch.setattr(registry, "_disabled_tools", set())
    monkeypatch.setattr(permissions, "_PERMISSIONS", None)

    def create(level):
        permissions.set_permissions(
            {"sheets": level, "chat": level, "drive": "full", "slides": "full"}
        )
        probe = FastMCP(
            "refresh-test", version=server.version, auth=SyntheticVerifier()
        )
        probe.add_middleware(AuthInfoMiddleware())
        probe.add_middleware(PortableSchemaMiddleware())
        for name, tool in registry.get_tool_components(server).items():
            if name in {
                "sheets_read",
                "sheets_manage",
                "sheets_delete",
                "read_sheet_values",
                "modify_sheet_values",
                "get_spreadsheet_info",
                "manage_spreadsheet_comment",
                "manage_presentation_comment",
                "list_sheet_tables",
                "format_sheet_range",
                "resize_sheet_dimensions",
                "manage_sheet_tab",
                "move_sheet_rows",
                "chat_read",
                "chat_manage",
                "batch_update_presentation",
            }:
                probe.add_tool(tool)
        registry.filter_server_tools(probe)
        return TestClient(
            probe.http_app(path="/mcp", stateless_http=True, json_response=True)
        )

    return create, service, built


def rpc(client, method, params=None, token="alice-token"):
    response = client.post(
        "/mcp",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-11-25",
        },
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
    )
    assert response.status_code == 200, response.text
    return response.json()


def call(client, name, **arguments):
    arguments.setdefault("user_google_email", "spoofed@example.test")
    return rpc(client, "tools/call", {"name": name, "arguments": arguments})


def success(response):
    assert "error" not in response, response
    assert not response["result"].get("isError"), response
    return response["result"]["content"][0]["text"]


def rejected(response):
    assert "error" in response or response["result"].get("isError"), response


@pytest.mark.parametrize("level", ["readonly", "manage", "full"])
def test_registered_profiles_and_real_initialize(native, level):
    create, service, _ = native
    with create(level) as client:
        init = rpc(
            client,
            "initialize",
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "synthetic", "version": "1"},
            },
        )
        assert init["result"]["serverInfo"]["version"] == "2.1.2+ome.1"
        tools = {t["name"]: t for t in rpc(client, "tools/list")["result"]["tools"]}
        assert "sheets_read" in tools
        assert ("sheets_manage" in tools) == (level != "readonly")
        assert ("sheets_delete" in tools) == (level == "full")
        assert ("move_sheet_rows" in tools) == (level == "full")
        assert tools["sheets_read"]["annotations"]["readOnlyHint"] is True
        if level == "full":
            assert tools["sheets_delete"]["annotations"]["destructiveHint"] is True
        actions = tools["sheets_read"]["inputSchema"]["properties"]["action"]["enum"]
        assert not {"get", "metadata", "named_range_get", "named_range_list"} & set(
            actions
        )
        success(
            call(
                client,
                "sheets_read",
                spreadsheet_id="ss1",
                action="export",
                params={"map": True},
            )
        )
        if level == "readonly":
            rejected(
                call(
                    client,
                    "sheets_manage",
                    spreadsheet_id="ss1",
                    action="table_create",
                    params={"table_name": "New", "range_name": "Sheet1!A1:B5"},
                )
            )
            service.spreadsheets().batchUpdate.assert_not_called()


SHEET_CASES = [
    ("read", "chart_get", {"chart_id": 42}),
    ("read", "chart_list", {}),
    ("read", "banding_list", {}),
    ("read", "validation_get", {"range_name": "Sheet1!A1:B5"}),
    ("read", "read_format", {"range_name": "Sheet1!A1"}),
    ("read", "export", {"range_name": "Sheet1!A1:B5", "navigate": "0", "head": 5}),
    ("read", "datasource_describe", {}),
    ("read", "datasource_table_describe", {"range_name": "Sheet1!A1"}),
    ("manage", "table_create", {"table_name": "New", "range_name": "Sheet1!A1:B5"}),
    ("manage", "find_replace", {"find": "alpha", "replacement": "beta"}),
    (
        "manage",
        "sheet_copy",
        {"sheet_name": "Sheet1", "destination_spreadsheet_id": "ss2"},
    ),
    (
        "manage",
        "copy_paste",
        {"source_range": "Sheet1!A1", "destination_range": "Sheet1!C1"},
    ),
    ("manage", "chart_create", {"data_range": "Sheet1!A1:B5"}),
    ("manage", "chart_update", {"chart_id": 42, "title": "New"}),
    ("manage", "chart_delete", {"chart_id": 42}),
    (
        "manage",
        "banding_set",
        {"range_name": "Sheet1!A1:B5", "header_color": "#112233"},
    ),
    ("manage", "banding_clear", {"banded_range_id": 7}),
    (
        "manage",
        "validation_set",
        {
            "range_name": "Sheet1!A1",
            "condition_type": "ONE_OF_LIST",
            "condition_values": ["A", "B"],
        },
    ),
    ("manage", "note_set", {"range_name": "Sheet1!A1", "note": "Synthetic"}),
    ("manage", "filter_set", {"range_name": "Sheet1!A1:B5"}),
    ("delete", "table_clear", {"table_id": "tbl1"}),
    ("delete", "table_delete", {"table_id": "tbl1"}),
]


@pytest.mark.parametrize("family,action,params", SHEET_CASES)
def test_all_retained_sheets_actions_through_http(native, family, action, params):
    create, service, built = native
    with create("full") as client:
        success(
            call(
                client,
                "sheets_" + family,
                spreadsheet_id="ss1",
                action=action,
                params=params,
            )
        )
    assert built == [("sheets", "alice@example.test")]
    if action == "datasource_table_describe":
        assert (
            "dataSourceTable" in service.spreadsheets().get.call_args.kwargs["fields"]
        )


@pytest.mark.parametrize(
    "name,args",
    [
        ("sheets_delete", {"action": "table_clear", "params": {"table_id": "tbl1"}}),
        ("modify_sheet_values", {"range_name": "Sheet1!A1", "clear_values": True}),
        ("manage_sheet_tab", {"sheet_name": "Sheet1", "action": " DELETE "}),
        (
            "resize_sheet_dimensions",
            {"sheet_name": "Sheet1", "delete_rows": [1], "column_sizes": {"A": 20}},
        ),
        (
            "resize_sheet_dimensions",
            {"sheet_name": "Sheet1", "delete_row_range": "1:5"},
        ),
        ("resize_sheet_dimensions", {"sheet_name": "Sheet1", "delete_columns": ["A"]}),
        (
            "move_sheet_rows",
            {
                "source_sheet": "Sheet1",
                "destination_sheet": "Sheet2",
                "start_row": 1,
                "end_row": 2,
            },
        ),
        ("format_sheet_range", {"range_name": "Sheet1!A1:B5", "merge_cells": True}),
        (
            "format_sheet_range",
            {"range_name": "Sheet1!A1:B5", "merge_type": "MERGE_ROWS"},
        ),
        (
            "sheets_manage",
            {
                "action": "batch_update",
                "params": {
                    "requests": [
                        {"addSheet": {"properties": {"title": "New"}}},
                        {"deleteSheet": {"sheetId": 0}},
                    ]
                },
            },
        ),
        (
            "sheets_manage",
            {
                "action": "table_create",
                "params": {
                    "table_name": "New",
                    "range_name": "Sheet1!A1:B5",
                    "requests": [{"deleteSheet": {"sheetId": 0}}],
                },
            },
        ),
        (
            "sheets_manage",
            {
                "action": "batch_update",
                "params": {
                    "requests": [
                        {
                            "updateSheetProperties": {
                                "properties": {
                                    "sheetId": 0,
                                    "gridProperties": {"rowCount": 1},
                                },
                                "fields": "gridProperties.rowCount",
                            }
                        }
                    ]
                },
            },
        ),
    ],
)
def test_manage_rejects_destructive_aliases_mixed_batches_and_shrink(
    native, name, args
):
    create, service, _ = native
    with create("manage") as client:
        service.reset_mock()
        rejected(call(client, name, spreadsheet_id="ss1", **args))
        service.spreadsheets().batchUpdate.assert_not_called()
        service.spreadsheets().values().clear.assert_not_called()


@pytest.mark.parametrize(
    "action,params",
    [
        ("find_space", {"query": "synthetic"}),
        ("list_reactions", {"message_id": "spaces/s1/messages/m1"}),
        ("list_threads", {"space_id": "spaces/s1"}),
        ("create_space", {"display_name": "New"}),
        ("dm_space", {"user_id": "users/123"}),
        ("delete_reaction", {"reaction_id": "spaces/s1/messages/m1/reactions/r1"}),
    ],
)
def test_chat_gap_actions_through_http(native, action, params):
    create, _, built = native
    with create("full") as client:
        family = (
            "read"
            if action in ("find_space", "list_reactions", "list_threads")
            else "manage"
        )
        success(call(client, "chat_" + family, action=action, params=params))
    assert built == [("chat", "alice@example.test")]


def test_native_multiuser_scope_and_input_rejection(native):
    create, service, built = native
    with create("manage") as client:
        # Native token verification and request identity survive caller-supplied spoofing.
        response = rpc(
            client,
            "tools/call",
            {
                "name": "sheets_read",
                "arguments": {
                    "user_google_email": "spoofed@example.test",
                    "spreadsheet_id": "ss1",
                    "action": "export",
                },
            },
            token="bob-token",
        )
        success(response)
        assert built[-1] == ("sheets", "bob@example.test")
        built.clear()
        response = rpc(
            client,
            "tools/call",
            {
                "name": "sheets_read",
                "arguments": {
                    "user_google_email": "alice@example.test",
                    "spreadsheet_id": "ss1",
                    "action": "export",
                },
            },
            token="limited-token",
        )
        assert "Permission required" in success(response)
        assert not built
        for name, args in [
            (
                "sheets_manage",
                {
                    "spreadsheet_id": "ss1",
                    "action": "table_create",
                    "params": {"table_name": "New"},
                },
            ),
            (
                "sheets_manage",
                {
                    "spreadsheet_id": "ss1",
                    "action": "chart_create",
                    "params": {"data_range": "Sheet1!A1:B5", "chart_type": "INVALID"},
                },
            ),
            ("chat_manage", {"action": "dm_space", "params": {"user_id": "invalid"}}),
            (
                "chat_manage",
                {
                    "action": "delete_reaction",
                    "params": {"reaction_id": "spaces/s1/messages/m1/reactions/r1"},
                },
            ),
        ]:
            service.reset_mock()
            rejected(call(client, name, **args))
            service.spreadsheets().batchUpdate.assert_not_called()
            service.spaces().setup.assert_not_called()
            service.spaces().messages().reactions().delete.assert_not_called()


def test_slides_portable_schema_and_null_cleanup_through_http(native):
    create, service, _ = native
    with create("full") as client:
        schema = next(
            t
            for t in rpc(client, "tools/list")["result"]["tools"]
            if t["name"] == "batch_update_presentation"
        )["inputSchema"]["properties"]["requests"]
        assert schema["minItems"] == 1
        assert len(schema["items"]["anyOf"]) == 44
        assert '"type": "null"' not in json.dumps(schema)
        request = {
            "createSlide": {"objectId": "synthetic-slide", "slideLayoutReference": None}
        }
        success(
            call(
                client,
                "batch_update_presentation",
                presentation_id="p1",
                requests=[request],
            )
        )
        assert service.presentations().batchUpdate.call_args.kwargs["body"][
            "requests"
        ] == [{"createSlide": {"objectId": "synthetic-slide"}}]
        for invalid in [
            [],
            [{"unknown": {}}],
            [{"createSlide": {}, "deleteObject": {}}],
            [{"createSlide": None}],
        ]:
            service.reset_mock()
            rejected(
                call(
                    client,
                    "batch_update_presentation",
                    presentation_id="p1",
                    requests=invalid,
                )
            )
            service.presentations().batchUpdate.assert_not_called()


@pytest.mark.asyncio
async def test_contract_resource_and_narrow_drive_scope():
    probe = FastMCP("contract-resource")
    probe.resource("sheets://actions/{action}")(sheets_tools.sheets_action_contract)
    async with Client(probe, mode="legacy") as client:
        result = await client.read_resource("sheets://actions/table_create")
        contract = json.loads(result[0].text)
        assert contract["parameters"]["table_name"]["required"]
    assert DRIVE_SCOPE not in permissions.get_scopes_for_permission("drive", "full")
    from auth.scopes import get_scopes_for_tools

    assert DRIVE_SCOPE not in get_scopes_for_tools(["drive"])
    assert set(
        sheets_dispatch_helpers.READ_ACTIONS
        + sheets_dispatch_helpers.MANAGE_ACTIONS
        + sheets_dispatch_helpers.DELETE_ACTIONS
    ) == {a for _, a, _ in SHEET_CASES}


@pytest.mark.parametrize(
    "name,args,key",
    [
        (
            "modify_sheet_values",
            {"range_name": "Sheet1!A1", "clear_values": True},
            None,
        ),
        (
            "resize_sheet_dimensions",
            {"sheet_name": "Sheet1", "delete_rows": [1]},
            "deleteDimension",
        ),
        (
            "format_sheet_range",
            {"range_name": "Sheet1!A1:B5", "merge_cells": True},
            "mergeCells",
        ),
    ],
)
def test_full_permits_upstream_destructive_operations(native, name, args, key):
    create, service, _ = native
    with create("full") as client:
        success(call(client, name, spreadsheet_id="ss1", **args))
    if key:
        assert any(
            key in request
            for request in service.spreadsheets().batchUpdate.call_args.kwargs["body"][
                "requests"
            ]
        )
    else:
        service.spreadsheets().values().clear.assert_called_once()


def test_named_range_read_uses_upstream_readonly_surface(native):
    create, _, _ = native
    with create("readonly") as client:
        text = success(call(client, "get_spreadsheet_info", spreadsheet_id="ss1"))
        assert "Totals" in text
        assert "Timezone: UTC" in text


def test_dm_setup_is_atomic_and_only_used_for_404(native):
    from googleapiclient.errors import HttpError

    create, service, _ = native
    service.spaces().findDirectMessage().execute.side_effect = HttpError(
        SimpleNamespace(status=404, reason="Not found"), b"{}"
    )
    service.spaces().setup().execute.return_value = {"name": "spaces/newdm"}
    service.reset_mock()
    with create("full") as client:
        success(
            call(
                client,
                "chat_manage",
                action="dm_space",
                params={"user_id": "users/123"},
            )
        )
    body = service.spaces().setup.call_args.kwargs["body"]
    assert body["space"]["spaceType"] == "DIRECT_MESSAGE"
    assert body["memberships"][0]["member"]["name"] == "users/123"
    service.spaces().create.assert_not_called()


def test_find_space_searches_subsequent_pages(native):
    create, service, _ = native
    service.spaces().list().execute.side_effect = [
        {"spaces": [], "nextPageToken": "next"},
        {"spaces": [{"name": "spaces/found", "displayName": "Wanted"}]},
    ]
    service.reset_mock()
    with create("readonly") as client:
        assert "spaces/found" in success(
            call(
                client,
                "chat_read",
                action="find_space",
                params={"query": "Wanted", "exact": True},
            )
        )
    assert service.spaces().list.call_args.kwargs["pageToken"] == "next"


def test_empty_values_still_reads_real_note_metadata(native):
    create, service, _ = native
    service.spreadsheets().values().get().execute.return_value = {
        "values": [],
        "range": "Sheet1!A1",
    }
    payload = service.spreadsheets().get().execute.return_value
    payload["sheets"][0]["data"][0]["rowData"][0]["values"][0]["note"] = (
        "Note without a value"
    )
    with create("readonly") as client:
        text = success(
            call(
                client,
                "read_sheet_values",
                spreadsheet_id="ss1",
                range_name="Sheet1!A1",
                include_notes=True,
            )
        )
        assert "Note without a value" in text


def test_all_oauth_defaults_keep_narrow_drive(monkeypatch):
    from auth.scopes import get_scopes_for_tools

    monkeypatch.setattr(permissions, "_PERMISSIONS", None)
    assert DRIVE_SCOPE not in get_scopes_for_tools()
    for service in ("drive", "appscript"):
        assert DRIVE_SCOPE not in permissions.get_scopes_for_permission(service, "full")


def test_retired_actions_use_extended_upstream_interfaces(native):
    create, service, _ = native
    with create("full") as client:
        text = success(
            call(client, "list_sheet_tables", spreadsheet_id="ss1", table_id="tbl1")
        )
        assert "Table1" in text and "TEXT" in text
        success(
            call(
                client,
                "format_sheet_range",
                spreadsheet_id="ss1",
                range_name="Sheet1!A1",
                clear_dropdown=True,
            )
        )
        assert any(
            "setDataValidation" in request or "repeatCell" in request
            for request in service.spreadsheets().batchUpdate.call_args.kwargs["body"][
                "requests"
            ]
        )
        formula = '=HYPERLINK("https://example.test","label")'
        success(
            call(
                client,
                "modify_sheet_values",
                spreadsheet_id="ss1",
                range_name="Sheet1!A1",
                values=[[formula]],
            )
        )
        assert service.spreadsheets().values().update.call_args.kwargs["body"][
            "values"
        ] == [[formula]]
        assert "https://example.test" in success(
            call(
                client,
                "read_sheet_values",
                spreadsheet_id="ss1",
                range_name="Sheet1!A1",
                include_hyperlinks=True,
            )
        )


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"display_name": "New", "space_type": "GROUP_CHAT"},
        {"display_name": "New", "external_user_allowed": True},
    ],
)
def test_chat_rejects_unsupported_creation_modes_before_write(native, params):
    create, service, _ = native
    with create("full") as client:
        rejected(call(client, "chat_manage", action="create_space", params=params))
    service.spaces().create.assert_not_called()


def test_permission_unions_do_not_bypass_delete_restrictions(native):
    create, service, _ = native
    with create("readonly+manage") as client:
        rejected(
            call(
                client,
                "modify_sheet_values",
                spreadsheet_id="ss1",
                range_name="Sheet1!A1",
                clear_values=True,
            )
        )
        service.spreadsheets().values().clear.assert_not_called()


def test_narrow_scope_profiles_preserve_comment_writers(native):
    create, _, built = native
    with create("manage") as client:
        names = {t["name"] for t in rpc(client, "tools/list")["result"]["tools"]}
        assert {"manage_spreadsheet_comment", "manage_presentation_comment"} <= names
        success(
            call(
                client,
                "manage_spreadsheet_comment",
                spreadsheet_id="ss1",
                action="create",
                comment_content="Synthetic note",
            )
        )
        success(
            call(
                client,
                "manage_presentation_comment",
                presentation_id="p1",
                action="create",
                comment_content="Synthetic note",
            )
        )
    assert all(email == "alice@example.test" for _, email in built)
