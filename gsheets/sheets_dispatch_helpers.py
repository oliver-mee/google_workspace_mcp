"""
Google Sheets dispatcher implementations.

Family implementations behind the sheets_read / sheets_manage / sheets_delete
dispatcher tools. Every function is decoupled from the MCP decorators so it
can be unit tested with a mock service, and takes explicit keyword arguments
rather than the dispatcher's loose params.

Convention: validate all arguments before any network call; return a
human-readable confirmation string.
"""

import asyncio
import copy
import json
import logging
from typing import List, Optional, Union

from core.utils import UserInputError
from gsheets.sheets_helpers import (
    _build_boolean_rule,
    _build_gradient_rule,
    _build_table_rows_properties,
    _column_to_index,
    _fetch_sheets_with_rules,
    _format_conditional_rules_section,
    _parse_a1_range,
    _parse_condition_values,
    _parse_gradient_points,
    _parse_hex_color,
    _parse_table_column_properties,
    _select_sheet,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared utilities
# ---------------------------------------------------------------------------


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


def _normalize_params(params: Optional[Union[str, dict]]) -> dict:
    """Accept a dict or JSON-encoded object of action-specific parameters."""
    if params is None:
        return {}
    if isinstance(params, str):
        try:
            params = json.loads(params)
        except json.JSONDecodeError as exc:
            raise UserInputError(
                "params must be an object or a JSON-encoded object."
            ) from exc
    if not isinstance(params, dict):
        raise UserInputError("params must be an object of action-specific fields.")
    return params


# ---------------------------------------------------------------------------
# Tables family (native Sheets tables: addTable / deleteTable / values.clear)
# ---------------------------------------------------------------------------


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
        f"'{sheet_title}'!{_col_name(start_col)}{start_row + 1}:"
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


# ---------------------------------------------------------------------------
# Tabs family
# ---------------------------------------------------------------------------


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


async def tab_add(
    service,
    spreadsheet_id: str,
    new_tab_name: Optional[str] = None,
    index: Optional[int] = None,
    tab_color: Optional[str] = None,
) -> str:
    """Add a tab (sheet) to the spreadsheet."""
    _require(new_tab_name, "new_tab_name", "tab_add")
    properties: dict = {"title": new_tab_name}
    if index is not None:
        if not isinstance(index, int) or index < 0:
            raise UserInputError("index must be a non-negative integer.")
        properties["index"] = index
    parsed_color = _parse_hex_color(tab_color)
    if parsed_color:
        properties["tabColor"] = parsed_color

    response = await _batch_update(
        service, spreadsheet_id, [{"addSheet": {"properties": properties}}]
    )
    replies = response.get("replies") or [{}]
    new_id = replies[0].get("addSheet", {}).get("properties", {}).get("sheetId")
    return (
        f"Added tab '{new_tab_name}' (sheetId: {new_id if new_id is not None else '(unavailable)'}) "
        f"to spreadsheet {spreadsheet_id}."
    )


async def tab_rename(
    service,
    spreadsheet_id: str,
    sheet_name: Optional[str] = None,
    new_tab_name: Optional[str] = None,
) -> str:
    """Rename a tab."""
    _require(sheet_name, "sheet_name", "tab_rename")
    _require(new_tab_name, "new_tab_name", "tab_rename")
    sheet = await _resolve_sheet(service, spreadsheet_id, sheet_name, "tab_rename")
    sheet_id = sheet["properties"]["sheetId"]
    await _batch_update(
        service,
        spreadsheet_id,
        [
            {
                "updateSheetProperties": {
                    "properties": {"sheetId": sheet_id, "title": new_tab_name},
                    "fields": "title",
                }
            }
        ],
    )
    return (
        f"Renamed tab '{sheet_name}' to '{new_tab_name}' "
        f"in spreadsheet {spreadsheet_id}."
    )


async def tab_reorder(
    service,
    spreadsheet_id: str,
    sheet_name: Optional[str] = None,
    index: Optional[int] = None,
) -> str:
    """Move a tab to a new 0-based position."""
    _require(sheet_name, "sheet_name", "tab_reorder")
    if index is None or not isinstance(index, int) or index < 0:
        raise UserInputError(
            "index (0-based target position) is required for the 'tab_reorder' action."
        )
    sheet = await _resolve_sheet(service, spreadsheet_id, sheet_name, "tab_reorder")
    sheet_id = sheet["properties"]["sheetId"]
    await _batch_update(
        service,
        spreadsheet_id,
        [
            {
                "updateSheetProperties": {
                    "properties": {"sheetId": sheet_id, "index": index},
                    "fields": "index",
                }
            }
        ],
    )
    return (
        f"Moved tab '{sheet_name}' to position {index} in spreadsheet {spreadsheet_id}."
    )


async def delete_tab(
    service,
    spreadsheet_id: str,
    sheet_name: Optional[str] = None,
) -> str:
    """Delete a tab and all its contents. Data-destructive."""
    _require(sheet_name, "sheet_name", "delete_tab")
    sheets = await _get_sheet_properties(service, spreadsheet_id)
    if len(sheets) <= 1:
        raise UserInputError(
            "Cannot delete the only tab of a spreadsheet; a spreadsheet must "
            "contain at least one sheet."
        )
    target = None
    for sheet in sheets:
        if sheet.get("properties", {}).get("title") == sheet_name:
            target = sheet
            break
    if not target:
        raise UserInputError(
            f"Sheet '{sheet_name}' not found in spreadsheet {spreadsheet_id}."
        )
    sheet_id = target["properties"]["sheetId"]
    await _batch_update(
        service, spreadsheet_id, [{"deleteSheet": {"sheetId": sheet_id}}]
    )
    return (
        f"Deleted tab '{sheet_name}' and all its contents "
        f"from spreadsheet {spreadsheet_id}."
    )


# ---------------------------------------------------------------------------
# Ranges family: merge, find-replace, named ranges, copy
# ---------------------------------------------------------------------------


MERGE_TYPES = {"MERGE_ALL", "MERGE_COLUMNS", "MERGE_ROWS"}


async def merge_cells(
    service,
    spreadsheet_id: str,
    range_name: Optional[str] = None,
    merge_type: str = "MERGE_ALL",
) -> str:
    """Merge the cells of range_name."""
    _require(range_name, "range_name", "merge")
    normalized = (merge_type or "").upper()
    if normalized not in MERGE_TYPES:
        raise UserInputError(f"merge_type must be one of {sorted(MERGE_TYPES)}.")
    sheets = await _get_sheet_properties(service, spreadsheet_id)
    grid_range = _parse_a1_range(range_name, sheets)
    await _batch_update(
        service,
        spreadsheet_id,
        [{"mergeCells": {"range": grid_range, "mergeType": normalized}}],
    )
    return (
        f"Merged range '{range_name}' ({normalized}) in spreadsheet {spreadsheet_id}."
    )


async def unmerge_cells(
    service,
    spreadsheet_id: str,
    range_name: Optional[str] = None,
) -> str:
    """Unmerge all merged cells intersecting range_name."""
    _require(range_name, "range_name", "unmerge")
    sheets = await _get_sheet_properties(service, spreadsheet_id)
    grid_range = _parse_a1_range(range_name, sheets)
    await _batch_update(
        service, spreadsheet_id, [{"unmergeCells": {"range": grid_range}}]
    )
    return f"Unmerged range '{range_name}' in spreadsheet {spreadsheet_id}."


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


async def _fetch_named_ranges(service, spreadsheet_id: str) -> List[dict]:
    spreadsheet = await asyncio.to_thread(
        service.spreadsheets()
        .get(spreadsheetId=spreadsheet_id, fields="namedRanges")
        .execute
    )
    return spreadsheet.get("namedRanges", []) or []


async def named_range_add(
    service,
    spreadsheet_id: str,
    name: Optional[str] = None,
    range_name: Optional[str] = None,
) -> str:
    """Name an A1 range."""
    _require(name, "name", "named_range_add")
    _require(range_name, "range_name", "named_range_add")
    sheets = await _get_sheet_properties(service, spreadsheet_id)
    grid_range = _parse_a1_range(range_name, sheets)
    response = await _batch_update(
        service,
        spreadsheet_id,
        [{"addNamedRange": {"namedRange": {"name": name, "range": grid_range}}}],
    )
    replies = response.get("replies") or [{}]
    nr_id = (
        replies[0].get("addNamedRange", {}).get("namedRange", {}).get("namedRangeId")
    )
    return (
        f"Added named range '{name}' over '{range_name}' "
        f"(ID: {nr_id or '(unavailable)'}) in spreadsheet {spreadsheet_id}."
    )


async def named_range_delete(
    service,
    spreadsheet_id: str,
    name: Optional[str] = None,
    named_range_id: Optional[str] = None,
) -> str:
    """Delete a named range by id or exact name. Cell values are unaffected."""
    if not named_range_id and not name:
        raise UserInputError(
            "'named_range_id' (or 'name') is required for the 'named_range_delete' action."
        )
    target_id = named_range_id
    if not target_id:
        for nr in await _fetch_named_ranges(service, spreadsheet_id):
            if nr.get("name") == name:
                target_id = nr.get("namedRangeId")
                break
        if not target_id:
            raise UserInputError(
                f"No named range '{name}' in spreadsheet {spreadsheet_id}."
            )
    await _batch_update(
        service, spreadsheet_id, [{"deleteNamedRange": {"namedRangeId": target_id}}]
    )
    return (
        f"Deleted named range '{name or target_id}' from spreadsheet {spreadsheet_id}. "
        "Cell values were unaffected."
    )


def _describe_named_range(nr: dict) -> str:
    grid = nr.get("range", {})
    return (
        f"- '{nr.get('name', '')}' (ID: {nr.get('namedRangeId', '')}): "
        f"sheetId {grid.get('sheetId', '?')}, "
        f"rows {grid.get('startRowIndex', '?')}–{grid.get('endRowIndex', '?')}, "
        f"cols {grid.get('startColumnIndex', '?')}–{grid.get('endColumnIndex', '?')} (0-based)"
    )


async def named_range_list(service, spreadsheet_id: str) -> str:
    """List all named ranges."""
    named_ranges = await _fetch_named_ranges(service, spreadsheet_id)
    if not named_ranges:
        return f"Spreadsheet {spreadsheet_id} has no named ranges."
    lines = [f"Named ranges in spreadsheet {spreadsheet_id}:"]
    lines.extend(_describe_named_range(nr) for nr in named_ranges)
    return "\n".join(lines)


async def named_range_get(
    service,
    spreadsheet_id: str,
    name: Optional[str] = None,
) -> str:
    """Describe one named range by exact name."""
    _require(name, "name", "named_range_get")
    for nr in await _fetch_named_ranges(service, spreadsheet_id):
        if nr.get("name") == name:
            return _describe_named_range(nr).lstrip("- ")
    raise UserInputError(f"No named range '{name}' in spreadsheet {spreadsheet_id}.")


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


# ---------------------------------------------------------------------------
# Charts family
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Banding family
# ---------------------------------------------------------------------------


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
        k: v for k, v in (("header_color", header_color),
                            ("footer_color", footer_color),
                            ("first_band_color", first_band_color),
                            ("second_band_color", second_band_color)) if v
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
            replies[0]
            .get("addBanding", {})
            .get("bandedRange", {})
            .get("bandedRangeId")
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
    banded_range_id: Optional[str] = None,
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


# ---------------------------------------------------------------------------
# Data validation family
# ---------------------------------------------------------------------------

# Condition types accepted by DataValidationRule.condition.type. Distinct from
# CONDITION_TYPES (conditional formatting): validation adds ONE_OF_LIST,
# BOOLEAN, DATE_IS_VALID, NUMBER_BETWEEN, NUMBER_NOT_BETWEEN, TEXT_IS_EMAIL
# and TEXT_IS_URL. Mirrors the Sheets v4 ConditionType enum.
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


async def validation_clear(
    service,
    spreadsheet_id: str,
    range_name: Optional[str] = None,
) -> str:
    """Remove data validation from range_name. Values are unaffected."""
    _require(range_name, "range_name", "validation_clear")
    sheets = await _get_sheet_properties(service, spreadsheet_id)
    grid_range = _parse_a1_range(range_name, sheets)
    await _batch_update(
        service,
        spreadsheet_id,
        [
            {
                "repeatCell": {
                    "range": grid_range,
                    "cell": {},
                    "fields": "dataValidation",
                }
            }
        ],
    )
    return (
        f"Cleared data validation from '{range_name}' in spreadsheet "
        f"{spreadsheet_id}. Cell values were unaffected."
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


# ---------------------------------------------------------------------------
# Notes, filters, links
# ---------------------------------------------------------------------------


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


async def links_set(
    service,
    spreadsheet_id: str,
    range_name: Optional[str] = None,
    url: Optional[str] = None,
    label: Optional[str] = None,
) -> str:
    """Write a hyperlink into each cell of range_name as a HYPERLINK formula."""
    _require(range_name, "range_name", "links_set")
    _require(url, "url", "links_set")
    if not (
        url.startswith("http://")
        or url.startswith("https://")
        or url.startswith("mailto:")
    ):
        raise UserInputError("url must start with http://, https:// or mailto:.")
    text = label or url
    escaped_url = url.replace('"', '""')
    escaped_text = text.replace('"', '""')
    formula = f'=HYPERLINK("{escaped_url}","{escaped_text}")'
    result = await asyncio.to_thread(
        service.spreadsheets()
        .values()
        .update(
            spreadsheetId=spreadsheet_id,
            range=range_name,
            valueInputOption="USER_ENTERED",
            body={"values": [[formula]]},
        )
        .execute
    )
    updated = result.get("updatedCells", "?")
    return (
        f"Wrote hyperlink to {url} into '{range_name}' "
        f"({updated} cell(s)) in spreadsheet {spreadsheet_id}."
    )


async def links_get(
    service,
    spreadsheet_id: str,
    range_name: Optional[str] = None,
) -> str:
    """List hyperlinks in range_name."""
    _require(range_name, "range_name", "links_get")
    response = await asyncio.to_thread(
        service.spreadsheets()
        .get(
            spreadsheetId=spreadsheet_id,
            ranges=[range_name],
            includeGridData=True,
            fields="sheets(data(startRow,startColumn,rowData(values(hyperlink,formattedValue))))",
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
                    link = cell.get("hyperlink")
                    if link:
                        found.append(
                            f"- R{start_row + r + 1}C{start_col + c + 1}: "
                            f"'{cell.get('formattedValue', '')}' -> {link}"
                        )
    if not found:
        return f"No hyperlinks in '{range_name}' of spreadsheet {spreadsheet_id}."
    return "\n".join(
        [f"Hyperlinks in '{range_name}' of spreadsheet {spreadsheet_id}:"] + found
    )


# ---------------------------------------------------------------------------
# Read family: metadata, format reads, get, export, datasources
# ---------------------------------------------------------------------------


async def sheets_get_metadata(service, spreadsheet_id: str) -> str:
    """Spreadsheet summary: title, locale, timezone, tabs with grid sizes."""
    spreadsheet = await asyncio.to_thread(
        service.spreadsheets()
        .get(
            spreadsheetId=spreadsheet_id,
            fields="properties(title,locale,timeZone),sheets(properties(sheetId,title,index,gridProperties))",
        )
        .execute
    )
    props = spreadsheet.get("properties", {})
    lines = [
        f"Spreadsheet '{props.get('title', '')}' ({spreadsheet_id})",
        f"  Locale: {props.get('locale', '?')}  Timezone: {props.get('timeZone', '?')}",
        "  Tabs:",
    ]
    for sheet in spreadsheet.get("sheets", []):
        sp = sheet.get("properties", {})
        grid = sp.get("gridProperties", {})
        lines.append(
            f"    [{sp.get('index', '?')}] '{sp.get('title', '')}' "
            f"(sheetId {sp.get('sheetId', '?')}, "
            f"{grid.get('rowCount', '?')} rows x {grid.get('columnCount', '?')} cols)"
        )
    return "\n".join(lines)


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
        target = sheets[0].get("properties", {}).get("title", "Sheet1")
    result = await asyncio.to_thread(
        service.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=target)
        .execute
    )
    values = result.get("values", [])
    if not values:
        return f"Range '{target}' in spreadsheet {spreadsheet_id} is empty."

    import csv
    import io

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerows(values)
    csv_text = buffer.getvalue()

    from gsheets import disclosure

    if not (map_flag or navigate is not None or head is not None or skip_tokens):
        return csv_text
    return disclosure.export_response(
        csv_text, map_flag=map_flag, navigate=navigate, head=head, skip_tokens=skip_tokens
    )


# Field masks for spreadsheet metadata reads. Keep the tokens aligned with the
# Sheets v4 discovery schema (see tests/gsheets/test_sheets_1_29_fixes.py):
# DataSource has dataSourceId/spec/sheetId (no `type`); DataSourceTable has
# dataSourceId/columnSelectionType/columns/dataExecutionStatus (no `syncState`).
DATASOURCE_DESCRIBE_FIELDS = "dataSources(dataSourceId,spec,sheetId)"
DATASOURCE_TABLE_DESCRIBE_FIELDS = (
    "sheets(properties(title),"
    "dataSourceTables(dataSourceId,columnSelectionType,columns,dataExecutionStatus),"
    "tables)"
)


def _datasource_table_sync_state(dst: dict) -> str:
    """Sync state of a DataSourceTable, from dataExecutionStatus.state."""
    status = dst.get("dataExecutionStatus") or {}
    return status.get("state", "?")


# Fields mask for the data source tables call. We only request the fields
# Google is guaranteed to accept on every sheet — the conditional
# dataSourceTables collection is intentionally omitted because it only
# exists on sheets with Connected Sheets data sources; an explicit mask
# for its leaf fields triggers a 400 "invalid field" on sheets without
# one. Google returns dataSourceTables in the Sheet field on the response
# regardless of whether it appears in the fields mask, when present.
DATASOURCE_TABLE_DESCRIBE_FIELDS = "sheets(properties(title),tables)"


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
        lines.append(f"- {ds.get('dataSourceId', '?')} [{ds.get('type', '?')}]{ref}")
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
        target = sheets[0].get("properties", {}).get("title", "Sheet1")
    response = await asyncio.to_thread(
        service.spreadsheets()
        .get(
            spreadsheetId=spreadsheet_id,
            ranges=[target],
            includeGridData=False,
            fields=DATASOURCE_TABLE_DESCRIBE_FIELDS,
        )
        .execute
    )
    lines = [f"Data source tables in '{target}' of spreadsheet {spreadsheet_id}:"]
    found = False
    for sheet in response.get("sheets", []):
        for dst in sheet.get("dataSourceTables", []) or []:
            found = True
            lines.append(
                f"- dataSourceId {dst.get('dataSourceId', '?')}, "
                f"sync state: {_datasource_table_sync_state(dst)}, "
                f"columns selected: {len(dst.get('columns', []) or [])}"
            )
    if not found:
        lines.append("  (none — this range is not backed by a connected data source)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Destructive: range_clear
# ---------------------------------------------------------------------------


async def range_clear(
    service,
    spreadsheet_id: str,
    range_name: Optional[str] = None,
) -> str:
    """Clear all values in range_name. Data-destructive. Formatting is kept."""
    _require(range_name, "range_name", "range_clear")
    result = await asyncio.to_thread(
        service.spreadsheets()
        .values()
        .clear(spreadsheetId=spreadsheet_id, range=range_name, body={})
        .execute
    )
    cleared = result.get("clearedRange", range_name)
    return (
        f"Cleared values in '{cleared}' of spreadsheet {spreadsheet_id}. "
        "Formatting was kept."
    )


# ---------------------------------------------------------------------------
# Escape hatch: batch_update
# ---------------------------------------------------------------------------


async def batch_update(
    service,
    spreadsheet_id: str,
    requests: Optional[Union[str, List[dict]]] = None,
) -> str:
    """Raw spreadsheets.batchUpdate passthrough. Escape hatch for operations
    without a dedicated action; requests is a list (or JSON list) of Sheets
    API request objects."""
    if requests is None:
        raise UserInputError("'requests' is required for the 'batch_update' action.")
    if isinstance(requests, str):
        try:
            requests = json.loads(requests)
        except json.JSONDecodeError as exc:
            raise UserInputError(
                "requests must be a list or a JSON-encoded list of request objects."
            ) from exc
    if not isinstance(requests, list) or not requests:
        raise UserInputError("requests must be a non-empty list of request objects.")
    for idx, req in enumerate(requests):
        if not isinstance(req, dict) or len(req) != 1:
            raise UserInputError(
                f"requests[{idx}] must be an object with exactly one request type key."
            )
    response = await _batch_update(service, spreadsheet_id, requests)
    replies = response.get("replies", []) or []
    return (
        f"Applied {len(requests)} request(s) to spreadsheet {spreadsheet_id} "
        f"({len(replies)} replie(s) returned)."
    )


# ---------------------------------------------------------------------------
# Absorbed existing tools family: format_range / conditional_format /
# resize_dimensions / move_rows
#
# These mirror the standalone tools format_sheet_range,
# manage_conditional_formatting (add/delete only), resize_sheet_dimensions,
# and move_sheet_rows in gsheets/sheets_tools.py. For equivalent inputs they
# produce the same Sheets API requests; the standalone tools remain live.
# ---------------------------------------------------------------------------


async def format_range(
    service,
    spreadsheet_id: str,
    range_name: Optional[str] = None,
    background_color: Optional[str] = None,
    text_color: Optional[str] = None,
    number_format_type: Optional[str] = None,
    number_format_pattern: Optional[str] = None,
    wrap_strategy: Optional[str] = None,
    horizontal_alignment: Optional[str] = None,
    vertical_alignment: Optional[str] = None,
    bold: Optional[bool] = None,
    italic: Optional[bool] = None,
    font_size: Optional[int] = None,
) -> str:
    """Apply formatting to range_name: colors, number formats, wrapping,
    alignment, and text styling. Mirrors format_sheet_range."""
    _require(range_name, "range_name", "format_range")
    assert range_name is not None  # narrowed by _require

    has_any_format = any(
        [
            background_color,
            text_color,
            number_format_type,
            wrap_strategy,
            horizontal_alignment,
            vertical_alignment,
            bold is not None,
            italic is not None,
            font_size is not None,
        ]
    )
    if not has_any_format:
        raise UserInputError(
            "Provide at least one formatting option (background_color, text_color, "
            "number_format_type, wrap_strategy, horizontal_alignment, vertical_alignment, "
            "bold, italic, or font_size)."
        )

    bg_color_parsed = _parse_hex_color(background_color)
    text_color_parsed = _parse_hex_color(text_color)

    number_format = None
    if number_format_type:
        allowed_number_formats = {
            "NUMBER",
            "NUMBER_WITH_GROUPING",
            "CURRENCY",
            "PERCENT",
            "SCIENTIFIC",
            "DATE",
            "TIME",
            "DATE_TIME",
            "TEXT",
        }
        normalized_type = number_format_type.upper()
        if normalized_type not in allowed_number_formats:
            raise UserInputError(
                f"number_format_type must be one of {sorted(allowed_number_formats)}."
            )
        number_format = {"type": normalized_type}
        if number_format_pattern:
            number_format["pattern"] = number_format_pattern

    wrap_strategy_normalized = None
    if wrap_strategy:
        allowed_wrap_strategies = {"WRAP", "CLIP", "OVERFLOW_CELL"}
        wrap_strategy_normalized = wrap_strategy.upper()
        if wrap_strategy_normalized not in allowed_wrap_strategies:
            raise UserInputError(
                f"wrap_strategy must be one of {sorted(allowed_wrap_strategies)}."
            )

    h_align_normalized = None
    if horizontal_alignment:
        allowed_h_alignments = {"LEFT", "CENTER", "RIGHT"}
        h_align_normalized = horizontal_alignment.upper()
        if h_align_normalized not in allowed_h_alignments:
            raise UserInputError(
                f"horizontal_alignment must be one of {sorted(allowed_h_alignments)}."
            )

    v_align_normalized = None
    if vertical_alignment:
        allowed_v_alignments = {"TOP", "MIDDLE", "BOTTOM"}
        v_align_normalized = vertical_alignment.upper()
        if v_align_normalized not in allowed_v_alignments:
            raise UserInputError(
                f"vertical_alignment must be one of {sorted(allowed_v_alignments)}."
            )

    sheets = await _get_sheet_properties(service, spreadsheet_id)
    grid_range = _parse_a1_range(range_name, sheets)

    user_entered_format = {}
    fields = []

    if bg_color_parsed:
        user_entered_format["backgroundColor"] = bg_color_parsed
        fields.append("userEnteredFormat.backgroundColor")

    text_format = {}
    text_format_fields = []
    if text_color_parsed:
        text_format["foregroundColor"] = text_color_parsed
        text_format_fields.append("userEnteredFormat.textFormat.foregroundColor")
    if bold is not None:
        text_format["bold"] = bold
        text_format_fields.append("userEnteredFormat.textFormat.bold")
    if italic is not None:
        text_format["italic"] = italic
        text_format_fields.append("userEnteredFormat.textFormat.italic")
    if font_size is not None:
        text_format["fontSize"] = font_size
        text_format_fields.append("userEnteredFormat.textFormat.fontSize")
    if text_format:
        user_entered_format["textFormat"] = text_format
        fields.extend(text_format_fields)

    if number_format:
        user_entered_format["numberFormat"] = number_format
        fields.append("userEnteredFormat.numberFormat")
    if wrap_strategy_normalized:
        user_entered_format["wrapStrategy"] = wrap_strategy_normalized
        fields.append("userEnteredFormat.wrapStrategy")
    if h_align_normalized:
        user_entered_format["horizontalAlignment"] = h_align_normalized
        fields.append("userEnteredFormat.horizontalAlignment")
    if v_align_normalized:
        user_entered_format["verticalAlignment"] = v_align_normalized
        fields.append("userEnteredFormat.verticalAlignment")

    if not user_entered_format:
        raise UserInputError(
            "No formatting applied. Verify provided formatting options."
        )

    await _batch_update(
        service,
        spreadsheet_id,
        [
            {
                "repeatCell": {
                    "range": grid_range,
                    "cell": {"userEnteredFormat": user_entered_format},
                    "fields": ",".join(fields),
                }
            }
        ],
    )

    applied_parts = []
    if bg_color_parsed:
        applied_parts.append(f"background {background_color}")
    if text_color_parsed:
        applied_parts.append(f"text color {text_color}")
    if number_format:
        nf_desc = number_format["type"]
        if number_format_pattern:
            nf_desc += f" (pattern: {number_format_pattern})"
        applied_parts.append(f"number format {nf_desc}")
    if wrap_strategy_normalized:
        applied_parts.append(f"wrap {wrap_strategy_normalized}")
    if h_align_normalized:
        applied_parts.append(f"horizontal align {h_align_normalized}")
    if v_align_normalized:
        applied_parts.append(f"vertical align {v_align_normalized}")
    if bold is not None:
        applied_parts.append("bold" if bold else "not bold")
    if italic is not None:
        applied_parts.append("italic" if italic else "not italic")
    if font_size is not None:
        applied_parts.append(f"font size {font_size}")

    return (
        f"Applied formatting to range '{range_name}' in spreadsheet "
        f"{spreadsheet_id}: {', '.join(applied_parts)}."
    )


async def conditional_format(
    service,
    spreadsheet_id: str,
    operation: Optional[str] = None,
    range_name: Optional[str] = None,
    condition_type: Optional[str] = None,
    condition_values: Optional[Union[str, List[Union[str, int, float]]]] = None,
    background_color: Optional[str] = None,
    text_color: Optional[str] = None,
    rule_index: Optional[int] = None,
    gradient_points: Optional[Union[str, List[dict]]] = None,
    sheet_name: Optional[str] = None,
) -> str:
    """Add or delete a conditional formatting rule. Mirrors the 'add' and
    'delete' operations of manage_conditional_formatting (rule listing stays
    on the standalone tool)."""
    _require(
        operation,
        "operation",
        "conditional_format",
        "Use 'add_rule' or 'delete_rule'.",
    )
    assert operation is not None  # narrowed by _require
    operation_normalized = operation.strip().lower()
    allowed_operations = {"add_rule", "delete_rule"}
    if operation_normalized not in allowed_operations:
        raise UserInputError(
            f"operation must be one of {sorted(allowed_operations)}, got '{operation}'."
        )

    if operation_normalized == "add_rule":
        if not range_name:
            raise UserInputError("range_name is required for operation 'add_rule'.")
        if not condition_type and not gradient_points:
            raise UserInputError(
                "condition_type (or gradient_points) is required for operation 'add_rule'."
            )
        if rule_index is not None and (
            not isinstance(rule_index, int) or rule_index < 0
        ):
            raise UserInputError(
                "rule_index must be a non-negative integer when provided."
            )

        gradient_points_list = _parse_gradient_points(gradient_points)
        condition_values_list = (
            None if gradient_points_list else _parse_condition_values(condition_values)
        )

        sheets, sheet_titles = await _fetch_sheets_with_rules(service, spreadsheet_id)
        grid_range = _parse_a1_range(range_name, sheets)

        target_sheet = None
        for sheet in sheets:
            if sheet.get("properties", {}).get("sheetId") == grid_range.get("sheetId"):
                target_sheet = sheet
                break
        if target_sheet is None:
            raise UserInputError(
                "Target sheet not found while adding conditional formatting."
            )

        current_rules = target_sheet.get("conditionalFormats", []) or []

        insert_at = rule_index if rule_index is not None else len(current_rules)
        if insert_at > len(current_rules):
            raise UserInputError(
                f"rule_index {insert_at} is out of range for sheet "
                f"'{target_sheet.get('properties', {}).get('title', 'Unknown')}' "
                f"(current count: {len(current_rules)})."
            )

        if gradient_points_list:
            new_rule = _build_gradient_rule([grid_range], gradient_points_list)
            rule_desc = "gradient"
            values_desc = ""
            applied_parts = [f"gradient points {len(gradient_points_list)}"]
        else:
            assert condition_type is not None  # guaranteed by the check above
            rule, cond_type_normalized = _build_boolean_rule(
                [grid_range],
                condition_type,
                condition_values_list,
                background_color,
                text_color,
            )
            new_rule = rule
            rule_desc = cond_type_normalized
            values_desc = ""
            if condition_values_list:
                values_desc = f" with values {condition_values_list}"
            applied_parts = []
            if background_color:
                applied_parts.append(f"background {background_color}")
            if text_color:
                applied_parts.append(f"text {text_color}")

        new_rules_state = copy.deepcopy(current_rules)
        new_rules_state.insert(insert_at, new_rule)

        add_rule_request: dict = {"rule": new_rule}
        if rule_index is not None:
            add_rule_request["index"] = rule_index

        await _batch_update(
            service,
            spreadsheet_id,
            [{"addConditionalFormatRule": add_rule_request}],
        )

        format_desc = ", ".join(applied_parts) if applied_parts else "format applied"
        sheet_title = target_sheet.get("properties", {}).get("title", "Unknown")
        state_text = _format_conditional_rules_section(
            sheet_title, new_rules_state, sheet_titles, indent=""
        )

        return "\n".join(
            [
                f"Added conditional format on '{range_name}' in spreadsheet "
                f"{spreadsheet_id}: "
                f"{rule_desc}{values_desc}; format: {format_desc}.",
                state_text,
            ]
        )

    # operation_normalized == "delete_rule"
    if rule_index is None:
        raise UserInputError("rule_index is required for operation 'delete_rule'.")
    if not isinstance(rule_index, int) or rule_index < 0:
        raise UserInputError("rule_index must be a non-negative integer.")

    sheets, sheet_titles = await _fetch_sheets_with_rules(service, spreadsheet_id)
    target_sheet = _select_sheet(sheets, sheet_name)

    sheet_props = target_sheet.get("properties", {})
    sheet_id = sheet_props.get("sheetId")
    target_sheet_name = sheet_props.get("title", f"Sheet {sheet_id}")
    rules = target_sheet.get("conditionalFormats", []) or []
    if rule_index >= len(rules):
        raise UserInputError(
            f"rule_index {rule_index} is out of range for sheet "
            f"'{target_sheet_name}' (current count: {len(rules)})."
        )

    new_rules_state = copy.deepcopy(rules)
    del new_rules_state[rule_index]

    await _batch_update(
        service,
        spreadsheet_id,
        [
            {
                "deleteConditionalFormatRule": {
                    "index": rule_index,
                    "sheetId": sheet_id,
                }
            }
        ],
    )

    state_text = _format_conditional_rules_section(
        target_sheet_name, new_rules_state, sheet_titles, indent=""
    )

    return "\n".join(
        [
            f"Deleted conditional format at index {rule_index} on sheet "
            f"'{target_sheet_name}' in spreadsheet {spreadsheet_id}.",
            state_text,
        ]
    )


def _build_column_visibility_requests(sheet_id, letters, hidden, label):
    """Build updateDimensionProperties requests to hide/unhide columns."""
    if not isinstance(letters, list):
        raise UserInputError(f"{label} must be a list of column letters.")
    reqs = []
    for col_letter in letters:
        col_idx = _column_to_index(str(col_letter).upper())
        if col_idx is None:
            raise UserInputError(f"Invalid column letter in {label}: '{col_letter}'.")
        reqs.append(
            {
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": sheet_id,
                        "dimension": "COLUMNS",
                        "startIndex": col_idx,
                        "endIndex": col_idx + 1,
                    },
                    "properties": {"hiddenByUser": hidden},
                    "fields": "hiddenByUser",
                }
            }
        )
    return reqs


def _build_row_visibility_requests(sheet_id, row_nums, hidden, label):
    """Build updateDimensionProperties requests to hide/unhide rows."""
    if not isinstance(row_nums, list):
        raise UserInputError(f"{label} must be a list of row numbers.")
    reqs = []
    for row_num in row_nums:
        try:
            row_num = int(row_num)
        except ValueError as exc:
            raise UserInputError(
                f"Row number must be an integer in {label}, got {row_num}."
            ) from exc
        if row_num < 1:
            raise UserInputError(f"Row number must be >= 1 in {label}, got {row_num}.")
        reqs.append(
            {
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": sheet_id,
                        "dimension": "ROWS",
                        "startIndex": row_num - 1,
                        "endIndex": row_num,
                    },
                    "properties": {"hiddenByUser": hidden},
                    "fields": "hiddenByUser",
                }
            }
        )
    return reqs


