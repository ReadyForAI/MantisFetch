"""PDF parsing: text + tables via PyMuPDF, selective local/LLM OCR, sectioning.

`parse_pdf` is the heaviest parser. It:

1. extracts native text + tables per page (PyMuPDF `find_tables` + bbox strip),
2. builds per-page baseline signals and asks `_plan_pdf_ocr` which pages need
   local (PaddleOCR) vs LLM (Gemini) OCR,
3. renders + OCRs those pages concurrently, with an on-disk cache,
4. cleans the OCR text, re-extracts tables from it, optionally runs
   profile-driven field-focused region OCR,
5. splits into sections (TOC-aware) and returns a `ParsedDocument`.

Most collaborators are leaf imports (models, profiles, ocr_text, sectioning,
the OCR cache helpers). The OCR engine entry points (`gemini_ocr`,
`local_ocr_with_layout`) are facade-patched by tests, and the OCR-planning /
quality / page-signal / mode helpers plus a couple of shared render-scale
constants live in the package `__init__`; both groups are reached via a
function-level relative import off the facade — that breaks the import cycle
(the facade imports this module early) and keeps test monkeypatches effective.
"""

from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from .models import (
    OCRBlocksSidecar,
    OCRPageBlocks,
    PageContent,
    ParsedDocument,
    ProcessingPolicy,
)
from .ocr.engines import (
    _is_ocr_failed_text,
    _ocr_cache_key,
    _ocr_cache_path,
    _ocr_cache_variant_path,
)
from .ocr_text import (
    _cleanup_ocr_text,
    _extract_pdf_page_tables,
    _extract_tables_from_ocr_text,
    _normalize_document_text,
    _strip_repeated_headers_footers,
    _strip_text_in_table_bboxes,
)
from .profiles import _apply_field_focused_ocr, _load_document_profile
from .sectioning import _split_sections, _split_sections_from_toc

logger = logging.getLogger("mantisfetch_docreader")

# PDF-only OCR config (kept here, re-exported from the facade for the endpoint).
OCR_THRESHOLD = 50
LOCAL_OCR_CONCURRENCY = max(1, int(os.environ.get("MANTISFETCH_LOCAL_OCR_CONCURRENCY", "1")))


