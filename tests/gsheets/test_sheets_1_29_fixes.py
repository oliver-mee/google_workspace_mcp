"""Regression tests for the 1.29.0 E2E-feedback fixes.

Covers: banding_set atomicity (never error after a successful mutation),
datasource field-mask validity (discovery-aligned), chart_create rectangular
range translation, read_sheet_values note/hyperlink-only early exit, and the
widened modify_sheet_values cell schema.
"""

import json
from unittest.mock import Mock

import pytest

from core.utils import UserInputError
from gsheets import sheets_dispatch_helpers as dispatch
from gsheets import sheets_tools

# Sheets v4 discovery-valid fields (2026-09-02):
#   DataSource -> dataSourceId, spec, sheetId, calculatedColumns        (no `type`)
#   DataSourceTable -> dataSourceId, columnSelectionType, columns,
#                      dataExecutionStatus, rowLimit, filterSpecs,
#                      sortSpecs                                          (no `syncState`)
VALID_DATASOURCE_FIELDS = {"dataSourceId", "spec", "sheetId", "calculatedColumns"}
VALID_DATASOURCE_TABLE_FIELDS = {
    "dataSourceId",
    "columnSelectionType",
    "columns",
    "dataExecutionStatus",
    "rowLimit",
    "filterSpecs",
    "sortSpecs",
}


def _mask_tokens(mask: str) -> list[str]:
    import re

    return re.findall(r"[A-Za-z][A-Za-z0-9_]*", mask)


# ---------------------------------------------------------------------------
# 1. banding_set atomicity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_banding_set_never_errors_after_mutation():
    """A batchUpdate that succeeded must never surface an error from reply parsing."""
    service = Mock()
    service.spreadsheets().batchUpdate().execute.return_value = {}  # no replies
    # read-back: one tab exists, no banded ranges yet
    service.spreadsheets().get().execute.return_value = {
        "sheets": [
            {"properties": {"sheetId": 0, "title": "Sheet1"}, "bandedRanges": []}
        ]
    }

    result = await dispatch.banding_set(
        service, "SS1", range_name="Sheet1!A1:B5", header_color="#FF0000"
    )
    assert "Applied banding" in result
    assert "(unavailable)" in result  # no exception, verified read-back found none


@pytest.mark.asyncio
async def test_banding_set_verifies_id_via_read_back():
    """When the reply omits the id, the id is recovered by re-reading the sheet."""
    service = Mock()
    service.spreadsheets().batchUpdate().execute.return_value = {}
    service.spreadsheets().get().execute.return_value = {
        "sheets": [
            {
                "properties": {"sheetId": 0, "title": "Sheet1"},
                "bandedRanges": [
                    {
                        "bandedRangeId": 1216472560,
                        "range": {"sheetId": 0},
                        "rowProperties": {"headerColor": {"red": 1.0}},
                    }
                ],
            }
        ]
    }

    result = await dispatch.banding_set(
        service, "SS1", range_name="Sheet1!A1:B5", header_color="#FF0000"
    )
    assert "1216472560" in result


@pytest.mark.asyncio
async def test_banding_set_preflight_error_blocks_api_call():
    """Validation errors still fire before any mutation."""
    service = Mock()
    with pytest.raises(UserInputError):
        await dispatch.banding_set(service, "SS1", range_name="Sheet1!A1:B5")
    service.spreadsheets().batchUpdate.assert_not_called()


# ---------------------------------------------------------------------------
# 2. datasource field masks
# ---------------------------------------------------------------------------


def test_datasource_field_masks_use_only_discovery_valid_fields():
    tokens = _mask_tokens(dispatch.DATASOURCE_DESCRIBE_FIELDS)
    leaf_tokens = set(tokens) - {"dataSources"}
    assert leaf_tokens <= VALID_DATASOURCE_FIELDS, leaf_tokens - VALID_DATASOURCE_FIELDS
    assert "type" not in leaf_tokens  # the field that broke the E2E call

    tokens = _mask_tokens(dispatch.DATASOURCE_TABLE_DESCRIBE_FIELDS)
    leaf_tokens = set(tokens) - {
        "sheets",
        "properties",
        "title",
        "dataSourceTables",
        "tables",
    }
    assert leaf_tokens <= VALID_DATASOURCE_TABLE_FIELDS
    assert "syncState" not in leaf_tokens  # the field that broke the E2E call


