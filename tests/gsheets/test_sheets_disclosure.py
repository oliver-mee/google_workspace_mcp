"""Unit tests for the progressive-disclosure module (gsheets/disclosure.py).

Pure string-level tests; no Google services involved. The CSV fixtures below
are exactly what export_csv emits from a values matrix (csv.writer output).
"""

import json

import pytest

from gsheets import disclosure
from core.utils import UserInputError

# 60 rows worth of CSV text (definitely > 1 block of BLOCK_SIZE=25)
BIG_CSV = "\n".join(f"row{i},value{i},x" for i in range(60)) + "\n"
SMALL_CSV = "header,count\napple,1\nbanana,2\n"  # 3 lines


def test_estimate_tokens_chars_per_4():
    assert disclosure.estimate_tokens("abcd") == 1  # ceil(4/4)
    assert disclosure.estimate_tokens("abcdefgh") == 2
    assert disclosure.estimate_tokens("") == 1  # never zero


def test_map_is_json_with_bounded_map_text():
    payload = json.loads(disclosure.build_map(BIG_CSV))
    assert payload["mode"] == "map"
    assert payload["total_rows"] == 60
    # 60 rows / 25 per block -> 3 blocks
    assert payload["total_blocks"] == 3
    assert payload["total_est_tokens"] == disclosure.estimate_tokens(BIG_CSV)
    # map_text stays under the 2,000-est-token budget
    assert (
        disclosure.estimate_tokens(payload["map_text"]) <= disclosure.MAP_TOKEN_BUDGET
    )
    assert "0 rows 1-25" in payload["map_text"]
    assert "2 rows 51-60" in payload["map_text"]


def test_map_empty_result():
    payload = json.loads(disclosure.build_map(""))
    assert payload["total_rows"] == 0
    assert "empty" in payload["map_text"]


def test_navigate_block_returns_full_rows():
    payload = json.loads(disclosure.navigate_or_extract(SMALL_CSV, "0"))
    assert payload["mode"] == "navigate"
    assert payload["content"] == SMALL_CSV.rstrip("\n")
    assert payload["truncated"] is False


def test_navigate_row_ordinal():
    payload = json.loads(disclosure.navigate_or_extract(SMALL_CSV, "0.1"))
    assert payload["mode"] == "navigate"
    assert "apple,1" in payload["content"]
    assert "banana" not in payload["content"]


def test_extract_windows_and_next_call():
    first = json.loads(disclosure.navigate_or_extract(BIG_CSV, "0", head=10))
    assert first["mode"] == "extract"
    assert first["truncated"] is True
    assert first["next_call"] is not None
    hint = json.loads(first["next_call"])
    assert hint["navigate"] == "0"
    assert hint["head"] == 10
    assert hint["skip_tokens"] == 10

    second = json.loads(
        disclosure.navigate_or_extract(BIG_CSV, "0", head=10, skip_tokens=10)
    )
    assert second["truncated"] is True
    assert json.loads(second["next_call"])["skip_tokens"] == 20


def test_extract_untail_when_finished():
    tiny = "abc,def\n"
    payload = json.loads(disclosure.navigate_or_extract(tiny, "0", head=1000))
    assert payload["content"] == "abc,def"
    assert payload["truncated"] is False
    assert payload["next_call"] is None


def test_error_on_ordinal_miss():
    with pytest.raises(UserInputError) as err:
        disclosure.navigate_or_extract(BIG_CSV, "9")
    assert "not found" in str(err.value)
    assert "Valid block ordinals" in str(err.value)
    assert "0" in str(err.value) and "2" in str(err.value)


def test_error_on_row_miss():
    with pytest.raises(UserInputError):
        disclosure.navigate_or_extract(SMALL_CSV, "0.99")


def test_error_on_invalid_ordinal_format():
    with pytest.raises(UserInputError):
        disclosure.navigate_or_extract(SMALL_CSV, "abc")
    with pytest.raises(UserInputError):
        disclosure.navigate_or_extract(SMALL_CSV, "0.1.2")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"map_flag": True, "navigate": "0"},
        {"map_flag": True, "head": 10},
        {"head": 10},
        {"skip_tokens": 10},
        {"navigate": "0", "skip_tokens": 10},
    ],
)
def test_invalid_parameter_combinations(kwargs):
    with pytest.raises(UserInputError):
        disclosure.export_response(SMALL_CSV, **kwargs)


def test_passthrough_without_disclosure_params():
    assert disclosure.export_response(SMALL_CSV) == SMALL_CSV


def test_map_via_export_response():
    payload = json.loads(disclosure.export_response(SMALL_CSV, map_flag=True))
    assert payload["mode"] == "map"
    assert payload["total_rows"] == 3