def parse_pdf(
    filepath: Path,
    force_ocr: bool = False,
    ocr_threshold: int = OCR_THRESHOLD,
    ocr_pages_spec: str | None = None,
    extract_tables: bool = True,
    max_tables_per_page: int = 3,
    concurrency: int = 3,
    cache_dir: Path | None = None,
    field_ocr_profile: str | None = None,
    field_ocr_config: str | None = None,
    parse_mode: str | None = None,
    manual_blank_pages_spec: str | None = None,
) -> ParsedDocument:
    import fitz

    # Facade imports: OCR engine entry points are monkeypatched by tests, and the
    # planning/quality/page-signal/mode helpers + shared render-scale constants
    # live in __init__ (defined after this module is imported). Function-level
    # `from . import` resolves them off the fully-loaded facade at call time.
    from . import (
        LOCAL_OCR_RENDER_SCALE,
        OCR_RENDER_SCALE,
        _assess_contract_quality,
        _classify_contract_text,
        _page_blank_signal,
        _parse_page_range,
        _plan_pdf_ocr,
        _resolve_ocr_render_scale,
        _resolve_pdf_parse_mode,
        _section_sid,
        _should_ocr,
        gemini_ocr,
        local_ocr_with_layout,
    )

    def _usable_page_text(raw_text: str, enhanced_text: str | None) -> str:
        if not enhanced_text:
            return raw_text
        if _is_ocr_failed_text(enhanced_text):
            return raw_text or enhanced_text
        return enhanced_text

    logger.info(f"Parsing PDF: {filepath.name}")
    profile = _load_document_profile(field_ocr_profile, field_ocr_config)
    selected_mode = _resolve_pdf_parse_mode(profile, parse_mode)
    processing_policy = (
        profile.processing_policy
        if profile
        else ProcessingPolicy(
            local_ocr_render_scale=LOCAL_OCR_RENDER_SCALE,
            llm_ocr_render_scale=OCR_RENDER_SCALE,
        )
    )
    source_size_bytes = filepath.stat().st_size
    large_file_threshold_bytes = processing_policy.large_file_threshold_mb * 1024 * 1024
    source_file_meta = {
        "size_bytes": source_size_bytes,
        "large_file_threshold_mb": processing_policy.large_file_threshold_mb,
        "large_file": source_size_bytes > large_file_threshold_bytes,
    }
    # Open with fitz for page count, TOC, and OCR rendering
    doc = fitz.open(str(filepath))
    try:
        total_pages = len(doc)
        logger.info(f"Total pages: {total_pages}")

        # PDF TOC (for section splitting)
        toc = doc.get_toc(simple=True)
        if toc:
            logger.info(f"PDF TOC detected: {len(toc)} entries")

        ocr_page_set: set[int] | None = None
        if ocr_pages_spec:
            ocr_page_set = _parse_page_range(ocr_pages_spec, total_pages)
            logger.info(f"OCR target pages: {sorted(ocr_page_set)}")
        manual_blank_pages = (
            _parse_page_range(manual_blank_pages_spec, total_pages)
            if manual_blank_pages_spec
            else set()
        )
        if manual_blank_pages:
            logger.info("Manual blank/skip OCR pages: %s", sorted(manual_blank_pages))

        # Build page-level baseline signals for selective enhancement.
        page_texts: dict[int, str] = {}
        page_signals: list[dict[str, Any]] = []
        pdf_tables_by_page: dict[int, list[str]] = {}
        # Pages where strip could not run; tables remain embedded in the raw page text,
        # so downstream must not re-append them to avoid duplication.
        pdf_tables_in_text_pages: set[int] = set()

        for i, page in enumerate(doc):
            page_num = i + 1
            text = page.get_text("text").strip()
            if extract_tables:
                page_tables, table_bboxes = _extract_pdf_page_tables(page)
                # Honor the documented max_tables_per_page cap (was ignored):
                # keep the first N tables; excess table text stays inline in body.
                if 0 <= max_tables_per_page < len(page_tables):
                    page_tables = page_tables[:max_tables_per_page]
                    table_bboxes = table_bboxes[:max_tables_per_page]
                pdf_tables_by_page[page_num] = page_tables
                if table_bboxes:
                    stripped = _strip_text_in_table_bboxes(page, table_bboxes)
                    if stripped is None:
                        page_texts[page_num] = text
                        pdf_tables_in_text_pages.add(page_num)
                    else:
                        page_texts[page_num] = stripped
                else:
                    page_texts[page_num] = text
            else:
                page_texts[page_num] = text
            image_count = 0
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

        # Drop running headers/footers that repeat across most pages (e.g. a
        # company-name banner on every page). Native PDF text keeps these on every
        # page; removing them here cleans both the section body text and the
        # classification text below.
        page_texts = _strip_repeated_headers_footers(page_texts, total_pages)

        # Contract/keyword classification must see the same content the dropped
        # MarkItDown pass produced: native body text PLUS table text. page_texts has
        # table regions stripped when extract_tables is on, so re-append the
        # extracted tables here; otherwise keywords living only inside tables would
        # be missed and matched_terms would be incomplete.
        classification_parts: list[str] = []
        for pn in sorted(page_texts):
            classification_parts.append(page_texts[pn])
            classification_parts.extend(pdf_tables_by_page.get(pn, []))
        native_full_text = "\n".join(classification_parts)
        assessment = _assess_contract_quality(native_full_text, page_signals, profile)
        ocr_plan = _plan_pdf_ocr(
            profile=profile,
            parse_mode=selected_mode,
            force_ocr=force_ocr,
            explicit_ocr_pages=ocr_page_set,
            assessment=assessment,
        )
        logger.info(
            "PDF parse plan: mode=%s quality=%s local_pages=%s llm_pages=%s region_llm=%s",
            ocr_plan["parse_mode"],
            assessment["document_quality"],
            ocr_plan["local_ocr_pages"],
            ocr_plan["llm_ocr_pages"],
            ocr_plan["region_llm"],
        )

        local_ocr_set = set(ocr_plan["local_ocr_pages"])
        llm_ocr_set = set(ocr_plan["llm_ocr_pages"])
        local_ocr_results: dict[int, str] = {}
        llm_ocr_results: dict[int, str] = {}
        local_ocr_layout_pages: dict[int, OCRPageBlocks] = {}
        local_tasks: list[tuple[int, bytes]] = []
        llm_tasks: list[tuple[int, bytes]] = []
        render_meta: dict[str, Any] = {
            "local_ocr_render_scale": processing_policy.local_ocr_render_scale,
            "llm_ocr_render_scale": processing_policy.llm_ocr_render_scale,
            "max_local_ocr_pixels": processing_policy.max_local_ocr_pixels,
            "max_llm_ocr_pixels": processing_policy.max_llm_ocr_pixels,
            "min_ocr_render_scale": processing_policy.min_ocr_render_scale,
            "pages_capped": [],
            "pages_skipped": [],
        }

        for page in doc:
            page_num = page.number + 1
            if page_num not in local_ocr_set and page_num not in llm_ocr_set:
                continue
            if page_num in llm_ocr_set:
                requested_scale = processing_policy.llm_ocr_render_scale
                max_pixels = processing_policy.max_llm_ocr_pixels
                cache_key = "llm"
            else:
                requested_scale = processing_policy.local_ocr_render_scale
                max_pixels = processing_policy.max_local_ocr_pixels
                cache_key = f"local-{ocr_plan['local_backend']}"
            scale, render_pixels, capped, skip = _resolve_ocr_render_scale(
                page,
                requested_scale=requested_scale,
                max_pixels=max_pixels,
                min_scale=processing_policy.min_ocr_render_scale,
            )
            if skip:
                logger.warning(
                    "Page %d/%d: skipping %s OCR — page too large to render within "
                    "the %d-pixel budget even at min scale",
                    page_num,
                    total_pages,
                    cache_key,
                    max_pixels,
                )
                render_meta["pages_skipped"].append(
                    {
                        "page_num": page_num,
                        "backend": cache_key,
                        "requested_scale": requested_scale,
                        "max_pixels": max_pixels,
                    }
                )
                # Drop from the OCR sets so ocr_page_count / is_ocr don't report a
                # page we never rendered as OCR'd.
                local_ocr_set.discard(page_num)
                llm_ocr_set.discard(page_num)
                continue
            if capped:
                logger.info(
                    "Page %d/%d: capped %s OCR render scale %.2f -> %.2f (%d px)",
                    page_num,
                    total_pages,
                    cache_key,
                    requested_scale,
                    scale,
                    render_pixels,
                )
                render_meta["pages_capped"].append(
                    {
                        "page_num": page_num,
                        "backend": cache_key,
                        "requested_scale": requested_scale,
                        "actual_scale": scale,
                        "render_pixels": render_pixels,
                        "max_pixels": max_pixels,
                    }
                )
            pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale))
            img_bytes = pix.tobytes("png")

            if cache_dir:
                ck = _ocr_cache_key(img_bytes)
                if page_num in llm_ocr_set:
                    cp = _ocr_cache_path(cache_dir, page_num)
                    ck_path = cp.with_suffix(f".{ck}.txt")
                else:
                    ck_path = _ocr_cache_variant_path(
                        cache_dir,
                        f"ocr_p{page_num:04d}.{cache_key}.{ck}.txt",
                    )
                if ck_path.exists():
                    cached = ck_path.read_text(encoding="utf-8")
                    if _is_ocr_failed_text(cached):
                        logger.info(
                            "Page %d/%d: ignoring failed %s OCR cache",
                            page_num,
                            total_pages,
                            cache_key,
                        )
                    else:
                        if page_num in llm_ocr_set:
                            llm_ocr_results[page_num] = cached
                        else:
                            local_ocr_results[page_num] = cached
                        logger.info("Page %d/%d: %s OCR cache hit", page_num, total_pages, cache_key)
                        continue
            if page_num in llm_ocr_set:
                llm_tasks.append((page_num, img_bytes))
            else:
                local_tasks.append((page_num, img_bytes))

    finally:
        doc.close()

    if local_tasks:
        logger.info(
            "Concurrent local OCR: %d pages (%d workers, backend=%s)...",
            len(local_tasks),
            LOCAL_OCR_CONCURRENCY,
            ocr_plan["local_backend"],
        )

        def _do_local_ocr(args):
            pn, img_b = args
            text, page_blocks = local_ocr_with_layout(img_b, pn, ocr_plan["local_backend"])
            return pn, img_b, text, page_blocks

        with ThreadPoolExecutor(max_workers=LOCAL_OCR_CONCURRENCY) as pool:
            futures = {pool.submit(_do_local_ocr, task): task for task in local_tasks}
            for fut in as_completed(futures):
                pn, img_b, result, page_blocks = fut.result()
                local_ocr_results[pn] = result
                if page_blocks is not None and not _is_ocr_failed_text(result):
                    local_ocr_layout_pages[pn] = page_blocks
                logger.info(f"Page {pn}/{total_pages}: local OCR done")
                if cache_dir and profile and profile.cache_policy.page_ocr:
                    if _is_ocr_failed_text(result):
                        logger.info("Page %d/%d: not caching failed local OCR result", pn, total_pages)
                        continue
                    cache_path = _ocr_cache_variant_path(
                        cache_dir,
                        f"ocr_p{pn:04d}.local-{ocr_plan['local_backend']}.{_ocr_cache_key(img_b)}.txt",
                    )
                    cache_path.write_text(result, encoding="utf-8")

    # Concurrent LLM OCR
    if llm_tasks:
        logger.info(f"Concurrent LLM OCR: {len(llm_tasks)} pages ({concurrency} workers)...")

        def _do_ocr(args):
            pn, img_b = args
            result = gemini_ocr(img_b, pn, proofread=ocr_plan["proofread"])
            return pn, img_b, result

        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = {pool.submit(_do_ocr, task): task for task in llm_tasks}
            for fut in as_completed(futures):
                pn, img_b, result = fut.result()
                llm_ocr_results[pn] = result
                logger.info(f"Page {pn}/{total_pages}: LLM OCR done")
                if cache_dir:
                    if _is_ocr_failed_text(result):
                        logger.info("Page %d/%d: not caching failed LLM OCR result", pn, total_pages)
                    else:
                        cp = _ocr_cache_path(cache_dir, pn)
                        ck = _ocr_cache_key(img_b)
                        ck_path = cp.with_suffix(f".{ck}.txt")
                        ck_path.write_text(result, encoding="utf-8")

    pages: list[PageContent] = []
    ocr_table_count = 0
    pdf_table_count = 0
    ocr_count = len(local_ocr_set | llm_ocr_set)
    for page_num in range(1, total_pages + 1):
        raw_text = page_texts.get(page_num, "")
        page_text = raw_text
        page_tables: list[str] = []
        tables_in_text = False
        enhanced = llm_ocr_results.get(page_num) or local_ocr_results.get(page_num)
        if enhanced:
            page_text = _cleanup_ocr_text(_usable_page_text(raw_text, enhanced))
            if extract_tables:
                page_text, page_tables = _extract_tables_from_ocr_text(page_text, page_num, total_pages)
                # Apply the documented per-page cap to OCR-derived tables too,
                # but keep overflow tables' content inline in the body (the OCR
                # extractor already removed them from page_text) so capping
                # never loses document content — matching the native path.
                if 0 <= max_tables_per_page < len(page_tables):
                    overflow = page_tables[max_tables_per_page:]
                    page_tables = page_tables[:max_tables_per_page]
                    page_text = page_text.rstrip() + "\n\n" + "\n\n".join(overflow)
                ocr_table_count += len(page_tables)
                tables_in_text = bool(page_tables)
                # If OCR found no tables on this page but the PyMuPDF pass did
                # (e.g. OCR failed and _usable_page_text fell back to the
                # already-stripped raw text), recover them so the page's tables
                # are not silently lost.
                if not page_tables and pdf_tables_by_page.get(page_num):
                    page_tables = pdf_tables_by_page[page_num]
                    pdf_table_count += len(page_tables)
                    # If we ended up using stripped raw text (OCR failed),
                    # pdf_tables_in_text_pages tells us whether the strip ran.
                    # If we ended up using enhanced OCR text, we cannot tell
                    # whether the OCR'd text contains the table cells inline,
                    # so be conservative and re-append (worst case duplicates,
                    # but no table content is lost from section text).
                    if _is_ocr_failed_text(enhanced):
                        tables_in_text = page_num in pdf_tables_in_text_pages
                    else:
                        tables_in_text = False
        elif extract_tables:
            page_tables = pdf_tables_by_page.get(page_num, [])
            pdf_table_count += len(page_tables)
            tables_in_text = page_num in pdf_tables_in_text_pages
        pages.append(
            PageContent(
                page_num=page_num,
                text=page_text.strip(),
                is_ocr=page_num in (local_ocr_set | llm_ocr_set),
                tables=page_tables,
                tables_in_text=tables_in_text,
            )
        )

    if llm_ocr_results:
        logger.info(f"LLM OCR pages: {sorted(llm_ocr_results)}")
    if local_ocr_results:
        logger.info(f"Local OCR pages: {sorted(local_ocr_results)}")

    if profile and not assessment.get("is_contract"):
        # Include reconstructed table markdown so terms inside table cells (e.g.
        # 甲方/乙方/合同金额) are visible to the classifier even when page.text was
        # bbox-stripped earlier in the pipeline. Skip append when the page already
        # carries the table content inline (tables_in_text=True) so we don't
        # double-count terms.
        combined_text = "\n".join(
            (
                (page.text or "")
                + (
                    "\n" + "\n".join(page.tables)
                    if (page.tables and not page.tables_in_text)
                    else ""
                )
            )
            for page in pages
            if page.text or page.tables
        )
        is_contract, matched_terms = _classify_contract_text(combined_text, profile)
        if is_contract:
            assessment["is_contract"] = True
            assessment["matched_terms"] = matched_terms
            assessment["classification_source"] = "enhanced_text"

    _normalize_document_text(pages)
    field_ocr_meta: dict[str, Any] = {}
    if profile and ocr_plan["region_llm"]:
        field_ocr_meta = _apply_field_focused_ocr(
            filepath,
            pages,
            profile,
            cache_dir=cache_dir,
            proofread=ocr_plan["proofread"],
        )
        _normalize_document_text(pages)

    # Section splitting: prefer TOC when available
    if toc:
        sections = _split_sections_from_toc(
            pages,
            toc,
            section_policy=profile.section_policy if profile else None,
        )
    else:
        sections = _split_sections(pages, section_policy=profile.section_policy if profile else None)

    for sec in sections:
        sec.sid = _section_sid(sec.title, sec.text)

    # Count tables actually materialized onto pages (so manifest matches files on disk).
    if extract_tables:
        table_count = ocr_table_count + pdf_table_count
    else:
        table_count = 0

    logger.info(
        f"Parse complete: {len(sections)} sections, {ocr_count} OCR pages, {table_count} tables"
    )

    return ParsedDocument(
        filename=filepath.name,
        file_type="pdf",
        total_pages=total_pages,
        pages=pages,
        sections=sections,
        ocr_page_count=ocr_count,
        table_count=table_count,
        ocr_blocks=(
            OCRBlocksSidecar(
                doc_id="",
                pages=tuple(local_ocr_layout_pages[pn] for pn in sorted(local_ocr_layout_pages)),
            )
            if local_ocr_layout_pages
            else None
        ),
        extract_tables=extract_tables,
        metadata={
            "document_profile": profile.name if profile else None,
            "pdf_parse_mode": selected_mode,
            "source_file": source_file_meta,
            "quality_assessment": assessment,
            "ocr_plan": ocr_plan,
            "ocr_rendering": render_meta,
            "field_ocr": field_ocr_meta,
        },
    )
