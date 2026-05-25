#!/usr/bin/env python3
"""Validate real LarkScout document outputs for scanned-PDF sidecar rollout."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

try:
    from scripts.sidecar_metrics import collect_doc_metrics
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts.sidecar_metrics import collect_doc_metrics


OCR_NOISE_PATTERNS = (
    "[Tp]",
    "tblaeta",
    "eeaeee",
    "Tbabla",
    "基调研云APM",
    "基调所元Network",
    "lava Agent",
    "语吉探针",
    "第 4 页 / 共",
    "-第1页共",
)


def _read_json(path: Path) -> Any:
    if not path.exists() or not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _read_text(path: Path) -> str:
    if not path.exists() or not path.is_file():
        return ""
    return path.read_text(encoding="utf-8", errors="ignore")


def _parse_metadata(doc_dir: Path) -> dict[str, Any]:
    manifest = _read_json(doc_dir / "manifest.json")
    if isinstance(manifest, dict) and isinstance(manifest.get("parse_metadata"), dict):
        return manifest["parse_metadata"]
    meta = _read_json(doc_dir / ".meta.json")
    if isinstance(meta, dict) and isinstance(meta.get("parse_metadata"), dict):
        return meta["parse_metadata"]
    return {}


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _page_list(value: Any) -> list[int]:
    if not isinstance(value, list):
        return []
    pages: list[int] = []
    for item in value:
        page = _as_int(item)
        if page:
            pages.append(page)
    return pages


def _structured_table_json_count(doc_dir: Path) -> int:
    tables_dir = doc_dir / "tables"
    if not tables_dir.exists():
        return 0
    return sum(1 for path in tables_dir.glob("*.json") if path.is_file())


def _noise_hits(full_text: str) -> list[str]:
    return sorted(pattern for pattern in OCR_NOISE_PATTERNS if pattern in full_text)


def validate_doc_output(
    doc_dir: Path,
    *,
    expected_table_sidecar: bool = False,
) -> dict[str, Any]:
    manifest = _read_json(doc_dir / "manifest.json")
    manifest_dict = manifest if isinstance(manifest, dict) else {}
    manifest_parse_metadata = (
        manifest_dict.get("parse_metadata")
        if isinstance(manifest_dict.get("parse_metadata"), dict)
        else {}
    )
    meta = _read_json(doc_dir / ".meta.json")
    meta_dict = meta if isinstance(meta, dict) else {}
    parse_metadata = _parse_metadata(doc_dir)
    quality = (
        parse_metadata.get("quality_assessment")
        if isinstance(parse_metadata.get("quality_assessment"), dict)
        else {}
    )
    ocr_plan = (
        parse_metadata.get("ocr_plan") if isinstance(parse_metadata.get("ocr_plan"), dict) else {}
    )
    metrics = collect_doc_metrics(doc_dir)
    full_text = _read_text(doc_dir / "full.md")
    total_pages = _as_int(
        manifest_parse_metadata.get("total_pages")
        or parse_metadata.get("total_pages")
        or meta_dict.get("total_pages")
    )
    ocr_page_count = _as_int(
        manifest_parse_metadata.get("ocr_page_count")
        or parse_metadata.get("ocr_page_count")
        or meta_dict.get("ocr_page_count")
    )

    blank_pages = _page_list(quality.get("blank_pages"))
    near_blank_pages = _page_list(quality.get("near_blank_pages"))
    manual_blank_pages = _page_list(quality.get("manual_blank_pages"))
    ocr_targets = set(_page_list(ocr_plan.get("local_ocr_pages"))) | set(
        _page_list(ocr_plan.get("llm_ocr_pages"))
    )
    blank_candidates = set(blank_pages) | set(near_blank_pages) | set(manual_blank_pages)
    structured_table_count = _structured_table_json_count(doc_dir)
    noise = _noise_hits(full_text)
    ocr_blocks = metrics["sidecars"]["ocr_blocks"]
    checks = {
        "page_counts_sane": total_pages > 0 and 0 <= ocr_page_count <= total_pages,
        "layout_sidecar_sane": (
            not ocr_blocks["available"]
            or 0 < ocr_blocks["page_count"] <= max(total_pages, ocr_page_count, 1)
        ),
        "blank_pages_sane": all(1 <= page <= total_pages for page in blank_candidates)
        and not (blank_candidates & ocr_targets),
        "table_sidecars_present": (not expected_table_sidecar) or structured_table_count > 0,
        "text_quality_clean": not noise,
        "default_payloads_low_token": not (
            metrics["large_geometry_in_manifest"] or metrics["large_geometry_in_sections"]
        ),
    }
    return {
        "doc_id": doc_dir.name,
        "total_pages": total_pages,
        "ocr_page_count": ocr_page_count,
        "section_count": metrics["section_count"],
        "table_count": metrics["table_count"],
        "structured_table_json_count": structured_table_count,
        "ocr_blocks": ocr_blocks,
        "blank_pages": blank_pages,
        "near_blank_pages": near_blank_pages,
        "manual_blank_pages": manual_blank_pages,
        "noise_hits": noise,
        "default_payload_bytes": sum(metrics["default_payload_bytes"].values()),
        "large_geometry_in_default_payloads": not checks["default_payloads_low_token"],
        "checks": checks,
        "passed": all(checks.values()),
    }


def validate_docs(
    docs_dir: Path,
    doc_ids: list[str],
    *,
    expected_table_sidecars: set[str] | None = None,
) -> dict[str, Any]:
    expected_table_sidecars = expected_table_sidecars or set()
    documents = [
        validate_doc_output(docs_dir / doc_id, expected_table_sidecar=doc_id in expected_table_sidecars)
        for doc_id in doc_ids
    ]
    return {
        "docs_dir": str(docs_dir),
        "doc_count": len(documents),
        "documents": documents,
        "passed": all(doc["passed"] for doc in documents),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("docs_dir", type=Path)
    parser.add_argument("doc_ids", nargs="+")
    parser.add_argument("--expect-table", action="append", default=[])
    args = parser.parse_args()
    result = validate_docs(
        args.docs_dir,
        args.doc_ids,
        expected_table_sidecars=set(args.expect_table),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
