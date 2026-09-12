"""A workbook's size is checked before anything expands it.

MarkItDown reads every sheet whole before anything can see how big it is:
measured, a 5 MB workbook of 100,000 rows x 10 columns peaked at 2 GB RSS and
took 30 s to convert, and the upload limit admits workbooks far larger.
`MANTISFETCH_MAX_PARSE_ROWS` looked like the guard and limited nothing — the
only thing it drove was a size warning computed after the conversion.

Now /parse streams the sheet XML and refuses a workbook over the row limit
before it is converted, and the OOXML unzip budget that only DOCX had covers
XLSX and PPTX too. Both refusals happen ahead of the doc_id: nothing is
reserved, stored or recorded for a file that was never going to be read.
"""

import io
import zipfile

import openpyxl
import pytest


@pytest.fixture()
def docs(monkeypatch, tmp_path):
    import mantisfetch_common.storage as cs

    d = tmp_path / "docs"
    d.mkdir()
    monkeypatch.setattr(cs, "DEFAULT_DOCS_DIR", d)
    return d


@pytest.fixture()
def conversions(monkeypatch):
    """Every workbook that reached MarkItDown."""
    import mantisfetch_docreader as dr

    seen: list[str] = []
    real = dr._convert_to_markdown

    def recording(path, *a, **kw):
        seen.append(path.name)
        return real(path, *a, **kw)

    monkeypatch.setattr(dr, "_convert_to_markdown", recording)
    return seen


def _workbook(*sheet_rows: int, cell: str = "v") -> bytes:
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for i, rows in enumerate(sheet_rows):
        ws = wb.create_sheet(f"S{i}")
        for r in range(rows):
            ws.append([f"{cell}{r}", r])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _parse(client, name, content):
    return client.post(
        "/doc/parse",
        files={"file": (name, content, "application/octet-stream")},
        data={"generate_summary": "false", "summary_mode": "off"},
    )


def _nothing_was_kept(docs_dir):
    import mantisfetch_common.doc_index_store as dis

    assert dis.list_documents(docs_dir) == []
    assert not (docs_dir / ".counter").exists(), "a doc_id was minted for a refused upload"


def test_rows_across_sheets_over_the_limit_are_refused_before_conversion(
    client, docs, conversions, monkeypatch
) -> None:
    import mantisfetch_docreader as dr

    monkeypatch.setattr(dr, "MAX_PARSE_ROWS", 50)
    resp = _parse(client, "multi.xlsx", _workbook(30, 30))

    assert resp.status_code == 422, resp.text
    assert "more than 50 rows" in resp.json()["detail"]
    assert conversions == [], "the workbook was converted before it was refused"
    _nothing_was_kept(docs)


def test_one_long_sheet_over_the_limit_is_refused(client, docs, conversions, monkeypatch) -> None:
    import mantisfetch_docreader as dr

    monkeypatch.setattr(dr, "MAX_PARSE_ROWS", 50)
    resp = _parse(client, "long.xlsx", _workbook(51))

    assert resp.status_code == 422, resp.text
    assert conversions == []


def test_a_workbook_at_the_limit_is_parsed_whole(client, docs, conversions, monkeypatch) -> None:
    import mantisfetch_docreader as dr

    monkeypatch.setattr(dr, "MAX_PARSE_ROWS", 50)
    resp = _parse(client, "edge.xlsx", _workbook(25, 25))

    assert resp.status_code == 200, resp.text
    assert len(conversions) == 1
    doc_id = resp.json()["doc_id"]
    full = client.get(f"/doc/library/{doc_id}/full").json()["content"]
    assert "v0" in full and "v24" in full


def test_few_rows_with_huge_cells_hit_the_unzip_budget(
    client, docs, conversions, monkeypatch
) -> None:
    """Rows are not the only way to be big: long strings in a few rows expand
    the sheet XML. That is what the unzip budget bounds. (30,000 characters is
    just under the 32,767 a cell may hold.)"""
    import mantisfetch_docreader as dr
    import mantisfetch_docreader.word as word

    monkeypatch.setattr(dr, "MAX_PARSE_ROWS", 50)
    monkeypatch.setattr(word, "_MAX_DOCX_ENTRY_BYTES", 1024 * 1024)
    resp = _parse(client, "wide.xlsx", _workbook(40, cell="x" * 30_000))

    assert resp.status_code == 422, resp.text
    assert "XLSX entry" in resp.json()["detail"]
    assert conversions == []
    _nothing_was_kept(docs)


def test_a_pptx_zip_bomb_is_refused_like_a_docx_one(client, docs, conversions) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("ppt/slides/slide1.xml", b"\x00" * (80 * 1024 * 1024))
    resp = _parse(client, "bomb.pptx", buf.getvalue())

    assert resp.status_code == 422, resp.text
    assert "PPTX entry" in resp.json()["detail"]
    assert conversions == []
    _nothing_was_kept(docs)


def test_the_count_stops_at_the_limit_and_allows_it_exactly(tmp_path) -> None:
    from mantisfetch_docreader.tabular import _check_xlsx_row_budget

    path = tmp_path / "big.xlsx"
    path.write_bytes(_workbook(5000))
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        _check_xlsx_row_budget(path, 10, "big.xlsx")
    assert "more than 10 rows" in str(exc.value.detail)
    _check_xlsx_row_budget(path, 5000, "big.xlsx")  # exactly at the limit: fine
