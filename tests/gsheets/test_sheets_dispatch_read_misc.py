"""
Unit tests for the Sheets dispatcher read remainder + misc:
metadata/get, read_format, export, datasource describes, range_clear
and the batch_update escape hatch.
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


# --------------------------------------------------------------------------
# metadata / get
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_metadata_summary():
    mock_service = create_mock_service(
        get_payload={
            "properties": {
                "title": "Budget",
                "locale": "en_US",
                "timeZone": "Asia/Hong_Kong",
            },
            "sheets": [
                {
                    "properties": {
                        "sheetId": 0,
                        "title": "Sheet1",
                        "index": 0,
                        "gridProperties": {"rowCount": 1000, "columnCount": 26},
                    }
                }
            ],
        }
    )

    result = await dispatch.sheets_get_metadata(mock_service, "ss_1")
    assert "'Budget'" in result
    assert "Asia/Hong_Kong" in result
    assert "1000 rows x 26 cols" in result


# --------------------------------------------------------------------------
# read_format
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_format_reports_number_format_and_styles():
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
                                        "formattedValue": "$5",
                                        "userEnteredFormat": {
                                            "numberFormat": {"type": "CURRENCY"},
                                            "textFormat": {"bold": True},
                                        },
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

    result = await dispatch.read_format(mock_service, "ss_1", range_name="Sheet1!A1")
    assert "CURRENCY" in result
    assert "bold" in result
    assert "R1C1" in result


# --------------------------------------------------------------------------
# export
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_export_csv():
    mock_service = create_mock_service()
    mock_service.spreadsheets().values().get().execute = Mock(
        return_value={"values": [["a", "b"], ["1", 'x"y']]}
    )

    result = await dispatch.export_csv(mock_service, "ss_1", range_name="Sheet1!A1:B2")
    assert result.splitlines() == ["a,b", '1,"x""y"']


@pytest.mark.asyncio
async def test_export_defaults_to_first_sheet():
    mock_service = create_mock_service()
    mock_service.spreadsheets().values().get().execute = Mock(
        return_value={"values": [["only"]]}
    )

    result = await dispatch.export_csv(mock_service, "ss_1")
    assert result.strip() == "only"


# --------------------------------------------------------------------------
# datasources
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_datasource_describe_none():
    mock_service = create_mock_service(get_payload={})

    result = await dispatch.datasource_describe(mock_service, "ss_1")
    assert "no connected data sources" in result


@pytest.mark.asyncio
async def test_datasource_describe_bigquery():
    mock_service = create_mock_service(
        get_payload={
            "dataSources": [
                {
                    "dataSourceId": "ds_1",
                    "type": "BIGQUERY",
                    "spec": {
                        "bigQuery": {
                            "projectId": "proj",
                            "tableSpec": {"datasetId": "d", "tableId": "t"},
                        }
                    },
                }
            ]
        }
    )

    result = await dispatch.datasource_describe(mock_service, "ss_1")
    assert "ds_1" in result
    assert "proj:d.t" in result


# --------------------------------------------------------------------------
# range_clear
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_range_clear():
    mock_service = create_mock_service()
    mock_service.spreadsheets().values().clear().execute = Mock(
        return_value={"clearedRange": "Sheet1!A1:B10"}
    )

    result = await dispatch.range_clear(
        mock_service, "ss_1", range_name="Sheet1!A1:B10"
    )
    assert "Cleared values" in result
    assert "Formatting was kept" in result


# --------------------------------------------------------------------------
# batch_update escape hatch
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_batch_update_passthrough():
    mock_service = create_mock_service(batch_response={"replies": [{}]})

    result = await dispatch.batch_update(
        mock_service,
        "ss_1",
        requests=[
            {
                "updateSpreadsheetProperties": {
                    "properties": {"title": "X"},
                    "fields": "title",
                }
            }
        ],
    )

    requests = get_requests(mock_service)
    assert "updateSpreadsheetProperties" in requests[0]
    assert "Applied 1 request(s)" in result


@pytest.mark.asyncio
async def test_batch_update_accepts_json_and_validates_shape():
    mock_service = create_mock_service(batch_response={"replies": []})

    await dispatch.batch_update(
        mock_service, "ss_1", requests='[{"addSheet": {"properties": {"title": "T"}}}]'
    )
    requests = get_requests(mock_service)
    assert "addSheet" in requests[0]

    with pytest.raises(UserInputError, match="exactly one request type"):
        await dispatch.batch_update(mock_service, "ss_1", requests=[{"a": {}, "b": {}}])

    with pytest.raises(UserInputError, match="non-empty list"):
        await dispatch.batch_update(mock_service, "ss_1", requests=[])
