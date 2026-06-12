"""PDF OCR planning: page analysis, render-scale capping, and the OCR plan.

Pure decision logic that sits in front of `parse_pdf` (pdf.py) — it inspects
pages and a document profile and decides what to OCR and how:

- page-range parsing (`_parse_page_range`, `_metadata_page_range_spec`),
- per-page OCR detection + blank detection (`_should_ocr`, `_page_blank_signal`),
- render-scale capping to a pixel budget (`_resolve_ocr_render_scale`),
- parse-mode resolution and contract/quality classification
  (`_resolve_pdf_parse_mode`, `_classify_contract_text`, `_assess_contract_quality`),
- the OCR plan itself (`_plan_pdf_ocr`) and the cheap prewarm probe
  (`_should_prewarm_local_ocr_for_pdf`).

Self-contained leaf: depends only on the domain models, the stdlib, and
PyMuPDF/Pillow (imported lazily inside the functions that render pixmaps).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .models import DocumentProfile, QualityPolicy


def _parse_page_range(spec: str, total_pages: int) -> set[int]:
    """Parse page range spec: "10-30" or "5,10-15,20"."""
    pages = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            start = max(1, int(a.strip()))
            end = min(total_pages, int(b.strip()))
            pages.update(range(start, end + 1))
        else:
            p = int(part.strip())
            if 1 <= p <= total_pages:
                pages.add(p)
    return pages


def _metadata_page_range_spec(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, (list, tuple)):
        parts = [str(v).strip() for v in value if str(v).strip()]
        return ",".join(parts) or None
    return str(value).strip() or None


def _should_ocr(page, text: str, threshold: int) -> bool:
    """
    Multi-signal OCR detection:
      Signal 1: too little text
      Signal 2: page has images and text is sparse (scan indicator)
      Signal 3: low useful-character ratio (garbled or mostly whitespace)
    """
    if len(text) < threshold:
        return True
    try:
        images = page.get_images(full=False)
        if len(images) > 0 and len(text) < threshold * 3:
            return True
    except Exception:
        pass
    if len(text) > 0:
        useful = sum(1 for c in text if c.isalnum() or "一" <= c <= "鿿")
        if useful / len(text) < 0.3 and len(text) < threshold * 5:
            return True
    return False


def _page_render_pixels(page: Any, scale: float) -> int:
    rect = page.rect
    return max(1, int(rect.width * scale)) * max(1, int(rect.height * scale))


def _resolve_ocr_render_scale(
    page: Any,
    requested_scale: float,
    max_pixels: int,
    min_scale: float,
) -> tuple[float, int, bool]:
    requested_scale = max(0.5, float(requested_scale))
    min_scale = min(requested_scale, max(0.5, float(min_scale)))
    max_pixels = max(1, int(max_pixels))
    requested_pixels = _page_render_pixels(page, requested_scale)
    if requested_pixels <= max_pixels:
        return requested_scale, requested_pixels, False

    rect = page.rect
    base_area = max(1.0, float(rect.width) * float(rect.height))
    capped_scale = (max_pixels / base_area) ** 0.5
    scale = max(min_scale, min(requested_scale, capped_scale))
    return scale, _page_render_pixels(page, scale), scale < requested_scale


def _page_blank_signal(page: Any, *, scale: float = 0.5) -> dict[str, Any]:
    import fitz
    from PIL import Image, ImageOps

    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
    gray = ImageOps.grayscale(img)
    hist = gray.histogram()
    total = max(1, gray.width * gray.height)
    nonwhite_ratio = sum(hist[:245]) / total
    dark_ratio = sum(hist[:180]) / total
    return {
        "blank_like": dark_ratio < 0.00002 and nonwhite_ratio < 0.001,
        "nonwhite_ratio": nonwhite_ratio,
        "dark_ratio": dark_ratio,
    }


def _resolve_pdf_parse_mode(profile: DocumentProfile | None, requested_mode: str | None) -> str:
    mode = (requested_mode or "").strip().lower()
    if not mode and profile:
        mode = profile.upgrade_policy.default_mode
    if not mode:
        mode = os.environ.get("LARKSCOUT_PDF_PARSE_MODE", "accurate").strip().lower()
    allowed = {"fast", "accurate", "full"}
    if mode not in allowed:
        raise RuntimeError("PDF parse mode must be one of: fast, accurate, full.")
    return mode


def _classify_contract_text(
    text: str,
    profile: DocumentProfile | None,
) -> tuple[bool, list[str]]:
    required_terms = profile.classification.required_terms if profile else ()
    if not required_terms:
        return True, []
    matched_terms = [term for term in required_terms if term and term in text]
    return bool(matched_terms), matched_terms


def _assess_contract_quality(
    markdown_text: str,
    page_signals: list[dict[str, Any]],
    profile: DocumentProfile | None,
) -> dict[str, Any]:
    quality_policy = profile.quality_policy if profile else QualityPolicy()
    total_pages = len(page_signals)
    sparse_pages = [s["page_num"] for s in page_signals if s["text_len"] < quality_policy.sparse_text_chars]
    usable_pages = [s["page_num"] for s in page_signals if s["text_len"] >= quality_policy.usable_text_chars]
    image_pages = [s["page_num"] for s in page_signals if s["image_count"] > 0]
    scan_like_pages = [s["page_num"] for s in page_signals if s["scan_like"]]
    blank_pages = [s["page_num"] for s in page_signals if s.get("blank_like")]
    manual_blank_pages = [s["page_num"] for s in page_signals if s.get("blank_override")]

    scan_ratio = len(scan_like_pages) / max(total_pages, 1)
    mixed_ratio = len(sparse_pages) / max(total_pages, 1)
    if scan_ratio >= quality_policy.scan_page_ratio:
        document_quality = "scan_only"
    elif mixed_ratio >= quality_policy.mixed_page_ratio:
        document_quality = "mixed"
    else:
        document_quality = "text"

    is_contract, matched_terms = _classify_contract_text(markdown_text, profile)

    return {
        "profile": profile.name if profile else None,
        "is_contract": is_contract,
        "matched_terms": matched_terms,
        "document_quality": document_quality,
        "scan_ratio": scan_ratio,
        "sparse_pages": sparse_pages,
        "usable_pages": usable_pages,
        "image_pages": image_pages,
        "scan_like_pages": scan_like_pages,
        "blank_pages": blank_pages,
        "near_blank_pages": blank_pages,
        "manual_blank_pages": manual_blank_pages,
        "page_signals": page_signals,
    }


def _plan_pdf_ocr(
    *,
    profile: DocumentProfile | None,
    parse_mode: str,
    force_ocr: bool,
    explicit_ocr_pages: set[int] | None,
    assessment: dict[str, Any],
) -> dict[str, Any]:
    quality = assessment.get("document_quality") or "text"
    scan_like_pages = set(assessment.get("scan_like_pages") or [])
    sparse_pages = set(assessment.get("sparse_pages") or [])
    blank_pages = set(assessment.get("blank_pages") or assessment.get("near_blank_pages") or [])
    problem_pages = (scan_like_pages | sparse_pages) - blank_pages

    local_backend = profile.upgrade_policy.local_ocr_backend if profile else "paddleocr"
    local_ocr_pages: set[int] = set()
    llm_ocr_pages: set[int] = set()
    region_llm = False
    proofread = False

    if explicit_ocr_pages:
        llm_ocr_pages |= set(explicit_ocr_pages)
        if parse_mode in {"fast", "accurate"} and quality in {"scan_only", "mixed"}:
            local_ocr_pages |= problem_pages
            region_llm = bool(
                parse_mode == "accurate"
                and profile
                and parse_mode in profile.upgrade_policy.region_llm_modes
            )
    elif force_ocr:
        llm_ocr_pages = set(scan_like_pages or sparse_pages or assessment.get("image_pages") or []) - blank_pages
        if not llm_ocr_pages:
            llm_ocr_pages = {
                signal["page_num"]
                for signal in assessment.get("page_signals", [])
                if signal["page_num"] not in blank_pages
            }
    elif parse_mode == "fast":
        if quality in {"scan_only", "mixed"}:
            local_ocr_pages |= problem_pages
    elif parse_mode == "accurate":
        if quality in {"scan_only", "mixed"}:
            local_ocr_pages |= problem_pages
            region_llm = bool(profile and parse_mode in profile.upgrade_policy.region_llm_modes)
    elif parse_mode == "full":
        llm_ocr_pages = {
            signal["page_num"]
            for signal in assessment.get("page_signals", [])
            if signal["page_num"] not in blank_pages
        }
        region_llm = bool(profile and parse_mode in profile.upgrade_policy.region_llm_modes)

    if profile and parse_mode in profile.upgrade_policy.proofread_modes:
        proofread = True
    if explicit_ocr_pages or force_ocr:
        proofread = True

    return {
        "parse_mode": parse_mode,
        "local_backend": local_backend,
        "local_ocr_pages": sorted(local_ocr_pages - llm_ocr_pages),
        "llm_ocr_pages": sorted(llm_ocr_pages),
        "region_llm": region_llm,
        "proofread": proofread,
    }


def _should_prewarm_local_ocr_for_pdf(
    filepath: Path,
    *,
    profile: DocumentProfile | None,
    parse_mode: str | None,
    force_ocr: bool,
    ocr_pages_spec: str | None,
    manual_blank_pages_spec: str | None,
    ocr_threshold: int,
) -> bool:
    if force_ocr or ocr_pages_spec:
        return False

    selected_mode = _resolve_pdf_parse_mode(profile, parse_mode)
    if selected_mode == "full":
        return False

    import fitz

    doc = fitz.open(str(filepath))
    try:
        manual_blank_pages = (
            _parse_page_range(manual_blank_pages_spec, len(doc))
            if manual_blank_pages_spec
            else set()
        )
        page_signals: list[dict[str, Any]] = []
        for i, page in enumerate(doc):
            page_num = i + 1
            text = page.get_text("text").strip()
            try:
                image_count = len(page.get_images(full=False))
            except Exception:
                image_count = 0
            manual_blank = page_num in manual_blank_pages
            scan_like = _should_ocr(page, text, ocr_threshold)
            blank_info: dict[str, Any] = {
                "blank_like": False,
                "blank_override": False,
                "nonwhite_ratio": None,
                "dark_ratio": None,
            }
            if manual_blank:
                blank_info["blank_like"] = True
                blank_info["blank_override"] = True
            elif scan_like and not text and image_count:
                blank_info = _page_blank_signal(page)
                blank_info["blank_override"] = False
            page_signals.append(
                {
                    "page_num": page_num,
                    "text_len": len(text),
                    "image_count": image_count,
                    "scan_like": scan_like,
                    **blank_info,
                }
            )
        assessment = _assess_contract_quality("", page_signals, profile)
        ocr_plan = _plan_pdf_ocr(
            profile=profile,
            parse_mode=selected_mode,
            force_ocr=False,
            explicit_ocr_pages=None,
            assessment=assessment,
        )
        return bool(ocr_plan["local_ocr_pages"])
    finally:
        doc.close()
