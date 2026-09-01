"""
Unit tests for the Sheets dispatcher charts/banding/validation/notes/filter/links
families (sheets_manage writes + sheets_read reads).
"""

import os
import sys

import pytest
from unittest.mock import Mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from core.utils import UserInputError
from gsheets import sheets_dispatch_helpers as dispatch


def create_mock_service(get_payload=None, batch_response=None):
    mock_service = Mock()
    if get_payload is None:
        get_payload = {"sheets": [{"properties": {"sheetId": 0, "title": "Sheet1"}}]}
    mock_service.spreadsheets().get().execute = Mock(return_value=get_payload)
    mock_service.spreadsheets().batchUpdate().execute = Mock(
        return_value=batch_response or {}
    )
    return mock_service


def get_requests(mock_service):
    call_args = mock_service.spreadsheets().batchUpdate.call_args
    return call_args[1]["body"]["requests"]


CHART = {
    "chartId": 42,
    "spec": {
        "title": "Sales",
        "basicChart": {
            "chartType": "COLUMN",
            "legendPosition": "RIGHT_LEGEND",
            "series": [{"series": {"sourceRange": {"sources": [{"sheetId": 0}]}}}],
        },
    },
}


# --------------------------------------------------------------------------
# Charts
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chart_create_payload():
    mock_service = create_mock_service(
        batch_response={"replies": [{"addChart": {"chart": {"chartId": 42}}}]}
    )

    result = await dispatch.chart_create(
        mock_service,
        "ss_1",
        data_range="Sheet1!A1:C10",
        chart_type="line",
        title="Trend",
    )

    (request,) = get_requests(mock_service)
    chart = request["addChart"]["chart"]
    basic = chart["spec"]["basicChart"]
    assert basic["chartType"] == "LINE"
    assert chart["spec"]["title"] == "Trend"
    assert (
        basic["domains"][0]["domain"]["sourceRange"]["sources"][0]["endRowIndex"] == 10
    )
    assert "Chart ID: 42" in result


@pytest.mark.asyncio
async def test_chart_update_preserves_untouched_spec():
    mock_service = create_mock_service(
        get_payload={
            "sheets": [
                {"properties": {"sheetId": 0, "title": "Sheet1"}, "charts": [CHART]}
            ]
        }
    )

    await dispatch.chart_update(mock_service, "ss_1", chart_id=42, title="Sales Q3")

    (request,) = get_requests(mock_service)
    spec = request["updateChartSpec"]["spec"]
    assert spec["title"] == "Sales Q3"
    # series survived the update
    assert spec["basicChart"]["series"] == CHART["spec"]["basicChart"]["series"]
    assert spec["basicChart"]["chartType"] == "COLUMN"


@pytest.mark.asyncio
async def test_chart_update_unknown_chart():
    mock_service = create_mock_service(
        get_payload={
            "sheets": [{"properties": {"sheetId": 0, "title": "Sheet1"}, "charts": []}]
        }
    )

    with pytest.raises(UserInputError, match="No chart with id 99"):
        await dispatch.chart_update(mock_service, "ss_1", chart_id=99, title="X")


@pytest.mark.asyncio
async def test_chart_delete():
    mock_service = create_mock_service()

    result = await dispatch.chart_delete(mock_service, "ss_1", chart_id=42)

    (request,) = get_requests(mock_service)
    assert request["deleteEmbeddedObject"] == {"objectId": 42}
    assert "unaffected" in result


@pytest.mark.asyncio
async def test_chart_list_and_get():
    mock_service = create_mock_service(
        get_payload={
            "sheets": [
                {"properties": {"sheetId": 0, "title": "Sheet1"}, "charts": [CHART]}
            ]
        }
    )

    listing = await dispatch.chart_list(mock_service, "ss_1")
    assert "chartId 42" in listing
    assert "COLUMN" in listing

    got = await dispatch.chart_get(mock_service, "ss_1", chart_id=42)
    assert "Sales" in got

    with pytest.raises(UserInputError, match="chart_list"):
        await dispatch.chart_get(mock_service, "ss_1", chart_id=99)


# --------------------------------------------------------------------------
# Banding
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_banding_set_payload():
    mock_service = create_mock_service(
        batch_response={
            "replies": [{"addBanding": {"bandedRange": {"bandedRangeId": "br_1"}}}]
        }
    )

    result = await dispatch.banding_set(
        mock_service,
        "ss_1",
        range_name="Sheet1!A1:D20",
        header_color="#4285f4",
        first_band_color="#ffffff",
    )

    (request,) = get_requests(mock_service)
    banded = request["addBanding"]["bandedRange"]
    assert "headerColorStyle" in banded["rowProperties"]
    assert "br_1" in result


@pytest.mark.asyncio
async def test_banding_set_requires_a_color():
    mock_service = create_mock_service()

    with pytest.raises(UserInputError, match="at least one"):
        await dispatch.banding_set(mock_service, "ss_1", range_name="Sheet1!A1:D20")


@pytest.mark.asyncio
async def test_banding_clear():
    mock_service = create_mock_service()

    await dispatch.banding_clear(mock_service, "ss_1", banded_range_id="br_1")

    (request,) = get_requests(mock_service)
    assert request["deleteBanding"] == {"bandedRangeId": "br_1"}


