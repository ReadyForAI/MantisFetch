"""Parsing/format correctness regressions (RMB overflow, blob aliases, table dims, selectors)."""


def test_rmb_amount_overflow_returns_none_not_indexerror():  # #37
    from larkscout_docreader.text_utils import _amount_to_uppercase_rmb

    # >= 1e16 has more 10^4 groups than the unit table; must return None, not raise.
    assert _amount_to_uppercase_rmb("10000000000000000") is None
    assert _amount_to_uppercase_rmb("99999999999999999.99") is None
    # Normal amounts still convert.
    assert _amount_to_uppercase_rmb("1234.56") is not None
    assert _amount_to_uppercase_rmb("0") is not None


def test_replace_blob_segment_end_alias_without_start_alias():  # #25
    from larkscout_docreader.models import FieldGroup
    from larkscout_docreader.profiles import _replace_blob_segment

    # start_alias is None; start is found via `aliases`, end via end_alias.
    group = FieldGroup(
        id="g", aliases=("BEGIN",), start_alias=None, end_alias="STOP",
        replace_mode="block_between_aliases",
    )
    text = "intro BEGIN old body STOP tail"
    out = _replace_blob_segment(text, group, "NEW")  # must not raise TypeError
    assert "NEW" in out
    assert "old body" not in out
    assert "tail" in out


def test_markdown_table_dimensions_ignores_all_empty_rows():  # #41
    from larkscout_docreader.ocr.tables import _markdown_table_dimensions

    table = "| A | B |\n|---|---|\n| 1 | 2 |\n|   |   |\n"
    dims = _markdown_table_dimensions(table)
    # The all-empty "|   |   |" row is content, not a separator; header detected
    # only from the real "|---|---|" line.
    assert dims["has_header"] is True
    assert dims["header_rows"] == 1
    assert dims["row_count"] == 3  # header + data + the empty row (3 content rows)


def test_trim_action_fields_never_truncates_css_selector():  # #24
    from larkscout_browser import _trim_action_fields

    short = {"strategy": {"type": "css", "selector": "div.foo > span.bar"}, "name": "x"}
    out = _trim_action_fields(short, name_max=50, selector_max=100)
    assert out["strategy"]["selector"] == "div.foo > span.bar"  # kept whole

    long_sel = "div.a > span.b:nth-child(3) > a.link[data-id='123456']"
    item = {"strategy": {"type": "css", "selector": long_sel}, "name": "x"}
    out2 = _trim_action_fields(item, name_max=50, selector_max=20)
    # Dropped, never truncated to a partial (unmatchable) selector.
    assert "selector" not in out2["strategy"]
