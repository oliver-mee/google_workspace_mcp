"""Sheets gap operations; upstream tools own overlapping functionality."""

import asyncio
import copy
import csv
import inspect
import io
import json
import logging
from typing import List, Optional, Union

from core.utils import UserInputError
from core.action_dispatch import normalize_action_params, call_action
from auth.permissions import require_action
from gsheets import disclosure
from gsheets.sheets_helpers import (
    _parse_a1_range,
    _parse_hex_color,
    _parse_condition_values,
    _quote_sheet_title_for_a1,
)

logger = logging.getLogger(__name__)

TABLE_COLUMN_TYPES = {
    "DOUBLE",
    "CURRENCY",
    "PERCENT",
    "DATE",
    "TIME",
    "DATE_TIME",
    "TEXT",
    "BOOLEAN",
    "DROPDOWN",
    "FILES_CHIP",
    "PEOPLE_CHIP",
    "FINANCE_CHIP",
    "PLACE_CHIP",
    "RATINGS_CHIP",
}

TABLE_ROW_COLOR_FIELDS = {
    "header_color": "headerColorStyle",
    "footer_color": "footerColorStyle",
    "first_band_color": "firstBandColorStyle",
    "second_band_color": "secondBandColorStyle",
}


def _parse_table_column_properties(
    column_properties: Optional[Union[str, List[dict]]],
) -> Optional[List[dict]]:
    """
    Normalize table column definitions into Sheets TableColumnProperties.

    Accepts a list of dicts or a JSON-encoded list. Each entry supports:
      - columnName (str): the column header label.
      - columnType (str): one of TABLE_COLUMN_TYPES.
      - columnIndex (int, optional): table-relative index; defaults to position.
      - values (list, optional): dropdown choices. Only valid for DROPDOWN,
        where the API accepts a validation rule whose condition is ONE_OF_LIST.
    """
    if column_properties is None:
        return None

    parsed = column_properties
    if isinstance(parsed, str):
        try:
            parsed = json.loads(parsed)
        except json.JSONDecodeError as exc:
            raise UserInputError(
                "column_properties must be a list or a JSON-encoded list "
                '(e.g., \'[{"columnName":"Status","columnType":"DROPDOWN",'
                '"values":["Open","Closed"]}]\').'
            ) from exc

    if not isinstance(parsed, list):
        raise UserInputError("column_properties must be a list of column objects.")

    normalized: List[dict] = []
    for idx, column in enumerate(parsed):
        if not isinstance(column, dict):
            raise UserInputError(
                f"column_properties[{idx}] must be an object with columnName/columnType."
            )

        raw_index = column.get("columnIndex")
        if raw_index is None:
            column_index = idx
        else:
            try:
                column_index = int(raw_index)
            except (TypeError, ValueError) as exc:
                raise UserInputError(
                    f"column_properties[{idx}].columnIndex must be an integer, "
                    f"got {raw_index!r}."
                ) from exc
            if column_index < 0:
                raise UserInputError(
                    f"column_properties[{idx}].columnIndex must be zero or greater."
                )

        entry: dict = {"columnIndex": column_index}

        name = column.get("columnName")
        if name is not None:
            entry["columnName"] = str(name)

        raw_type = column.get("columnType")
        column_type = str(raw_type).upper() if raw_type is not None else None
        if column_type is not None:
            if column_type not in TABLE_COLUMN_TYPES:
                raise UserInputError(
                    f"column_properties[{idx}].columnType must be one of "
                    f"{sorted(TABLE_COLUMN_TYPES)}."
                )
            entry["columnType"] = column_type

        values = column.get("values")
        if values is not None and column_type != "DROPDOWN":
            raise UserInputError(
                f"column_properties[{idx}].values is only supported on DROPDOWN "
                "columns; the Sheets API rejects column validation elsewhere."
            )
        if column_type == "DROPDOWN":
            if not values:
                raise UserInputError(
                    f"column_properties[{idx}] is a DROPDOWN column and needs a "
                    "non-empty 'values' list of choices."
                )
            if not isinstance(values, list):
                raise UserInputError(
                    f"column_properties[{idx}].values must be a list of choices."
                )
            entry["dataValidationRule"] = {
                "condition": {
                    "type": "ONE_OF_LIST",
                    "values": [{"userEnteredValue": str(v)} for v in values],
                }
            }

        normalized.append(entry)

    return normalized


def _build_table_rows_properties(**colors: Optional[str]) -> Optional[dict]:
    """
    Build TableRowsProperties from hex colors.

    Accepts the keyword names in TABLE_ROW_COLOR_FIELDS. Returns None when no
    color was supplied so the caller can omit rowsProperties entirely.
    """
    rows_properties: dict = {}
    for key, api_field in TABLE_ROW_COLOR_FIELDS.items():
        parsed = _parse_hex_color(colors.get(key))
        if parsed:
            rows_properties[api_field] = {"rgbColor": parsed}

    return rows_properties or None


def _require(value, name: str, action: str, hint: str = ""):
    if value is None or (isinstance(value, str) and not value.strip()):
        suffix = f" {hint}" if hint else ""
        raise UserInputError(f"'{name}' is required for the '{action}' action.{suffix}")
    return value


async def _get_sheet_properties(service, spreadsheet_id: str) -> List[dict]:
    """Fetch sheet (tab) properties for A1-range resolution."""
    spreadsheet = await asyncio.to_thread(
        service.spreadsheets()
        .get(spreadsheetId=spreadsheet_id, fields="sheets(properties(sheetId,title))")
        .execute
    )
    return spreadsheet.get("sheets", [])


async def _batch_update(service, spreadsheet_id: str, requests: List[dict]) -> dict:
    return await asyncio.to_thread(
        service.spreadsheets()
        .batchUpdate(spreadsheetId=spreadsheet_id, body={"requests": requests})
        .execute
    )


