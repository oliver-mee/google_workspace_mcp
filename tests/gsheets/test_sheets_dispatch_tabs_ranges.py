"""
Unit tests for the Sheets dispatcher tabs + ranges families.

Covers tab_add / tab_rename / tab_reorder / delete_tab, merge / unmerge,
find_replace, named_range_add / named_range_delete / named_range_get /
named_range_list, sheet_copy and copy_paste.
"""

import os
import sys

import pytest
from unittest.mock import Mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from core.utils import UserInputError
from gsheets import sheets_dispatch_helpers as dispatch


def create_mock_service(sheets=None, batch_response=None, named_ranges=None):
    mock_service = Mock()
    if sheets is None:
        sheets = [{"properties": {"sheetId": 0, "title": "Sheet1"}}]
    mock_service._sheets_payload = {"sheets": sheets}
    if named_ranges is not None:
        mock_service._named_ranges_payload = {"namedRanges": named_ranges}
    else:
        mock_service._named_ranges_payload = {}

    def get_execute():
        # named-range fetches use fields="namedRanges"; everything else wants sheets
        call = mock_service.spreadsheets().get.call_args
        if call and "namedRanges" in str(call):
            return mock_service._named_ranges_payload
        return mock_service._sheets_payload

    mock_service.spreadsheets().get().execute = Mock(side_effect=get_execute)
    mock_service.spreadsheets().batchUpdate().execute = Mock(
        return_value=batch_response or {}
    )
    return mock_service


def get_requests(mock_service):
    call_args = mock_service.spreadsheets().batchUpdate.call_args
    return call_args[1]["body"]["requests"]


TWO_TABS = [
    {"properties": {"sheetId": 0, "title": "Sheet1"}},
    {"properties": {"sheetId": 1, "title": "Data"}},
]


# --------------------------------------------------------------------------
# Tabs
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tab_add_sends_add_sheet():
    mock_service = create_mock_service(
        batch_response={"replies": [{"addSheet": {"properties": {"sheetId": 7}}}]}
    )

    result = await dispatch.tab_add(mock_service, "ss_1", new_tab_name="Report")

    (request,) = get_requests(mock_service)
    assert request["addSheet"]["properties"]["title"] == "Report"
    assert "sheetId: 7" in result


@pytest.mark.asyncio
async def test_tab_rename():
    mock_service = create_mock_service()

    await dispatch.tab_rename(
        mock_service, "ss_1", sheet_name="Sheet1", new_tab_name="Q3"
    )

    (request,) = get_requests(mock_service)
    update = request["updateSheetProperties"]
    assert update["properties"] == {"sheetId": 0, "title": "Q3"}
    assert update["fields"] == "title"


@pytest.mark.asyncio
async def test_tab_reorder_requires_index():
    mock_service = create_mock_service()

    mock_service.spreadsheets().batchUpdate.reset_mock()
    with pytest.raises(UserInputError, match="index"):
        await dispatch.tab_reorder(mock_service, "ss_1", sheet_name="Sheet1")

    mock_service.spreadsheets().batchUpdate.assert_not_called()


@pytest.mark.asyncio
async def test_delete_tab_sends_delete_sheet():
    mock_service = create_mock_service(sheets=TWO_TABS)

    result = await dispatch.delete_tab(mock_service, "ss_1", sheet_name="Data")

    (request,) = get_requests(mock_service)
    assert request["deleteSheet"] == {"sheetId": 1}
    assert "Deleted tab 'Data'" in result


@pytest.mark.asyncio
async def test_delete_tab_refuses_last_tab():
    mock_service = create_mock_service()

    mock_service.spreadsheets().batchUpdate.reset_mock()
    with pytest.raises(UserInputError, match="only tab"):
        await dispatch.delete_tab(mock_service, "ss_1", sheet_name="Sheet1")

    mock_service.spreadsheets().batchUpdate.assert_not_called()


# --------------------------------------------------------------------------
# Merge / unmerge
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_merge_cells_payload():
    mock_service = create_mock_service()

    await dispatch.merge_cells(mock_service, "ss_1", range_name="Sheet1!A1:C1")

    (request,) = get_requests(mock_service)
    assert request["mergeCells"]["mergeType"] == "MERGE_ALL"
    assert request["mergeCells"]["range"]["endColumnIndex"] == 3


@pytest.mark.asyncio
async def test_merge_cells_rejects_bad_type():
    mock_service = create_mock_service()

    with pytest.raises(UserInputError, match="merge_type"):
        await dispatch.merge_cells(
            mock_service, "ss_1", range_name="Sheet1!A1:C1", merge_type="SIDEWAYS"
        )


@pytest.mark.asyncio
async def test_unmerge_cells_payload():
    mock_service = create_mock_service()

    await dispatch.unmerge_cells(mock_service, "ss_1", range_name="Sheet1!A1:C3")

    (request,) = get_requests(mock_service)
    assert "unmergeCells" in request


