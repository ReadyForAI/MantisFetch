"""Edge cases for _extract_markdown_table_blocks and the cell-aware separator
check that anchors it. Covers fence-marker matching, indented code blocks,
and strict GFM separator semantics — issue #65."""

import pytest
from larkscout_docreader import _extract_markdown_table_blocks, _is_markdown_table_separator


class TestIsMarkdownTableSeparator:
    @pytest.mark.parametrize(
        "row",
        [
            "| --- | --- |",
            "|---|---|",
            "|:---|---:|:---:|",
            "| :---: |",
            "|-|",
        ],
    )
    def test_valid_separators(self, row):
        assert _is_markdown_table_separator(row) is True

    @pytest.mark.parametrize(
        "row",
        [
            "|  |  |",
            "|  | - |  |",
            "|abc|---|",
            "| --- |   |",
            "no pipes at all",
            "| ---",
        ],
    )
    def test_invalid_separators(self, row):
        assert _is_markdown_table_separator(row) is False


class TestFenceMarkerTracking:
    def test_nested_tilde_inside_backtick_fence_is_ignored(self):
        """A ~~~ marker inside an open ``` block must NOT close it — the
        pseudo-table inside the inner fence must not be extracted."""
        text = (
            "```python\n"
            "说明\n"
            "~~~\n"
            "| pseudo | tbl |\n"
            "| --- | --- |\n"
            "| a | b |\n"
            "~~~\n"
            "```\n"
            "\n"
            "| Real | Tbl |\n"
            "| --- | --- |\n"
            "| 1 | 2 |\n"
        )
        blocks = _extract_markdown_table_blocks(text)
        assert len(blocks) == 1
        assert "Real" in blocks[0]
        assert "pseudo" not in blocks[0]

    def test_long_fence_marker_still_detected(self):
        """````` (5+ backticks) is a valid CommonMark fence opener."""
        text = (
            "`````\n"
            "| pseudo |\n"
            "| --- |\n"
            "| x |\n"
            "`````\n"
        )
        assert _extract_markdown_table_blocks(text) == []

    def test_matching_marker_closes_fence(self):
        """Real ```...``` boundary still works."""
        text = (
            "```\n"
            "| skip | me |\n"
            "| --- | --- |\n"
            "| x | y |\n"
            "```\n"
            "\n"
            "| Real | Tbl |\n"
            "| --- | --- |\n"
            "| 1 | 2 |\n"
        )
        blocks = _extract_markdown_table_blocks(text)
        assert len(blocks) == 1
        assert "Real" in blocks[0]


class TestIndentedCodeBlocks:
    def test_four_space_indented_pseudo_table_skipped(self):
        text = (
            "Code sample:\n"
            "\n"
            "    | not | a table |\n"
            "    | --- | --- |\n"
            "    | x | y |\n"
            "\n"
            "Real:\n"
            "\n"
            "| h1 | h2 |\n"
            "| --- | --- |\n"
            "| 1 | 2 |\n"
        )
        blocks = _extract_markdown_table_blocks(text)
        assert len(blocks) == 1
        assert "h1" in blocks[0]
        assert "not" not in blocks[0]

    def test_tab_indented_pseudo_table_skipped(self):
        text = (
            "\t| tabbed | pseudo |\n"
            "\t| --- | --- |\n"
            "\t| x | y |\n"
            "\n"
            "| real | tbl |\n"
            "| --- | --- |\n"
            "| 1 | 2 |\n"
        )
        blocks = _extract_markdown_table_blocks(text)
        assert len(blocks) == 1
        assert "real" in blocks[0]


class TestExtractorPreservesPriorBehavior:
    def test_back_to_back_tables_split(self):
        text = (
            "| h1 | h2 |\n"
            "| --- | --- |\n"
            "| a | b |\n"
            "| h3 | h4 |\n"
            "| --- | --- |\n"
            "| c | d |\n"
        )
        blocks = _extract_markdown_table_blocks(text)
        assert len(blocks) == 2

    def test_bare_separator_is_not_a_table(self):
        assert _extract_markdown_table_blocks("|---|") == []
        assert _extract_markdown_table_blocks("| h |\n|---|") == []

    def test_all_empty_cell_row_is_not_separator(self):
        """The legacy regex `[\\s\\-:|]+` matched all-empty-cell rows; the
        cell-aware check must not."""
        text = (
            "|  |  |  |\n"
            "| h1 | h2 | h3 |\n"
        )
        # No separator → no table
        assert _extract_markdown_table_blocks(text) == []
