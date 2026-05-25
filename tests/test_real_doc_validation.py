import json


def test_validate_doc_output_passes_for_sidecar_and_table_outputs(tmp_path):
    from scripts.real_doc_validation import validate_doc_output

    doc_dir = tmp_path / "NBS260336"
    (doc_dir / "tables").mkdir(parents=True)
    manifest = {
        "doc_id": "NBS260336",
        "parse_metadata": {
            "total_pages": 5,
            "ocr_page_count": 5,
            "quality_assessment": {
                "blank_pages": [],
                "near_blank_pages": [],
                "manual_blank_pages": [],
            },
            "ocr_plan": {"local_ocr_pages": [1, 2, 3, 4, 5], "llm_ocr_pages": []},
        },
        "sections": [{"sid": "a", "title": "A"}],
    }
    (doc_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (doc_dir / "sections.json").write_text(json.dumps(manifest["sections"]), encoding="utf-8")
    (doc_dir / "tables.json").write_text(
        json.dumps([{"id": "table-01", "file": "tables/table-01.md"}]),
        encoding="utf-8",
    )
    (doc_dir / "tables" / "table-01.json").write_text(
        json.dumps({"table_id": "table-01", "rows": []}),
        encoding="utf-8",
    )
    (doc_dir / "full.md").write_text("合同正文\n基调听云APM", encoding="utf-8")
    (doc_dir / "digest.md").write_text("digest", encoding="utf-8")
    (doc_dir / "brief.md").write_text("brief", encoding="utf-8")
    (doc_dir / "ocr_blocks.json").write_text(
        json.dumps(
            {
                "doc_id": "NBS260336",
                "coordinate_system": "image_pixels",
                "pages": [{"page": 1, "blocks": [{"text": "合同正文"}]}],
            }
        ),
        encoding="utf-8",
    )

    result = validate_doc_output(doc_dir, expected_table_sidecar=True)

    assert result["passed"] is True
    assert result["structured_table_json_count"] == 1
    assert result["ocr_blocks"]["block_count"] == 1
    assert result["checks"]["default_payloads_low_token"] is True


def test_validate_doc_output_flags_missing_table_sidecar_noise_and_blank_regression(tmp_path):
    from scripts.real_doc_validation import validate_doc_output

    doc_dir = tmp_path / "NBS250523"
    doc_dir.mkdir()
    manifest = {
        "doc_id": "NBS250523",
        "parse_metadata": {
            "total_pages": 3,
            "ocr_page_count": 3,
            "quality_assessment": {
                "blank_pages": [2],
                "near_blank_pages": [],
                "manual_blank_pages": [],
            },
            "ocr_plan": {"local_ocr_pages": [1, 2, 3], "llm_ocr_pages": []},
        },
        "sections": [{"sid": "a", "blocks": [{"bbox": [0, 0, 1, 1]}]}],
    }
    (doc_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (doc_dir / "sections.json").write_text(json.dumps(manifest["sections"]), encoding="utf-8")
    (doc_dir / "full.md").write_text("基调研云APM\nlava Agent\n", encoding="utf-8")

    result = validate_doc_output(doc_dir, expected_table_sidecar=True)

    assert result["passed"] is False
    assert result["checks"]["blank_pages_sane"] is False
    assert result["checks"]["table_sidecars_present"] is False
    assert result["checks"]["text_quality_clean"] is False
    assert result["checks"]["default_payloads_low_token"] is False