async def table_create(
    service,
    spreadsheet_id: str,
    table_name: Optional[str],
    range_name: Optional[str],
    column_properties: Optional[Union[str, List[dict]]] = None,
    header_color: Optional[str] = None,
    footer_color: Optional[str] = None,
    first_band_color: Optional[str] = None,
    second_band_color: Optional[str] = None,
) -> str:
    """Create a native table over range_name (which must include the header row)."""
    _require(table_name, "table_name", "table_create")
    _require(
        range_name,
        "range_name",
        "table_create",
        "It must include the header row (e.g., 'Sheet1!A1:D50').",
    )
    assert table_name is not None and range_name is not None  # narrowed by _require

    parsed_columns = _parse_table_column_properties(column_properties)
    rows_properties = _build_table_rows_properties(
        header_color=header_color,
        footer_color=footer_color,
        first_band_color=first_band_color,
        second_band_color=second_band_color,
    )

    sheets = await _get_sheet_properties(service, spreadsheet_id)
    grid_range = _parse_a1_range(range_name, sheets)

    table: dict = {"name": table_name, "range": grid_range}
    # Truthiness is intentional: on create, None and [] both mean "no typed
    # columns", so an empty columnProperties array would be sent for nothing.
    if parsed_columns:
        table["columnProperties"] = parsed_columns
    if rows_properties:
        table["rowsProperties"] = rows_properties

    response = await _batch_update(
        service, spreadsheet_id, [{"addTable": {"table": table}}]
    )

    # Read the first reply defensively: an explicitly empty "replies" list
    # would make [0] raise. Report a missing id rather than failing the call.
    replies = response.get("replies") or [{}]
    created_id = replies[0].get("addTable", {}).get("table", {}).get("tableId")

    summary = f"created table '{table_name}' over {range_name}"
    if parsed_columns:
        summary += f" with {len(parsed_columns)} typed column(s)"
    return f"{summary} in spreadsheet {spreadsheet_id}. Table ID: {created_id or '(id unavailable)'}."


async def _fetch_tables(service, spreadsheet_id: str) -> List[dict]:
    spreadsheet = await asyncio.to_thread(
        service.spreadsheets()
        .get(
            spreadsheetId=spreadsheet_id,
            fields="sheets(properties(sheetId,title),tables)",
        )
        .execute
    )
    tables = []
    for sheet in spreadsheet.get("sheets", []):
        title = sheet.get("properties", {}).get("title", "")
        for table in sheet.get("tables", []) or []:
            tables.append({**table, "_sheet_title": title})
    return tables


async def _find_table(
    service,
    spreadsheet_id: str,
    action: str,
    table_id: Optional[str] = None,
    table_name: Optional[str] = None,
) -> dict:
    """Locate one table by id (preferred) or exact name."""
    if not table_id and not table_name:
        raise UserInputError(
            f"'table_id' (or 'table_name') is required for the '{action}' action. "
            "Use list_sheet_tables to find it."
        )
    tables = await _fetch_tables(service, spreadsheet_id)
    for table in tables:
        if table_id and table.get("tableId") == table_id:
            return table
        if table_name and table.get("name") == table_name:
            return table
    ref = table_id or table_name
    raise UserInputError(
        f"No table matching '{ref}' in spreadsheet {spreadsheet_id}. "
        "Use list_sheet_tables to see valid tables."
    )


def _describe_table(table: dict) -> str:
    lines = [
        f"Table '{table.get('name', '')}' (ID: {table.get('tableId', '')}) "
        f"on sheet '{table.get('_sheet_title', '')}'"
    ]
    grid = table.get("range", {})
    lines.append(
        f"  Range: rows {grid.get('startRowIndex', '?')}–{grid.get('endRowIndex', '?')}, "
        f"columns {grid.get('startColumnIndex', '?')}–{grid.get('endColumnIndex', '?')} (0-based, end-exclusive)"
    )
    columns = table.get("columnProperties", []) or []
    if columns:
        lines.append("  Columns:")
        for col in columns:
            desc = f"    [{col.get('columnIndex', '?')}] {col.get('columnName', '')}"
            if col.get("columnType"):
                desc += f" ({col['columnType']})"
            rule = col.get("dataValidationRule", {}).get("condition", {})
            if rule.get("values"):
                choices = ", ".join(
                    v.get("userEnteredValue", "") for v in rule["values"]
                )
                desc += f" — choices: {choices}"
            lines.append(desc)
    return "\n".join(lines)


async def table_get(
    service,
    spreadsheet_id: str,
    table_id: Optional[str] = None,
    table_name: Optional[str] = None,
) -> str:
    """Describe one native table: range, typed columns, dropdown choices."""
    table = await _find_table(
        service, spreadsheet_id, "table_get", table_id=table_id, table_name=table_name
    )
    return _describe_table(table)


async def table_clear(
    service,
    spreadsheet_id: str,
    table_id: Optional[str] = None,
    table_name: Optional[str] = None,
) -> str:
    """Clear all data rows of a table, keeping the table and its header row."""
    table = await _find_table(
        service, spreadsheet_id, "table_clear", table_id=table_id, table_name=table_name
    )
    grid = table.get("range", {})
    sheet_title = table.get("_sheet_title", "")
    start_row = grid.get("startRowIndex", 0) + 1  # keep the header row
    end_row = grid.get("endRowIndex", start_row)
    if end_row <= start_row:
        return (
            f"Table '{table.get('name', '')}' has no data rows to clear "
            f"in spreadsheet {spreadsheet_id}."
        )
    start_col = grid.get("startColumnIndex", 0)
    end_col = grid.get("endColumnIndex", start_col)

    def _col_name(index: int) -> str:
        name = ""
        index += 1
        while index:
            index, rem = divmod(index - 1, 26)
            name = chr(65 + rem) + name
        return name

    a1 = (
        f"'{sheet_title.replace(chr(39), chr(39) * 2)}'!{_col_name(start_col)}{start_row + 1}:"
        f"{_col_name(end_col - 1)}{end_row}"
    )
    await asyncio.to_thread(
        service.spreadsheets()
        .values()
        .clear(spreadsheetId=spreadsheet_id, range=a1, body={})
        .execute
    )
    return (
        f"Cleared {end_row - start_row} data row(s) of table "
        f"'{table.get('name', '')}' (header kept) in spreadsheet {spreadsheet_id}."
    )