@pytest.mark.asyncio
async def test_datasource_actions_send_the_aligned_masks():
    service = Mock()
    service.spreadsheets().get().execute.return_value = {"dataSources": []}
    await dispatch.datasource_describe(service, "SS1")
    assert (
        service.spreadsheets().get.call_args.kwargs["fields"]
        == dispatch.DATASOURCE_DESCRIBE_FIELDS
    )

    service.reset_mock()
    service.spreadsheets().get().execute.return_value = {
        "sheets": [{"properties": {"title": "Sheet1"}, "dataSourceTables": []}]
    }
    await dispatch.datasource_table_describe(service, "SS1")
    assert (
        service.spreadsheets().get.call_args.kwargs["fields"]
        == dispatch.DATASOURCE_TABLE_DESCRIBE_FIELDS
    )


def test_datasource_table_sync_state_derivation():
    assert (
        dispatch._datasource_table_sync_state(
            {"dataExecutionStatus": {"state": "RUNNING"}}
        )
        == "RUNNING"
    )
    assert dispatch._datasource_table_sync_state({}) == "?"


# ---------------------------------------------------------------------------
# 3. chart_create rectangular range translation
# ---------------------------------------------------------------------------


def test_basic_chart_spec_translates_rectangular_range():
    grid = {
        "sheetId": 42,
        "startRowIndex": 0,
        "endRowIndex": 5,
        "startColumnIndex": 0,
        "endColumnIndex": 4,
    }
    spec = dispatch._build_basic_chart_spec("COLUMN", "T", "RIGHT_LEGEND", grid)
    basic = spec["basicChart"]
    domain_sources = basic["domains"][0]["domain"]["sourceRange"]["sources"]
    series_sources = [s["series"]["sourceRange"]["sources"][0] for s in basic["series"]]
    # domain = first column; series = the remaining three columns
    assert len(domain_sources) == 1
    assert (
        domain_sources[0]["startColumnIndex"],
        domain_sources[0]["endColumnIndex"],
    ) == (
        0,
        1,
    )
    assert len(series_sources) == 3
    assert [(s["startColumnIndex"], s["endColumnIndex"]) for s in series_sources] == [
        (1, 2),
        (2, 3),
        (3, 4),
    ]
    for source in domain_sources + series_sources:
        assert source["sheetId"] == 42
        assert source["startRowIndex"] == 0 and source["endRowIndex"] == 5


def test_basic_chart_spec_single_column_keeps_legacy_shape():
    grid = {
        "sheetId": 7,
        "startRowIndex": 0,
        "endRowIndex": 10,
        "startColumnIndex": 2,
        "endColumnIndex": 3,
    }
    spec = dispatch._build_basic_chart_spec("COLUMN", None, "RIGHT_LEGEND", grid)
    assert spec["basicChart"]["domains"][0]["domain"]["sourceRange"]["sources"] == [
        grid
    ]
    assert len(spec["basicChart"]["series"]) == 1


# ---------------------------------------------------------------------------
# 4. read_sheet_values note-only early exit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_sheet_values_notes_only_cells_not_dropped(monkeypatch):
    async def fake_grid_metadata(
        service,
        spreadsheet_id,
        resolved_range,
        values,
        include_hyperlinks=False,
        include_notes=False,
    ):
        return ("", "NOTES:\n- H1: Dispatcher test note")

    monkeypatch.setattr(sheets_tools, "_fetch_grid_metadata", fake_grid_metadata)
    service = Mock()
    service.spreadsheets().values().get().execute.return_value = {
        "values": [],
        "range": "'Sheet1'!H1",
    }

    fn = sheets_tools.read_sheet_values
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__

    result = await fn(
        service, "user@example.com", "SS1", "Sheet1!H1", include_notes=True
    )
    assert "No data found" not in result
    assert "Dispatcher test note" in result


# ---------------------------------------------------------------------------
# 5. modify_sheet_values cell schema
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_modify_sheet_values_schema_allows_numbers_and_bools():
    # The shared server singleton gets tier-filtered by earlier tests, so probe
    # the raw function on a fresh FastMCP instance instead.
    from gsheets import sheets_tools as st

    fn = st.modify_sheet_values
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__

    from fastmcp import FastMCP

    probe = FastMCP("schema-probe")
    probe.tool()(fn)
    tools = {t.name: t for t in await probe.list_tools()}
    schema = tools["modify_sheet_values"].parameters
    values_prop = json.dumps(schema["properties"]["values"])
    assert '"integer"' in values_prop or '"number"' in values_prop
    assert '"boolean"' in values_prop
    assert '"null"' in values_prop