async def resize_dimensions(
    service,
    spreadsheet_id: str,
    sheet_name: Optional[str] = None,
    column_sizes: Optional[Union[str, dict]] = None,
    row_sizes: Optional[Union[str, dict]] = None,
    auto_resize_columns: Optional[Union[str, List[str]]] = None,
    auto_resize_rows: Optional[Union[str, List[int]]] = None,
    frozen_row_count: Optional[int] = None,
    frozen_column_count: Optional[int] = None,
    hide_columns: Optional[Union[str, List[str]]] = None,
    unhide_columns: Optional[Union[str, List[str]]] = None,
    hide_rows: Optional[Union[str, List[int]]] = None,
    unhide_rows: Optional[Union[str, List[int]]] = None,
    insert_rows: Optional[int] = None,
    insert_rows_at: Optional[int] = None,
    insert_columns: Optional[int] = None,
    insert_columns_at: Optional[str] = None,
) -> str:
    """Manage sheet-level dimension properties: resize/auto-resize columns
    and rows, freeze, hide/unhide, and insert rows/columns. Mirrors
    resize_sheet_dimensions, minus the delete operations — those are
    data-destructive and live in the delete_dimension action."""
    has_any = any(
        [
            column_sizes,
            row_sizes,
            auto_resize_columns,
            auto_resize_rows,
            frozen_row_count is not None,
            frozen_column_count is not None,
            hide_columns,
            unhide_columns,
            hide_rows,
            unhide_rows,
            insert_rows is not None,
            insert_columns is not None,
        ]
    )
    if not has_any:
        raise UserInputError(
            "Provide at least one of: column_sizes, row_sizes, "
            "auto_resize_columns, auto_resize_rows, frozen_row_count, "
            "frozen_column_count, hide_columns, unhide_columns, "
            "hide_rows, unhide_rows, insert_rows, insert_columns."
        )

    def _parse_json(value, name):
        if not isinstance(value, str):
            return value
        try:
            return json.loads(value)
        except json.JSONDecodeError as e:
            raise UserInputError(f"Invalid JSON for {name}: {e}")

    column_sizes = _parse_json(column_sizes, "column_sizes")
    row_sizes = _parse_json(row_sizes, "row_sizes")
    auto_resize_columns = _parse_json(auto_resize_columns, "auto_resize_columns")
    auto_resize_rows = _parse_json(auto_resize_rows, "auto_resize_rows")
    hide_columns = _parse_json(hide_columns, "hide_columns")
    unhide_columns = _parse_json(unhide_columns, "unhide_columns")
    hide_rows = _parse_json(hide_rows, "hide_rows")
    unhide_rows = _parse_json(unhide_rows, "unhide_rows")

    sheets = await _get_sheet_properties(service, spreadsheet_id)
    if not sheets:
        raise UserInputError("No sheets found in spreadsheet.")

    target_sheet = None
    if sheet_name:
        for sheet in sheets:
            if sheet.get("properties", {}).get("title") == sheet_name:
                target_sheet = sheet
                break
        if not target_sheet:
            raise UserInputError(f"Sheet '{sheet_name}' not found.")
    else:
        target_sheet = sheets[0]

    sheet_id = target_sheet["properties"]["sheetId"]

    requests = []
    applied_parts = []

    if column_sizes:
        if not isinstance(column_sizes, dict):
            raise UserInputError(
                "column_sizes must be a dict mapping column letters to pixel widths."
            )
        for col_letter, pixel_size in column_sizes.items():
            col_idx = _column_to_index(col_letter.upper())
            if col_idx is None:
                raise UserInputError(f"Invalid column letter: '{col_letter}'.")
            if not isinstance(pixel_size, (int, float)) or pixel_size <= 0:
                raise UserInputError(
                    f"Pixel size for column '{col_letter}' must be a positive number."
                )
            requests.append(
                {
                    "updateDimensionProperties": {
                        "range": {
                            "sheetId": sheet_id,
                            "dimension": "COLUMNS",
                            "startIndex": col_idx,
                            "endIndex": col_idx + 1,
                        },
                        "properties": {"pixelSize": int(pixel_size)},
                        "fields": "pixelSize",
                    }
                }
            )
        applied_parts.append(
            f"resized columns: {', '.join(f'{k}={v}px' for k, v in column_sizes.items())}"
        )

    if row_sizes:
        if not isinstance(row_sizes, dict):
            raise UserInputError(
                "row_sizes must be a dict mapping row numbers to pixel heights."
            )
        for row_num_str, pixel_size in row_sizes.items():
            try:
                row_num = int(row_num_str)
            except ValueError as exc:
                raise UserInputError(
                    f"Row number must be an integer >= 1, got {row_num_str}."
                ) from exc
            if row_num < 1:
                raise UserInputError(f"Row number must be >= 1, got {row_num}.")
            if not isinstance(pixel_size, (int, float)) or pixel_size <= 0:
                raise UserInputError(
                    f"Pixel size for row {row_num} must be a positive number."
                )
            requests.append(
                {
                    "updateDimensionProperties": {
                        "range": {
                            "sheetId": sheet_id,
                            "dimension": "ROWS",
                            "startIndex": row_num - 1,
                            "endIndex": row_num,
                        },
                        "properties": {"pixelSize": int(pixel_size)},
                        "fields": "pixelSize",
                    }
                }
            )
        applied_parts.append(
            f"resized rows: {', '.join(f'{k}={v}px' for k, v in row_sizes.items())}"
        )

    if auto_resize_columns:
        if not isinstance(auto_resize_columns, list):
            raise UserInputError(
                "auto_resize_columns must be a list of column letters."
            )
        for col_letter in auto_resize_columns:
            col_idx = _column_to_index(str(col_letter).upper())
            if col_idx is None:
                raise UserInputError(f"Invalid column letter: '{col_letter}'.")
            requests.append(
                {
                    "autoResizeDimensions": {
                        "dimensions": {
                            "sheetId": sheet_id,
                            "dimension": "COLUMNS",
                            "startIndex": col_idx,
                            "endIndex": col_idx + 1,
                        }
                    }
                }
            )
        applied_parts.append(
            f"auto-resized columns: {', '.join(str(c) for c in auto_resize_columns)}"
        )

    if auto_resize_rows:
        if not isinstance(auto_resize_rows, list):
            raise UserInputError("auto_resize_rows must be a list of row numbers.")
        for row_num in auto_resize_rows:
            try:
                parsed_row_num = int(row_num)
            except ValueError as exc:
                raise UserInputError(
                    f"Row number must be an integer >= 1, got {row_num}."
                ) from exc
            if parsed_row_num < 1:
                raise UserInputError(f"Row number must be >= 1, got {parsed_row_num}.")
            requests.append(
                {
                    "autoResizeDimensions": {
                        "dimensions": {
                            "sheetId": sheet_id,
                            "dimension": "ROWS",
                            "startIndex": parsed_row_num - 1,
                            "endIndex": parsed_row_num,
                        }
                    }
                }
            )
        applied_parts.append(
            f"auto-resized rows: {', '.join(str(r) for r in auto_resize_rows)}"
        )

    grid_properties = {}
    grid_fields = []
    if frozen_row_count is not None:
        if not isinstance(frozen_row_count, int) or frozen_row_count < 0:
            raise UserInputError("frozen_row_count must be a non-negative integer.")
        grid_properties["frozenRowCount"] = frozen_row_count
        grid_fields.append("gridProperties.frozenRowCount")
        applied_parts.append(
            f"froze {frozen_row_count} row(s)"
            if frozen_row_count > 0
            else "unfroze rows"
        )

    if frozen_column_count is not None:
        if not isinstance(frozen_column_count, int) or frozen_column_count < 0:
            raise UserInputError("frozen_column_count must be a non-negative integer.")
        grid_properties["frozenColumnCount"] = frozen_column_count
        grid_fields.append("gridProperties.frozenColumnCount")
        applied_parts.append(
            f"froze {frozen_column_count} column(s)"
            if frozen_column_count > 0
            else "unfroze columns"
        )

    if grid_properties:
        requests.append(
            {
                "updateSheetProperties": {
                    "properties": {
                        "sheetId": sheet_id,
                        "gridProperties": grid_properties,
                    },
                    "fields": ",".join(grid_fields),
                }
            }
        )

    if hide_columns:
        requests.extend(
            _build_column_visibility_requests(
                sheet_id, hide_columns, True, "hide_columns"
            )
        )
        applied_parts.append(f"hid columns: {', '.join(str(c) for c in hide_columns)}")

    if unhide_columns:
        requests.extend(
            _build_column_visibility_requests(
                sheet_id, unhide_columns, False, "unhide_columns"
            )
        )
        applied_parts.append(
            f"unhid columns: {', '.join(str(c) for c in unhide_columns)}"
        )

    if hide_rows:
        requests.extend(
            _build_row_visibility_requests(sheet_id, hide_rows, True, "hide_rows")
        )
        applied_parts.append(f"hid rows: {', '.join(str(r) for r in hide_rows)}")

    if unhide_rows:
        requests.extend(
            _build_row_visibility_requests(sheet_id, unhide_rows, False, "unhide_rows")
        )
        applied_parts.append(f"unhid rows: {', '.join(str(r) for r in unhide_rows)}")

    if insert_rows is not None:
        if not isinstance(insert_rows, int) or insert_rows < 1:
            raise UserInputError("insert_rows must be a positive integer.")
        if insert_rows_at is not None:
            if not isinstance(insert_rows_at, int) or insert_rows_at < 1:
                raise UserInputError(
                    "insert_rows_at must be a positive integer (1-based)."
                )
            start_idx = insert_rows_at - 1
        else:
            start_idx = None

        if start_idx is not None:
            requests.append(
                {
                    "insertDimension": {
                        "range": {
                            "sheetId": sheet_id,
                            "dimension": "ROWS",
                            "startIndex": start_idx,
                            "endIndex": start_idx + insert_rows,
                        },
                        "inheritFromBefore": start_idx > 0,
                    }
                }
            )
            applied_parts.append(
                f"inserted {insert_rows} row(s) at row {insert_rows_at}"
            )
        else:
            requests.append(
                {
                    "appendDimension": {
                        "sheetId": sheet_id,
                        "dimension": "ROWS",
                        "length": insert_rows,
                    }
                }
            )
            applied_parts.append(f"appended {insert_rows} row(s)")

    if insert_columns is not None:
        if not isinstance(insert_columns, int) or insert_columns < 1:
            raise UserInputError("insert_columns must be a positive integer.")
        if insert_columns_at is not None:
            col_idx = _column_to_index(str(insert_columns_at).upper())
            if col_idx is None:
                raise UserInputError(
                    f"Invalid column letter for insert_columns_at: '{insert_columns_at}'."
                )
            requests.append(
                {
                    "insertDimension": {
                        "range": {
                            "sheetId": sheet_id,
                            "dimension": "COLUMNS",
                            "startIndex": col_idx,
                            "endIndex": col_idx + insert_columns,
                        },
                        "inheritFromBefore": col_idx > 0,
                    }
                }
            )
            applied_parts.append(
                f"inserted {insert_columns} column(s) at column {insert_columns_at}"
            )
        else:
            requests.append(
                {
                    "appendDimension": {
                        "sheetId": sheet_id,
                        "dimension": "COLUMNS",
                        "length": insert_columns,
                    }
                }
            )
            applied_parts.append(f"appended {insert_columns} column(s)")

    await _batch_update(service, spreadsheet_id, requests)

    return (
        f"Applied dimension changes in spreadsheet {spreadsheet_id}: "
        f"{'; '.join(applied_parts)}."
    )