async def table_delete(
    service,
    spreadsheet_id: str,
    table_id: Optional[str] = None,
    table_name: Optional[str] = None,
) -> str:
    """Delete a native table by id. The cell values are left in place."""
    table = await _find_table(
        service,
        spreadsheet_id,
        "table_delete",
        table_id=table_id,
        table_name=table_name,
    )
    await _batch_update(
        service, spreadsheet_id, [{"deleteTable": {"tableId": table["tableId"]}}]
    )
    return (
        f"Deleted table '{table.get('name', '')}' (ID: {table['tableId']}) "
        f"from spreadsheet {spreadsheet_id}. Cell values were left in place."
    )


async def _resolve_sheet(
    service, spreadsheet_id: str, sheet_name: str, action: str
) -> dict:
    sheets = await _get_sheet_properties(service, spreadsheet_id)
    for sheet in sheets:
        if sheet.get("properties", {}).get("title") == sheet_name:
            return sheet
    raise UserInputError(
        f"Sheet '{sheet_name}' not found in spreadsheet {spreadsheet_id} "
        f"for the '{action}' action."
    )


async def find_replace(
    service,
    spreadsheet_id: str,
    find: Optional[str] = None,
    replacement: Optional[str] = None,
    sheet_name: Optional[str] = None,
    range_name: Optional[str] = None,
    match_case: bool = False,
    match_entire_cell: bool = False,
    search_by_regex: bool = False,
    include_formulas: bool = False,
) -> str:
    """Find and replace across the spreadsheet, one tab, or an A1 range."""
    _require(find, "find", "find_replace")
    if replacement is None:
        raise UserInputError("'replacement' is required for the 'find_replace' action.")
    if sheet_name and range_name:
        raise UserInputError("Provide sheet_name or range_name, not both.")

    request: dict = {
        "find": find,
        "replacement": replacement,
        "matchCase": match_case,
        "matchEntireCell": match_entire_cell,
        "searchByRegex": search_by_regex,
        "includeFormulas": include_formulas,
    }
    if range_name:
        sheets = await _get_sheet_properties(service, spreadsheet_id)
        request["range"] = _parse_a1_range(range_name, sheets)
    elif sheet_name:
        sheet = await _resolve_sheet(
            service, spreadsheet_id, sheet_name, "find_replace"
        )
        request["sheetId"] = sheet["properties"]["sheetId"]
    else:
        request["allSheets"] = True

    response = await _batch_update(service, spreadsheet_id, [{"findReplace": request}])
    replies = response.get("replies") or [{}]
    occurrences = replies[0].get("findReplace", {}).get("occurrencesChanged", "?")
    scope = range_name or sheet_name or "all sheets"
    return (
        f"Replaced {occurrences} occurrence(s) of '{find}' with '{replacement}' "
        f"in {scope} of spreadsheet {spreadsheet_id}."
    )


async def sheet_copy(
    service,
    spreadsheet_id: str,
    sheet_name: Optional[str] = None,
    destination_spreadsheet_id: Optional[str] = None,
) -> str:
    """Copy a tab into another spreadsheet (Sheets copyTo)."""
    _require(sheet_name, "sheet_name", "sheet_copy")
    _require(
        destination_spreadsheet_id,
        "destination_spreadsheet_id",
        "sheet_copy",
    )
    sheet = await _resolve_sheet(service, spreadsheet_id, sheet_name, "sheet_copy")
    sheet_id = sheet["properties"]["sheetId"]
    response = await asyncio.to_thread(
        service.spreadsheets()
        .sheets()
        .copyTo(
            spreadsheetId=spreadsheet_id,
            sheetId=sheet_id,
            body={"destinationSpreadsheetId": destination_spreadsheet_id},
        )
        .execute
    )
    new_title = response.get("title", f"Copy of {sheet_name}")
    return (
        f"Copied tab '{sheet_name}' from {spreadsheet_id} to "
        f"{destination_spreadsheet_id} as '{new_title}'."
    )


PASTE_TYPES = {
    "PASTE_NORMAL",
    "PASTE_VALUES",
    "PASTE_FORMAT",
    "PASTE_NO_BORDERS",
    "PASTE_FORMULA",
    "PASTE_DATA_VALIDATION",
    "PASTE_CONDITIONAL_FORMATTING",
}


async def copy_paste(
    service,
    spreadsheet_id: str,
    source_range: Optional[str] = None,
    destination_range: Optional[str] = None,
    paste_type: str = "PASTE_NORMAL",
) -> str:
    """Copy-paste between A1 ranges within the spreadsheet."""
    _require(source_range, "source_range", "copy_paste")
    _require(destination_range, "destination_range", "copy_paste")
    normalized = (paste_type or "").upper()
    if normalized not in PASTE_TYPES:
        raise UserInputError(f"paste_type must be one of {sorted(PASTE_TYPES)}.")
    sheets = await _get_sheet_properties(service, spreadsheet_id)
    source_grid = _parse_a1_range(source_range, sheets)
    destination_grid = _parse_a1_range(destination_range, sheets)
    await _batch_update(
        service,
        spreadsheet_id,
        [
            {
                "copyPaste": {
                    "source": source_grid,
                    "destination": destination_grid,
                    "pasteType": normalized,
                }
            }
        ],
    )
    return (
        f"Copied '{source_range}' to '{destination_range}' ({normalized}) "
        f"in spreadsheet {spreadsheet_id}."
    )


