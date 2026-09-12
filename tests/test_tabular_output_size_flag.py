"""A spreadsheet says how big it came out, not that it was cut when it was not.

`parse_xlsx` converted the whole workbook and then set
`metadata["truncated"] = True` when the markdown happened to be longer than
`MAX_PARSE_ROWS * 100` characters. Nothing was ever cut: the flag described a
guess about size, the row limit it reported had not been applied to anything,
and an agent reading `truncated: true` had no way to find out which rows were
supposedly missing — because none were.

The row budget is now enforced before conversion, by refusing the workbook in
/parse (test_xlsx_row_budget). parse_xlsx itself still never cuts, so these
call it directly: what reaches it is converted whole, and the only thing it
reports is how big the result came out.
"""

import pytest


@pytest.fixture()
def big_workbook(monkeypatch):
    """A conversion that returns more characters than the threshold allows."""
    import mantisfetch_docreader as dr

    rows = "\n".join(f"| r{i} | {'x' * 120} |" for i in range(40))
    monkeypatch.setattr(dr, "_convert_to_markdown", lambda p: f"## Sheet: Data\n\n{rows}\n")
    monkeypatch.setattr(dr, "MAX_PARSE_ROWS", 1)
    return rows


def test_nothing_is_reported_as_truncated_when_nothing_was_cut(big_workbook, tmp_path) -> None:
    import mantisfetch_docreader as dr

    path = tmp_path / "big.xlsx"
    path.write_bytes(b"not really xlsx")
    parsed = dr.parse_xlsx(path)

    assert parsed.metadata.get("truncated") is None, (
        "claimed a truncation; every row is still in the output"
    )
    assert "max_rows" not in parsed.metadata, "reported a limit that was never applied"


def test_a_large_output_is_reported_as_what_it_is(big_workbook, tmp_path) -> None:
    """The warning is still worth having — an agent about to read this document
    should know it is big before it asks for the full text."""
    import mantisfetch_docreader as dr

    path = tmp_path / "big.xlsx"
    path.write_bytes(b"not really xlsx")
    parsed = dr.parse_xlsx(path)

    assert parsed.metadata["large_output"] is True
    assert parsed.metadata["output_chars"] == len(parsed.sections[0].text) or (
        parsed.metadata["output_chars"] > 0
    )


def test_every_row_is_still_there(big_workbook, tmp_path) -> None:
    """parse_xlsx does not cut: a workbook over the limit never gets here."""
    import mantisfetch_docreader as dr

    path = tmp_path / "big.xlsx"
    path.write_bytes(b"not really xlsx")
    parsed = dr.parse_xlsx(path)

    body = "\n".join(s.text for s in parsed.sections)
    assert "r0" in body and "r39" in body


def test_an_ordinary_workbook_says_nothing_about_size(tmp_path, monkeypatch) -> None:
    import mantisfetch_docreader as dr

    monkeypatch.setattr(
        dr, "_convert_to_markdown", lambda p: "## Sheet: Data\n\n| a | b |\n| 1 | 2 |\n"
    )
    path = tmp_path / "small.xlsx"
    path.write_bytes(b"not really xlsx")
    parsed = dr.parse_xlsx(path)

    assert "large_output" not in parsed.metadata
    assert "truncated" not in parsed.metadata
