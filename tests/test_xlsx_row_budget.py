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


def _with_part(xlsx: bytes, name: str, data: bytes) -> bytes:
    """The workbook with one more part added to its archive."""
    buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(xlsx)) as src, zipfile.ZipFile(buf, "w") as dst:
        for info in src.infolist():
            dst.writestr(info, src.read(info))
        dst.writestr(name, data)
    return buf.getvalue()


def _sheet_xml(rows: int) -> bytes:
    body = "".join(f'<row r="{i + 1}"><c r="A{i + 1}"><v>{i}</v></c></row>' for i in range(rows))
    return (
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<sheetData>{body}</sheetData></worksheet>"
    ).encode()


def test_a_sheet_outside_the_usual_folder_is_still_counted(tmp_path) -> None:
    """The reader finds sheets through the package relationships, so a sheet can
    sit anywhere in the archive — counting only xl/worksheets/ let it through."""
    from fastapi import HTTPException
    from mantisfetch_docreader.tabular import _check_xlsx_row_budget

    path = tmp_path / "moved.xlsx"
    path.write_bytes(_with_part(_workbook(1), "xl/elsewhere/data.xml", _sheet_xml(60)))
    with pytest.raises(HTTPException):
        _check_xlsx_row_budget(path, 50, "moved.xlsx")


def test_one_unreadable_part_does_not_end_the_count(tmp_path) -> None:
    """A malformed part used to abort the whole count, and everything after it
    went uncounted — however big."""
    from fastapi import HTTPException
    from mantisfetch_docreader.tabular import _check_xlsx_row_budget

    broken = _with_part(_workbook(1), "xl/worksheets/aaa-broken.xml", b"<worksheet><sheetData>")
    path = tmp_path / "broken-first.xlsx"
    path.write_bytes(_with_part(broken, "xl/worksheets/zzz-big.xml", _sheet_xml(60)))
    with pytest.raises(HTTPException):
        _check_xlsx_row_budget(path, 50, "broken-first.xlsx")


def test_picture_anchors_are_not_rows(tmp_path) -> None:
    """A drawing's ``xdr:row`` is a cell coordinate, not a row of data; counting
    it would refuse a workbook for having pictures."""
    from mantisfetch_docreader.tabular import _check_xlsx_row_budget

    anchors = "".join(
        f"<xdr:twoCellAnchor><xdr:from><xdr:col>0</xdr:col><xdr:row>{i}</xdr:row></xdr:from>"
        f"<xdr:to><xdr:col>1</xdr:col><xdr:row>{i + 1}</xdr:row></xdr:to></xdr:twoCellAnchor>"
        for i in range(40)
    )
    drawing = (
        '<xdr:wsDr xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing">'
        f"{anchors}</xdr:wsDr>"
    ).encode()
    path = tmp_path / "pictures.xlsx"
    path.write_bytes(_with_part(_workbook(40), "xl/drawings/drawing1.xml", drawing))
    _check_xlsx_row_budget(path, 50, "pictures.xlsx")  # 40 rows, 80 anchors: fine


def test_a_large_non_sheet_part_is_streamed_not_built(tmp_path) -> None:
    """The count must not build the tree of a big part it only passes through —
    a shared-strings table the size of the unzip budget, say."""
    import tracemalloc

    from mantisfetch_docreader.tabular import _check_xlsx_row_budget

    strings = "".join(f"<si><t>{'s' * 40}{i}</t></si>" for i in range(200_000))
    sst = (
        '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"{strings}</sst>"
    ).encode()
    path = tmp_path / "strings.xlsx"
    path.write_bytes(_with_part(_workbook(1), "xl/sharedStrings2.xml", sst))

    tracemalloc.start()
    _check_xlsx_row_budget(path, 50, "strings.xlsx")
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert peak < len(sst) // 2, f"peak {peak} bytes for a {len(sst)}-byte part"


# ── Sparse sheets: the grid the converter fills in, not the rows stored (F06) ─

_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def _raw_sheet(body: str, dimension: str = "") -> bytes:
    return (
        f'<worksheet xmlns="{_NS}">{dimension}<sheetData>{body}</sheetData></worksheet>'
    ).encode()


def _budget(tmp_path, content: bytes, limit: int) -> None:
    from mantisfetch_docreader.tabular import _check_xlsx_row_budget

    path = tmp_path / "sparse.xlsx"
    path.write_bytes(content)
    _check_xlsx_row_budget(path, limit, "sparse.xlsx")


def _sparse_workbook(*cells: str) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    for ref in cells:
        ws[ref] = f"value at {ref}"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_two_cells_far_apart_count_as_every_row_between_them(tmp_path) -> None:
    """The report's repro: A1 and A201 are two row elements and 201 rows of
    Markdown once converted. The old count admitted it under a limit of 10."""
    from fastapi import HTTPException

    with pytest.raises(HTTPException, match="spans more than 10 rows"):
        _budget(tmp_path, _sparse_workbook("A1", "A201"), 10)
    _budget(tmp_path, _sparse_workbook("A1", "A201"), 201)  # exactly the grid: fine