CHART_TYPES = {
    "BAR",
    "LINE",
    "AREA",
    "COLUMN",
    "SCATTER",
    "COMBO",
    "PIE",
    "STEPPED_AREA",
}

LEGEND_POSITIONS = {
    "BOTTOM_LEGEND",
    "LEFT_LEGEND",
    "RIGHT_LEGEND",
    "TOP_LEGEND",
    "NO_LEGEND",
}


async def _fetch_charts(service, spreadsheet_id: str) -> List[dict]:
    spreadsheet = await asyncio.to_thread(
        service.spreadsheets()
        .get(
            spreadsheetId=spreadsheet_id,
            fields="sheets(properties(sheetId,title),charts)",
        )
        .execute
    )
    charts = []
    for sheet in spreadsheet.get("sheets", []):
        title = sheet.get("properties", {}).get("title", "")
        for chart in sheet.get("charts", []) or []:
            charts.append({**chart, "_sheet_title": title})
    return charts


def _describe_chart(chart: dict) -> str:
    spec = chart.get("spec", {})
    basic = spec.get("basicChart", {})
    chart_type = basic.get("chartType") or next(
        (
            k
            for k in (
                "pieChart",
                "bubbleChart",
                "candlestickChart",
                "orgChart",
                "scorecardChart",
            )
            if k in spec
        ),
        "unknown",
    )
    return (
        f"- chartId {chart.get('chartId', '?')}: '{spec.get('title', '(untitled)')}' "
        f"[{chart_type}] on sheet '{chart.get('_sheet_title', '')}'"
    )


async def chart_list(service, spreadsheet_id: str) -> str:
    """List all charts in the spreadsheet."""
    charts = await _fetch_charts(service, spreadsheet_id)
    if not charts:
        return f"Spreadsheet {spreadsheet_id} has no charts."
    lines = [f"Charts in spreadsheet {spreadsheet_id}:"]
    lines.extend(_describe_chart(c) for c in charts)
    return "\n".join(lines)


async def chart_get(
    service,
    spreadsheet_id: str,
    chart_id: Optional[int] = None,
) -> str:
    """Describe one chart by chart_id (see chart_list for IDs)."""
    if chart_id is None:
        raise UserInputError("'chart_id' is required for the 'chart_get' action.")
    for chart in await _fetch_charts(service, spreadsheet_id):
        if chart.get("chartId") == chart_id:
            return _describe_chart(chart).lstrip("- ")
    raise UserInputError(
        f"No chart with id {chart_id} in spreadsheet {spreadsheet_id}. "
        "Use chart_list to see valid chart IDs."
    )


def _build_basic_chart_spec(
    chart_type: str,
    title: Optional[str],
    legend_position: str,
    data_grid: dict,
) -> dict:
    """Build a basicChart spec over data_grid.

    A rectangular range (multiple columns) is translated the way the Sheets
    UI does: the first column becomes the domain (labels), each remaining
    column becomes a series. A single-column range keeps the legacy
    single-source shape (Google accepts it; the one-column E2E case passed).
    """
    start_col = data_grid.get("startColumnIndex", 0)
    end_col = data_grid.get("endColumnIndex", start_col + 1)
    column_count = max(1, end_col - start_col)

    if column_count <= 1:
        domain_sources = [data_grid]
        series_sources = [data_grid]
    else:
        domain_sources = [
            {
                **data_grid,
                "startColumnIndex": start_col,
                "endColumnIndex": start_col + 1,
            }
        ]
        series_sources = [
            {
                **data_grid,
                "startColumnIndex": start_col + k,
                "endColumnIndex": start_col + k + 1,
            }
            for k in range(1, column_count)
        ]

    spec: dict = {
        "basicChart": {
            "chartType": chart_type,
            "legendPosition": legend_position,
            "headerCount": 1,
            "domains": [{"domain": {"sourceRange": {"sources": domain_sources}}}],
            "series": [
                {"series": {"sourceRange": {"sources": [source]}}}
                for source in series_sources
            ],
        }
    }
    if title:
        spec["title"] = title
    return spec


async def chart_create(
    service,
    spreadsheet_id: str,
    data_range: Optional[str] = None,
    chart_type: str = "COLUMN",
    title: Optional[str] = None,
    legend_position: str = "RIGHT_LEGEND",
    anchor_cell: Optional[str] = None,
) -> str:
    """Create a basic chart over data_range (first row = headers).

    anchor_cell is the A1 cell the chart floats over (default: the sheet of
    data_range at H1).
    """
    _require(data_range, "data_range", "chart_create")
    normalized_type = (chart_type or "").upper()
    if normalized_type not in CHART_TYPES:
        raise UserInputError(f"chart_type must be one of {sorted(CHART_TYPES)}.")
    normalized_legend = (legend_position or "").upper()
    if normalized_legend not in LEGEND_POSITIONS:
        raise UserInputError(
            f"legend_position must be one of {sorted(LEGEND_POSITIONS)}."
        )

    sheets = await _get_sheet_properties(service, spreadsheet_id)
    data_grid = _parse_a1_range(data_range, sheets)
    anchor_grid = _parse_a1_range(anchor_cell, sheets) if anchor_cell else None

    spec = _build_basic_chart_spec(normalized_type, title, normalized_legend, data_grid)
    anchor = anchor_grid or {
        "sheetId": data_grid.get("sheetId"),
        "startRowIndex": 0,
        "endRowIndex": 1,
        "startColumnIndex": 7,
        "endColumnIndex": 8,
    }
    chart = {
        "spec": spec,
        "position": {
            "overlayPosition": {
                "anchorCell": {
                    "sheetId": anchor.get("sheetId"),
                    "rowIndex": anchor.get("startRowIndex", 0),
                    "columnIndex": anchor.get("startColumnIndex", 7),
                }
            }
        },
    }
    response = await _batch_update(
        service, spreadsheet_id, [{"addChart": {"chart": chart}}]
    )
    replies = response.get("replies") or [{}]
    chart_id = replies[0].get("addChart", {}).get("chart", {}).get("chartId")
    return (
        f"Created {normalized_type} chart over '{data_range}' "
        f"in spreadsheet {spreadsheet_id}. Chart ID: {chart_id if chart_id is not None else '(unavailable)'}."
    )


