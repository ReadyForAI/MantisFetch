def _block(block_id, text, bbox, confidence=0.9):
    from larkscout_docreader import OCRTextBlock

    return OCRTextBlock(
        block_id=block_id,
        text=text,
        bbox=bbox,
        confidence=confidence,
    )


def test_detect_table_candidates_from_ocr_grid():
    from larkscout_docreader import (
        OCRBlocksSidecar,
        OCRPageBlocks,
        _detect_table_candidates_from_ocr_blocks,
    )

    sidecar = OCRBlocksSidecar(
        doc_id="DOC-001",
        pages=(
            OCRPageBlocks(
                page=2,
                width=1000,
                height=1000,
                blocks=(
                    _block("p2-b0001", "品名", (100, 100, 180, 120)),
                    _block("p2-b0002", "数量", (300, 100, 360, 120)),
                    _block("p2-b0003", "金额", (500, 100, 560, 120)),
                    _block("p2-b0004", "软件", (100, 140, 180, 160)),
                    _block("p2-b0005", "1", (300, 140, 330, 160)),
                    _block("p2-b0006", "100", (500, 140, 560, 160)),
                    _block("p2-b0007", "服务", (100, 180, 180, 200)),
                    _block("p2-b0008", "2", (300, 180, 330, 200)),
                    _block("p2-b0009", "200", (500, 180, 560, 200)),
                ),
            ),
        ),
    )

    candidates = _detect_table_candidates_from_ocr_blocks(sidecar)

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate["candidate_id"] == "p2-tc0001"
    assert candidate["page"] == 2
    assert candidate["bbox"] == [100, 100, 560, 200]
    assert candidate["row_count"] == 3
    assert candidate["column_count"] == 3
    assert candidate["source"] == "ocr_geometry"
    assert candidate["ocr_block_refs"] == [f"p2-b{i:04d}" for i in range(1, 10)]


def test_detect_table_candidates_ignores_paragraph_like_blocks():
    from larkscout_docreader import (
        OCRBlocksSidecar,
        OCRPageBlocks,
        _detect_table_candidates_from_ocr_blocks,
    )

    sidecar = OCRBlocksSidecar(
        doc_id="DOC-002",
        pages=(
            OCRPageBlocks(
                page=1,
                width=1000,
                height=1000,
                blocks=(
                    _block("p1-b0001", "第一段合同正文", (100, 100, 500, 120)),
                    _block("p1-b0002", "第二段合同正文", (100, 140, 500, 160)),
                    _block("p1-b0003", "第三段合同正文", (100, 180, 500, 200)),
                ),
            ),
        ),
    )

    candidates = _detect_table_candidates_from_ocr_blocks(sidecar)

    assert candidates == []


def test_detect_table_candidates_requires_multiple_rows():
    from larkscout_docreader import (
        OCRBlocksSidecar,
        OCRPageBlocks,
        _detect_table_candidates_from_ocr_blocks,
    )

    sidecar = OCRBlocksSidecar(
        doc_id="DOC-003",
        pages=(
            OCRPageBlocks(
                page=1,
                width=1000,
                height=1000,
                blocks=(
                    _block("p1-b0001", "品名", (100, 100, 180, 120)),
                    _block("p1-b0002", "数量", (300, 100, 360, 120)),
                    _block("p1-b0003", "金额", (500, 100, 560, 120)),
                ),
            ),
        ),
    )

    candidates = _detect_table_candidates_from_ocr_blocks(sidecar)

    assert candidates == []


def test_reconstruct_table_from_candidate_preserves_cell_refs():
    from larkscout_docreader import (
        OCRBlocksSidecar,
        OCRPageBlocks,
        _detect_table_candidates_from_ocr_blocks,
        _markdown_from_structured_table,
        _reconstruct_table_from_candidate,
    )

    sidecar = OCRBlocksSidecar(
        doc_id="DOC-004",
        pages=(
            OCRPageBlocks(
                page=1,
                width=1000,
                height=1000,
                blocks=(
                    _block("p1-b0001", "品名", (100, 100, 180, 120)),
                    _block("p1-b0002", "金额", (300, 100, 360, 120)),
                    _block("p1-b0003", "软件", (100, 140, 180, 160)),
                    _block("p1-b0004", "100", (300, 140, 360, 160)),
                ),
            ),
        ),
    )
    candidate = _detect_table_candidates_from_ocr_blocks(sidecar)[0]

    table = _reconstruct_table_from_candidate(sidecar, candidate, "table-01")

    assert table["table_id"] == "table-01"
    assert table["page"] == 1
    assert table["row_count"] == 2
    assert table["column_count"] == 2
    assert table["rows"][0]["cells"][0]["text"] == "品名"
    assert table["rows"][0]["cells"][0]["ocr_block_refs"] == ["p1-b0001"]
    assert table["rows"][1]["cells"][1]["text"] == "100"
    assert _markdown_from_structured_table(table) == "| 品名 | 金额 |\n| --- | --- |\n| 软件 | 100 |"