async def delete_dimension(
    service,
    spreadsheet_id: str,
    sheet_name: Optional[str] = None,
    delete_rows: Optional[Union[str, List[int]]] = None,
    delete_row_range: Optional[str] = None,
    delete_columns: Optional[Union[str, List[str]]] = None,
) -> str:
    """Delete rows or columns from a sheet. Data-destructive.

    Exactly one of delete_rows (list of 1-based row numbers), delete_row_range
    ("5:10") or delete_columns (list of column letters) is required.
    """
    provided = [bool(delete_rows), bool(delete_row_range), bool(delete_columns)]
    if sum(provided) != 1:
        raise UserInputError(
            "Provide exactly one of: delete_rows, delete_row_range, delete_columns."
        )

    def _parse_json(value, name):
        if not isinstance(value, str):
            return value
        try:
            return json.loads(value)
        except json.JSONDecodeError as e:
            raise UserInputError(f"Invalid JSON for {name}: {e}")

    delete_rows = _parse_json(delete_rows, "delete_rows")
    delete_columns = _parse_json(delete_columns, "delete_columns")

    sheets = await _get_sheet_properties(service, spreadsheet_id)
    if not sheets:
        raise UserInputError("No sheets found in spreadsheet.")

    target_sheet = None
    if sheet_name:
        for sheet in sheets:
            if sheet.get("properties", {}).get("title") == sheet_name:
                target_sheet = sheet
                break
        if not target_sheet:
            raise UserInputError(f"Sheet '{sheet_name}' not found.")
    else:
        target_sheet = sheets[0]

    sheet_id = target_sheet["properties"]["sheetId"]
    requests = []
    applied_parts = []

    if delete_rows:
        if not isinstance(delete_rows, list):
            raise UserInputError("delete_rows must be a list of row numbers.")
        parsed_delete_rows = []
        for row_num in delete_rows:
            try:
                parsed_delete_rows.append(int(row_num))
            except ValueError as exc:
                raise UserInputError(
                    f"Row number must be an integer >= 1 in delete_rows, got {row_num}."
                ) from exc
        sorted_rows = sorted(parsed_delete_rows, reverse=True)
        for row_num in sorted_rows:
            if row_num < 1:
                raise UserInputError(
                    f"Row number must be >= 1 in delete_rows, got {row_num}."
                )
            requests.append(
                {
                    "deleteDimension": {
                        "range": {
                            "sheetId": sheet_id,
                            "dimension": "ROWS",
                            "startIndex": row_num - 1,
                            "endIndex": row_num,
                        }
                    }
                }
            )
        applied_parts.append(f"deleted rows: {', '.join(str(r) for r in delete_rows)}")

    if delete_row_range:
        if isinstance(delete_row_range, str) and ":" in delete_row_range:
            parts = delete_row_range.split(":", 1)
            try:
                range_start = int(parts[0])
                range_end = int(parts[1])
            except ValueError as exc:
                raise UserInputError(
                    f"Invalid delete_row_range format: '{delete_row_range}'. "
                    f"Expected 'start:end' with integer row numbers."
                ) from exc
        else:
            raise UserInputError(
                f"delete_row_range must be a 'start:end' string (e.g. '5:10'), "
                f"got: '{delete_row_range}'."
            )
        if range_start < 1 or range_end < range_start:
            raise UserInputError(
                f"Invalid row range: start={range_start}, end={range_end}. "
                f"Rows are 1-based and end must be >= start."
            )
        requests.append(
            {
                "deleteDimension": {
                    "range": {
                        "sheetId": sheet_id,
                        "dimension": "ROWS",
                        "startIndex": range_start - 1,
                        "endIndex": range_end,
                    }
                }
            }
        )
        num_range_deleted = range_end - range_start + 1
        applied_parts.append(
            f"deleted row range {range_start}-{range_end} ({num_range_deleted} row(s))"
        )

    if delete_columns:
        if not isinstance(delete_columns, list):
            raise UserInputError("delete_columns must be a list of column letters.")
        col_indices = []
        for col_letter in delete_columns:
            col_idx = _column_to_index(str(col_letter).upper())
            if col_idx is None:
                raise UserInputError(
                    f"Invalid column letter in delete_columns: '{col_letter}'."
                )
            col_indices.append((col_letter, col_idx))
        # Sort by index descending to keep indices stable during deletion
        col_indices.sort(key=lambda x: x[1], reverse=True)
        for _, col_idx in col_indices:
            requests.append(
                {
                    "deleteDimension": {
                        "range": {
                            "sheetId": sheet_id,
                            "dimension": "COLUMNS",
                            "startIndex": col_idx,
                            "endIndex": col_idx + 1,
                        }
                    }
                }
            )
        applied_parts.append(
            f"deleted columns: {', '.join(str(c) for c in delete_columns)}"
        )

    await _batch_update(service, spreadsheet_id, requests)

    return (
        f"Applied deletions in spreadsheet {spreadsheet_id}: "
        f"{'; '.join(applied_parts)}."
    )


