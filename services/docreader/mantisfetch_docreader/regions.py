"""PDF region cropping, region re-OCR, and visual debug artifacts.

Backs the /library/{doc_id} region-crop, region-OCR-rerun, and debug-overlay
endpoints: resolve a stored document's source PDF, clip a bbox to a pixmap,
optionally re-run OCR on the crop, and render debug overlays. fitz (PyMuPDF)
and PIL are imported lazily inside the functions, matching the rest of the
package.

Three package helpers are pulled in via function-level relative imports to
avoid an import cycle and to honour test monkeypatches on the facade:
_resolve_doc_dir, _validate_doc_id (storage/validation, shared), and
local_ocr_with_layout (tests patch it at mantisfetch_docreader.*).
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from i18n import t
from mantisfetch_common.atomic import _write_bytes, _write_json, _write_text

from .models import (
    OCR_BLOCKS_COORDINATE_SYSTEM,
    OCR_BLOCKS_SIDECAR_PATH,
    OCRPageBlocks,
    _normalize_layout_bbox,
)
from .ocr.engines import LOCAL_OCR_ENABLED, gemini_ocr

CROP_ARTIFACT_DIR = "derived/crops"
REGION_OCR_ARTIFACT_DIR = "derived/region_ocr"
VISUAL_DEBUG_ARTIFACT_DIR = "derived/debug"

def _normalize_crop_bbox(bbox: list[float] | tuple[float, float, float, float]) -> list[float]:
    if len(bbox) != 4:
        raise HTTPException(422, "crop bbox must contain exactly four coordinates")
    normalized = [float(v) for v in bbox]
    if not all(math.isfinite(v) for v in normalized):
        raise HTTPException(422, "crop bbox coordinates must be finite numbers")
    x0, y0, x1, y1 = normalized
    if x1 <= x0 or y1 <= y0:
        raise HTTPException(422, "crop bbox must have positive area ordered as [x0, y0, x1, y1]")
    return normalized


def _load_manifest_dict(doc_dir: Path, doc_id: str) -> dict[str, Any]:
    manifest_path = doc_dir / "manifest.json"
    if not manifest_path.exists():
        raise HTTPException(404, t("doc_not_found", doc_id=doc_id))
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(500, f"manifest unreadable for {doc_id}: {exc}") from exc
    if not isinstance(manifest, dict):
        raise HTTPException(500, f"manifest unreadable for {doc_id}")
    return manifest


def _resolve_doc_source_file(docs_dir: Path, doc_id: str) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    from . import _resolve_doc_dir, _validate_doc_id

    _validate_doc_id(doc_id)
    doc_dir = _resolve_doc_dir(docs_dir, doc_id)
    manifest = _load_manifest_dict(doc_dir, doc_id)
    source_file = manifest.get("source_file") if isinstance(manifest.get("source_file"), dict) else {}
    source_ref = str(source_file.get("ref") or "")
    if not source_ref:
        raise HTTPException(409, f"source file is not available for {doc_id}")
    raw_ref = Path(source_ref)
    if raw_ref.is_absolute() or ".." in raw_ref.parts or not raw_ref.parts or raw_ref.parts[0] != "source":
        raise HTTPException(500, f"invalid source file ref for {doc_id}: {source_ref}")
    source_path = (doc_dir / raw_ref).resolve()
    try:
        source_path.relative_to(doc_dir.resolve())
    except ValueError as exc:
        raise HTTPException(500, f"invalid source file ref for {doc_id}: {source_ref}") from exc
    if not source_path.exists() or not source_path.is_file():
        raise HTTPException(404, f"source file missing for {doc_id}: {source_ref}")
    return source_path, manifest, source_file


def _ocr_page_dimensions(doc_dir: Path, page_num: int) -> tuple[float, float] | None:
    sidecar_path = doc_dir / OCR_BLOCKS_SIDECAR_PATH
    if not sidecar_path.exists():
        return None
    try:
        sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    pages = sidecar.get("pages") if isinstance(sidecar, dict) else []
    if not isinstance(pages, list):
        return None
    for page in pages:
        if isinstance(page, dict) and int(page.get("page") or 0) == page_num:
            width = float(page.get("width") or 0)
            height = float(page.get("height") or 0)
            if width > 0 and height > 0:
                return width, height
    return None


def _ensure_bbox_inside_bounds(bbox: list[float], width: float, height: float, coordinate_system: str) -> None:
    x0, y0, x1, y1 = bbox
    if x0 < 0 or y0 < 0 or x1 > width or y1 > height:
        raise HTTPException(
            422,
            f"crop bbox outside {coordinate_system} bounds: width={width:g}, height={height:g}",
        )


def _crop_clip_rect(
    doc_dir: Path,
    page_obj: Any,
    page_num: int,
    bbox: list[float],
    coordinate_system: str,
) -> tuple[Any, list[float], dict[str, Any]]:
    import fitz

    rect = page_obj.rect
    if coordinate_system == "page_points":
        _ensure_bbox_inside_bounds(bbox, float(rect.width), float(rect.height), coordinate_system)
        clip = fitz.Rect(
            rect.x0 + bbox[0],
            rect.y0 + bbox[1],
            rect.x0 + bbox[2],
            rect.y0 + bbox[3],
        )
        return clip, [float(clip.x0), float(clip.y0), float(clip.x1), float(clip.y1)], {
            "width": float(rect.width),
            "height": float(rect.height),
            "unit": "points",
        }
    if coordinate_system != OCR_BLOCKS_COORDINATE_SYSTEM:
        raise HTTPException(422, f"unsupported crop coordinate_system: {coordinate_system}")
    dimensions = _ocr_page_dimensions(doc_dir, page_num)
    if dimensions is None:
        raise HTTPException(409, f"ocr block dimensions unavailable for {doc_dir.name} page {page_num}")
    image_width, image_height = dimensions
    _ensure_bbox_inside_bounds(bbox, image_width, image_height, coordinate_system)
    x_scale = float(rect.width) / image_width
    y_scale = float(rect.height) / image_height
    clip = fitz.Rect(
        rect.x0 + bbox[0] * x_scale,
        rect.y0 + bbox[1] * y_scale,
        rect.x0 + bbox[2] * x_scale,
        rect.y0 + bbox[3] * y_scale,
    )
    return clip, [float(clip.x0), float(clip.y0), float(clip.x1), float(clip.y1)], {
        "width": image_width,
        "height": image_height,
        "unit": "pixels",
    }


def export_pdf_region_crop(
    docs_dir: Path,
    doc_id: str,
    page: int,
    bbox: list[float] | tuple[float, float, float, float],
    *,
    dpi: int = 144,
    coordinate_system: str = OCR_BLOCKS_COORDINATE_SYSTEM,
) -> dict[str, Any]:
    import fitz

    from . import _resolve_doc_dir

    try:
        page_num = int(page)
    except (TypeError, ValueError) as exc:
        raise HTTPException(422, "page must be a 1-based positive integer") from exc
    if page_num < 1:
        raise HTTPException(422, "page must be a 1-based positive integer")
    try:
        dpi = int(dpi)
    except (TypeError, ValueError) as exc:
        raise HTTPException(422, "dpi must be between 36 and 600") from exc
    if dpi < 36 or dpi > 600:
        raise HTTPException(422, "dpi must be between 36 and 600")
    normalized_bbox = _normalize_crop_bbox(bbox)
    source_path, manifest, source_file = _resolve_doc_source_file(docs_dir, doc_id)
    if str(manifest.get("file_type") or "").lower() != "pdf" and source_path.suffix.lower() != ".pdf":
        raise HTTPException(422, "region crop export currently supports PDF source files")

    doc_dir = _resolve_doc_dir(docs_dir, doc_id)
    try:
        pdf = fitz.open(str(source_path))
    except Exception as exc:
        raise HTTPException(500, f"source PDF could not be opened for {doc_id}: {exc}") from exc
    try:
        if page_num > len(pdf):
            raise HTTPException(422, f"page out of range for {doc_id}: {page_num} > {len(pdf)}")
        page_obj = pdf[page_num - 1]
        clip, clip_rect, source_bounds = _crop_clip_rect(
            doc_dir,
            page_obj,
            page_num,
            normalized_bbox,
            coordinate_system,
        )
        scale = dpi / 72.0
        pix = page_obj.get_pixmap(matrix=fitz.Matrix(scale, scale), clip=clip, alpha=False)
        png_bytes = pix.tobytes("png")
    finally:
        pdf.close()

    crop_hash = hashlib.sha256(
        json.dumps(
            {
                "doc_id": doc_id,
                "page": page_num,
                "bbox": normalized_bbox,
                "dpi": dpi,
                "coordinate_system": coordinate_system,
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:16]
    artifact_id = f"crop-p{page_num:04d}-{crop_hash}"
    crops_dir = doc_dir / CROP_ARTIFACT_DIR
    crops_dir.mkdir(parents=True, exist_ok=True)
    output_rel = f"{CROP_ARTIFACT_DIR}/{artifact_id}.png"
    metadata_rel = f"{CROP_ARTIFACT_DIR}/{artifact_id}.json"
    metadata = {
        "artifact_id": artifact_id,
        "doc_id": doc_id,
        "page": page_num,
        "bbox": normalized_bbox,
        "coordinate_system": coordinate_system,
        "source_bounds": source_bounds,
        "clip_rect": clip_rect,
        "dpi": dpi,
        "scale": scale,
        "output_path": output_rel,
        "metadata_path": metadata_rel,
        "mime_type": "image/png",
        "size_bytes": len(png_bytes),
        "sha256": hashlib.sha256(png_bytes).hexdigest(),
        "source_ref": source_file.get("ref", ""),
        "derived": True,
        "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    _write_bytes(doc_dir / output_rel, png_bytes)
    _write_json(doc_dir / metadata_rel, metadata)
    return metadata


def _normalize_region_ocr_backend(backend: str) -> tuple[str, str]:
    name = (backend or "").strip().lower()
    if name in {"", "paddleocr", "local", "local-paddleocr"}:
        if not LOCAL_OCR_ENABLED:
            # Local OCR is off in this build — serve the region via the LLM provider
            # instead of a local worker that isn't there (response reports both the
            # requested and the selected backend).
            return "llm", "gemini"
        return "local", "paddleocr"
    if name in {"llm", "gemini"}:
        return "llm", "gemini"
    raise HTTPException(422, "region OCR backend must be one of: paddleocr, local, llm, gemini")


def _safe_artifact_id(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-._")
    return safe[:80] or "artifact"


def rerun_region_ocr(
    docs_dir: Path,
    doc_id: str,
    page: int,
    bbox: list[float] | tuple[float, float, float, float],
    *,
    backend: str = "paddleocr",
    dpi: int = 144,
    coordinate_system: str = OCR_BLOCKS_COORDINATE_SYSTEM,
    run_id: str | None = None,
) -> dict[str, Any]:
    from . import _resolve_doc_dir, _validate_doc_id, local_ocr_with_layout

    backend_kind, selected_backend = _normalize_region_ocr_backend(backend)
    requested_artifact_id = _safe_artifact_id(run_id) if run_id else None
    if requested_artifact_id:
        _validate_doc_id(doc_id)
        doc_dir = _resolve_doc_dir(docs_dir, doc_id)
        text_rel = f"{REGION_OCR_ARTIFACT_DIR}/{requested_artifact_id}.txt"
        metadata_rel = f"{REGION_OCR_ARTIFACT_DIR}/{requested_artifact_id}.json"
        if (doc_dir / text_rel).exists() or (doc_dir / metadata_rel).exists():
            raise HTTPException(409, f"region OCR artifact already exists: {requested_artifact_id}")
    crop = export_pdf_region_crop(
        docs_dir,
        doc_id,
        page,
        bbox,
        dpi=dpi,
        coordinate_system=coordinate_system,
    )
    doc_dir = _resolve_doc_dir(docs_dir, doc_id)
    crop_path = doc_dir / crop["output_path"]
    image_bytes = crop_path.read_bytes()

    page_num = int(crop["page"])
    page_blocks: OCRPageBlocks | None = None
    if backend_kind == "local":
        text, page_blocks = local_ocr_with_layout(image_bytes, page_num, selected_backend)
    else:
        text = gemini_ocr(image_bytes, page_num).strip()

    block_dicts = [block.to_dict() for block in page_blocks.blocks] if page_blocks else []
    confidences = [float(block.get("confidence") or 0.0) for block in block_dicts]
    confidence = round(sum(confidences) / len(confidences), 4) if confidences else None

    if requested_artifact_id:
        artifact_id = requested_artifact_id
    else:
        run_hash = hashlib.sha256(
            f"{doc_id}:{page_num}:{crop['artifact_id']}:{backend_kind}:{selected_backend}:{time.time_ns()}".encode()
        ).hexdigest()[:16]
        artifact_id = f"region-ocr-p{page_num:04d}-{run_hash}"
    region_dir = doc_dir / REGION_OCR_ARTIFACT_DIR
    region_dir.mkdir(parents=True, exist_ok=True)
    text_rel = f"{REGION_OCR_ARTIFACT_DIR}/{artifact_id}.txt"
    metadata_rel = f"{REGION_OCR_ARTIFACT_DIR}/{artifact_id}.json"
    if (doc_dir / text_rel).exists() or (doc_dir / metadata_rel).exists():
        raise HTTPException(409, f"region OCR artifact already exists: {artifact_id}")

    metadata = {
        "artifact_id": artifact_id,
        "doc_id": doc_id,
        "page": page_num,
        "bbox": crop["bbox"],
        "coordinate_system": crop["coordinate_system"],
        "text": text,
        "confidence": confidence,
        "blocks": block_dicts,
        "backend": {
            "requested": backend,
            "kind": backend_kind,
            "selected": selected_backend,
            "dpi": dpi,
        },
        "crop": {
            "artifact_id": crop["artifact_id"],
            "output_path": crop["output_path"],
            "metadata_path": crop["metadata_path"],
            "sha256": crop["sha256"],
        },
        "source_ref": crop.get("source_ref", ""),
        "text_path": text_rel,
        "metadata_path": metadata_rel,
        "derived": True,
        "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    _write_text(doc_dir / text_rel, text + ("\n" if text and not text.endswith("\n") else ""))
    _write_json(doc_dir / metadata_rel, metadata)
    return metadata


def _load_ocr_debug_overlays(doc_dir: Path) -> dict[int, dict[str, Any]]:
    sidecar_path = doc_dir / OCR_BLOCKS_SIDECAR_PATH
    if not sidecar_path.exists():
        return {}
    try:
        sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    overlays: dict[int, dict[str, Any]] = {}
    for page in sidecar.get("pages") or []:
        if not isinstance(page, dict):
            continue
        page_num = int(page.get("page") or 0)
        width = float(page.get("width") or 0)
        height = float(page.get("height") or 0)
        if page_num < 1 or width <= 0 or height <= 0:
            continue
        overlays[page_num] = {
            "width": width,
            "height": height,
            "blocks": [block for block in (page.get("blocks") or []) if isinstance(block, dict)],
        }
    return overlays


def _load_table_debug_overlays(doc_dir: Path) -> dict[int, list[dict[str, Any]]]:
    tables_path = doc_dir / "tables.json"
    if not tables_path.exists():
        return {}
    try:
        tables = json.loads(tables_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    if not isinstance(tables, list):
        return {}
    overlays: dict[int, list[dict[str, Any]]] = {}
    for table in tables:
        if not isinstance(table, dict) or not isinstance(table.get("bbox"), list):
            continue
        page_num = int(table.get("page") or table.get("page_start") or 0)
        if page_num < 1:
            continue
        overlays.setdefault(page_num, []).append(table)
    return overlays


def _debug_bbox_to_pixels(
    bbox: list[float],
    *,
    source_width: float,
    source_height: float,
    target_width: int,
    target_height: int,
) -> list[float] | None:
    try:
        x0, y0, x1, y1 = _normalize_layout_bbox(bbox)
    except (TypeError, ValueError):
        return None
    if source_width <= 0 or source_height <= 0:
        return None
    return [
        max(0.0, min(float(target_width), x0 * target_width / source_width)),
        max(0.0, min(float(target_height), y0 * target_height / source_height)),
        max(0.0, min(float(target_width), x1 * target_width / source_width)),
        max(0.0, min(float(target_height), y1 * target_height / source_height)),
    ]


def generate_visual_debug_artifacts(
    docs_dir: Path,
    doc_id: str,
    *,
    dpi: int = 144,
    include_ocr_blocks: bool = True,
    include_tables: bool = True,
) -> dict[str, Any]:
    import fitz
    from PIL import Image, ImageDraw

    from . import _resolve_doc_dir

    dpi = int(dpi)
    if dpi < 36 or dpi > 600:
        raise HTTPException(422, "dpi must be between 36 and 600")
    source_path, _manifest, source_file = _resolve_doc_source_file(docs_dir, doc_id)
    doc_dir = _resolve_doc_dir(docs_dir, doc_id)
    # Always load the OCR sidecar: its per-page width/height is the pixel space
    # table bboxes live in, needed to scale table overlays even when OCR blocks
    # are not drawn. Block drawing (and OCR-only page rendering) stays gated on
    # include_ocr_blocks below.
    ocr_pages = _load_ocr_debug_overlays(doc_dir)
    table_pages = _load_table_debug_overlays(doc_dir) if include_tables else {}
    pages_to_render = sorted((set(ocr_pages) if include_ocr_blocks else set()) | set(table_pages))

    debug_dir = doc_dir / VISUAL_DEBUG_ARTIFACT_DIR
    debug_dir.mkdir(parents=True, exist_ok=True)
    page_entries: list[dict[str, Any]] = []
    scale = dpi / 72.0
    try:
        pdf = fitz.open(str(source_path))
    except Exception as exc:
        raise HTTPException(500, f"source PDF could not be opened for {doc_id}: {exc}") from exc
    try:
        for page_num in pages_to_render:
            if page_num > len(pdf):
                continue
            page_obj = pdf[page_num - 1]
            pix = page_obj.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
            image = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
            draw = ImageDraw.Draw(image, "RGBA")
            line_width = max(2, int(scale * 2))
            ocr_page = ocr_pages.get(page_num) or {}
            ocr_count = 0
            if include_ocr_blocks:
                for block in ocr_page.get("blocks") or []:
                    rect = _debug_bbox_to_pixels(
                        block.get("bbox") or [],
                        source_width=float(ocr_page.get("width") or 0),
                        source_height=float(ocr_page.get("height") or 0),
                        target_width=image.width,
                        target_height=image.height,
                    )
                    if rect is None:
                        continue
                    draw.rectangle(rect, outline=(0, 102, 255, 230), width=line_width)
                    ocr_count += 1
            table_count = 0
            # Table bboxes are in image-pixel space (the OCR render), so scale
            # them against the OCR sidecar page dimensions — now loaded even when
            # include_ocr_blocks=False (the image.width fallback only applies if
            # the sidecar is missing entirely).
            for table in table_pages.get(page_num) or []:
                rect = _debug_bbox_to_pixels(
                    table.get("bbox") or [],
                    source_width=float(ocr_page.get("width") or image.width),
                    source_height=float(ocr_page.get("height") or image.height),
                    target_width=image.width,
                    target_height=image.height,
                )
                if rect is None:
                    continue
                draw.rectangle(rect, fill=(255, 128, 0, 35), outline=(255, 96, 0, 255), width=line_width + 1)
                draw.text((rect[0] + 4, max(0, rect[1] - 14)), str(table.get("table_id") or "table"), fill=(255, 96, 0, 255))
                table_count += 1
            output_rel = f"{VISUAL_DEBUG_ARTIFACT_DIR}/page-{page_num:04d}.png"
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            _write_bytes(doc_dir / output_rel, buffer.getvalue())
            page_entries.append(
                {
                    "page": page_num,
                    "output_path": output_rel,
                    "dpi": dpi,
                    "ocr_block_count": ocr_count,
                    "table_region_count": table_count,
                }
            )
    finally:
        pdf.close()

    metadata_rel = f"{VISUAL_DEBUG_ARTIFACT_DIR}/manifest.json"
    metadata = {
        "doc_id": doc_id,
        "metadata_path": metadata_rel,
        "artifact_dir": f"{VISUAL_DEBUG_ARTIFACT_DIR}/",
        "source_ref": source_file.get("ref", ""),
        "derived": True,
        "opt_in": True,
        "coordinate_system": OCR_BLOCKS_COORDINATE_SYSTEM,
        "legend": {
            "ocr_blocks": "blue rectangles",
            "tables": "orange translucent rectangles",
        },
        "options": {
            "dpi": dpi,
            "include_ocr_blocks": include_ocr_blocks,
            "include_tables": include_tables,
        },
        "pages": page_entries,
        "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    _write_json(doc_dir / metadata_rel, metadata)
    return metadata


