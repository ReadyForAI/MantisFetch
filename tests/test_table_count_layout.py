"""table_count must count what is actually materialized on disk.

Observed on a 162-page scanned ingest: the parse response and manifest said
`table_count: 0` while `tables.json` held 125 geometry-reconstructed tables —
scanned documents get all their tables from the storage-time layout path,
which the parse-time counter never saw.
"""

import json

from mantisfetch_docreader import (
    OCRBlocksSidecar,
    OCRPageBlocks,
    OCRTextBlock,
    PageContent,
    ParsedDocument,
    Section,
    write_output_extract_only,
)


def _grid_page(page: int) -> OCRPageBlocks:
    def block(i, text, bbox):
        return OCRTextBlock(block_id=f"p{page}-b{i:04d}", text=text, bbox=bbox, confidence=0.9)

    return OCRPageBlocks(
        page=page,
        width=1000,
        height=1000,
        blocks=(
            block(1, "品名", (100, 100, 180, 120)),
            block(2, "数量", (300, 100, 360, 120)),
            block(3, "金额", (500, 100, 560, 120)),
            block(4, "软件", (100, 140, 180, 160)),
            block(5, "1", (300, 140, 330, 160)),
            block(6, "100", (500, 140, 560, 160)),
            block(7, "服务", (100, 180, 180, 200)),
            block(8, "2", (300, 180, 330, 200)),
            block(9, "200", (500, 180, 560, 200)),
        ),
    )


def _scanned_parsed() -> ParsedDocument:
    section = Section(
        index=1,
        title="Page 1",
        level=1,
        text="扫描页正文",
        page_range="p.1-1",
    )
    return ParsedDocument(
        filename="scan.pdf",
        file_type="pdf",
        total_pages=2,
        pages=[
            PageContent(page_num=1, text="扫描页正文", is_ocr=True),
            PageContent(page_num=2, text="", is_ocr=True),
        ],
        sections=[section],
        ocr_page_count=2,
        table_count=0,  # parse-time counter saw no native/OCR-text tables
        ocr_blocks=OCRBlocksSidecar(doc_id="", pages=(_grid_page(2),)),
        extract_tables=True,
    )


def test_table_count_includes_layout_tables(tmp_path):
    parsed = _scanned_parsed()
    docs_dir = tmp_path / "docs"

    write_output_extract_only("DOC-T10", parsed, docs_dir, content_type="General")

    doc_dir = docs_dir / "General" / "DOC-T10"
    tables = json.loads((doc_dir / "tables.json").read_text(encoding="utf-8"))
    manifest = json.loads((doc_dir / "manifest.json").read_text(encoding="utf-8"))
    meta = json.loads((doc_dir / ".meta.json").read_text(encoding="utf-8"))

    assert len(tables) == 1
    assert tables[0]["source"] == "layout"
    assert manifest["table_count"] == 1
    assert meta["table_count"] == 1
    # The in-memory document is synced too, so the parse response (built from
    # parsed.table_count after persist) agrees with the manifest.
    assert parsed.table_count == 1


def test_table_count_zero_when_extract_tables_disabled(tmp_path):
    parsed = _scanned_parsed()
    parsed.extract_tables = False
    docs_dir = tmp_path / "docs"

    write_output_extract_only("DOC-T11", parsed, docs_dir, content_type="General")

    manifest = json.loads(
        (docs_dir / "General" / "DOC-T11" / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["table_count"] == 0
    assert parsed.table_count == 0