async def chart_update(
    service,
    spreadsheet_id: str,
    chart_id: Optional[int] = None,
    title: Optional[str] = None,
    chart_type: Optional[str] = None,
    legend_position: Optional[str] = None,
) -> str:
    """Retitle / retype / re-legend a chart. Fetches the current spec and
    applies the given overrides, so untouched spec fields survive."""
    if chart_id is None:
        raise UserInputError("'chart_id' is required for the 'chart_update' action.")
    if not any([title, chart_type, legend_position]):
        raise UserInputError(
            "Provide at least one of: title, chart_type, legend_position."
        )
    target = None
    for chart in await _fetch_charts(service, spreadsheet_id):
        if chart.get("chartId") == chart_id:
            target = chart
            break
    if not target:
        raise UserInputError(
            f"No chart with id {chart_id} in spreadsheet {spreadsheet_id}."
        )

    spec = copy.deepcopy(target.get("spec", {}))
    if title:
        spec["title"] = title
    basic = spec.get("basicChart")
    if basic is None and (chart_type or legend_position):
        raise UserInputError(
            f"Chart {chart_id} is not a basic chart; chart_type and "
            "legend_position overrides are only supported for basic charts."
        )
    if chart_type:
        normalized_type = chart_type.upper()
        if normalized_type not in CHART_TYPES:
            raise UserInputError(f"chart_type must be one of {sorted(CHART_TYPES)}.")
        basic["chartType"] = normalized_type
    if legend_position:
        normalized_legend = legend_position.upper()
        if normalized_legend not in LEGEND_POSITIONS:
            raise UserInputError(
                f"legend_position must be one of {sorted(LEGEND_POSITIONS)}."
            )
        basic["legendPosition"] = normalized_legend

    await _batch_update(
        service,
        spreadsheet_id,
        [{"updateChartSpec": {"chartId": chart_id, "spec": spec}}],
    )
    return f"Updated chart {chart_id} in spreadsheet {spreadsheet_id}."


async def chart_delete(
    service,
    spreadsheet_id: str,
    chart_id: Optional[int] = None,
) -> str:
    """Delete a chart by chart_id. The underlying data is unaffected."""
    if chart_id is None:
        raise UserInputError("'chart_id' is required for the 'chart_delete' action.")
    await _batch_update(
        service,
        spreadsheet_id,
        [{"deleteEmbeddedObject": {"objectId": chart_id}}],
    )
    return (
        f"Deleted chart {chart_id} from spreadsheet {spreadsheet_id}. "
        "The underlying data was unaffected."
    )


async def banding_set(
    service,
    spreadsheet_id: str,
    range_name: Optional[str] = None,
    header_color: Optional[str] = None,
    footer_color: Optional[str] = None,
    first_band_color: Optional[str] = None,
    second_band_color: Optional[str] = None,
) -> str:
    """Apply alternating row colors to range_name."""
    _require(range_name, "range_name", "banding_set")
    # Color params may be supplied at the top level OR inside params (see
    # sheets_manage dispatch); pick them up from both so either path works.
    provided = {
        k: v
        for k, v in (
            ("header_color", header_color),
            ("footer_color", footer_color),
            ("first_band_color", first_band_color),
            ("second_band_color", second_band_color),
        )
        if v
    }
    rows_properties = _build_table_rows_properties(**provided)
    if not rows_properties:
        raise UserInputError(
            "banding_set requires at least one of: header_color, footer_color, "
            "first_band_color, second_band_color. Pass them at the top level of "
            "sheets_manage or inside the `params` object. Use the form '#RRGGBB'."
        )
    sheets = await _get_sheet_properties(service, spreadsheet_id)
    grid_range = _parse_a1_range(range_name, sheets)
    response = await _batch_update(
        service,
        spreadsheet_id,
        [
            {
                "addBanding": {
                    "bandedRange": {
                        "range": grid_range,
                        "rowProperties": rows_properties,
                    }
                }
            }
        ],
    )
    # Atomicity: once the batchUpdate call has succeeded the mutation HAS
    # happened — never surface an error from response introspection. If the
    # reply omits the bandedRangeId, verify it by re-reading the sheet.
    banding_id = None
    try:
        replies = response.get("replies") or [{}]
        banding_id = (
            replies[0].get("addBanding", {}).get("bandedRange", {}).get("bandedRangeId")
        )
    except (AttributeError, IndexError, TypeError):
        banding_id = None
    if banding_id is None:
        try:
            banded = await _fetch_banded_ranges(service, spreadsheet_id)
            banding_id = banded[0].get("bandedRangeId") if banded else None
        except Exception as exc:  # verification is best-effort, never fatal
            logger.warning(
                "[banding_set] Banding applied but read-back failed: %s", exc
            )
    return (
        f"Applied banding to '{range_name}' in spreadsheet {spreadsheet_id}. "
        f"Banded range ID: {banding_id if banding_id is not None else '(unavailable)'}."
    )


async def _fetch_banded_ranges(service, spreadsheet_id: str) -> List[dict]:
    spreadsheet = await asyncio.to_thread(
        service.spreadsheets()
        .get(
            spreadsheetId=spreadsheet_id,
            fields="sheets(properties(sheetId,title),bandedRanges)",
        )
        .execute
    )
    banded = []
    for sheet in spreadsheet.get("sheets", []):
        title = sheet.get("properties", {}).get("title", "")
        for br in sheet.get("bandedRanges", []) or []:
            banded.append({**br, "_sheet_title": title})
    return banded