@pytest.mark.asyncio
async def test_banding_list():
    mock_service = create_mock_service(
        get_payload={
            "sheets": [
                {
                    "properties": {"sheetId": 0, "title": "Sheet1"},
                    "bandedRanges": [
                        {"bandedRangeId": "br_1", "range": {"sheetId": 0}}
                    ],
                }
            ]
        }
    )

    result = await dispatch.banding_list(mock_service, "ss_1")
    assert "br_1" in result


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validation_set_one_of_list():
    mock_service = create_mock_service()

    await dispatch.validation_set(
        mock_service,
        "ss_1",
        range_name="Sheet1!C2:C100",
        condition_type="ONE_OF_LIST",
        condition_values=["Open", "Won"],
    )

    (request,) = get_requests(mock_service)
    rule = request["setDataValidation"]["rule"]
    assert rule["condition"]["type"] == "ONE_OF_LIST"
    assert [v["userEnteredValue"] for v in rule["condition"]["values"]] == [
        "Open",
        "Won",
    ]
    assert rule["strict"] is True


@pytest.mark.asyncio
async def test_validation_set_list_needs_values():
    mock_service = create_mock_service()

    mock_service.spreadsheets().batchUpdate.reset_mock()
    with pytest.raises(UserInputError, match="condition_values"):
        await dispatch.validation_set(
            mock_service,
            "ss_1",
            range_name="Sheet1!C2:C100",
            condition_type="ONE_OF_LIST",
        )

    mock_service.spreadsheets().batchUpdate.assert_not_called()


@pytest.mark.asyncio
async def test_validation_clear():
    mock_service = create_mock_service()

    await dispatch.validation_clear(mock_service, "ss_1", range_name="Sheet1!C2:C100")

    (request,) = get_requests(mock_service)
    assert request["repeatCell"]["fields"] == "dataValidation"


@pytest.mark.asyncio
async def test_validation_get():
    grid_payload = {
        "sheets": [
            {
                "data": [
                    {
                        "startRow": 1,
                        "startColumn": 2,
                        "rowData": [
                            {
                                "values": [
                                    {
                                        "dataValidation": {
                                            "condition": {
                                                "type": "ONE_OF_LIST",
                                                "values": [
                                                    {"userEnteredValue": "Open"}
                                                ],
                                            }
                                        }
                                    }
                                ]
                            }
                        ],
                    }
                ]
            }
        ]
    }
    mock_service = create_mock_service(get_payload=grid_payload)

    result = await dispatch.validation_get(
        mock_service, "ss_1", range_name="Sheet1!C2:C5"
    )
    assert "ONE_OF_LIST" in result
    assert "R2C3" in result


# --------------------------------------------------------------------------
# Notes, filters, links
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_note_set():
    mock_service = create_mock_service()

    await dispatch.note_set(
        mock_service, "ss_1", range_name="Sheet1!A1", note="check this"
    )

    (request,) = get_requests(mock_service)
    assert request["repeatCell"]["cell"] == {"note": "check this"}
    assert request["repeatCell"]["fields"] == "note"


@pytest.mark.asyncio
async def test_filter_set_and_clear():
    mock_service = create_mock_service()

    await dispatch.filter_set(mock_service, "ss_1", range_name="Sheet1!A1:D50")
    (request,) = get_requests(mock_service)
    assert "setBasicFilter" in request

    await dispatch.filter_set(mock_service, "ss_1", sheet_name="Sheet1", clear=True)
    (request,) = get_requests(mock_service)
    assert request["clearBasicFilter"] == {"sheetId": 0}


@pytest.mark.asyncio
async def test_links_set_writes_hyperlink_formula():
    mock_service = create_mock_service()
    mock_service.spreadsheets().values().update().execute = Mock(
        return_value={"updatedCells": 1}
    )

    result = await dispatch.links_set(
        mock_service,
        "ss_1",
        range_name="Sheet1!A1",
        url="https://preface.ai",
        label="Preface",
    )

    call = mock_service.spreadsheets().values().update.call_args
    assert call[1]["valueInputOption"] == "USER_ENTERED"
    formula = call[1]["body"]["values"][0][0]
    assert formula == '=HYPERLINK("https://preface.ai","Preface")'
    assert "1 cell(s)" in result


@pytest.mark.asyncio
async def test_links_set_rejects_bad_url():
    mock_service = create_mock_service()

    with pytest.raises(UserInputError, match="http"):
        await dispatch.links_set(
            mock_service, "ss_1", range_name="Sheet1!A1", url="notaurl"
        )


@pytest.mark.asyncio
async def test_links_get():
    grid_payload = {
        "sheets": [
            {
                "data": [
                    {
                        "startRow": 0,
                        "startColumn": 0,
                        "rowData": [
                            {
                                "values": [
                                    {
                                        "formattedValue": "Preface",
                                        "hyperlink": "https://preface.ai",
                                    }
                                ]
                            }
                        ],
                    }
                ]
            }
        ]
    }
    mock_service = create_mock_service(get_payload=grid_payload)

    result = await dispatch.links_get(mock_service, "ss_1", range_name="Sheet1!A1:A5")
    assert "https://preface.ai" in result
    assert "R1C1" in result
