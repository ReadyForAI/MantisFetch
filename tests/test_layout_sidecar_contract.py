import importlib.util
import json
import sys
from pathlib import Path

import pytest


def test_ocr_blocks_sidecar_contract_shape(tmp_path: Path):
    from mantisfetch_docreader import OCRBlocksSidecar, OCRPageBlocks, OCRTextBlock

    sidecar = OCRBlocksSidecar(
        doc_id="DOC-001",
        pages=(
            OCRPageBlocks(
                page=1,
                width=2480,
                height=3508,
                blocks=(
                    OCRTextBlock(
                        block_id="p1-b0001",
                        text="示例文本",
                        bbox=(100, 220, 680, 260),
                        confidence=0.94,
                        source="local_ocr",
                        line_index=12,
                        order=12,
                    ),
                ),
            ),
        ),
    )

    data = sidecar.to_dict()

    assert data["version"] == 1
    assert data["doc_id"] == "DOC-001"
    assert data["coordinate_system"] == "image_pixels"
    assert len(data["pages"]) == 1
    page = data["pages"][0]
    assert page["page"] == 1
    assert page["width"] == 2480
    assert page["height"] == 3508
    block = page["blocks"][0]
    assert block == {
        "block_id": "p1-b0001",
        "text": "示例文本",
        "bbox": [100.0, 220.0, 680.0, 260.0],
        "confidence": 0.94,
        "source": "local_ocr",
        "line_index": 12,
        "order": 12,
    }


def test_layout_manifest_entry_is_low_token_metadata_only():
    from mantisfetch_docreader import _build_layout_manifest_entry

    layout = _build_layout_manifest_entry(available=True)

    assert layout == {
        "available": True,
        "ocr_blocks_path": "ocr_blocks.json",
        "version": 1,
        "coordinate_system": "image_pixels",
    }
    assert "pages" not in layout
    assert "blocks" not in layout


def test_unavailable_layout_manifest_entry_has_no_sidecar_path():
    from mantisfetch_docreader import _build_layout_manifest_entry

    layout = _build_layout_manifest_entry(available=False)

    assert layout["available"] is False
    assert layout["ocr_blocks_path"] == ""
    assert layout["version"] == 1
    assert layout["coordinate_system"] == "image_pixels"


def test_write_ocr_blocks_sidecar_returns_manifest_metadata(tmp_path: Path):
    from mantisfetch_docreader import (
        OCRBlocksSidecar,
        OCRPageBlocks,
        OCRTextBlock,
        _write_ocr_blocks_sidecar,
    )

    sidecar = OCRBlocksSidecar(
        doc_id="DOC-002",
        pages=(
            OCRPageBlocks(
                page=1,
                width=100,
                height=200,
                blocks=(OCRTextBlock(block_id="p1-b0001", text="A", bbox=(1, 2, 3, 4)),),
            ),
        ),
    )

    layout = _write_ocr_blocks_sidecar(tmp_path, sidecar)
    written = json.loads((tmp_path / "ocr_blocks.json").read_text(encoding="utf-8"))

    assert layout["available"] is True
    assert layout["ocr_blocks_path"] == "ocr_blocks.json"
    assert "pages" not in layout
    assert written["doc_id"] == "DOC-002"
    assert written["pages"][0]["blocks"][0]["block_id"] == "p1-b0001"


def test_ocr_block_rejects_malformed_bbox():
    from mantisfetch_docreader import OCRTextBlock

    with pytest.raises(ValueError, match="exactly four"):
        OCRTextBlock(block_id="bad", text="A", bbox=(1, 2, 3)).to_dict()  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="ordered"):
        OCRTextBlock(block_id="bad", text="A", bbox=(5, 2, 3, 4)).to_dict()


def test_paddle_worker_extracts_v2_geometry():
    worker_path = Path(__file__).parents[1] / "services" / "docreader" / "paddle_ocr_worker.py"
    spec = importlib.util.spec_from_file_location("paddle_ocr_worker_layout_test", worker_path)
    assert spec is not None and spec.loader is not None
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)

    result = [
        [
            [
                [[10, 20], [110, 20], [110, 40], [10, 40]],
                ("甲方：测试公司", 0.98),
            ]
        ]
    ]

    blocks = worker._extract_paddle_ocr_blocks(result)

    assert blocks == [
        {
            "text": "甲方：测试公司",
            "bbox": [10.0, 20.0, 110.0, 40.0],
            "confidence": 0.98,
            "line_index": 0,
            "order": 0,
        }
    ]
    assert worker._flatten_paddle_ocr_result(result) == "甲方：测试公司"


def test_paddle_worker_extracts_v3_geometry():
    worker_path = Path(__file__).parents[1] / "services" / "docreader" / "paddle_ocr_worker.py"
    spec = importlib.util.spec_from_file_location("paddle_ocr_worker_layout_test_v3", worker_path)
    assert spec is not None and spec.loader is not None
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)

    result = [
        {
            "rec_texts": ["品名", "金额"],
            "rec_scores": [0.9, 0.88],
            "rec_boxes": [[1, 2, 20, 10], [30, 2, 60, 10]],
        }
    ]

    blocks = worker._extract_paddle_ocr_blocks(result)

    assert blocks[0]["text"] == "品名"
    assert blocks[0]["bbox"] == [1.0, 2.0, 20.0, 10.0]
    assert blocks[0]["confidence"] == 0.9
    assert blocks[1]["text"] == "金额"
    assert blocks[1]["bbox"] == [30.0, 2.0, 60.0, 10.0]


