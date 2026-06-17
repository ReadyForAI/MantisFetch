#!/usr/bin/env python3
"""Collect payload-size and sidecar metrics for MantisFetch document outputs."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

DEFAULT_ENDPOINT_FILES = {
    "manifest": "manifest.json",
    "digest": "digest.md",
    "brief": "brief.md",
    "full": "full.md",
    "sections": "sections.json",
    "tables": "tables.json",
}


def _file_size(path: Path) -> int:
    return path.stat().st_size if path.exists() and path.is_file() else 0


def _read_json(path: Path) -> Any:
    if not path.exists() or not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        # Skip a corrupt/unreadable file instead of crashing the whole batch.
        print(f"warning: skipping unreadable JSON {path}: {exc}", file=sys.stderr)
        return None


def _contains_large_geometry(value: Any) -> bool:
    if isinstance(value, dict):
        if isinstance(value.get("blocks"), list):
            return True
        if isinstance(value.get("ocr_blocks"), list):
            return True
        return any(_contains_large_geometry(child) for child in value.values())
    if isinstance(value, list):
        return any(_contains_large_geometry(child) for child in value)
    return False


def _ocr_sidecar_summary(doc_dir: Path) -> dict[str, Any]:
    sidecar = _read_json(doc_dir / "ocr_blocks.json")
    if not isinstance(sidecar, dict):
        return {
            "available": False,
            "bytes": 0,
            "page_count": 0,
            "block_count": 0,
            "coordinate_system": "",
        }
    pages = sidecar.get("pages") if isinstance(sidecar.get("pages"), list) else []
    block_count = 0
    for page in pages:
        if isinstance(page, dict) and isinstance(page.get("blocks"), list):
            block_count += len(page["blocks"])
    return {
        "available": True,
        "bytes": _file_size(doc_dir / "ocr_blocks.json"),
        "page_count": len(pages),
        "block_count": block_count,
        "coordinate_system": str(sidecar.get("coordinate_system") or ""),
    }


def collect_doc_metrics(doc_dir: Path) -> dict[str, Any]:
    started = time.perf_counter()
    manifest = _read_json(doc_dir / "manifest.json")
    sections = _read_json(doc_dir / "sections.json")
    tables = _read_json(doc_dir / "tables.json")
    default_payload_bytes = {
        name: _file_size(doc_dir / rel_path) for name, rel_path in DEFAULT_ENDPOINT_FILES.items()
    }
    sidecars = {
        "ocr_blocks": _ocr_sidecar_summary(doc_dir),
        "tables_json_bytes": _file_size(doc_dir / "tables.json"),
        "structured_table_json_bytes": sum(path.stat().st_size for path in (doc_dir / "tables").glob("*.json"))
        if (doc_dir / "tables").exists()
        else 0,
        "debug_bytes": sum(path.stat().st_size for path in (doc_dir / "derived" / "debug").glob("*"))
        if (doc_dir / "derived" / "debug").exists()
        else 0,
        "region_ocr_bytes": sum(path.stat().st_size for path in (doc_dir / "derived" / "region_ocr").glob("*"))
        if (doc_dir / "derived" / "region_ocr").exists()
        else 0,
        "crop_bytes": sum(path.stat().st_size for path in (doc_dir / "derived" / "crops").glob("*"))
        if (doc_dir / "derived" / "crops").exists()
        else 0,
    }
    return {
        "doc_id": doc_dir.name,
        "default_payload_bytes": default_payload_bytes,
        "sidecars": sidecars,
        "table_count": len(tables) if isinstance(tables, list) else 0,
        "section_count": len(sections) if isinstance(sections, list) else 0,
        "large_geometry_in_manifest": _contains_large_geometry(manifest),
        "large_geometry_in_sections": _contains_large_geometry(sections),
        "collection_ms": round((time.perf_counter() - started) * 1000, 3),
    }


def collect_metrics(docs_dir: Path, doc_ids: list[str] | None = None) -> dict[str, Any]:
    if doc_ids:
        doc_dirs = [docs_dir / doc_id for doc_id in doc_ids]
    else:
        doc_dirs = sorted(path for path in docs_dir.iterdir() if path.is_dir() and (path / "manifest.json").exists())
    documents = [collect_doc_metrics(doc_dir) for doc_dir in doc_dirs if doc_dir.exists()]
    return {
        "docs_dir": str(docs_dir),
        "doc_count": len(documents),
        "documents": documents,
        "summary": {
            "default_payload_bytes": sum(
                sum(doc["default_payload_bytes"].values()) for doc in documents
            ),
            "ocr_blocks_bytes": sum(doc["sidecars"]["ocr_blocks"]["bytes"] for doc in documents),
            "ocr_block_count": sum(doc["sidecars"]["ocr_blocks"]["block_count"] for doc in documents),
            "large_geometry_in_default_payloads": any(
                doc["large_geometry_in_manifest"] or doc["large_geometry_in_sections"]
                for doc in documents
            ),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("docs_dir", type=Path)
    parser.add_argument("doc_ids", nargs="*")
    args = parser.parse_args()
    print(json.dumps(collect_metrics(args.docs_dir, args.doc_ids or None), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