async def banding_list(service, spreadsheet_id: str) -> str:
    """List banded ranges."""
    banded = await _fetch_banded_ranges(service, spreadsheet_id)
    if not banded:
        return f"Spreadsheet {spreadsheet_id} has no banded ranges."
    lines = [f"Banded ranges in spreadsheet {spreadsheet_id}:"]
    for br in banded:
        grid = br.get("range", {})
        lines.append(
            f"- bandedRangeId {br.get('bandedRangeId', '?')}: sheet "
            f"'{br.get('_sheet_title', '')}', rows "
            f"{grid.get('startRowIndex', '?')}–{grid.get('endRowIndex', '?')}, "
            f"cols {grid.get('startColumnIndex', '?')}–{grid.get('endColumnIndex', '?')} (0-based)"
        )
    return "\n".join(lines)


async def banding_clear(
    service,
    spreadsheet_id: str,
    banded_range_id: Optional[int] = None,
) -> str:
    """Remove banding by banded_range_id (see banding_list). Values are unaffected."""
    _require(banded_range_id, "banded_range_id", "banding_clear")
    await _batch_update(
        service,
        spreadsheet_id,
        [{"deleteBanding": {"bandedRangeId": banded_range_id}}],
    )
    return (
        f"Removed banding {banded_range_id} from spreadsheet {spreadsheet_id}. "
        "Cell values were unaffected."
    )


VALIDATION_CONDITION_TYPES = {
    "NUMBER_GREATER",
    "NUMBER_GREATER_THAN_EQ",
    "NUMBER_LESS",
    "NUMBER_LESS_THAN_EQ",
    "NUMBER_EQ",
    "NUMBER_NOT_EQ",
    "NUMBER_BETWEEN",
    "NUMBER_NOT_BETWEEN",
    "TEXT_CONTAINS",
    "TEXT_NOT_CONTAINS",
    "TEXT_EQ",
    "TEXT_STARTS_WITH",
    "TEXT_ENDS_WITH",
    "TEXT_IS_EMAIL",
    "TEXT_IS_URL",
    "DATE_EQ",
    "DATE_BEFORE",
    "DATE_AFTER",
    "DATE_ON_OR_BEFORE",
    "DATE_ON_OR_AFTER",
    "DATE_BETWEEN",
    "DATE_NOT_BETWEEN",
    "DATE_IS_VALID",
    "ONE_OF_RANGE",
    "ONE_OF_LIST",
    "BLANK",
    "NOT_BLANK",
    "CUSTOM_FORMULA",
    "BOOLEAN",
}


def _build_validation_rule(
    condition_type: str,
    condition_values: Optional[List[Union[str, int, float]]],
    strict: bool,
    show_custom_ui: bool,
    input_message: Optional[str],
) -> dict:
    normalized = condition_type.upper()
    if normalized not in VALIDATION_CONDITION_TYPES:
        raise UserInputError(
            f"condition_type must be one of {sorted(VALIDATION_CONDITION_TYPES)}."
        )
    rule: dict = {
        "condition": {"type": normalized},
        "strict": strict,
        "showCustomUi": show_custom_ui,
    }
    if condition_values:
        rule["condition"]["values"] = [
            {"userEnteredValue": str(v)} for v in condition_values
        ]
    if input_message:
        rule["inputMessage"] = input_message
    return rule


async def validation_set(
    service,
    spreadsheet_id: str,
    range_name: Optional[str] = None,
    condition_type: Optional[str] = None,
    condition_values: Optional[Union[str, List[Union[str, int, float]]]] = None,
    strict: bool = True,
    show_custom_ui: bool = True,
    input_message: Optional[str] = None,
) -> str:
    """Set a data validation rule on range_name.

    condition_type uses the same vocabulary as conditional_format (e.g.
    ONE_OF_LIST, NUMBER_GREATER, DATE_BEFORE, TEXT_CONTAINS,
    BOOLEAN, DATE_IS_VALID). ONE_OF_LIST needs condition_values.
    """
    _require(range_name, "range_name", "validation_set")
    _require(condition_type, "condition_type", "validation_set")
    values_list = _parse_condition_values(condition_values)
    rule = _build_validation_rule(
        condition_type, values_list, strict, show_custom_ui, input_message
    )
    if rule["condition"]["type"] == "ONE_OF_LIST" and not values_list:
        raise UserInputError(
            "condition_values is required for a ONE_OF_LIST validation rule."
        )
    sheets = await _get_sheet_properties(service, spreadsheet_id)
    grid_range = _parse_a1_range(range_name, sheets)
    await _batch_update(
        service,
        spreadsheet_id,
        [{"setDataValidation": {"range": grid_range, "rule": rule}}],
    )
    return (
        f"Set {rule['condition']['type']} validation on '{range_name}' "
        f"in spreadsheet {spreadsheet_id}."
    )


async def validation_get(
    service,
    spreadsheet_id: str,
    range_name: Optional[str] = None,
) -> str:
    """Show the data validation rules in range_name."""
    _require(range_name, "range_name", "validation_get")
    response = await asyncio.to_thread(
        service.spreadsheets()
        .get(
            spreadsheetId=spreadsheet_id,
            ranges=[range_name],
            includeGridData=True,
            fields="sheets(data(startRow,startColumn,rowData(values(dataValidation))))",
        )
        .execute
    )
    found = []
    for sheet in response.get("sheets", []):
        for grid_data in sheet.get("data", []):
            start_row = grid_data.get("startRow", 0)
            start_col = grid_data.get("startColumn", 0)
            for r, row in enumerate(grid_data.get("rowData", []) or []):
                for c, cell in enumerate(row.get("values", []) or []):
                    rule = cell.get("dataValidation")
                    if rule:
                        condition = rule.get("condition", {})
                        values = ", ".join(
                            v.get("userEnteredValue", "")
                            for v in condition.get("values", []) or []
                        )
                        found.append(
                            f"- R{start_row + r + 1}C{start_col + c + 1}: "
                            f"{condition.get('type', '?')}"
                            + (f" [{values}]" if values else "")
                        )
    if not found:
        return f"No data validation rules in '{range_name}' of spreadsheet {spreadsheet_id}."
    return "\n".join(
        [f"Data validation in '{range_name}' of spreadsheet {spreadsheet_id}:"] + found
    )


