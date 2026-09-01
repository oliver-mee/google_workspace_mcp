"""
Unit tests for the Sheets dispatcher tables family.

Covers table_create / table_get / table_clear / table_delete behind
sheets_manage / sheets_read / sheets_delete, including column typing,
dropdown validation, defensive reply handling, and the permission deny gate.
"""

import os
import sys

import pytest
from unittest.mock import Mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from core.utils import UserInputError
from gsheets import sheets_dispatch_helpers as dispatch


def create_mock_service(tables=None):
    """Mock Sheets service exposing one sheet, plus optional existing tables."""
    mock_service = Mock()

    sheet = {"properties": {"sheetId": 0, "title": "Sheet1"}}
    if tables is not None:
        sheet["tables"] = tables

    mock_service.spreadsheets().get().execute = Mock(return_value={"sheets": [sheet]})
    mock_service.spreadsheets().batchUpdate().execute = Mock(
        return_value={
            "replies": [{"addTable": {"table": {"tableId": "tbl_generated"}}}]
        }
    )
    return mock_service


def get_requests(mock_service):
    """Pull the requests list out of the last batchUpdate call."""
    call_args = mock_service.spreadsheets().batchUpdate.call_args
    return call_args[1]["body"]["requests"]


PIPELINE_TABLE = {
    "tableId": "tbl_1",
    "name": "Pipeline",
    "range": {
        "sheetId": 0,
        "startRowIndex": 0,
        "endRowIndex": 10,
        "startColumnIndex": 0,
        "endColumnIndex": 3,
    },
    "columnProperties": [
        {"columnIndex": 0, "columnName": "Client", "columnType": "TEXT"},
        {"columnIndex": 1, "columnName": "Fee", "columnType": "CURRENCY"},
        {
            "columnIndex": 2,
            "columnName": "Stage",
            "columnType": "DROPDOWN",
            "dataValidationRule": {
                "condition": {
                    "type": "ONE_OF_LIST",
                    "values": [
                        {"userEnteredValue": "Open"},
                        {"userEnteredValue": "Won"},
                    ],
                }
            },
        },
    ],
}


# --------------------------------------------------------------------------
# table_create
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_sends_add_table_with_grid_range():
    mock_service = create_mock_service()

    result = await dispatch.table_create(
        mock_service, "ss_1", table_name="Pipeline", range_name="Sheet1!A1:C10"
    )

    requests = get_requests(mock_service)
    table = requests[0]["addTable"]["table"]
    assert table["name"] == "Pipeline"
    assert table["range"] == {
        "sheetId": 0,
        "startRowIndex": 0,
        "endRowIndex": 10,
        "startColumnIndex": 0,
        "endColumnIndex": 3,
    }
    assert "tableId" not in table, "tableId must be left for Sheets to generate"
    assert "tbl_generated" in result


@pytest.mark.parametrize(
    "batch_response",
    [
        pytest.param({}, id="no-replies-key"),
        pytest.param({"replies": []}, id="empty-replies-list"),
        pytest.param({"replies": [{}]}, id="reply-without-addTable"),
        pytest.param({"replies": [{"addTable": {}}]}, id="addTable-without-table"),
    ],
)
@pytest.mark.asyncio
async def test_create_reports_missing_table_id_without_raising(batch_response):
    """An empty 'replies' list must degrade to '(id unavailable)', not IndexError."""
    mock_service = create_mock_service()
    mock_service.spreadsheets().batchUpdate().execute = Mock(
        return_value=batch_response
    )

    result = await dispatch.table_create(
        mock_service, "ss_1", table_name="Pipeline", range_name="Sheet1!A1:C10"
    )

    assert "(id unavailable)" in result


@pytest.mark.asyncio
async def test_create_assigns_sequential_column_indexes():
    """columnIndex is table-relative and inferred from position when omitted."""
    mock_service = create_mock_service()

    await dispatch.table_create(
        mock_service,
        "ss_1",
        table_name="Pipeline",
        range_name="Sheet1!A1:C10",
        column_properties=[
            {"columnName": "Client", "columnType": "TEXT"},
            {"columnName": "Fee", "columnType": "CURRENCY"},
        ],
    )

    columns = get_requests(mock_service)[0]["addTable"]["table"]["columnProperties"]
    assert [c["columnIndex"] for c in columns] == [0, 1]
    assert columns[1]["columnType"] == "CURRENCY"


@pytest.mark.asyncio
async def test_create_dropdown_column_builds_one_of_list_rule():
    mock_service = create_mock_service()

    await dispatch.table_create(
        mock_service,
        "ss_1",
        table_name="Pipeline",
        range_name="Sheet1!A1:C10",
        column_properties=[
            {
                "columnName": "Stage",
                "columnType": "DROPDOWN",
                "values": ["Open", "Won", "Lost"],
            }
        ],
    )

    column = get_requests(mock_service)[0]["addTable"]["table"]["columnProperties"][0]
    condition = column["dataValidationRule"]["condition"]
    assert condition["type"] == "ONE_OF_LIST"
    assert [v["userEnteredValue"] for v in condition["values"]] == [
        "Open",
        "Won",
        "Lost",
    ]


@pytest.mark.asyncio
async def test_create_accepts_json_encoded_column_properties():
    mock_service = create_mock_service()

    await dispatch.table_create(
        mock_service,
        "ss_1",
        table_name="Pipeline",
        range_name="Sheet1!A1:C10",
        column_properties='[{"columnName": "Client", "columnType": "TEXT"}]',
    )

    columns = get_requests(mock_service)[0]["addTable"]["table"]["columnProperties"]
    assert columns[0]["columnName"] == "Client"