# --------------------------------------------------------------------------
# find_replace
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_find_replace_all_sheets_default():
    mock_service = create_mock_service(
        batch_response={"replies": [{"findReplace": {"occurrencesChanged": 4}}]}
    )

    result = await dispatch.find_replace(
        mock_service, "ss_1", find="FY25", replacement="FY26"
    )

    (request,) = get_requests(mock_service)
    fr = request["findReplace"]
    assert fr["find"] == "FY25"
    assert fr["replacement"] == "FY26"
    assert fr["allSheets"] is True
    assert "4 occurrence(s)" in result


@pytest.mark.asyncio
async def test_find_replace_sheet_scope():
    mock_service = create_mock_service()

    await dispatch.find_replace(
        mock_service,
        "ss_1",
        find="a",
        replacement="b",
        sheet_name="Sheet1",
        match_case=True,
    )

    (request,) = get_requests(mock_service)
    fr = request["findReplace"]
    assert fr["sheetId"] == 0
    assert fr["matchCase"] is True
    assert "allSheets" not in fr


@pytest.mark.asyncio
async def test_find_replace_rejects_both_scopes():
    mock_service = create_mock_service()

    with pytest.raises(UserInputError, match="not both"):
        await dispatch.find_replace(
            mock_service,
            "ss_1",
            find="a",
            replacement="b",
            sheet_name="Sheet1",
            range_name="Sheet1!A1:A5",
        )


# --------------------------------------------------------------------------
# Named ranges
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_named_range_add():
    mock_service = create_mock_service(
        batch_response={
            "replies": [{"addNamedRange": {"namedRange": {"namedRangeId": "nr_1"}}}]
        }
    )

    result = await dispatch.named_range_add(
        mock_service, "ss_1", name="Rates", range_name="Sheet1!B2:B10"
    )

    (request,) = get_requests(mock_service)
    nr = request["addNamedRange"]["namedRange"]
    assert nr["name"] == "Rates"
    assert nr["range"]["startColumnIndex"] == 1
    assert "nr_1" in result


@pytest.mark.asyncio
async def test_named_range_delete_resolves_name_to_id():
    mock_service = create_mock_service(
        named_ranges=[{"namedRangeId": "nr_1", "name": "Rates", "range": {}}]
    )

    await dispatch.named_range_delete(mock_service, "ss_1", name="Rates")

    (request,) = get_requests(mock_service)
    assert request["deleteNamedRange"] == {"namedRangeId": "nr_1"}


@pytest.mark.asyncio
async def test_named_range_delete_unknown_name():
    mock_service = create_mock_service(named_ranges=[])

    with pytest.raises(UserInputError, match="No named range"):
        await dispatch.named_range_delete(mock_service, "ss_1", name="Nope")


@pytest.mark.asyncio
async def test_named_range_list_and_get():
    mock_service = create_mock_service(
        named_ranges=[
            {
                "namedRangeId": "nr_1",
                "name": "Rates",
                "range": {
                    "sheetId": 0,
                    "startRowIndex": 1,
                    "endRowIndex": 10,
                    "startColumnIndex": 1,
                    "endColumnIndex": 2,
                },
            }
        ]
    )

    listing = await dispatch.named_range_list(mock_service, "ss_1")
    assert "'Rates'" in listing
    assert "nr_1" in listing

    got = await dispatch.named_range_get(mock_service, "ss_1", name="Rates")
    assert "'Rates'" in got

    with pytest.raises(UserInputError, match="No named range"):
        await dispatch.named_range_get(mock_service, "ss_1", name="Nope")


# --------------------------------------------------------------------------
# sheet_copy / copy_paste
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sheet_copy_sends_copy_to():
    mock_service = create_mock_service()
    mock_service.spreadsheets().sheets().copyTo().execute = Mock(
        return_value={"title": "Copy of Sheet1"}
    )

    result = await dispatch.sheet_copy(
        mock_service, "ss_1", sheet_name="Sheet1", destination_spreadsheet_id="ss_2"
    )

    call = mock_service.spreadsheets().sheets().copyTo.call_args
    assert call[1]["spreadsheetId"] == "ss_1"
    assert call[1]["sheetId"] == 0
    assert call[1]["body"] == {"destinationSpreadsheetId": "ss_2"}
    assert "Copy of Sheet1" in result


@pytest.mark.asyncio
async def test_copy_paste_payload():
    mock_service = create_mock_service()

    await dispatch.copy_paste(
        mock_service,
        "ss_1",
        source_range="Sheet1!A1:B2",
        destination_range="Sheet1!D1:E2",
        paste_type="PASTE_VALUES",
    )

    (request,) = get_requests(mock_service)
    cp = request["copyPaste"]
    assert cp["pasteType"] == "PASTE_VALUES"
    assert cp["destination"]["startColumnIndex"] == 3


@pytest.mark.asyncio
async def test_copy_paste_rejects_bad_type():
    mock_service = create_mock_service()

    with pytest.raises(UserInputError, match="paste_type"):
        await dispatch.copy_paste(
            mock_service,
            "ss_1",
            source_range="Sheet1!A1:B2",
            destination_range="Sheet1!D1:E2",
            paste_type="PASTE_EVERYTHING",
        )