def test_paddle_worker_handles_array_like_v3_geometry_without_truthiness():
    worker_path = Path(__file__).parents[1] / "services" / "docreader" / "paddle_ocr_worker.py"
    spec = importlib.util.spec_from_file_location("paddle_ocr_worker_layout_array_test", worker_path)
    assert spec is not None and spec.loader is not None
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)

    class ArrayLike:
        def __init__(self, values):
            self.values = values

        def tolist(self):
            return self.values

        def __bool__(self):
            raise ValueError("ambiguous truth value")

    result = [
        {
            "rec_texts": ArrayLike(["合计"]),
            "rec_scores": ArrayLike([0.91]),
            "rec_boxes": ArrayLike([[1, 2, 30, 10]]),
        }
    ]

    blocks = worker._extract_paddle_ocr_blocks(result)

    assert blocks[0]["text"] == "合计"
    assert blocks[0]["bbox"] == [1.0, 2.0, 30.0, 10.0]
    assert blocks[0]["confidence"] == 0.91


def test_local_ocr_with_layout_reads_worker_blocks(tmp_path: Path, monkeypatch):
    import mantisfetch_docreader

    worker = tmp_path / "worker.py"
    worker.write_text(
        "\n".join(
            [
                "import json, sys",
                "print(json.dumps({'type': 'ready'}), flush=True)",
                "for line in sys.stdin:",
                "    req = json.loads(line)",
                "    print(json.dumps({",
                "      'ok': True,",
                "      'page_num': req['page_num'],",
                "      'text': '甲方：测试公司',",
                "      'width': 100,",
                "      'height': 200,",
                "      'blocks': [{'text': '甲方：测试公司', 'bbox': [1, 2, 50, 12], 'confidence': 0.96, 'line_index': 0, 'order': 0}],",
                "    }, ensure_ascii=False), flush=True)",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MANTISFETCH_LOCAL_OCR_WORKER_CMD", f"{sys.executable} {worker}")
    monkeypatch.setattr(mantisfetch_docreader.ocr.engines, "_local_ocr_disabled_until", 0.0)
    monkeypatch.setattr(mantisfetch_docreader.ocr.engines, "LOCAL_OCR_WORKER_STARTUP_TIMEOUT_SEC", 3.0)
    monkeypatch.setattr(mantisfetch_docreader.ocr.engines, "LOCAL_OCR_WORKER_REQUEST_TIMEOUT_SEC", 3.0)

    try:
        text, page_blocks = mantisfetch_docreader.local_ocr_with_layout(
            b"not-an-image",
            3,
            "paddleocr",
        )
    finally:
        mantisfetch_docreader._stop_local_ocr_worker()

    assert text == "甲方：测试公司"
    assert page_blocks is not None
    assert page_blocks.page == 3
    assert page_blocks.width == 100
    assert page_blocks.height == 200
    assert page_blocks.blocks[0].block_id == "p3-b0001"
    assert page_blocks.blocks[0].bbox == (1.0, 2.0, 50.0, 12.0)


def test_write_output_extract_only_writes_ocr_blocks_and_manifest_layout(tmp_path: Path):
    from mantisfetch_docreader import (
        OCRBlocksSidecar,
        OCRPageBlocks,
        OCRTextBlock,
        ParsedDocument,
        Section,
        write_output_extract_only,
    )

    parsed = ParsedDocument(
        filename="scan.pdf",
        file_type="pdf",
        total_pages=1,
        pages=[],
        sections=[
            Section(
                index=1,
                title="OCR",
                level=1,
                text="甲方：测试公司",
                page_range="1-1",
                sid="s_ocr",
            )
        ],
        ocr_page_count=1,
        table_count=0,
        ocr_blocks=OCRBlocksSidecar(
            doc_id="",
            pages=(
                OCRPageBlocks(
                    page=1,
                    width=100,
                    height=200,
                    blocks=(
                        OCRTextBlock(
                            block_id="p1-b0001",
                            text="甲方：测试公司",
                            bbox=(1, 2, 50, 12),
                            confidence=0.96,
                        ),
                    ),
                ),
            ),
        ),
    )

    write_output_extract_only("DOC-020", parsed, tmp_path, tags=[], source="upload")

    sidecar = json.loads((tmp_path / "DOC-020" / "ocr_blocks.json").read_text(encoding="utf-8"))
    manifest = json.loads((tmp_path / "DOC-020" / "manifest.json").read_text(encoding="utf-8"))

    assert sidecar["doc_id"] == "DOC-020"
    assert sidecar["pages"][0]["blocks"][0]["block_id"] == "p1-b0001"
    assert manifest["layout"] == {
        "available": True,
        "ocr_blocks_path": "ocr_blocks.json",
        "version": 1,
        "coordinate_system": "image_pixels",
    }
    assert "pages" not in manifest["layout"]
    assert "blocks" not in manifest["layout"]


def test_write_output_extract_only_marks_layout_unavailable_without_ocr_blocks(tmp_path: Path):
    from mantisfetch_docreader import ParsedDocument, Section, write_output_extract_only

    parsed = ParsedDocument(
        filename="text.pdf",
        file_type="pdf",
        total_pages=1,
        pages=[],
        sections=[
            Section(
                index=1,
                title="Text",
                level=1,
                text="selectable text",
                page_range="1-1",
                sid="s_text",
            )
        ],
        ocr_page_count=0,
        table_count=0,
    )

    write_output_extract_only("DOC-021", parsed, tmp_path, tags=[], source="upload")

    doc_dir = tmp_path / "DOC-021"
    manifest = json.loads((doc_dir / "manifest.json").read_text(encoding="utf-8"))

    assert not (doc_dir / "ocr_blocks.json").exists()
    assert manifest["layout"] == {
        "available": False,
        "ocr_blocks_path": "",
        "version": 1,
        "coordinate_system": "image_pixels",
    }
    assert manifest["paths"]["ocr_blocks"] == ""