@pytest.mark.asyncio
async def test_create_rejects_values_on_non_dropdown_column():
    mock_service = create_mock_service()

    mock_service.spreadsheets().batchUpdate.reset_mock()
    with pytest.raises(UserInputError, match="only supported on DROPDOWN"):
        await dispatch.table_create(
            mock_service,
            "ss_1",
            table_name="Pipeline",
            range_name="Sheet1!A1:C10",
            column_properties=[
                {"columnName": "Fee", "columnType": "CURRENCY", "values": ["a"]}
            ],
        )

    mock_service.spreadsheets().batchUpdate.assert_not_called()


@pytest.mark.asyncio
async def test_create_rejects_unknown_column_type():
    mock_service = create_mock_service()

    with pytest.raises(UserInputError, match="columnType"):
        await dispatch.table_create(
            mock_service,
            "ss_1",
            table_name="Pipeline",
            range_name="Sheet1!A1:C10",
            column_properties=[{"columnName": "X", "columnType": "NOT_A_TYPE"}],
        )


@pytest.mark.asyncio
async def test_create_requires_name_and_range():
    mock_service = create_mock_service()

    mock_service.spreadsheets().batchUpdate.reset_mock()
    with pytest.raises(UserInputError, match="table_name"):
        await dispatch.table_create(
            mock_service, "ss_1", table_name="", range_name="Sheet1!A1:C10"
        )
    with pytest.raises(UserInputError, match="range_name"):
        await dispatch.table_create(
            mock_service, "ss_1", table_name="Pipeline", range_name=None
        )

    mock_service.spreadsheets().batchUpdate.assert_not_called()


@pytest.mark.asyncio
async def test_create_applies_row_colors():
    mock_service = create_mock_service()

    await dispatch.table_create(
        mock_service,
        "ss_1",
        table_name="Pipeline",
        range_name="Sheet1!A1:C10",
        header_color="#4285f4",
        first_band_color="#ffffff",
        second_band_color="#eeeeee",
    )

    rows = get_requests(mock_service)[0]["addTable"]["table"]["rowsProperties"]
    assert "headerColorStyle" in rows
    assert "firstBandColorStyle" in rows
    assert "secondBandColorStyle" in rows
    assert "footerColorStyle" not in rows


# --------------------------------------------------------------------------
# table_get
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_describes_columns_and_dropdown_choices():
    mock_service = create_mock_service(tables=[PIPELINE_TABLE])

    result = await dispatch.table_get(mock_service, "ss_1", table_id="tbl_1")

    assert "Pipeline" in result
    assert "tbl_1" in result
    assert "Fee (CURRENCY)" in result
    assert "Open, Won" in result


@pytest.mark.asyncio
async def test_get_finds_table_by_name():
    mock_service = create_mock_service(tables=[PIPELINE_TABLE])

    result = await dispatch.table_get(mock_service, "ss_1", table_name="Pipeline")

    assert "tbl_1" in result


@pytest.mark.asyncio
async def test_get_unknown_table_is_a_clear_error():
    mock_service = create_mock_service(tables=[PIPELINE_TABLE])

    with pytest.raises(UserInputError, match="list_sheet_tables"):
        await dispatch.table_get(mock_service, "ss_1", table_id="tbl_nope")


# --------------------------------------------------------------------------
# table_clear
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clear_clears_data_rows_below_header():
    mock_service = create_mock_service(tables=[PIPELINE_TABLE])

    result = await dispatch.table_clear(mock_service, "ss_1", table_id="tbl_1")

    clear_call = mock_service.spreadsheets().values().clear.call_args
    assert clear_call[1]["range"] == "'Sheet1'!A2:C10"
    assert "9 data row(s)" in result
    assert "header kept" in result


@pytest.mark.asyncio
async def test_clear_header_only_table_is_a_noop():
    header_only = dict(PIPELINE_TABLE)
    header_only["range"] = {
        "sheetId": 0,
        "startRowIndex": 0,
        "endRowIndex": 1,
        "startColumnIndex": 0,
        "endColumnIndex": 3,
    }
    mock_service = create_mock_service(tables=[header_only])

    result = await dispatch.table_clear(mock_service, "ss_1", table_id="tbl_1")

    assert "no data rows" in result
    mock_service.spreadsheets().values().clear.assert_not_called()


# --------------------------------------------------------------------------
# table_delete
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_sends_delete_table():
    mock_service = create_mock_service(tables=[PIPELINE_TABLE])

    result = await dispatch.table_delete(mock_service, "ss_1", table_id="tbl_1")

    requests = get_requests(mock_service)
    assert requests == [{"deleteTable": {"tableId": "tbl_1"}}]
    assert "left in place" in result


@pytest.mark.asyncio
async def test_delete_requires_an_identifier():
    mock_service = create_mock_service(tables=[PIPELINE_TABLE])

    mock_service.spreadsheets().batchUpdate.reset_mock()
    with pytest.raises(UserInputError, match="table_id"):
        await dispatch.table_delete(mock_service, "ss_1")

    mock_service.spreadsheets().batchUpdate.assert_not_called()


# --------------------------------------------------------------------------
# Permission deny gate
# --------------------------------------------------------------------------


def test_sheets_delete_actions_denied_at_manage_level():
    """Every sheets_delete action must be denied under sheets:manage."""
    import auth.permissions as permissions
    from gsheets.sheets_tools import SHEETS_DELETE_ACTIONS

    denied = permissions.SERVICE_DENIED_ACTIONS["sheets"]["manage"]
    for action in SHEETS_DELETE_ACTIONS:
        assert action in denied, f"'{action}' is not denied at sheets:manage"


def test_sheets_full_level_denies_nothing():
    import auth.permissions as permissions

    assert "full" not in permissions.SERVICE_DENIED_ACTIONS.get("sheets", {})
