import json
from pathlib import Path


def test_build_table_entries_include_generic_metadata():
    from larkscout_docreader import PageContent, ParsedDocument, _build_table_entries

    parsed = ParsedDocument(
        filename="scan.pdf",
        file_type="pdf",
        total_pages=1,
        pages=[
            PageContent(
                page_num=5,
                text="body",
                is_ocr=True,
                tables=["| A | B |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |"],
                tables_in_text=True,
            )
        ],
        sections=[],
        ocr_page_count=1,
        table_count=1,
    )

    entries = _build_table_entries(parsed)

    assert entries[0]["table_id"] == "table-01"
    assert entries[0]["page_start"] == 5
    assert entries[0]["page_end"] == 5
    assert entries[0]["row_count"] == 3
    assert entries[0]["column_count"] == 2
    assert entries[0]["header_rows"] == 1
    assert entries[0]["has_header"] is True
    assert entries[0]["source"] == "ocr"
    assert entries[0]["continued_from"] is None
    assert entries[0]["continued_to"] is None
    assert entries[0]["type"] == "markdown"


def test_markdown_table_dimensions_handle_empty_and_uneven_rows():
    from larkscout_docreader import _markdown_table_dimensions

    dimensions = _markdown_table_dimensions("| A | B | C |\n|---|---|---|\n| 1 || 3 |\n| 4 | 5 |")

    assert dimensions == {
        "row_count": 3,
        "column_count": 3,
        "header_rows": 1,
        "has_header": True,
    }


def test_markdown_table_dimensions_do_not_count_separator_rows():
    from larkscout_docreader import _markdown_table_dimensions

    dimensions = _markdown_table_dimensions(
        "| A | B |\n| :--- | ---: |\n| 1 | 2 |\n| 3 | 4 |"
    )

    assert dimensions["row_count"] == 3
    assert dimensions["column_count"] == 2
    assert dimensions["header_rows"] == 1
    assert dimensions["has_header"] is True


def test_markdown_table_dimensions_without_separator_has_no_header():
    from larkscout_docreader import _markdown_table_dimensions

    dimensions = _markdown_table_dimensions("| 1 | 2 |\n| 3 | 4 |")

    assert dimensions == {
        "row_count": 2,
        "column_count": 2,
        "header_rows": 0,
        "has_header": False,
    }


def test_markdown_table_dimensions_separator_only_is_empty_table():
    from larkscout_docreader import _markdown_table_dimensions

    dimensions = _markdown_table_dimensions("| --- | --- |")

    assert dimensions == {
        "row_count": 0,
        "column_count": 0,
        "header_rows": 0,
        "has_header": False,
    }


def test_write_tables_preserves_table_endpoint_markdown(tmp_path: Path):
    from larkscout_docreader import PageContent, ParsedDocument, _write_tables

    table_md = "| A | B |\n|---|---|\n| 1 | 2 |"
    parsed = ParsedDocument(
        filename="scan.pdf",
        file_type="pdf",
        total_pages=1,
        pages=[PageContent(page_num=1, text="body", is_ocr=True, tables=[table_md])],
        sections=[],
        ocr_page_count=1,
        table_count=1,
    )

    entries = _write_tables(tmp_path, parsed)
    table_file = tmp_path / "tables" / "table-01.md"

    assert entries[0]["row_count"] == 2
    assert table_file.read_text(encoding="utf-8") == "# Table 1 (page 1)\n\n" + table_md + "\n"


def test_manifest_tables_include_metadata_without_changing_table_api(tmp_path: Path):
    from larkscout_docreader import PageContent, ParsedDocument, Section, write_output_extract_only

    table_md = "| A | B |\n|---|---|\n| 1 | 2 |"
    parsed = ParsedDocument(
        filename="scan.pdf",
        file_type="pdf",
        total_pages=1,
        pages=[PageContent(page_num=1, text="body", is_ocr=True, tables=[table_md])],
        sections=[
            Section(
                index=1,
                title="Body",
                level=1,
                text="body",
                page_range="1-1",
                sid="s_body",
            )
        ],
        ocr_page_count=1,
        table_count=1,
    )

    write_output_extract_only("DOC-030", parsed, tmp_path, tags=[], source="upload")

    doc_dir = tmp_path / "DOC-030"
    tables_json = json.loads((doc_dir / "tables.json").read_text(encoding="utf-8"))
    manifest = json.loads((doc_dir / "manifest.json").read_text(encoding="utf-8"))

    assert tables_json[0]["row_count"] == 2
    assert tables_json[0]["column_count"] == 2
    assert manifest["tables"][0]["source"] == "ocr"
    assert (doc_dir / "tables" / "table-01.md").read_text(encoding="utf-8").endswith(
        table_md + "\n"
    )
