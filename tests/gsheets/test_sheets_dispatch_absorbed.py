"""
Unit tests for the Sheets dispatcher absorbed family.

Covers format_range, conditional_format, resize_dimensions, move_rows and
delete_dimension — the actions mirroring the existing standalone tools
(format_sheet_range, manage_conditional_formatting, resize_sheet_dimensions,
move_sheet_rows), with dimension deletes split into the data-destructive
delete_dimension action.
"""

import os
import sys

import pytest
from unittest.mock import Mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from core.utils import UserInputError
from gsheets import sheets_dispatch_helpers as dispatch


def create_mock_service(sheets=None):
    """Mock Sheets service. `sheets` defaults to one Sheet1 tab."""
    mock_service = Mock()
    if sheets is None:
        sheets = [{"properties": {"sheetId": 0, "title": "Sheet1"}}]
    mock_service.spreadsheets().get().execute = Mock(return_value={"sheets": sheets})
    mock_service.spreadsheets().batchUpdate().execute = Mock(return_value={})
    return mock_service


def get_requests(mock_service):
    call_args = mock_service.spreadsheets().batchUpdate.call_args
    return call_args[1]["body"]["requests"]


# --------------------------------------------------------------------------
# format_range
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_format_range_builds_repeat_cell():
    mock_service = create_mock_service()

    await dispatch.format_range(
        mock_service,
        "ss_1",
        range_name="Sheet1!A1:B2",
        background_color="#ff0000",
        bold=True,
        number_format_type="CURRENCY",
    )

    (request,) = get_requests(mock_service)
    repeat = request["repeatCell"]
    assert repeat["range"]["endColumnIndex"] == 2
    fmt = repeat["cell"]["userEnteredFormat"]
    assert "backgroundColor" in fmt
    assert fmt["textFormat"]["bold"] is True
    assert fmt["numberFormat"] == {"type": "CURRENCY"}
    fields = repeat["fields"]
    assert "userEnteredFormat.backgroundColor" in fields
    assert "userEnteredFormat.textFormat.bold" in fields
    assert "userEnteredFormat.numberFormat" in fields


@pytest.mark.asyncio
async def test_format_range_requires_an_option():
    mock_service = create_mock_service()

    mock_service.spreadsheets().batchUpdate.reset_mock()
    with pytest.raises(UserInputError, match="at least one formatting option"):
        await dispatch.format_range(mock_service, "ss_1", range_name="Sheet1!A1:B2")

    mock_service.spreadsheets().batchUpdate.assert_not_called()


@pytest.mark.asyncio
async def test_format_range_rejects_bad_number_format():
    mock_service = create_mock_service()

    with pytest.raises(UserInputError, match="number_format_type"):
        await dispatch.format_range(
            mock_service,
            "ss_1",
            range_name="Sheet1!A1:B2",
            number_format_type="DOGE",
        )


# --------------------------------------------------------------------------
# conditional_format
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_conditional_format_adds_boolean_rule():
    mock_service = create_mock_service()

    await dispatch.conditional_format(
        mock_service,
        "ss_1",
        operation="add_rule",
        range_name="Sheet1!A1:A10",
        condition_type="NUMBER_GREATER",
        condition_values=["5"],
        background_color="#00ff00",
    )

    (request,) = get_requests(mock_service)
    add = request["addConditionalFormatRule"]
    rule = add["rule"]
    assert "booleanRule" in rule
    assert rule["booleanRule"]["condition"]["type"] == "NUMBER_GREATER"
    assert "index" not in add  # omitted when rule_index is not given


@pytest.mark.asyncio
async def test_conditional_format_adds_gradient_rule():
    mock_service = create_mock_service()

    await dispatch.conditional_format(
        mock_service,
        "ss_1",
        operation="add_rule",
        range_name="Sheet1!A1:A10",
        gradient_points=[
            {"type": "MIN", "color": "#ff0000"},
            {"type": "MAX", "color": "#00ff00"},
        ],
    )

    (request,) = get_requests(mock_service)
    rule = request["addConditionalFormatRule"]["rule"]
    assert "gradientRule" in rule


@pytest.mark.asyncio
async def test_conditional_format_delete_rule():
    sheet = {
        "properties": {"sheetId": 0, "title": "Sheet1"},
        "conditionalFormats": [
            {"booleanRule": {"condition": {"type": "TEXT_CONTAINS"}}}
        ],
    }
    mock_service = create_mock_service(sheets=[sheet])

    await dispatch.conditional_format(
        mock_service, "ss_1", operation="delete_rule", rule_index=0
    )

    (request,) = get_requests(mock_service)
    assert request["deleteConditionalFormatRule"] == {"index": 0, "sheetId": 0}


@pytest.mark.asyncio
async def test_conditional_format_requires_condition_for_add():
    mock_service = create_mock_service()

    mock_service.spreadsheets().batchUpdate.reset_mock()
    with pytest.raises(UserInputError, match="condition_type"):
        await dispatch.conditional_format(
            mock_service, "ss_1", operation="add_rule", range_name="Sheet1!A1:A10"
        )

    mock_service.spreadsheets().batchUpdate.assert_not_called()


@pytest.mark.asyncio
async def test_conditional_format_delete_out_of_range():
    mock_service = create_mock_service()

    with pytest.raises(UserInputError, match="out of range"):
        await dispatch.conditional_format(
            mock_service, "ss_1", operation="delete_rule", rule_index=3
        )