async def move_rows(
    service,
    spreadsheet_id: str,
    source_sheet: Optional[str] = None,
    start_row: Optional[int] = None,
    end_row: Optional[int] = None,
    destination_sheet: Optional[str] = None,
) -> str:
    """Move rows from one sheet to another within the same spreadsheet
    (copyPaste followed by deleteDimension in one batchUpdate). Row numbers
    are 1-based. Mirrors move_sheet_rows."""
    _require(source_sheet, "source_sheet", "move_rows")
    _require(destination_sheet, "destination_sheet", "move_rows")
    _require(start_row, "start_row", "move_rows", "1-based, inclusive.")
    _require(end_row, "end_row", "move_rows", "1-based, inclusive.")
    assert (  # narrowed by _require
        source_sheet is not None
        and destination_sheet is not None
        and start_row is not None
        and end_row is not None
    )

    if start_row < 1 or end_row < start_row:
        raise UserInputError(
            f"Invalid row range: start_row={start_row}, end_row={end_row}. "
            f"Rows are 1-based and end_row must be >= start_row."
        )

    if source_sheet == destination_sheet:
        raise UserInputError("source_sheet and destination_sheet must be different.")

    spreadsheet = await asyncio.to_thread(
        service.spreadsheets()
        .get(
            spreadsheetId=spreadsheet_id,
            fields="sheets(properties(sheetId,title,gridProperties))",
        )
        .execute
    )
    sheets = spreadsheet.get("sheets", [])
    src = _select_sheet(sheets, source_sheet)
    dst = _select_sheet(sheets, destination_sheet)
    src_id = src["properties"]["sheetId"]
    dst_id = dst["properties"]["sheetId"]
    dst_grid_rows = dst["properties"].get("gridProperties", {}).get("rowCount", 0)

    # Validate that the source row block actually contains data.
    safe_source = source_sheet.replace("'", "''")
    src_range = f"'{safe_source}'!{start_row}:{end_row}"
    src_values = await asyncio.to_thread(
        service.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=src_range)
        .execute
    )
    if not src_values.get("values"):
        raise UserInputError(
            f"Source range '{source_sheet}' rows {start_row}-{end_row} "
            f"contains no data. Nothing to move."
        )

    # Find the last row with actual data in the destination sheet.
    # gridProperties.rowCount is the allocated grid size (e.g. 1000 for a new
    # sheet), not the count of rows containing data.  Fetch all columns so the
    # append position reflects any non-empty cell, not just column A.
    safe_destination = destination_sheet.replace("'", "''")
    dst_values = await asyncio.to_thread(
        service.spreadsheets()
        .values()
        .get(
            spreadsheetId=spreadsheet_id,
            range=f"'{safe_destination}'",
            majorDimension="ROWS",
        )
        .execute
    )
    dst_data_rows = len(dst_values.get("values", []))

    num_rows = end_row - start_row + 1
    paste_start = dst_data_rows

    # If pasting beyond the current grid, expand the destination sheet first.
    requests = []
    if paste_start + num_rows > dst_grid_rows:
        requests.append(
            {
                "appendDimension": {
                    "sheetId": dst_id,
                    "dimension": "ROWS",
                    "length": (paste_start + num_rows) - dst_grid_rows,
                }
            }
        )

    requests.extend(
        [
            {
                "copyPaste": {
                    "source": {
                        "sheetId": src_id,
                        "startRowIndex": start_row - 1,
                        "endRowIndex": end_row,
                    },
                    "destination": {
                        "sheetId": dst_id,
                        "startRowIndex": paste_start,
                        "endRowIndex": paste_start + num_rows,
                    },
                    "pasteType": "PASTE_NORMAL",
                }
            },
            {
                "deleteDimension": {
                    "range": {
                        "sheetId": src_id,
                        "dimension": "ROWS",
                        "startIndex": start_row - 1,
                        "endIndex": end_row,
                    }
                }
            },
        ]
    )

    await _batch_update(service, spreadsheet_id, requests)

    return (
        f"Successfully moved {num_rows} row(s) from '{source_sheet}' "
        f"(rows {start_row}-{end_row}) to '{destination_sheet}' "
        f"in spreadsheet {spreadsheet_id}."
    )
