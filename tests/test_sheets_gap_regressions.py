"""Table identity, bounded CSV maps and discovery-aligned charts over native HTTP."""

from copy import deepcopy
import json

from googleapiclient.discovery_cache import get_static_doc
import pytest

from core.utils import UserInputError
from gsheets import disclosure, sheets_dispatch_helpers as dispatch
from tests.test_refresh_mcp_surface import call, native as native, rejected, success


@pytest.mark.parametrize("action", ["table_clear", "table_delete"])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize(
    "selectors,target",
    [
        ({"table_id": "id-b", "table_name": "NameA"}, None),
        ({"table_id": "missing", "table_name": "NameA"}, None),
        ({"table_id": "", "table_name": "NameA"}, None),
        ({"table_id": "id-b", "table_name": "missing"}, None),
        ({"table_id": "id-b", "table_name": "NameB"}, "id-b"),
        ({"table_id": "id-b"}, "id-b"),
        ({"table_name": "NameA"}, "id-a"),
    ],
)
def test_destructive_table_identity_is_order_independent(
    native, action, reverse, selectors, target
):
    create, service, _ = native
    payload = service.spreadsheets().get().execute.return_value
    original = deepcopy(payload["sheets"][0]["tables"][0])
    table_a = {**original, "tableId": "id-a", "name": "NameA"}
    table_b = {**deepcopy(original), "tableId": "id-b", "name": "NameB"}
    table_b["range"].update(startRowIndex=10, endRowIndex=15)
    payload["sheets"][0]["tables"] = (
        [table_b, table_a] if reverse else [table_a, table_b]
    )
    service.reset_mock()
    with create("full") as client:
        result = call(
            client,
            "sheets_delete",
            spreadsheet_id="ss1",
            action=action,
            params=selectors,
        )
    if target is None:
        rejected(result)
        service.spreadsheets().values().clear.assert_not_called()
        service.spreadsheets().batchUpdate.assert_not_called()
    else:
        success(result)
        if action == "table_clear":
            expected = "'Sheet1'!A12:B15" if target == "id-b" else "'Sheet1'!A2:B5"
            assert (
                service.spreadsheets().values().clear.call_args.kwargs["range"]
                == expected
            )
        else:
            assert service.spreadsheets().batchUpdate.call_args.kwargs["body"][
                "requests"
            ] == [{"deleteTable": {"tableId": target}}]


def test_large_registered_csv_map_pages_are_bounded_and_cover_every_ordinal(native):
    create, service, _ = native
    service.spreadsheets().values().get().execute.return_value = {
        "values": [[f"row{i}", '漢字\\quoted"\n' * 10] for i in range(10000)]
    }
    seen, starts = [], []
    params = {"map": True, "range_name": "Sheet1!A1:B10000"}
    with create("readonly") as client:
        while True:
            result = success(
                call(
                    client,
                    "sheets_read",
                    spreadsheet_id="ss1",
                    action="export",
                    params=params,
                )
            )
            assert disclosure.estimate_tokens(result) < disclosure.MAP_TOKEN_BUDGET
            page = json.loads(result)
            assert page["total_blocks"] == 400
            starts.append(page["map_start"])
            ordinals = [
                int(line.split(" ", 1)[0]) for line in page["map_text"].splitlines()
            ]
            assert ordinals and ordinals[0] == page["map_start"]
            seen.extend(ordinals)
            if page["next_call"] is None:
                assert page["next_map_start"] is None
                break
            params = {**params, **json.loads(page["next_call"])}
            assert params["map"] is True
            assert params["map_start"] == ordinals[-1] + 1
            assert params["map_start"] > starts[-1]
        last = json.loads(
            success(
                call(
                    client,
                    "sheets_read",
                    spreadsheet_id="ss1",
                    action="export",
                    params={"navigate": "399.24"},
                )
            )
        )
        assert "row9999" in last["content"]
    assert seen == list(range(400))
    assert len(starts) > 1


@pytest.mark.parametrize("start", [-1, 1, True, "0"])
def test_invalid_map_page_start(start):
    with pytest.raises(UserInputError):
        disclosure.build_map("one,row\n", map_start=start)


def test_map_start_requires_map_mode():
    with pytest.raises(UserInputError, match="requires map=true"):
        disclosure.export_response("one,row\n", map_start=1)


@pytest.mark.parametrize("action", ["chart_create", "chart_update"])
def test_pie_is_rejected_before_google_calls(native, action):
    create, service, _ = native
    params = {"chart_type": "PIE"}
    params.update(
        {"data_range": "Sheet1!A1:B5"} if action == "chart_create" else {"chart_id": 42}
    )
    with create("manage") as client:
        rejected(
            call(
                client,
                "sheets_manage",
                spreadsheet_id="ss1",
                action=action,
                params=params,
            )
        )
    service.spreadsheets().batchUpdate.assert_not_called()
    service.spreadsheets().get.assert_not_called()


@pytest.mark.parametrize("chart_type", sorted(dispatch.CHART_TYPES))
@pytest.mark.parametrize("action", ["chart_create", "chart_update"])
def test_supported_chart_types_match_pinned_google_discovery(
    native, chart_type, action
):
    discovery = json.loads(get_static_doc("sheets", "v4"))["schemas"]
    basic = discovery["BasicChartSpec"]["properties"]
    assert dispatch.CHART_TYPES <= set(basic["chartType"]["enum"])
    assert "PIE" not in dispatch.CHART_TYPES
    create, service, _ = native
    params = {"chart_type": chart_type}
    params.update(
        {"data_range": "Sheet1!A1:B5"} if action == "chart_create" else {"chart_id": 42}
    )
    with create("manage") as client:
        success(
            call(
                client,
                "sheets_manage",
                spreadsheet_id="ss1",
                action=action,
                params=params,
            )
        )
    request = service.spreadsheets().batchUpdate.call_args.kwargs["body"]["requests"][0]
    spec = (
        request["addChart"]["chart"]["spec"]
        if action == "chart_create"
        else request["updateChartSpec"]["spec"]
    )
    assert set(spec) <= discovery["ChartSpec"]["properties"].keys()
    assert set(spec["basicChart"]) <= basic.keys()
    assert spec["basicChart"]["chartType"] in basic["chartType"]["enum"]
    if "legendPosition" in spec["basicChart"]:
        assert spec["basicChart"]["legendPosition"] in basic["legendPosition"]["enum"]