def test_sparse_sheets_add_up_across_the_workbook(tmp_path) -> None:
    from fastapi import HTTPException

    two_sheets = _with_part(
        _sparse_workbook("A1", "A30"), "xl/worksheets/extra.xml",
        _raw_sheet('<row r="1"><c r="A1"/></row><row r="30"><c r="A30"/></row>'),
    )
    _budget(tmp_path, two_sheets, 60)
    with pytest.raises(HTTPException):
        _budget(tmp_path, two_sheets, 59)


def test_a_row_without_a_number_follows_the_one_before(tmp_path) -> None:
    """`r` is optional on rows: a writer may leave it out of consecutive ones."""
    from fastapi import HTTPException

    body = '<row r="100"><c><v>1</v></c></row>' + "<row><c><v>2</v></c></row>" * 3
    content = _with_part(_workbook(0), "xl/worksheets/norefs.xml", _raw_sheet(body))
    _budget(tmp_path, content, 103)
    with pytest.raises(HTTPException):
        _budget(tmp_path, content, 102)


def test_a_cell_past_its_row_counts_where_the_cell_is(tmp_path) -> None:
    from fastapi import HTTPException

    body = '<row r="1"><c r="A1"/><c r="B5000"/></row>'
    content = _with_part(_workbook(0), "xl/worksheets/odd.xml", _raw_sheet(body))
    with pytest.raises(HTTPException):
        _budget(tmp_path, content, 4999)


def test_a_dimension_that_understates_the_sheet_is_not_believed(tmp_path) -> None:
    from fastapi import HTTPException

    body = '<row r="1"><c r="A1"/></row><row r="900"><c r="A900"/></row>'
    content = _with_part(
        _workbook(0), "xl/worksheets/liar.xml", _raw_sheet(body, '<dimension ref="A1:A2"/>')
    )
    with pytest.raises(HTTPException):
        _budget(tmp_path, content, 100)


def test_a_sparse_workbook_over_the_limit_is_refused_before_conversion(
    client, docs, conversions, monkeypatch
) -> None:
    import mantisfetch_docreader as dr

    monkeypatch.setattr(dr, "MAX_PARSE_ROWS", 50)
    resp = _parse(client, "sparse.xlsx", _sparse_workbook("A1", "A5000"))

    assert resp.status_code == 422, resp.text
    assert conversions == [], "the workbook was converted before it was refused"
    _nothing_was_kept(docs)


# ── Wide sheets: columns are filled in too ────────────────────────────────────


def _cells(tmp_path, content: bytes, cell_limit: int, row_limit: int = 10**9) -> None:
    from mantisfetch_docreader.tabular import _check_xlsx_row_budget

    path = tmp_path / "wide.xlsx"
    path.write_bytes(content)
    _check_xlsx_row_budget(path, row_limit, "wide.xlsx", cell_limit)


def test_two_cells_at_opposite_corners_count_as_the_grid_between(tmp_path) -> None:
    """A1 and XFD200: 200 rows, inside any row limit, and a 200 x 16384 grid —
    measured at 80 s and 3.5 GB to convert a 4.8 KB file."""
    from fastapi import HTTPException

    with pytest.raises(HTTPException, match="spans more than 1000000 cells"):
        _cells(tmp_path, _sparse_workbook("A1", "XFD200"), 1_000_000)
    _cells(tmp_path, _sparse_workbook("A1", "XFD200"), 200 * 16384)  # exactly: fine


def test_a_dense_sheet_counts_rows_times_columns(tmp_path) -> None:
    from fastapi import HTTPException

    wb = openpyxl.Workbook()
    for r in range(100):
        wb.active.append(list(range(10)))
    buf = io.BytesIO()
    wb.save(buf)
    _cells(tmp_path, buf.getvalue(), 1000)
    with pytest.raises(HTTPException):
        _cells(tmp_path, buf.getvalue(), 999)


def test_cells_without_a_reference_take_the_next_column(tmp_path) -> None:
    from fastapi import HTTPException

    body = '<row r="1">' + "<c><v>1</v></c>" * 7 + "</row>" + '<row r="10"><c><v>2</v></c></row>'
    content = _with_part(_workbook(0), "xl/worksheets/norefs.xml", _raw_sheet(body))
    _cells(tmp_path, content, 70)  # 10 rows x 7 columns
    with pytest.raises(HTTPException):
        _cells(tmp_path, content, 69)


def test_wide_sheets_add_up_across_the_workbook(tmp_path) -> None:
    from fastapi import HTTPException

    content = _with_part(
        _sparse_workbook("A1", "J10"), "xl/worksheets/extra.xml",
        _raw_sheet('<row r="10"><c r="J10"/></row>'),
    )
    _cells(tmp_path, content, 200)  # 100 + 100
    with pytest.raises(HTTPException):
        _cells(tmp_path, content, 199)


def test_a_wide_sparse_workbook_is_refused_before_conversion(
    client, docs, conversions
) -> None:
    """At the shipped defaults, through /parse."""
    resp = _parse(client, "wide.xlsx", _sparse_workbook("A1", "XFD200"))

    assert resp.status_code == 422, resp.text
    assert "MANTISFETCH_MAX_PARSE_CELLS" in resp.json()["detail"]
    assert conversions == [], "the workbook was converted before it was refused"
    _nothing_was_kept(docs)