# --------------------------------------------------------------------------
# resize_dimensions
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resize_dimensions_sizes_and_freeze():
    mock_service = create_mock_service()

    result = await dispatch.resize_dimensions(
        mock_service,
        "ss_1",
        column_sizes={"A": 200},
        row_sizes={"1": 40},
        frozen_row_count=1,
    )

    requests = get_requests(mock_service)
    kinds = [list(r.keys())[0] for r in requests]
    assert kinds == [
        "updateDimensionProperties",
        "updateDimensionProperties",
        "updateSheetProperties",
    ]
    assert requests[0]["updateDimensionProperties"]["properties"]["pixelSize"] == 200
    assert "resized columns: A=200px" in result


@pytest.mark.asyncio
async def test_resize_dimensions_inserts_rows():
    mock_service = create_mock_service()

    await dispatch.resize_dimensions(
        mock_service, "ss_1", insert_rows=3, insert_rows_at=5
    )

    (request,) = get_requests(mock_service)
    insert = request["insertDimension"]
    assert insert["range"]["startIndex"] == 4
    assert insert["range"]["endIndex"] == 7
    assert insert["inheritFromBefore"] is True


@pytest.mark.asyncio
async def test_resize_dimensions_has_no_delete_capability():
    """Dimension deletes moved to the data-destructive delete_dimension action."""
    import inspect

    params = inspect.signature(dispatch.resize_dimensions).parameters
    assert "delete_rows" not in params
    assert "delete_row_range" not in params
    assert "delete_columns" not in params


# --------------------------------------------------------------------------
# delete_dimension (data-destructive; gated by sheets:full in the tool)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_dimension_rows_descending_order():
    mock_service = create_mock_service()

    result = await dispatch.delete_dimension(mock_service, "ss_1", delete_rows=[2, 5])

    requests = get_requests(mock_service)
    starts = [r["deleteDimension"]["range"]["startIndex"] for r in requests]
    assert starts == [4, 1], "deletes must run highest-first to keep indices stable"
    assert "deleted rows" in result


@pytest.mark.asyncio
async def test_delete_dimension_columns():
    mock_service = create_mock_service()

    await dispatch.delete_dimension(mock_service, "ss_1", delete_columns=["A", "C"])

    requests = get_requests(mock_service)
    starts = [r["deleteDimension"]["range"]["startIndex"] for r in requests]
    assert starts == [2, 0]


@pytest.mark.asyncio
async def test_delete_dimension_row_range():
    mock_service = create_mock_service()

    await dispatch.delete_dimension(mock_service, "ss_1", delete_row_range="5:10")

    (request,) = get_requests(mock_service)
    rng = request["deleteDimension"]["range"]
    assert rng["startIndex"] == 4
    assert rng["endIndex"] == 10


@pytest.mark.asyncio
async def test_delete_dimension_requires_exactly_one_mode():
    mock_service = create_mock_service()

    mock_service.spreadsheets().batchUpdate.reset_mock()
    with pytest.raises(UserInputError, match="exactly one"):
        await dispatch.delete_dimension(mock_service, "ss_1")
    with pytest.raises(UserInputError, match="exactly one"):
        await dispatch.delete_dimension(
            mock_service, "ss_1", delete_rows=[1], delete_columns=["A"]
        )

    mock_service.spreadsheets().batchUpdate.assert_not_called()


# --------------------------------------------------------------------------
# move_rows
# --------------------------------------------------------------------------


def create_two_sheet_mock_service():
    mock_service = Mock()
    sheets = [
        {
            "properties": {
                "sheetId": 0,
                "title": "Src",
                "gridProperties": {"rowCount": 100},
            }
        },
        {
            "properties": {
                "sheetId": 1,
                "title": "Dst",
                "gridProperties": {"rowCount": 100},
            }
        },
    ]
    mock_service.spreadsheets().get().execute = Mock(return_value={"sheets": sheets})
    # First values().get call = source rows (has data), second = destination (2 rows).
    mock_service.spreadsheets().values().get().execute = Mock(
        side_effect=[
            {"values": [["a"], ["b"]]},
            {"values": [["x"], ["y"]]},
        ]
    )
    mock_service.spreadsheets().batchUpdate().execute = Mock(return_value={})
    return mock_service


@pytest.mark.asyncio
async def test_move_rows_copy_paste_then_delete():
    mock_service = create_two_sheet_mock_service()

    await dispatch.move_rows(
        mock_service,
        "ss_1",
        source_sheet="Src",
        start_row=1,
        end_row=2,
        destination_sheet="Dst",
    )

    requests = get_requests(mock_service)
    kinds = [list(r.keys())[0] for r in requests]
    assert kinds == ["copyPaste", "deleteDimension"]
    copy = requests[0]["copyPaste"]
    assert copy["source"]["sheetId"] == 0
    assert copy["destination"]["sheetId"] == 1
    assert copy["destination"]["startRowIndex"] == 2  # appended below 2 data rows
    assert requests[1]["deleteDimension"]["range"]["sheetId"] == 0


@pytest.mark.asyncio
async def test_move_rows_rejects_same_sheet():
    mock_service = create_two_sheet_mock_service()

    with pytest.raises(UserInputError, match="must be different"):
        await dispatch.move_rows(
            mock_service,
            "ss_1",
            source_sheet="Src",
            start_row=1,
            end_row=2,
            destination_sheet="Src",
        )
