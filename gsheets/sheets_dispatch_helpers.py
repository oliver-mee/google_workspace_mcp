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
import json
import logging
from typing import List, Optional, Union

from core.utils import UserInputError
from gsheets.sheets_helpers import (
    _build_table_rows_properties,
    _parse_a1_range,
    _parse_table_column_properties,
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