async def note_set(
    service,
    spreadsheet_id: str,
    range_name: Optional[str] = None,
    note: Optional[str] = None,
) -> str:
    """Set (or replace) the note on every cell of range_name. Empty note clears."""
    _require(range_name, "range_name", "note_set")
    if note is None:
        raise UserInputError(
            "'note' is required for the 'note_set' action (use '' to clear)."
        )
    sheets = await _get_sheet_properties(service, spreadsheet_id)
    grid_range = _parse_a1_range(range_name, sheets)
    await _batch_update(
        service,
        spreadsheet_id,
        [
            {
                "repeatCell": {
                    "range": grid_range,
                    "cell": {"note": note},
                    "fields": "note",
                }
            }
        ],
    )
    verb = "Cleared note on" if note == "" else "Set note on"
    return f"{verb} '{range_name}' in spreadsheet {spreadsheet_id}."


async def filter_set(
    service,
    spreadsheet_id: str,
    sheet_name: Optional[str] = None,
    range_name: Optional[str] = None,
    clear: bool = False,
) -> str:
    """Set or clear the basic filter. filter_set needs range_name; clearing
    needs only sheet_name."""
    if clear:
        _require(sheet_name, "sheet_name", "filter_set (clear)")
        sheet = await _resolve_sheet(service, spreadsheet_id, sheet_name, "filter_set")
        await _batch_update(
            service,
            spreadsheet_id,
            [{"clearBasicFilter": {"sheetId": sheet["properties"]["sheetId"]}}],
        )
        return f"Cleared the basic filter on '{sheet_name}' in spreadsheet {spreadsheet_id}."

    _require(range_name, "range_name", "filter_set")
    sheets = await _get_sheet_properties(service, spreadsheet_id)
    grid_range = _parse_a1_range(range_name, sheets)
    await _batch_update(
        service,
        spreadsheet_id,
        [{"setBasicFilter": {"filter": {"range": grid_range}}}],
    )
    return f"Set a basic filter over '{range_name}' in spreadsheet {spreadsheet_id}."


async def read_format(
    service,
    spreadsheet_id: str,
    range_name: Optional[str] = None,
) -> str:
    """Show number formats and text styling in range_name (compact)."""
    _require(range_name, "range_name", "read_format")
    response = await asyncio.to_thread(
        service.spreadsheets()
        .get(
            spreadsheetId=spreadsheet_id,
            ranges=[range_name],
            includeGridData=True,
            fields="sheets(data(startRow,startColumn,rowData(values(userEnteredFormat(numberFormat,textFormat),formattedValue))))",
        )
        .execute
    )
    lines = [f"Formats in '{range_name}' of spreadsheet {spreadsheet_id}:"]
    seen = 0
    for sheet in response.get("sheets", []):
        for grid_data in sheet.get("data", []):
            start_row = grid_data.get("startRow", 0)
            start_col = grid_data.get("startColumn", 0)
            for r, row in enumerate(grid_data.get("rowData", []) or []):
                for c, cell in enumerate(row.get("values", []) or []):
                    fmt = cell.get("userEnteredFormat", {})
                    nf = fmt.get("numberFormat")
                    tf = fmt.get("textFormat", {})
                    parts = []
                    if nf:
                        desc = nf.get("type", "")
                        if nf.get("pattern"):
                            desc += f" ({nf['pattern']})"
                        parts.append(f"number: {desc}")
                    styles = [
                        k
                        for k in ("bold", "italic", "strikethrough", "underline")
                        if tf.get(k)
                    ]
                    if styles:
                        parts.append(f"text: {', '.join(styles)}")
                    if parts:
                        seen += 1
                        if seen <= 50:
                            lines.append(
                                f"  R{start_row + r + 1}C{start_col + c + 1} "
                                f"'{cell.get('formattedValue', '')}' — {'; '.join(parts)}"
                            )
    if seen == 0:
        lines.append("  (no explicit formatting)")
    elif seen > 50:
        lines.append(f"  ... {seen - 50} more cells with formatting")
    return "\n".join(lines)


async def export_csv(
    service,
    spreadsheet_id: str,
    range_name: Optional[str] = None,
    map_flag: bool = False,
    navigate: Optional[str] = None,
    head: Optional[int] = None,
    skip_tokens: int = 0,
) -> str:
    """Export range_name (or the first sheet) as CSV text.

    Optional progressive-disclosure controls (see gsheets/disclosure.py):
    map=True returns a token-bounded block index; navigate='<block>' or
    '<block>.<row>' returns content at that ordinal; head=<tokens> (with
    navigate) returns a token-bounded window plus a next_call hint;
    skip_tokens=<tokens> continues a window. With none of these set the CSV
    is returned unchanged (PASSTHROUGH).
    """
    target = range_name
    if not target:
        sheets = await _get_sheet_properties(service, spreadsheet_id)
        if not sheets:
            raise UserInputError("No sheets found in spreadsheet.")
        target = _quote_sheet_title_for_a1(
            sheets[0].get("properties", {}).get("title", "Sheet1")
        )
    result = await asyncio.to_thread(
        service.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=target)
        .execute
    )
    values = result.get("values", [])
    if not values:
        return f"Range '{target}' in spreadsheet {spreadsheet_id} is empty."

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerows(values)
    csv_text = buffer.getvalue()

    if not (map_flag or navigate is not None or head is not None or skip_tokens):
        return csv_text
    return disclosure.export_response(
        csv_text,
        map_flag=map_flag,
        navigate=navigate,
        head=head,
        skip_tokens=skip_tokens,
    )


