import json


def test_collect_doc_metrics_keeps_geometry_out_of_default_payloads(tmp_path):
    from scripts.sidecar_metrics import collect_doc_metrics

    doc_dir = tmp_path / "DOC-001"
    doc_dir.mkdir()
    (doc_dir / "digest.md").write_text("short", encoding="utf-8")
    (doc_dir / "brief.md").write_text("brief", encoding="utf-8")
    (doc_dir / "full.md").write_text("full", encoding="utf-8")
    manifest = {
        "doc_id": "DOC-001",
        "paths": {"ocr_blocks": "ocr_blocks.json"},
        "layout": {"available": True, "ocr_blocks_path": "ocr_blocks.json"},
        "sections": [{"sid": "a", "file": "sections/01-a.md"}],
    }
    (doc_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (doc_dir / "sections.json").write_text(json.dumps(manifest["sections"]), encoding="utf-8")
    (doc_dir / "tables.json").write_text(json.dumps([]), encoding="utf-8")
    sidecar = {
        "version": 1,
        "doc_id": "DOC-001",
        "coordinate_system": "image_pixels",
        "pages": [
            {
                "page": 1,
                "width": 100,
                "height": 100,
                "blocks": [{"block_id": "p1-b0001", "text": "A", "bbox": [1, 2, 3, 4]}],
            }
        ],
    }
    (doc_dir / "ocr_blocks.json").write_text(json.dumps(sidecar), encoding="utf-8")

    metrics = collect_doc_metrics(doc_dir)

    assert metrics["doc_id"] == "DOC-001"
    assert metrics["sidecars"]["ocr_blocks"]["available"] is True
    assert metrics["sidecars"]["ocr_blocks"]["page_count"] == 1
    assert metrics["sidecars"]["ocr_blocks"]["block_count"] == 1
    assert metrics["large_geometry_in_manifest"] is False
    assert metrics["large_geometry_in_sections"] is False
    assert metrics["default_payload_bytes"]["manifest"] > 0
    assert metrics["sidecars"]["ocr_blocks"]["bytes"] > 0


def test_collect_metrics_flags_default_geometry_regression(tmp_path):
    from scripts.sidecar_metrics import collect_metrics

    doc_dir = tmp_path / "DOC-002"
    doc_dir.mkdir()
    (doc_dir / "manifest.json").write_text(
        json.dumps({"doc_id": "DOC-002", "blocks": [{"bbox": [0, 0, 1, 1]}]}),
        encoding="utf-8",
    )
    (doc_dir / "sections.json").write_text(json.dumps([]), encoding="utf-8")

    metrics = collect_metrics(tmp_path)

    assert metrics["doc_count"] == 1
    assert metrics["summary"]["large_geometry_in_default_payloads"] is True
