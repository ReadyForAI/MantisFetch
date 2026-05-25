import json

import pytest
from fastapi import HTTPException


def _write_pdf(path):
    import fitz

    doc = fitz.open()
    page = doc.new_page(width=200, height=100)
    page.draw_rect(fitz.Rect(0, 0, 200, 100), color=(1, 1, 1), fill=(1, 1, 1))
    page.draw_rect(fitz.Rect(40, 20, 120, 70), color=(1, 0, 0), fill=(1, 0, 0))
    doc.save(path)
    doc.close()


def _write_doc_fixture(tmp_path, *, with_ocr_blocks=False):
    docs_dir = tmp_path / "docs"
    doc_dir = docs_dir / "DOC-001"
    source_dir = doc_dir / "source"
    source_dir.mkdir(parents=True)
    pdf_path = source_dir / "sample.pdf"
    _write_pdf(pdf_path)
    manifest = {
        "doc_id": "DOC-001",
        "filename": "sample.pdf",
        "file_type": "pdf",
        "source_file": {
            "kind": "upload",
            "filename": "sample.pdf",
            "stored_filename": "sample.pdf",
            "ref": "source/sample.pdf",
        },
    }
    (doc_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    if with_ocr_blocks:
        sidecar = {
            "version": 1,
            "doc_id": "DOC-001",
            "coordinate_system": "image_pixels",
            "pages": [{"page": 1, "width": 400, "height": 200, "blocks": []}],
        }
        (doc_dir / "ocr_blocks.json").write_text(json.dumps(sidecar), encoding="utf-8")
    return docs_dir, doc_dir


def test_export_pdf_region_crop_writes_derived_artifacts(tmp_path):
    from larkscout_docreader import export_pdf_region_crop

    docs_dir, doc_dir = _write_doc_fixture(tmp_path)

    metadata = export_pdf_region_crop(
        docs_dir,
        "DOC-001",
        1,
        [40, 20, 120, 70],
        dpi=144,
        coordinate_system="page_points",
    )

    output_path = doc_dir / metadata["output_path"]
    metadata_path = doc_dir / metadata["metadata_path"]
    assert metadata["derived"] is True
    assert metadata["coordinate_system"] == "page_points"
    assert metadata["dpi"] == 144
    assert metadata["source_ref"] == "source/sample.pdf"
    assert metadata["source_bounds"] == {"width": 200.0, "height": 100.0, "unit": "points"}
    assert output_path.read_bytes().startswith(b"\x89PNG")
    assert json.loads(metadata_path.read_text(encoding="utf-8"))["output_path"] == metadata["output_path"]
    assert metadata["output_path"].startswith("derived/crops/")


def test_export_pdf_region_crop_converts_image_pixel_bbox(tmp_path):
    from larkscout_docreader import export_pdf_region_crop

    docs_dir, _doc_dir = _write_doc_fixture(tmp_path, with_ocr_blocks=True)

    metadata = export_pdf_region_crop(
        docs_dir,
        "DOC-001",
        1,
        [80, 40, 240, 140],
        dpi=72,
        coordinate_system="image_pixels",
    )

    assert metadata["source_bounds"] == {"width": 400.0, "height": 200.0, "unit": "pixels"}
    assert metadata["clip_rect"] == [40.0, 20.0, 120.0, 70.0]


def test_export_pdf_region_crop_rejects_invalid_page(tmp_path):
    from larkscout_docreader import export_pdf_region_crop

    docs_dir, _doc_dir = _write_doc_fixture(tmp_path)

    with pytest.raises(HTTPException) as exc:
        export_pdf_region_crop(
            docs_dir,
            "DOC-001",
            2,
            [40, 20, 120, 70],
            coordinate_system="page_points",
        )

    assert exc.value.status_code == 422
    assert "page out of range" in str(exc.value.detail)


def test_export_pdf_region_crop_rejects_invalid_bbox(tmp_path):
    from larkscout_docreader import export_pdf_region_crop

    docs_dir, _doc_dir = _write_doc_fixture(tmp_path)

    with pytest.raises(HTTPException) as exc:
        export_pdf_region_crop(
            docs_dir,
            "DOC-001",
            1,
            [120, 20, 40, 70],
            coordinate_system="page_points",
        )

    assert exc.value.status_code == 422
    assert "positive area" in str(exc.value.detail)


def test_rerun_region_ocr_writes_separate_artifact(tmp_path, monkeypatch):
    import larkscout_docreader
    from larkscout_docreader import OCRPageBlocks, OCRTextBlock, rerun_region_ocr

    docs_dir, doc_dir = _write_doc_fixture(tmp_path)
    canonical = doc_dir / "full.md"
    canonical.write_text("canonical output\n", encoding="utf-8")

    def fake_local_ocr(image_bytes, page_num, backend):
        assert image_bytes.startswith(b"\x89PNG")
        assert page_num == 1
        assert backend == "paddleocr"
        return (
            "甲方：测试公司",
            OCRPageBlocks(
                page=1,
                width=160,
                height=100,
                blocks=(
                    OCRTextBlock(
                        block_id="p1-b0001",
                        text="甲方：测试公司",
                        bbox=(2, 4, 80, 20),
                        confidence=0.91,
                        source="local-paddleocr",
                    ),
                ),
            ),
        )

    monkeypatch.setattr(larkscout_docreader, "local_ocr_with_layout", fake_local_ocr)

    result = rerun_region_ocr(
        docs_dir,
        "DOC-001",
        1,
        [40, 20, 120, 70],
        backend="paddleocr",
        dpi=144,
        coordinate_system="page_points",
        run_id="case-1",
    )

    assert canonical.read_text(encoding="utf-8") == "canonical output\n"
    assert result["artifact_id"] == "case-1"
    assert result["text"] == "甲方：测试公司"
    assert result["confidence"] == 0.91
    assert result["backend"] == {
        "requested": "paddleocr",
        "kind": "local",
        "selected": "paddleocr",
        "dpi": 144,
    }
    assert result["blocks"][0]["text"] == "甲方：测试公司"
    assert result["crop"]["output_path"].startswith("derived/crops/")
    assert result["text_path"] == "derived/region_ocr/case-1.txt"
    assert (doc_dir / result["text_path"]).read_text(encoding="utf-8") == "甲方：测试公司\n"
    assert json.loads((doc_dir / result["metadata_path"]).read_text(encoding="utf-8"))["derived"] is True


def test_rerun_region_ocr_rejects_invalid_region(tmp_path):
    from larkscout_docreader import rerun_region_ocr

    docs_dir, _doc_dir = _write_doc_fixture(tmp_path)

    with pytest.raises(HTTPException) as exc:
        rerun_region_ocr(
            docs_dir,
            "DOC-001",
            1,
            [40, 20, 240, 70],
            backend="paddleocr",
            coordinate_system="page_points",
        )

    assert exc.value.status_code == 422
    assert "outside page_points bounds" in str(exc.value.detail)


def test_generate_visual_debug_artifacts_is_opt_in_and_annotates_overlays(tmp_path):
    from larkscout_docreader import generate_visual_debug_artifacts

    docs_dir, doc_dir = _write_doc_fixture(tmp_path, with_ocr_blocks=True)
    ocr_sidecar = {
        "version": 1,
        "doc_id": "DOC-001",
        "coordinate_system": "image_pixels",
        "pages": [
            {
                "page": 1,
                "width": 400,
                "height": 200,
                "blocks": [
                    {
                        "block_id": "p1-b0001",
                        "text": "甲方",
                        "bbox": [80, 40, 160, 80],
                        "confidence": 0.9,
                    }
                ],
            }
        ],
    }
    (doc_dir / "ocr_blocks.json").write_text(json.dumps(ocr_sidecar), encoding="utf-8")
    tables = [
        {
            "table_id": "table-01",
            "page": 1,
            "bbox": [80, 40, 240, 140],
            "source": "layout",
        }
    ]
    (doc_dir / "tables.json").write_text(json.dumps(tables), encoding="utf-8")
    manifest_before = (doc_dir / "manifest.json").read_text(encoding="utf-8")

    metadata = generate_visual_debug_artifacts(docs_dir, "DOC-001", dpi=72)

    assert (doc_dir / "manifest.json").read_text(encoding="utf-8") == manifest_before
    assert metadata["opt_in"] is True
    assert metadata["artifact_dir"] == "derived/debug/"
    assert metadata["legend"]["ocr_blocks"] == "blue rectangles"
    assert metadata["legend"]["tables"] == "orange translucent rectangles"
    assert metadata["pages"] == [
        {
            "page": 1,
            "output_path": "derived/debug/page-0001.png",
            "dpi": 72,
            "ocr_block_count": 1,
            "table_region_count": 1,
        }
    ]
    assert (doc_dir / "derived/debug/page-0001.png").read_bytes().startswith(b"\x89PNG")
    written = json.loads((doc_dir / metadata["metadata_path"]).read_text(encoding="utf-8"))
    assert written["pages"][0]["output_path"] == "derived/debug/page-0001.png"