DATASOURCE_DESCRIBE_FIELDS = "dataSources(dataSourceId,spec,sheetId)"


def _datasource_table_sync_state(dst: dict) -> str:
    """Sync state of a DataSourceTable, from dataExecutionStatus.state."""
    status = dst.get("dataExecutionStatus") or {}
    return status.get("state", "?")


async def datasource_describe(service, spreadsheet_id: str) -> str:
    """List Connected Sheets data sources (BigQuery etc.)."""
    spreadsheet = await asyncio.to_thread(
        service.spreadsheets()
        .get(
            spreadsheetId=spreadsheet_id,
            fields=DATASOURCE_DESCRIBE_FIELDS,
        )
        .execute
    )
    sources = spreadsheet.get("dataSources", []) or []
    if not sources:
        return f"Spreadsheet {spreadsheet_id} has no connected data sources."
    lines = [f"Data sources in spreadsheet {spreadsheet_id}:"]
    for ds in sources:
        spec = ds.get("spec", {})
        detail = spec.get("bigQuery", {})
        project = detail.get("projectId", "")
        table = detail.get("tableSpec", {})
        ref = ""
        if table:
            ref = f" ({project}:{table.get('datasetId', '?')}.{table.get('tableId', '?')})"
        elif detail.get("querySpec"):
            ref = f" ({project}: custom query)"
        lines.append(f"- {ds.get('dataSourceId', '?')} [{next(iter(spec), '?')}]{ref}")
    return "\n".join(lines)


async def datasource_table_describe(
    service,
    spreadsheet_id: str,
    range_name: Optional[str] = None,
) -> str:
    """Describe the data source backing the range/sheet, if any."""
    target = range_name
    if not target:
        sheets = await _get_sheet_properties(service, spreadsheet_id)
        if not sheets:
            raise UserInputError("No sheets found in spreadsheet.")
        target = _quote_sheet_title_for_a1(
            sheets[0].get("properties", {}).get("title", "Sheet1")
        )
    response = await asyncio.to_thread(
        service.spreadsheets()
        .get(
            spreadsheetId=spreadsheet_id,
            ranges=[target],
            includeGridData=True,
            fields="sheets(properties(title),data(rowData(values(dataSourceTable))))",
        )
        .execute
    )
    lines = [f"Data source tables in '{target}' of spreadsheet {spreadsheet_id}:"]
    found = False
    for sheet in response.get("sheets", []):
        for dst in (
            cell["dataSourceTable"]
            for grid in sheet.get("data", [])
            for row in grid.get("rowData", [])
            for cell in row.get("values", [])
            if cell.get("dataSourceTable")
        ):
            found = True
            lines.append(
                f"- dataSourceId {dst.get('dataSourceId', '?')}, "
                f"sync state: {_datasource_table_sync_state(dst)}, "
                f"columns selected: {len(dst.get('columns', []) or [])}"
            )
    if not found:
        lines.append("  (none — this range is not backed by a connected data source)")
    return "\n".join(lines)


READ_ACTIONS = (
    "chart_get",
    "chart_list",
    "banding_list",
    "validation_get",
    "read_format",
    "export",
    "datasource_describe",
    "datasource_table_describe",
)
MANAGE_ACTIONS = (
    "table_create",
    "find_replace",
    "sheet_copy",
    "copy_paste",
    "chart_create",
    "chart_update",
    "chart_delete",
    "banding_set",
    "banding_clear",
    "validation_set",
    "note_set",
    "filter_set",
)
DELETE_ACTIONS = ("table_clear", "table_delete")


async def dispatch_action(
    service, spreadsheet_id: str, family: str, action: str, params=None
) -> str:
    """Validate the whole action contract before any Google request."""
    actions = {
        "read": READ_ACTIONS,
        "manage": MANAGE_ACTIONS,
        "delete": DELETE_ACTIONS,
    }[family]
    if action not in actions:
        raise UserInputError(
            f"Unknown {family} action: {action}. Valid: {', '.join(actions)}"
        )
    require_action("sheets", "delete" if family == "delete" else family)
    function = export_csv if action == "export" else globals()[action]
    extra = normalize_action_params(params)
    if action == "export" and "map" in extra:
        extra["map_flag"] = extra.pop("map")
    try:
        inspect.signature(function).bind(service, spreadsheet_id, **extra)
    except TypeError as exc:
        raise UserInputError(f"Invalid {action} parameters: {exc}") from exc
    return await call_action(function, service, spreadsheet_id, **extra)


def action_contract(action: str) -> dict:
    """Inspectable parameter contract for retained gap actions."""
    if action not in READ_ACTIONS + MANAGE_ACTIONS + DELETE_ACTIONS:
        raise UserInputError(f"Unknown Sheets action: {action}")
    fn = export_csv if action == "export" else globals()[action]
    parameters = {}
    for name, parameter in inspect.signature(fn).parameters.items():
        if name in ("service", "spreadsheet_id"):
            continue
        public_name = "map" if name == "map_flag" else name
        parameters[public_name] = {
            "type": str(parameter.annotation),
            "required": parameter.default is inspect.Parameter.empty,
        }
        if parameter.default is not inspect.Parameter.empty:
            parameters[public_name]["default"] = parameter.default
    return {
        "action": action,
        "description": inspect.getdoc(fn),
        "parameters": parameters,
    }


async def list_tables(
    service, spreadsheet_id: str, table_id=None, table_name=None
) -> str:
    """Native table listing with optional selection and complete typed-column detail."""
    if table_id or table_name:
        return await table_get(
            service, spreadsheet_id, table_id=table_id, table_name=table_name
        )
    tables = await _fetch_tables(service, spreadsheet_id)
    return (
        "\n\n".join(_describe_table(table) for table in tables)
        or f"No structured tables found in spreadsheet {spreadsheet_id}."
    )