def test_write_tables_emits_structured_table_sidecar(tmp_path):
    import json

    from larkscout_docreader import (
        OCRBlocksSidecar,
        OCRPageBlocks,
        ParsedDocument,
        _write_tables,
    )

    parsed = ParsedDocument(
        filename="scan.pdf",
        file_type="pdf",
        total_pages=1,
        pages=[],
        sections=[],
        ocr_page_count=1,
        table_count=0,
        ocr_blocks=OCRBlocksSidecar(
            doc_id="DOC-005",
            pages=(
                OCRPageBlocks(
                    page=1,
                    width=1000,
                    height=1000,
                    blocks=(
                        _block("p1-b0001", "品名", (100, 100, 180, 120)),
                        _block("p1-b0002", "金额", (300, 100, 360, 120)),
                        _block("p1-b0003", "软件", (100, 140, 180, 160)),
                        _block("p1-b0004", "100", (300, 140, 360, 160)),
                    ),
                ),
            ),
        ),
    )

    entries = _write_tables(tmp_path, parsed)

    table_md = (tmp_path / "tables" / "table-01.md").read_text(encoding="utf-8")
    table_json = json.loads((tmp_path / "tables" / "table-01.json").read_text(encoding="utf-8"))
    assert entries[0]["source"] == "layout"
    assert entries[0]["json_file"] == "tables/table-01.json"
    assert entries[0]["ocr_block_refs"] == ["p1-b0001", "p1-b0002", "p1-b0003", "p1-b0004"]
    assert "| 品名 | 金额 |" in table_md
    assert table_json["rows"][1]["cells"][1]["text"] == "100"


def test_write_tables_links_cross_page_continuation(tmp_path):
    import json

    from larkscout_docreader import (
        OCRBlocksSidecar,
        OCRPageBlocks,
        ParsedDocument,
        _write_tables,
    )

    parsed = ParsedDocument(
        filename="scan.pdf",
        file_type="pdf",
        total_pages=2,
        pages=[],
        sections=[],
        ocr_page_count=2,
        table_count=0,
        ocr_blocks=OCRBlocksSidecar(
            doc_id="DOC-006",
            pages=(
                OCRPageBlocks(
                    page=1,
                    width=1000,
                    height=1000,
                    blocks=(
                        _block("p1-b0001", "品名", (100, 740, 180, 760)),
                        _block("p1-b0002", "金额", (300, 740, 360, 760)),
                        _block("p1-b0003", "软件", (100, 780, 180, 800)),
                        _block("p1-b0004", "100", (300, 780, 360, 800)),
                    ),
                ),
                OCRPageBlocks(
                    page=2,
                    width=1000,
                    height=1000,
                    blocks=(
                        _block("p2-b0001", "品名", (100, 80, 180, 100)),
                        _block("p2-b0002", "金额", (300, 80, 360, 100)),
                        _block("p2-b0003", "服务", (100, 120, 180, 140)),
                        _block("p2-b0004", "200", (300, 120, 360, 140)),
                    ),
                ),
            ),
        ),
    )

    entries = _write_tables(tmp_path, parsed)

    table_1_json = json.loads((tmp_path / "tables" / "table-01.json").read_text(encoding="utf-8"))
    table_2_json = json.loads((tmp_path / "tables" / "table-02.json").read_text(encoding="utf-8"))
    assert entries[0]["continued_to"] == "table-02"
    assert entries[1]["continued_from"] == "table-01"
    assert table_1_json["continued_to"] == "table-02"
    assert table_2_json["continued_from"] == "table-01"


def test_write_tables_does_not_link_unrelated_adjacent_tables(tmp_path):
    from larkscout_docreader import (
        OCRBlocksSidecar,
        OCRPageBlocks,
        ParsedDocument,
        _write_tables,
    )

    parsed = ParsedDocument(
        filename="scan.pdf",
        file_type="pdf",
        total_pages=2,
        pages=[],
        sections=[],
        ocr_page_count=2,
        table_count=0,
        ocr_blocks=OCRBlocksSidecar(
            doc_id="DOC-007",
            pages=(
                OCRPageBlocks(
                    page=1,
                    width=1000,
                    height=1000,
                    blocks=(
                        _block("p1-b0001", "品名", (100, 100, 180, 120)),
                        _block("p1-b0002", "金额", (300, 100, 360, 120)),
                        _block("p1-b0003", "软件", (100, 140, 180, 160)),
                        _block("p1-b0004", "100", (300, 140, 360, 160)),
                    ),
                ),
                OCRPageBlocks(
                    page=2,
                    width=1000,
                    height=1000,
                    blocks=(
                        _block("p2-b0001", "条款", (100, 100, 180, 120)),
                        _block("p2-b0002", "日期", (300, 100, 360, 120)),
                        _block("p2-b0003", "付款", (100, 140, 180, 160)),
                        _block("p2-b0004", "2026", (300, 140, 360, 160)),
                    ),
                ),
            ),
        ),
    )

    entries = _write_tables(tmp_path, parsed)

    assert entries[0]["continued_to"] is None
    assert entries[1]["continued_from"] is None


def test_write_tables_skips_when_extract_tables_false(tmp_path):
    from larkscout_docreader import (
        OCRBlocksSidecar,
        OCRPageBlocks,
        PageContent,
        ParsedDocument,
        _write_tables,
    )

    parsed = ParsedDocument(
        filename="scan.pdf",
        file_type="pdf",
        total_pages=1,
        pages=[
            PageContent(
                page_num=1,
                text="品名 金额\n软件 100",
                tables=["| 品名 | 金额 |\n| --- | --- |\n| 软件 | 100 |"],
            )
        ],
        sections=[],
        ocr_page_count=1,
        table_count=0,
        ocr_blocks=OCRBlocksSidecar(
            doc_id="DOC-008",
            pages=(
                OCRPageBlocks(
                    page=1,
                    width=1000,
                    height=1000,
                    blocks=(
                        _block("p1-b0001", "品名", (100, 100, 180, 120)),
                        _block("p1-b0002", "金额", (300, 100, 360, 120)),
                        _block("p1-b0003", "软件", (100, 140, 180, 160)),
                        _block("p1-b0004", "100", (300, 140, 360, 160)),
                    ),
                ),
            ),
        ),
        extract_tables=False,
    )

    entries = _write_tables(tmp_path, parsed)

    assert entries == []
    assert not (tmp_path / "tables").exists()
