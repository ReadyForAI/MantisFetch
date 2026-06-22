#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["markitdown[pdf,docx,pptx,xlsx,xls]", "pymupdf", "google-genai", "Pillow", "fastapi", "uvicorn", "python-multipart", "paddleocr", "paddlepaddle"]
# ///

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from pydantic import BaseModel, Field

from i18n import init_locale, t, tmpl_for_locale
from mantisfetch_common.atomic import _write_json, _write_text
from mantisfetch_common.paths import _mask_path
from mantisfetch_common.storage import (
    DEFAULT_DOCS_DIR,
    _doc_storage_dir,
    _doc_storage_rel_path,
    _get_docs_dir,
    _normalize_content_type,
)

from .images import (
    _convert_vector_image_to_png as _convert_vector_image_to_png,
)
from .images import (
    _extract_image_context_keywords as _extract_image_context_keywords,
)
from .images import (
    _image_average_hash as _image_average_hash,
)
from .images import (
    _image_dimensions as _image_dimensions,
)
from .images import (
    _inventory_hints_for_image as _inventory_hints_for_image,
)
from .images import (
    _ocr_embedded_image as _ocr_embedded_image,
)
from .images import (
    _populate_embedded_image_inventory as _populate_embedded_image_inventory,
)
from .images import (
    _render_embedded_image as _render_embedded_image,
)
from .images import (
    _render_raster_image_to_png as _render_raster_image_to_png,
)
from .models import (
    OCR_BLOCKS_COORDINATE_SYSTEM,
    OCR_BLOCKS_SIDECAR_PATH,
    OCR_BLOCKS_SIDECAR_VERSION,
    EmbeddedImage,
    OCRBlocksSidecar,
    ParsedDocument,
    Section,
)

# Profile-domain model classes used by profiles.py, kept on the facade so
# `from mantisfetch_docreader import FieldGroup, ...` keeps resolving.
from .models import (
    CachePolicy as CachePolicy,
)
from .models import (
    ClassificationPolicy as ClassificationPolicy,
)
from .models import (
    DocumentProfile as DocumentProfile,
)
from .models import (
    FieldCrop as FieldCrop,
)
from .models import (
    FieldGroup as FieldGroup,
)
from .models import (
    FieldRule as FieldRule,
)

# Models the parsers (now in pdf.py/word.py/tabular.py) construct; __init__ no
# longer references them itself but they stay on the facade for importers/tests.
from .models import (
    OCRPageBlocks as OCRPageBlocks,
)
from .models import (
    OCRTextBlock as OCRTextBlock,
)
from .models import (
    PageContent as PageContent,
)
from .models import (
    ProcessingPolicy as ProcessingPolicy,
)
from .models import (
    QualityPolicy as QualityPolicy,
)
from .models import (
    SectionPolicy as SectionPolicy,
)
from .models import (
    SummaryPolicy as SummaryPolicy,
)
from .models import (
    UpgradePolicy as UpgradePolicy,
)
from .models import (
    _normalize_layout_bbox as _normalize_layout_bbox,
)
from .ocr.engines import (
    _get_local_ocr_worker,
    _local_ocr_worker_initializing,
    _local_ocr_worker_lock,
    _local_ocr_worker_ready,
)

# OCR engine entry points + cache helpers used by the parsers (pdf.py); __init__
# no longer references them itself. `gemini_ocr` / `local_ocr_with_layout` are
# also facade-patched by tests, so they must stay reachable off the facade.
from .ocr.engines import (
    _is_ocr_failed_text as _is_ocr_failed_text,
)
from .ocr.engines import (
    _ocr_cache_key as _ocr_cache_key,
)
from .ocr.engines import (
    _ocr_cache_path as _ocr_cache_path,
)
from .ocr.engines import (
    _ocr_cache_variant_path as _ocr_cache_variant_path,
)
from .ocr.engines import _stop_local_ocr_worker as _stop_local_ocr_worker
from .ocr.engines import (
    gemini_ocr as gemini_ocr,
)
from .ocr.engines import (
    local_ocr as local_ocr,
)
from .ocr.engines import (
    local_ocr_with_layout as local_ocr_with_layout,
)
from .ocr.tables import (
    _apply_table_continuation_links,
    _detect_table_candidates_from_ocr_blocks,
    _markdown_from_structured_table,
    _markdown_table_dimensions,
    _reconstruct_table_from_candidate,
)

# Used by the tabular/word parsers (and the markdown-table tests via the facade);
# __init__ no longer references them directly, so keep them as explicit re-exports.
from .ocr.tables import (
    _count_markdown_tables as _count_markdown_tables,
)
from .ocr.tables import (
    _extract_markdown_table_blocks as _extract_markdown_table_blocks,
)
from .ocr.tables import _is_markdown_table_separator as _is_markdown_table_separator

# OCR/PDF text post-processing: noise cleanup, table detection + extraction,
# document normalization. Re-exported so parse_pdf and the sectioning/profiles/
# images submodules' function-level `from . import` calls keep resolving, with
# no behavior change.
from .ocr_text import (
    _TABLE_FOOTER_TERMS as _TABLE_FOOTER_TERMS,
)
from .ocr_text import (
    _TABLE_HEADER_TERMS as _TABLE_HEADER_TERMS,
)
from .ocr_text import (
    _cleanup_ocr_text as _cleanup_ocr_text,
)
from .ocr_text import (
    _extract_pdf_page_tables as _extract_pdf_page_tables,
)
from .ocr_text import (
    _extract_tables_from_ocr_text as _extract_tables_from_ocr_text,
)
from .ocr_text import (
    _is_markdown_table_delimiter as _is_markdown_table_delimiter,
)
from .ocr_text import (
    _looks_like_bracket_noise as _looks_like_bracket_noise,
)
from .ocr_text import (
    _looks_like_markdown_table_row as _looks_like_markdown_table_row,
)
from .ocr_text import (
    _looks_like_page_footer as _looks_like_page_footer,
)
from .ocr_text import (
    _looks_like_plain_table_footer as _looks_like_plain_table_footer,
)
from .ocr_text import (
    _looks_like_plain_table_header as _looks_like_plain_table_header,
)
from .ocr_text import (
    _looks_like_plain_table_row as _looks_like_plain_table_row,
)
from .ocr_text import (
    _normalize_document_text as _normalize_document_text,
)
from .ocr_text import (
    _remove_footer_page_number as _remove_footer_page_number,
)
from .ocr_text import (
    _strip_text_in_table_bboxes as _strip_text_in_table_bboxes,
)

# PDF parser + its PDF-only OCR config constants. Re-exported so the /doc/parse
# dispatch and endpoint (which pass OCR_THRESHOLD to parse_pdf) keep resolving.
from .pdf import (
    LOCAL_OCR_CONCURRENCY as LOCAL_OCR_CONCURRENCY,
)
from .pdf import (
    OCR_THRESHOLD as OCR_THRESHOLD,
)
from .pdf import (
    parse_pdf as parse_pdf,
)

# PDF OCR planning (page analysis, render-scale capping, OCR plan). Re-exported
# so parse_pdf's function-level imports, the /doc/parse endpoint, and the
# planning tests keep resolving these off the facade.
from .pdf_planning import (
    _assess_contract_quality as _assess_contract_quality,
)
from .pdf_planning import (
    _classify_contract_text as _classify_contract_text,
)
from .pdf_planning import (
    _metadata_page_range_spec as _metadata_page_range_spec,
)
from .pdf_planning import (
    _page_blank_signal as _page_blank_signal,
)
from .pdf_planning import (
    _page_render_pixels as _page_render_pixels,
)
from .pdf_planning import (
    _parse_page_range as _parse_page_range,
)
from .pdf_planning import (
    _plan_pdf_ocr as _plan_pdf_ocr,
)
from .pdf_planning import (
    _resolve_ocr_render_scale as _resolve_ocr_render_scale,
)
from .pdf_planning import (
    _resolve_pdf_parse_mode as _resolve_pdf_parse_mode,
)
from .pdf_planning import (
    _should_ocr as _should_ocr,
)
from .pdf_planning import (
    _should_prewarm_local_ocr_for_pdf as _should_prewarm_local_ocr_for_pdf,
)

# Document-profile loading + profile-driven field extraction. Re-exported so
# parse_pdf, the /doc/parse endpoint, and `from mantisfetch_docreader import X`
# tests keep resolving these names off the facade with no behavior change.
from .profiles import (
    _DOCUMENT_PROFILE_ALIASES as _DOCUMENT_PROFILE_ALIASES,
)
from .profiles import (
    DOCUMENT_PROFILE_CONFIG_DIR as DOCUMENT_PROFILE_CONFIG_DIR,
)
from .profiles import (
    FIELD_OCR_CONFIG_DIR as FIELD_OCR_CONFIG_DIR,
)
from .profiles import (
    FIELD_OCR_RENDER_SCALE as FIELD_OCR_RENDER_SCALE,
)
from .profiles import (
    _apply_field_focused_ocr as _apply_field_focused_ocr,
)
from .profiles import (
    _blob_has_alias as _blob_has_alias,
)
from .profiles import (
    _extract_profile_fields as _extract_profile_fields,
)
from .profiles import (
    _field_value_quality as _field_value_quality,
)
from .profiles import (
    _load_document_profile as _load_document_profile,
)
from .profiles import (
    _normalize_cover_label_lines as _normalize_cover_label_lines,
)
from .profiles import (
    _page_blob as _page_blob,
)
from .profiles import (
    _prepend_source_contract_no_if_missing as _prepend_source_contract_no_if_missing,
)
from .profiles import (
    _replace_blob_segment as _replace_blob_segment,
)
from .profiles import (
    _set_page_blob as _set_page_blob,
)
from .profiles import (
    _source_filename_contract_no as _source_filename_contract_no,
)
from .regions import (
    CROP_ARTIFACT_DIR as CROP_ARTIFACT_DIR,
)
from .regions import (
    REGION_OCR_ARTIFACT_DIR as REGION_OCR_ARTIFACT_DIR,
)
from .regions import (
    VISUAL_DEBUG_ARTIFACT_DIR,
)
from .regions import (
    _crop_clip_rect as _crop_clip_rect,
)
from .regions import (
    _debug_bbox_to_pixels as _debug_bbox_to_pixels,
)
from .regions import (
    _ensure_bbox_inside_bounds as _ensure_bbox_inside_bounds,
)
from .regions import (
    _load_manifest_dict as _load_manifest_dict,
)
from .regions import (
    _load_ocr_debug_overlays as _load_ocr_debug_overlays,
)
from .regions import (
    _load_table_debug_overlays as _load_table_debug_overlays,
)
from .regions import (
    _normalize_crop_bbox as _normalize_crop_bbox,
)
from .regions import (
    _normalize_region_ocr_backend as _normalize_region_ocr_backend,
)
from .regions import (
    _ocr_page_dimensions as _ocr_page_dimensions,
)
from .regions import (
    _resolve_doc_source_file as _resolve_doc_source_file,
)
from .regions import (
    _safe_artifact_id as _safe_artifact_id,
)
from .regions import (
    export_pdf_region_crop as export_pdf_region_crop,
)
from .regions import (
    generate_visual_debug_artifacts as generate_visual_debug_artifacts,
)
from .regions import (
    rerun_region_ocr as rerun_region_ocr,
)
from .sectioning import (
    _compact_toc_for_section_boundaries as _compact_toc_for_section_boundaries,
)
from .sectioning import (
    _demote_toc_stub_sections as _demote_toc_stub_sections,
)
from .sectioning import (
    _detect_markdown_section_level as _detect_markdown_section_level,
)
from .sectioning import (
    _is_arabic_numbered_heading_candidate as _is_arabic_numbered_heading_candidate,
)
from .sectioning import (
    _is_heading as _is_heading,
)
from .sectioning import (
    _line_index_for_toc_title as _line_index_for_toc_title,
)
from .sectioning import (
    _looks_like_numeric_identifier_heading as _looks_like_numeric_identifier_heading,
)
from .sectioning import (
    _looks_like_numeric_table_value as _looks_like_numeric_table_value,
)
from .sectioning import (
    _looks_like_ocr_chrome_heading as _looks_like_ocr_chrome_heading,
)
from .sectioning import (
    _looks_like_polluted_heading_text as _looks_like_polluted_heading_text,
)
from .sectioning import (
    _looks_like_toc_stub_body as _looks_like_toc_stub_body,
)
from .sectioning import (
    _markdown_heading_level as _markdown_heading_level,
)
from .sectioning import (
    _merge_short_ocr_sections as _merge_short_ocr_sections,
)
from .sectioning import (
    _merge_short_sections as _merge_short_sections,
)
from .sectioning import (
    _normalize_heading_key as _normalize_heading_key,
)
from .sectioning import (
    _numeric_heading_level as _numeric_heading_level,
)
from .sectioning import (
    _numeric_heading_prefix as _numeric_heading_prefix,
)
from .sectioning import (
    _prefers_formal_chinese_sectioning as _prefers_formal_chinese_sectioning,
)
from .sectioning import (
    _prepare_toc_section_boundaries as _prepare_toc_section_boundaries,
)
from .sectioning import (
    _promote_parent_sections_to_first_child as _promote_parent_sections_to_first_child,
)
from .sectioning import (
    _renumber_sections as _renumber_sections,
)
from .sectioning import (
    _split_leading_toc_lines as _split_leading_toc_lines,
)
from .sectioning import (
    _split_sections as _split_sections,
)
from .sectioning import (
    _split_sections_from_toc as _split_sections_from_toc,
)
from .sectioning import (
    _strip_heading_markup as _strip_heading_markup,
)
from .sectioning import (
    _toc_chapter_prefix as _toc_chapter_prefix,
)
from .sectioning import (
    _toc_has_dense_same_page_entries as _toc_has_dense_same_page_entries,
)
from .sectioning import (
    _toc_parent_for_child as _toc_parent_for_child,
)

# Docreader-side storage: doc_id reservation, doc-index read/write, doc-dir
# resolution. Re-exported as shared references (locks/WeakValueDictionary are
# mutated in place) so endpoints and `from mantisfetch_docreader import X` keep
# working with no behavior change.
from .storage import (
    _DOC_ID_RE as _DOC_ID_RE,
)
from .storage import (
    _doc_content_type as _doc_content_type,
)
from .storage import (
    _doc_counter_lock as _doc_counter_lock,
)
from .storage import (
    _doc_entry_from_manifest as _doc_entry_from_manifest,
)
from .storage import (
    _doc_exists_anywhere as _doc_exists_anywhere,
)
from .storage import (
    _doc_id_parse_locks as _doc_id_parse_locks,
)
from .storage import (
    _doc_id_parse_locks_guard as _doc_id_parse_locks_guard,
)
from .storage import (
    _doc_id_strategy as _doc_id_strategy,
)
from .storage import (
    _doc_index_lock as _doc_index_lock,
)
from .storage import (
    _find_doc_index_entry as _find_doc_index_entry,
)
from .storage import (
    _indexable_metadata as _indexable_metadata,
)
from .storage import (
    _load_doc_index as _load_doc_index,
)
from .storage import (
    _load_doc_tags as _load_doc_tags,
)
from .storage import (
    _next_doc_id as _next_doc_id,
)
from .storage import (
    _next_filename_doc_id as _next_filename_doc_id,
)
from .storage import (
    _optional_doc_id_lock as _optional_doc_id_lock,
)
from .storage import (
    _resolve_doc_dir as _resolve_doc_dir,
)
from .storage import (
    _resolve_doc_id as _resolve_doc_id,
)
from .storage import (
    _resolve_index_storage_path as _resolve_index_storage_path,
)
from .storage import (
    _sanitize_doc_id_candidate as _sanitize_doc_id_candidate,
)
from .storage import (
    _update_doc_index as _update_doc_index,
)
from .storage import (
    _validate_doc_id as _validate_doc_id,
)

# Three-tier summary generation (per-section → brief → digest) + the LLM
# summarize wrapper. Re-exported so write_output / the deferred-summary thread /
# the endpoint and the summary tests (which monkeypatch gemini_summarize off the
# facade) keep resolving these.
from .summaries import (
    SUMMARY_BATCH_CONCURRENCY as SUMMARY_BATCH_CONCURRENCY,
)
from .summaries import (
    SUMMARY_BRIEF_MAX_INPUT_CHARS as SUMMARY_BRIEF_MAX_INPUT_CHARS,
)
from .summaries import (
    SUMMARY_BRIEF_SECTION_EXCERPT_CHARS as SUMMARY_BRIEF_SECTION_EXCERPT_CHARS,
)
from .summaries import (
    SUMMARY_MAX_CHARS as SUMMARY_MAX_CHARS,
)
from .summaries import (
    SUMMARY_REQUEST_MIN_INTERVAL_SEC as SUMMARY_REQUEST_MIN_INTERVAL_SEC,
)
from .summaries import (
    SUMMARY_SECTION_DETAIL_LIMIT as SUMMARY_SECTION_DETAIL_LIMIT,
)
from .summaries import (
    _classify_summary_error as _classify_summary_error,
)
from .summaries import (
    _compress_sections_for_brief as _compress_sections_for_brief,
)
from .summaries import (
    _current_summary_attempts as _current_summary_attempts,
)
from .summaries import (
    _local_section_preview as _local_section_preview,
)
from .summaries import (
    _resolve_summary_mode as _resolve_summary_mode,
)
from .summaries import (
    _sections_overview_for_brief as _sections_overview_for_brief,
)
from .summaries import (
    _sections_overview_from_text as _sections_overview_from_text,
)
from .summaries import (
    _set_next_summary_llm_allowed_at as _set_next_summary_llm_allowed_at,
)
from .summaries import (
    _set_summary_metadata as _set_summary_metadata,
)
from .summaries import (
    _should_skip_section_summaries as _should_skip_section_summaries,
)
from .summaries import (
    _summarize_batch as _summarize_batch,
)
from .summaries import (
    _summary_failed_text as _summary_failed_text,
)
from .summaries import (
    _summary_llm_lock as _summary_llm_lock,
)
from .summaries import (
    _summary_placeholder_text as _summary_placeholder_text,
)
from .summaries import (
    gemini_summarize as gemini_summarize,
)
from .summaries import (
    generate_summaries as generate_summaries,
)

# Tabular + generic MarkItDown-backed parsers. Re-exported so the /doc/parse
# dispatch and the xlsx/csv parse tests keep resolving these off the facade.
from .tabular import (
    parse_csv as parse_csv,
)
from .tabular import (
    parse_generic as parse_generic,
)
from .tabular import (
    parse_xlsx as parse_xlsx,
)
from .text_utils import (
    _amount_to_uppercase_rmb as _amount_to_uppercase_rmb,
)
from .text_utils import (
    _apply_company_name_replacements as _apply_company_name_replacements,
)
from .text_utils import (
    _build_company_name_replacements as _build_company_name_replacements,
)
from .text_utils import (
    _cleanup_extracted_text_noise as _cleanup_extracted_text_noise,
)
from .text_utils import (
    _collect_company_names as _collect_company_names,
)
from .text_utils import (
    _looks_like_signature_watermark_line as _looks_like_signature_watermark_line,
)
from .text_utils import (
    _normalize_amount_phrases as _normalize_amount_phrases,
)
from .text_utils import (
    _split_company_name as _split_company_name,
)
from .word import (
    _IMAGE_MIME_BY_EXT as _IMAGE_MIME_BY_EXT,
)

# Word (.docx) parser + embedded-image extraction. Re-exported so the /doc/parse
# endpoint, parse dispatch, and the word-image tests' `docreader.parse_word` /
# `docreader._extract_word_embedded_images` facade calls keep resolving.
from .word import (
    WORD_XML_NS as WORD_XML_NS,
)
from .word import (
    _anchor_word_images_to_sections as _anchor_word_images_to_sections,
)
from .word import (
    _count_word_embedded_image_references as _count_word_embedded_image_references,
)
from .word import (
    _extract_word_embedded_images as _extract_word_embedded_images,
)
from .word import (
    _word_heading_level as _word_heading_level,
)
from .word import (
    _word_image_context_text as _word_image_context_text,
)
from .word import (
    _word_image_relationships as _word_image_relationships,
)
from .word import (
    _word_paragraph_image_rel_ids as _word_paragraph_image_rel_ids,
)
from .word import (
    _word_paragraph_style as _word_paragraph_style,
)
from .word import (
    _word_paragraph_text as _word_paragraph_text,
)
from .word import (
    _word_rel_target_to_package_path as _word_rel_target_to_package_path,
)
from .word import (
    parse_word as parse_word,
)

init_locale()

logger = logging.getLogger("mantisfetch_docreader")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

# ═══════════════════════════════════════════
# Config
# ═══════════════════════════════════════════
MAX_PARSE_ROWS = int(os.environ.get("MANTISFETCH_MAX_PARSE_ROWS", "100000"))
_MAX_CONCURRENT_PARSE = int(os.environ.get("MANTISFETCH_MAX_CONCURRENT_PARSE", "2"))
_parse_sem = asyncio.Semaphore(_MAX_CONCURRENT_PARSE)

# Bound concurrent upload reads so a burst of large requests can't allocate
# unbounded memory before any parse slot is acquired. The doc_id reservation
# and `_parse_sem` are deliberately downstream — this gate covers only the
# upload-buffer footprint.
_MAX_CONCURRENT_UPLOAD = int(
    os.environ.get("MANTISFETCH_MAX_CONCURRENT_UPLOAD", str(_MAX_CONCURRENT_PARSE))
)
_upload_sem = asyncio.Semaphore(_MAX_CONCURRENT_UPLOAD)


SUPPORTED_FORMATS = [
    "pdf",
    "doc",
    "docx",
    "ppt",
    "pptx",
    "xls",
    "xlsx",
    "csv",
    "html",
    "htm",
    "txt",
    "text",
    "json",
    "jsonl",
    "xml",
]
SUPPORTED_EXTENSIONS = {f".{fmt}" for fmt in SUPPORTED_FORMATS}
# Backward-compat: tender_cn was renamed to bid_cn to match the Bid storage directory.

# Lazy-initialized MarkItDown converter
_md_converter = None
_md_converter_lock = threading.Lock()


def _get_converter():
    """Return a lazily-initialized MarkItDown converter (thread-safe)."""
    global _md_converter
    if _md_converter is None:
        with _md_converter_lock:
            if _md_converter is None:
                from markitdown import MarkItDown

                _md_converter = MarkItDown()
    return _md_converter


def _convert_to_markdown(filepath: Path) -> str:
    """Convert a document to Markdown text via MarkItDown."""
    try:
        result = _get_converter().convert(str(filepath))
        return result.text_content or ""
    except Exception as e:
        raise RuntimeError(t("file_open_failed", path=str(filepath))) from e


def _office_converter_binary() -> str:
    binary = shutil.which("soffice") or shutil.which("libreoffice")
    if not binary:
        raise RuntimeError(t("office_converter_missing"))
    return binary


def _convert_legacy_office(filepath: Path, target_ext: str) -> Path:
    """Convert legacy binary Office files (.doc/.ppt) to modern OOXML files."""
    target_ext = target_ext.lower().lstrip(".")
    out_dir = filepath.parent / f"{filepath.stem}.{target_ext}.converted"
    out_dir.mkdir(parents=True, exist_ok=True)
    user_install = filepath.parent / "libreoffice-profile"
    user_install.mkdir(parents=True, exist_ok=True)
    timeout = int(os.environ.get("MANTISFETCH_OFFICE_CONVERT_TIMEOUT_SEC", "120"))
    cmd = [
        _office_converter_binary(),
        "--headless",
        "--nologo",
        "--nofirststartwizard",
        f"-env:UserInstallation=file://{user_install}",
        "--convert-to",
        target_ext,
        "--outdir",
        str(out_dir),
        str(filepath),
    ]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    converted = out_dir / f"{filepath.stem}.{target_ext}"
    if proc.returncode != 0 or not converted.exists():
        details = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(t("office_conversion_failed", src=filepath.suffix, dst=target_ext, err=details))
    return converted


def _detect_text_locale(text: str) -> str:
    sample = text[:20000]
    cjk = sum(1 for ch in sample if "\u4e00" <= ch <= "\u9fff")
    alpha = sum(1 for ch in sample if ch.isascii() and ch.isalpha())
    return "zh" if cjk >= 20 or cjk > alpha else "en"


def _parsed_document_locale(parsed: ParsedDocument) -> str:
    value = str(parsed.metadata.get("summary_locale") or parsed.metadata.get("language") or "").strip()
    if value.startswith(("zh", "en")):
        return value[:2]
    sample_parts = [parsed.filename]
    sample_parts.extend(sec.title for sec in parsed.sections[:5])
    sample_parts.extend(sec.text[:1000] for sec in parsed.sections[:5])
    locale = _detect_text_locale("\n".join(sample_parts))
    parsed.metadata["summary_locale"] = locale
    return locale


# ═══════════════════════════════════════════
# LLM provider wrapper
# ═══════════════════════════════════════════


# ═══════════════════════════════════════════
# Token estimation
# ═══════════════════════════════════════════


def _estimate_tokens(text: str) -> int:
    """Rough token estimate. CJK ~2.5 chars/tok, Latin ~4 chars/tok."""
    if not text:
        return 0
    cjk_count = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    ratio = cjk_count / max(len(text), 1)
    chars_per_token = 2.5 * ratio + 4.0 * (1 - ratio)
    return int(len(text) / chars_per_token)


# ═══════════════════════════════════════════
# Smart OCR detection
# ═══════════════════════════════════════════

OCR_RENDER_SCALE = float(os.environ.get("MANTISFETCH_OCR_RENDER_SCALE", "3.0"))
LOCAL_OCR_RENDER_SCALE = float(os.environ.get("MANTISFETCH_LOCAL_OCR_RENDER_SCALE", "2.0"))
DEFERRED_SUMMARY_MAX_CONCURRENT = max(
    1,
    int(os.environ.get("MANTISFETCH_DEFERRED_SUMMARY_MAX_CONCURRENT", "1")),
)
DEFERRED_SUMMARY_TIMEOUT_SEC = max(
    10.0,
    float(os.environ.get("MANTISFETCH_DEFERRED_SUMMARY_TIMEOUT_SEC", "180")),
)
DEFERRED_SUMMARY_MAX_ATTEMPTS = max(
    1,
    int(os.environ.get("MANTISFETCH_DEFERRED_SUMMARY_MAX_ATTEMPTS", "3")),
)
WORD_IMAGE_OCR_MAX_IMAGES = max(
    0,
    int(os.environ.get("MANTISFETCH_WORD_IMAGE_OCR_MAX_IMAGES", "80")),
)


_deferred_summary_sem = threading.BoundedSemaphore(DEFERRED_SUMMARY_MAX_CONCURRENT)
DEFERRED_SUMMARY_LOCAL_OCR_WAIT_SEC = float(
    os.environ.get("MANTISFETCH_DEFERRED_SUMMARY_LOCAL_OCR_WAIT_SEC", "30")
)


# ═══════════════════════════════════════════
# Section stable ID
# ═══════════════════════════════════════════


def _section_sid(title: str, text: str) -> str:
    raw = (title + text[:200]).encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:12]


# ═══════════════════════════════════════════
# Summary generation
# ═══════════════════════════════════════════


# ═══════════════════════════════════════════
# Output file writing
# ═══════════════════════════════════════════


def _reset_generated_output_dirs(doc_dir: Path, include_extracted: bool = True) -> None:
    dirs = ["sections"]
    files = ["sections.json"]
    if include_extracted:
        dirs += ["tables", "images"]
        files += ["tables.json", "images.json", OCR_BLOCKS_SIDECAR_PATH]
    for child in dirs:
        path = doc_dir / child
        if path.exists():
            shutil.rmtree(path)
    for child in files:
        path = doc_dir / child
        if path.exists():
            path.unlink()


def _resolve_extracted_outputs(
    doc_dir: Path, doc_id: str, parsed: ParsedDocument, preserve_extracted: bool
) -> tuple[int, int, list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Write tables/images/ocr-blocks from `parsed`, or keep the existing on-disk
    artifacts and carry their manifest metadata forward when preserving.

    The summary-retry path reconstructs `parsed` from storage with no
    pages/tables/images/ocr_blocks; regenerating would wipe those artifacts, so
    preserve_extracted keeps them untouched and reuses the prior manifest's
    table/image/layout entries. Returns
    (table_count, image_count, table_entries, image_entries, layout_entry).
    """
    if preserve_extracted:
        prior: dict[str, Any] = {}
        manifest_path = doc_dir / "manifest.json"
        if manifest_path.exists():
            try:
                prior = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                prior = {}
        table_entries = prior.get("tables") if isinstance(prior.get("tables"), list) else []
        image_entries = prior.get("images") if isinstance(prior.get("images"), list) else []
        layout_entry = (
            prior.get("layout")
            if isinstance(prior.get("layout"), dict)
            else _build_layout_manifest_entry(available=False)
        )
        return (
            int(prior.get("table_count") or 0),
            int(prior.get("image_count") or 0),
            table_entries,
            image_entries,
            layout_entry,
        )

    table_entries = _write_tables(doc_dir, parsed)
    if table_entries:
        _write_json(doc_dir / "tables.json", table_entries)
    image_entries = _write_images(doc_dir, parsed)
    if image_entries:
        _write_json(doc_dir / "images.json", image_entries)
    layout_entry = _build_layout_manifest_entry(available=False)
    if parsed.ocr_blocks is not None:
        layout_entry = _write_ocr_blocks_sidecar(
            doc_dir,
            OCRBlocksSidecar(
                doc_id=doc_id,
                pages=parsed.ocr_blocks.pages,
                version=parsed.ocr_blocks.version,
                coordinate_system=parsed.ocr_blocks.coordinate_system,
            ),
        )
    return (parsed.table_count, len(parsed.images), table_entries, image_entries, layout_entry)


def _build_doc_meta(
    doc_id: str,
    parsed: ParsedDocument,
    *,
    table_count: int,
    image_count: int,
    metadata: dict[str, Any] | None,
    source_record: dict[str, Any] | None,
    content_type: str | None,
    storage_path: str,
) -> dict[str, Any]:
    """Build the .meta.json dict shared by write_output / write_output_extract_only."""
    return {
        "doc_id": doc_id,
        "filename": parsed.filename,
        "file_type": parsed.file_type,
        "total_pages": parsed.total_pages,
        "section_count": len(parsed.sections),
        "ocr_page_count": parsed.ocr_page_count,
        "table_count": table_count,
        "image_count": image_count,
        "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "metadata": metadata or {},
        "parse_metadata": parsed.metadata or {},
        "source_file": source_record or {},
        "content_type": content_type or "General",
        "storage_path": storage_path,
        "sections": [
            {
                "index": sec.index,
                "sid": sec.sid,
                "title": sec.title,
                "page_range": sec.page_range,
                "page_start": _page_bounds(sec.page_range)[0],
                "page_end": _page_bounds(sec.page_range)[1],
                "char_count": len(sec.text),
                "image_refs": list(sec.image_refs),
            }
            for sec in parsed.sections
        ],
    }


def write_output(
    doc_id: str,
    parsed: ParsedDocument,
    digest: str,
    brief: str,
    output_dir: Path,
    tags: list[str] | None = None,
    source: str = "upload",
    original_path: str | None = None,
    metadata: dict[str, Any] | None = None,
    source_record: dict[str, Any] | None = None,
    content_type: str | None = None,
    preserve_extracted: bool = False,
):
    normalized_content_type = _normalize_content_type(content_type) if content_type else None
    storage_path = _doc_storage_rel_path(doc_id, normalized_content_type)
    doc_dir = output_dir / storage_path
    sections_dir = doc_dir / "sections"
    doc_dir.mkdir(parents=True, exist_ok=True)
    _reset_generated_output_dirs(doc_dir, include_extracted=not preserve_extracted)
    sections_dir.mkdir(exist_ok=True)
    output_locale = _parsed_document_locale(parsed)

    table_count, image_count, table_entries, image_entries, layout_entry = (
        _resolve_extracted_outputs(doc_dir, doc_id, parsed, preserve_extracted)
    )

    meta = _build_doc_meta(
        doc_id,
        parsed,
        table_count=table_count,
        image_count=image_count,
        metadata=metadata,
        source_record=source_record,
        content_type=normalized_content_type,
        storage_path=storage_path,
    )
    _write_json(doc_dir / ".meta.json", meta)
    logger.info(".meta.json written")

    _write_text(doc_dir / "digest.md", f"# {doc_id}: {parsed.filename}\n\n{digest}\n")
    logger.info("digest.md written")

    brief_header = tmpl_for_locale(
        output_locale,
        "brief_header",
        doc_id=doc_id,
        filename=parsed.filename,
        pages=parsed.total_pages,
        sections=len(parsed.sections),
        ocr_pages=parsed.ocr_page_count,
    )
    _write_text(doc_dir / "brief.md", brief_header + brief + "\n")
    logger.info("brief.md written")

    full_parts = [
        f"{'#' * min(sec.level + 1, 4)} {sec.title}\n\n{sec.text}" for sec in parsed.sections
    ]
    _write_text(
        doc_dir / "full.md", f"# {parsed.filename}\n\n" + "\n\n---\n\n".join(full_parts) + "\n"
    )
    logger.info("full.md written")

    for sec in parsed.sections:
        sec_filename = f"{sec.index:02d}-{sec.sid}-{_safe_filename(sec.title)}.md"
        sec_content = tmpl_for_locale(
            output_locale,
            "section_header",
            title=sec.title,
            index=sec.index,
            sid=sec.sid,
            page_range=sec.page_range,
        )
        if sec.summary:
            sec_content += tmpl_for_locale(
                output_locale, "section_summary_line", summary=sec.summary
            )
        sec_content += sec.text + "\n"
        _write_text(sections_dir / sec_filename, sec_content)
    logger.info(f"sections/ ({len(parsed.sections)} files)")

    # v3: content_hash
    full_text = "\n".join(sec.text for sec in parsed.sections)
    content_hash = (
        "sha256:" + hashlib.sha256(full_text.encode("utf-8", errors="ignore")).hexdigest()
    )

    # manifest.json + v3 provenance
    manifest = {
        "doc_id": doc_id,
        "filename": parsed.filename,
        "file_type": parsed.file_type,
        "source": source,
        "content_type": normalized_content_type or "General",
        "storage_path": storage_path,
        "tags": list(tags) if tags else [],
        "total_pages": parsed.total_pages,
        "section_count": len(parsed.sections),
        "table_count": table_count,
        "image_count": image_count,
        "ocr_page_count": parsed.ocr_page_count,
        "metadata": metadata or {},
        "parse_metadata": parsed.metadata or {},
        "source_file": source_record or {},
        "paths": {
            "digest": "digest.md",
            "brief": "brief.md",
            "full": "full.md",
            "sections_dir": "sections/",
            "sections": "sections.json",
            "tables_dir": "tables/",
            "tables": "tables.json",
            "images_dir": "images/",
            "images": "images.json",
            "ocr_blocks": layout_entry["ocr_blocks_path"],
        },
        "sections": [
            _build_section_entry(
                sec,
                summary_preview=(sec.summary[:120] + "...") if len(sec.summary) > 120 else sec.summary,
            )
            for sec in parsed.sections
        ],
        "tables": table_entries,
        "images": image_entries,
        "layout": layout_entry,
        "provenance": {
            "source": source,
            "source_url": original_path or str(parsed.filename),
            "created_at": meta["created_at"],
            "content_hash": content_hash,
            "source_kind": (source_record or {}).get("kind", ""),
            "source_filename": (source_record or {}).get("filename", ""),
            "source_ref": (source_record or {}).get("ref", ""),
            "source_sha256": (source_record or {}).get("sha256", ""),
            "source_size_bytes": (source_record or {}).get("size_bytes", 0),
        },
    }
    _attach_table_refs(manifest["sections"], table_entries)
    _write_json(doc_dir / "sections.json", manifest["sections"])
    _write_json(doc_dir / "manifest.json", manifest)
    logger.info("manifest.json written")

    _update_doc_index(
        output_dir,
        meta,
        digest,
        tags=tags,
        source=source,
        source_url=original_path or str(parsed.filename),
        content_hash=content_hash,
        metadata=metadata,
        source_record=source_record,
        content_type=normalized_content_type,
        storage_path=storage_path,
    )


def write_output_extract_only(
    doc_id: str,
    parsed: ParsedDocument,
    output_dir: Path,
    tags: list[str] | None = None,
    source: str = "upload",
    metadata: dict[str, Any] | None = None,
    source_record: dict[str, Any] | None = None,
    summary_placeholder: str | None = None,
    content_type: str | None = None,
    preserve_extracted: bool = False,
):
    normalized_content_type = _normalize_content_type(content_type) if content_type else None
    storage_path = _doc_storage_rel_path(doc_id, normalized_content_type)
    doc_dir = output_dir / storage_path
    sections_dir = doc_dir / "sections"
    doc_dir.mkdir(parents=True, exist_ok=True)
    _reset_generated_output_dirs(doc_dir, include_extracted=not preserve_extracted)
    sections_dir.mkdir(exist_ok=True)
    output_locale = _parsed_document_locale(parsed)

    table_count, image_count, table_entries, image_entries, layout_entry = (
        _resolve_extracted_outputs(doc_dir, doc_id, parsed, preserve_extracted)
    )

    meta = _build_doc_meta(
        doc_id,
        parsed,
        table_count=table_count,
        image_count=image_count,
        metadata=metadata,
        source_record=source_record,
        content_type=normalized_content_type,
        storage_path=storage_path,
    )
    _write_json(doc_dir / ".meta.json", meta)

    full_parts = [
        f"{'#' * min(sec.level + 1, 4)} {sec.title}\n\n{sec.text}" for sec in parsed.sections
    ]
    _write_text(
        doc_dir / "full.md", f"# {parsed.filename}\n\n" + "\n\n---\n\n".join(full_parts) + "\n"
    )

    for sec in parsed.sections:
        fn = f"{sec.index:02d}-{sec.sid}-{_safe_filename(sec.title)}.md"
        _write_text(sections_dir / fn, f"# {sec.title}\n\n{sec.text}\n")

    placeholder = summary_placeholder or _summary_placeholder_text("pending", locale=output_locale)
    _write_text(
        doc_dir / "digest.md",
        f"{tmpl_for_locale(output_locale, 'digest_title', doc_id=doc_id, filename=parsed.filename)}\n\n{placeholder}\n",
    )
    _write_text(
        doc_dir / "brief.md",
        f"{tmpl_for_locale(output_locale, 'digest_title', doc_id=doc_id, filename=parsed.filename)}\n\n{placeholder}\n",
    )

    full_text = "\n".join(sec.text for sec in parsed.sections)
    content_hash = (
        "sha256:" + hashlib.sha256(full_text.encode("utf-8", errors="ignore")).hexdigest()
    )

    manifest = {
        "doc_id": doc_id,
        "filename": parsed.filename,
        "file_type": parsed.file_type,
        "source": source,
        "content_type": normalized_content_type or "General",
        "storage_path": storage_path,
        "tags": list(tags) if tags else [],
        "total_pages": parsed.total_pages,
        "section_count": len(parsed.sections),
        "table_count": table_count,
        "image_count": image_count,
        "ocr_page_count": parsed.ocr_page_count,
        "metadata": metadata or {},
        "parse_metadata": parsed.metadata or {},
        "source_file": source_record or {},
        "paths": {
            "digest": "digest.md",
            "brief": "brief.md",
            "full": "full.md",
            "sections_dir": "sections/",
            "sections": "sections.json",
            "tables_dir": "tables/",
            "tables": "tables.json",
            "images_dir": "images/",
            "images": "images.json",
            "ocr_blocks": layout_entry["ocr_blocks_path"],
        },
        "sections": [
            _build_section_entry(sec, summary_preview="")
            for sec in parsed.sections
        ],
        "tables": table_entries,
        "images": image_entries,
        "layout": layout_entry,
        "provenance": {
            "source": source,
            "source_url": str(parsed.filename),
            "created_at": meta["created_at"],
            "content_hash": content_hash,
            "source_kind": (source_record or {}).get("kind", ""),
            "source_filename": (source_record or {}).get("filename", ""),
            "source_ref": (source_record or {}).get("ref", ""),
            "source_sha256": (source_record or {}).get("sha256", ""),
            "source_size_bytes": (source_record or {}).get("size_bytes", 0),
        },
    }
    _attach_table_refs(manifest["sections"], table_entries)
    _write_json(doc_dir / "sections.json", manifest["sections"])
    _write_json(doc_dir / "manifest.json", manifest)
    _update_doc_index(
        output_dir,
        meta,
        placeholder,
        tags=tags,
        source=source,
        source_url=str(parsed.filename),
        content_hash=content_hash,
        metadata=metadata,
        source_record=source_record,
        content_type=normalized_content_type,
        storage_path=storage_path,
    )
    logger.info(f"Text extraction complete (no summary): {doc_dir}")


def _generate_deferred_summary(
    doc_id: str,
    parsed: ParsedDocument,
    output_dir: Path,
    concurrency: int,
    tags: list[str] | None,
    metadata: dict[str, Any] | None,
    source_record: dict[str, Any] | None,
    content_type: str | None = None,
    preserve_extracted: bool = False,
) -> None:
    logger.info("Deferred summary thread started: %s", doc_id)
    attempts = _current_summary_attempts(parsed) + 1
    acquired = False
    try:
        if attempts > DEFERRED_SUMMARY_MAX_ATTEMPTS:
            raise RuntimeError(
                f"summary attempt limit reached ({DEFERRED_SUMMARY_MAX_ATTEMPTS})"
            )
        if _local_ocr_worker_initializing.is_set() and DEFERRED_SUMMARY_LOCAL_OCR_WAIT_SEC > 0:
            logger.info(
                "Deferred summary waiting for local OCR init: %s (timeout=%ss)",
                doc_id,
                DEFERRED_SUMMARY_LOCAL_OCR_WAIT_SEC,
            )
            _local_ocr_worker_ready.wait(timeout=DEFERRED_SUMMARY_LOCAL_OCR_WAIT_SEC)
        _deferred_summary_sem.acquire()
        acquired = True
        _set_summary_metadata(parsed, mode="defer", status="running", attempts=attempts)
        write_output_extract_only(
            doc_id,
            parsed,
            output_dir,
            tags=tags,
            source="upload",
            metadata=metadata,
            source_record=source_record,
            content_type=content_type,
            preserve_extracted=preserve_extracted,
            summary_placeholder=_summary_placeholder_text(
                "running", locale=_parsed_document_locale(parsed)
            ),
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                generate_summaries,
                parsed,
                concurrency,
                False,
            )
            digest_text, brief_text, _ = future.result(timeout=DEFERRED_SUMMARY_TIMEOUT_SEC)
        _set_summary_metadata(parsed, mode="defer", status="completed", attempts=attempts)
        write_output(
            doc_id,
            parsed,
            digest_text,
            brief_text,
            output_dir,
            tags=tags,
            source="upload",
            original_path=str(parsed.filename),
            metadata=metadata,
            source_record=source_record,
            content_type=content_type,
            preserve_extracted=preserve_extracted,
        )
        logger.info("Deferred summary complete: %s", doc_id)
    except Exception as exc:
        error_code, error_message = _classify_summary_error(exc)
        logger.exception("Deferred summary failed for %s [%s]: %s", doc_id, error_code, exc)
        _set_summary_metadata(
            parsed,
            mode="defer",
            status="failed",
            error=error_message,
            error_code=error_code,
            attempts=attempts,
        )
        write_output_extract_only(
            doc_id,
            parsed,
            output_dir,
            tags=tags,
            source="upload",
            metadata=metadata,
            source_record=source_record,
            content_type=content_type,
            preserve_extracted=preserve_extracted,
            summary_placeholder=_summary_placeholder_text(
                "failed", error_message, locale=_parsed_document_locale(parsed)
            ),
        )
    finally:
        if acquired:
            try:
                _deferred_summary_sem.release()
            except ValueError:
                pass


# ═══════════════════════════════════════════
# Utility functions
# ═══════════════════════════════════════════


def _build_layout_manifest_entry(
    *,
    available: bool,
    ocr_blocks_path: str = OCR_BLOCKS_SIDECAR_PATH,
    coordinate_system: str = OCR_BLOCKS_COORDINATE_SYSTEM,
    version: int = OCR_BLOCKS_SIDECAR_VERSION,
) -> dict[str, Any]:
    """Build low-token manifest metadata for layout sidecars."""
    return {
        "available": bool(available),
        "ocr_blocks_path": ocr_blocks_path if available else "",
        "version": int(version),
        "coordinate_system": coordinate_system,
    }


def _write_ocr_blocks_sidecar(doc_dir: Path, sidecar: OCRBlocksSidecar) -> dict[str, Any]:
    """Write the OCR geometry sidecar and return manifest metadata for discovery."""
    _write_json(doc_dir / OCR_BLOCKS_SIDECAR_PATH, sidecar.to_dict())
    return _build_layout_manifest_entry(
        available=True,
        ocr_blocks_path=OCR_BLOCKS_SIDECAR_PATH,
        coordinate_system=sidecar.coordinate_system,
        version=sidecar.version,
    )


def _safe_filename(title: str, max_len: int = 40) -> str:
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", title)
    safe = safe.strip().replace(" ", "-")
    return (safe[:max_len] if len(safe) > max_len else safe) or "untitled"


# ═══════════════════════════════════════════
# HTTP API（FastAPI）
# ═══════════════════════════════════════════

MAX_UPLOAD_BYTES = int(os.environ.get("MANTISFETCH_MAX_UPLOAD_MB", "200")) * 1024 * 1024
SEARCH_LIMIT_MAX = int(os.environ.get("MANTISFETCH_SEARCH_LIMIT_MAX", "200"))
STORE_SOURCE_FILES = os.environ.get("MANTISFETCH_STORE_SOURCE_FILES", "true").lower() not in {
    "0",
    "false",
    "no",
}

_TABLE_ID_RE = re.compile(r"^(table-)?\d+$")
_IMAGE_ID_RE = re.compile(r"^(IMG-)?\d{1,6}$", re.IGNORECASE)


def _validate_table_id(table_id: str) -> None:
    """Reject table_id values that could cause path traversal."""
    if not _TABLE_ID_RE.match(table_id):
        raise HTTPException(400, f"invalid table_id: {table_id!r}")


def _validate_image_id(image_id: str) -> None:
    """Reject image_id values that could cause path traversal."""
    if not _IMAGE_ID_RE.match(image_id):
        raise HTTPException(400, f"invalid image_id: {image_id!r}")


def _normalize_image_id(image_id: str) -> str:
    _validate_image_id(image_id)
    value = image_id.upper()
    if value.startswith("IMG-"):
        number = int(value.split("-", 1)[1])
    else:
        number = int(value)
    return f"IMG-{number:03d}"


# ---- Pydantic Models ----


class ParseResponse(BaseModel):
    doc_id: str
    content_type: str = "General"
    storage_path: str = ""
    filename: str
    file_type: str
    total_pages: int
    section_count: int
    table_count: int
    image_count: int = 0
    ocr_page_count: int
    digest: str
    manifest_path: str
    processing_time_sec: float
    source_ref: str | None = None
    # "miss"     — new parse, no collision
    # "replaced" — explicit doc_id collided and replace=true allowed overwrite
    dedup: str = "miss"


class SectionInfo(BaseModel):
    sid: str
    index: int
    title: str
    page_range: str
    char_count: int
    summary_preview: str = ""


class ManifestResponse(BaseModel):
    doc_id: str
    filename: str
    file_type: str | None = None
    source: str | None = None
    paths: dict[str, str]
    sections: list[dict[str, Any]]
    provenance: dict[str, Any] | None = None


class SearchResult(BaseModel):
    doc_id: str
    filename: str
    file_type: str
    content_type: str = "General"
    storage_path: str | None = None
    digest: str
    tags: list[str] = []
    source: str = "upload"
    created_at: str | None = None
    score: float = 1.0
    metadata: dict[str, Any] = Field(default_factory=dict)
    source_ref: str | None = None
    source_filename: str | None = None
    source_available: bool = False
    summary_mode: str | None = None
    summary_status: str | None = None
    summary_error_code: str | None = None
    sid: str | None = None
    section_title: str | None = None
    page_range: str | None = None
    page_start: int | None = None
    page_end: int | None = None
    snippet: str | None = None
    content: str | None = None


class SearchResponse(BaseModel):
    results: list[SearchResult]
    total: int


class SectionSearchRequest(BaseModel):
    q: str
    limit: int = Field(default=20, ge=1, le=200)
    include_content: bool = False
    case_sensitive: bool = False


class ChunkRequest(BaseModel):
    max_tokens_per_chunk: int = Field(default=4000, ge=200, le=50000)
    overlap_tokens: int = Field(default=200, ge=0, le=5000)
    merge_short_sections: bool = True
    merge_threshold_tokens: int = Field(default=500, ge=0, le=10000)
    include_text: bool = True


class SectionBatchRequest(BaseModel):
    # Length is validated in the handler (HTTPException 422) rather than via Field
    # min_length/max_length: those are Pydantic-v2-only on list fields and raise at
    # import under Pydantic v1, which FastAPI's dependency range still permits.
    sids: list[str]


# ---- FastAPI app ----

app = FastAPI(title="Doc Reader API", version="3.0.0")
PREWARM_LOCAL_OCR = os.environ.get("MANTISFETCH_PREWARM_LOCAL_OCR", "true").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}


@app.on_event("startup")
async def _startup_prewarm_local_ocr() -> None:
    if not PREWARM_LOCAL_OCR:
        return
    try:
        with _local_ocr_worker_lock:
            _get_local_ocr_worker()
        logger.info("Local OCR worker prewarmed")
    except Exception as exc:
        logger.warning("Local OCR worker prewarm skipped: %s", exc)


@app.on_event("startup")
async def _startup_backfill_manifest_tags() -> None:
    try:
        stats = _backfill_manifest_tags(_get_docs_dir())
    except Exception as exc:
        logger.warning("Manifest tags backfill skipped: %s", exc)
        return
    if stats["patched"] or stats["errors"]:
        logger.info("Manifest tags backfill: %s", stats)


def _parse_metadata_form(metadata: str | None) -> dict[str, Any]:
    if not metadata:
        return {}
    try:
        value = json.loads(metadata)
    except json.JSONDecodeError as exc:
        raise HTTPException(422, f"metadata must be a JSON object: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise HTTPException(422, "metadata must be a JSON object")
    return value


def _metadata_value_matches(actual: Any, expected: str) -> bool:
    expected_lower = expected.lower()
    if isinstance(actual, list):
        return any(_metadata_value_matches(item, expected) for item in actual)
    if actual is None:
        return expected_lower in {"", "null", "none"}
    return str(actual).lower() == expected_lower


def _metadata_filters_from_request(request: Request) -> dict[str, str]:
    filters: dict[str, str] = {}
    for key, value in request.query_params.multi_items():
        if key.startswith("metadata."):
            meta_key = key.split(".", 1)[1].strip()
            if meta_key:
                filters[meta_key] = value
    return filters


def _matches_metadata_filters(metadata: dict[str, Any], filters: dict[str, str]) -> bool:
    for key, expected in filters.items():
        if not _metadata_value_matches(metadata.get(key), expected):
            return False
    return True


def _page_bounds(page_range: str | None) -> tuple[int | None, int | None]:
    if not page_range:
        return None, None
    cleaned = page_range.strip()
    m = re.fullmatch(r"(?:p\.)?(\d+)(?:-(\d+))?", cleaned)
    if not m:
        return None, None
    start = int(m.group(1))
    end = int(m.group(2) or m.group(1))
    return start, end


def _build_section_entry(sec: Section, summary_preview: str = "") -> dict[str, Any]:
    page_start, page_end = _page_bounds(sec.page_range)
    text_hash = hashlib.sha256(sec.text.encode("utf-8", errors="ignore")).hexdigest()
    return {
        "sid": sec.sid,
        "index": sec.index,
        "order": sec.index,
        "title": sec.title,
        "page_range": sec.page_range,
        "page_start": page_start,
        "page_end": page_end,
        "char_count": len(sec.text),
        "token_estimate": _estimate_tokens(sec.text),
        "text_hash": f"sha256:{text_hash}",
        "table_refs": [],
        "image_refs": list(sec.image_refs),
        "ocr_quality": None,
        "type": "text",
        "summary_preview": summary_preview,
        "file": f"sections/{sec.index:02d}-{sec.sid}-{_safe_filename(sec.title)}.md",
    }


def _attach_table_refs(
    section_entries: list[dict[str, Any]], table_entries: list[dict[str, Any]]
) -> None:
    """Populate each section's table_refs with table_ids whose page range overlaps the section.

    Skipped when every section and every table collapse to a single identical page
    span (typical for DOCX/XLSX/CSV/HTML where the parser sets page_num=1 everywhere):
    in that case the page-overlap heuristic would attach every table to every
    section, which is worse than leaving table_refs empty.
    """
    if not table_entries:
        return

    def _coalesce_page(entry: dict[str, Any], primary: str, secondary: str) -> int | None:
        value = entry.get(primary)
        if value is None:
            value = entry.get(secondary)
        return value if isinstance(value, int) else None

    # The page-overlap heuristic degenerates when many sections share one page
    # span (typical for DOCX/XLSX/CSV/HTML where the parser sets page_num=1
    # everywhere): every section would receive every table_id. A single-section
    # doc with multiple tables, on the other hand, is correctly served by
    # attaching all tables to the one section, so we only skip when there is
    # both span-collapse AND more than one section.
    section_spans = {
        (sec.get("page_start"), sec.get("page_end"))
        for sec in section_entries
        if sec.get("page_start") is not None and sec.get("page_end") is not None
    }
    if len(section_spans) == 1 and len(section_entries) > 1:
        return

    for sec in section_entries:
        s_start = sec.get("page_start")
        s_end = sec.get("page_end")
        if s_start is None or s_end is None:
            continue
        refs: list[str] = []
        for entry in table_entries:
            t_start = _coalesce_page(entry, "page_start", "page")
            t_end = _coalesce_page(entry, "page_end", "page")
            if t_start is None or t_end is None:
                continue
            if t_start <= s_end and t_end >= s_start:
                refs.append(entry["table_id"])
        sec["table_refs"] = refs


def _build_table_entries(parsed: ParsedDocument) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for i, (page_num, table_md, is_ocr) in enumerate(
        ((p.page_num, table, p.is_ocr) for p in parsed.pages for table in p.tables),
        1,
    ):
        text_hash = hashlib.sha256(table_md.encode("utf-8", errors="ignore")).hexdigest()
        dimensions = _markdown_table_dimensions(table_md)
        entries.append(
            {
                "table_id": f"table-{i:02d}",
                "index": i,
                "page": page_num,
                "page_start": page_num,
                "page_end": page_num,
                "row_count": dimensions["row_count"],
                "column_count": dimensions["column_count"],
                "header_rows": dimensions["header_rows"],
                "has_header": dimensions["has_header"],
                "source": "ocr" if is_ocr else (parsed.file_type or "extracted"),
                "continued_from": None,
                "continued_to": None,
                "char_count": len(table_md),
                "token_estimate": _estimate_tokens(table_md),
                "text_hash": f"sha256:{text_hash}",
                "type": "markdown",
                "file": f"tables/table-{i:02d}.md",
            }
        )
    return entries


def _build_structured_table_entries(
    parsed: ParsedDocument,
    start_index: int,
) -> list[tuple[dict[str, Any], dict[str, Any], str]]:
    if parsed.ocr_blocks is None:
        return []
    entries: list[tuple[dict[str, Any], dict[str, Any], str]] = []
    candidates = _detect_table_candidates_from_ocr_blocks(parsed.ocr_blocks)
    for offset, candidate in enumerate(candidates, start_index):
        table_id = f"table-{offset:02d}"
        table_json = _reconstruct_table_from_candidate(parsed.ocr_blocks, candidate, table_id)
        table_md = _markdown_from_structured_table(table_json)
        text_hash = hashlib.sha256(table_md.encode("utf-8", errors="ignore")).hexdigest()
        entry = {
            "table_id": table_id,
            "index": offset,
            "page": table_json["page"],
            "page_start": table_json["page"],
            "page_end": table_json["page"],
            "row_count": table_json["row_count"],
            "column_count": table_json["column_count"],
            "header_rows": 1 if table_json["row_count"] else 0,
            "has_header": bool(table_json["row_count"]),
            "source": "layout",
            "continued_from": None,
            "continued_to": None,
            "char_count": len(table_md),
            "token_estimate": _estimate_tokens(table_md),
            "text_hash": f"sha256:{text_hash}",
            "type": "markdown",
            "file": f"tables/{table_id}.md",
            "json_file": f"tables/{table_id}.json",
            "bbox": table_json["bbox"],
            "ocr_block_refs": candidate.get("ocr_block_refs") or [],
        }
        entries.append((entry, table_json, table_md))
    _apply_table_continuation_links(entries)
    return entries


def _write_tables(doc_dir: Path, parsed: ParsedDocument) -> list[dict[str, Any]]:
    if not parsed.extract_tables:
        return []
    table_entries = _build_table_entries(parsed)
    structured_entries = _build_structured_table_entries(parsed, start_index=len(table_entries) + 1)
    if not table_entries and not structured_entries:
        return []
    tables_dir = doc_dir / "tables"
    tables_dir.mkdir(exist_ok=True)
    for entry in table_entries:
        page_num = entry["page"]
        table_index = entry["index"]
        table_md = next(
            t
            for idx, (_page, t) in enumerate(
                ((p.page_num, table) for p in parsed.pages for table in p.tables),
                1,
            )
            if idx == table_index
        )
        _write_text(
            tables_dir / f"{entry['table_id']}.md",
            f"# Table {table_index} (page {page_num})\n\n{table_md}\n",
        )
    for entry, table_json, table_md in structured_entries:
        _write_text(
            tables_dir / f"{entry['table_id']}.md",
            f"# Table {entry['index']} (page {entry['page']})\n\n{table_md}\n",
        )
        _write_json(tables_dir / f"{entry['table_id']}.json", table_json)
        table_entries.append(entry)
    return table_entries


def _embedded_image_entry(image: EmbeddedImage) -> dict[str, Any]:
    original_name = f"{image.image_id}.original{image.original_ext or '.bin'}"
    rendered_name = f"{image.image_id}{image.rendered_ext or '.png'}"
    ocr_name = f"{image.image_id}.ocr.txt"
    entry = {
        "image_id": image.image_id,
        "source": {
            "container": "word/document.xml",
            "media_path": image.media_path,
            "relationship_id": image.relationship_id,
            "paragraph_index": image.paragraph_index,
            "order": image.order,
        },
        "anchor": {
            "anchor_sid": image.anchor_sid,
            "near_heading": image.near_heading,
            "near_text": image.paragraph_text,
            "context_text": image.context_text,
            "section_title": image.section_title,
        },
        "media": {
            "original_type": image.original_type,
            "original_path": f"images/{original_name}",
            "rendered_type": image.rendered_type,
            "rendered_path": f"images/{rendered_name}" if image.rendered_bytes else "",
            "render_status": image.render_status,
            "render_error": image.render_error,
        },
        "inventory": {
            "width": image.width,
            "height": image.height,
            "aspect_ratio": image.aspect_ratio,
            "original_size_bytes": image.original_size_bytes,
            "rendered_size_bytes": image.rendered_size_bytes,
            "original_sha256": image.original_sha256,
            "rendered_sha256": image.rendered_sha256,
            "average_hash": image.average_hash,
            "context_keywords": list(image.context_keywords),
            "hints": list(image.inventory_hints),
        },
        "ocr": {
            "enabled": image.ocr_enabled,
            "backend": image.ocr_backend,
            "status": image.ocr_status,
            "text_path": f"images/{ocr_name}" if image.ocr_text else "",
            "text": image.ocr_text,
            "error": image.ocr_error,
        },
    }
    return entry


def _write_images(doc_dir: Path, parsed: ParsedDocument) -> list[dict[str, Any]]:
    if not parsed.images:
        return []
    images_dir = doc_dir / "images"
    images_dir.mkdir(exist_ok=True)
    entries: list[dict[str, Any]] = []
    for image in parsed.images:
        original_name = f"{image.image_id}.original{image.original_ext or '.bin'}"
        if image.original_bytes:
            (images_dir / original_name).write_bytes(image.original_bytes)
        if image.rendered_bytes:
            rendered_name = f"{image.image_id}{image.rendered_ext or '.png'}"
            (images_dir / rendered_name).write_bytes(image.rendered_bytes)
        if image.ocr_text:
            _write_text(images_dir / f"{image.image_id}.ocr.txt", image.ocr_text + "\n")
        entries.append(_embedded_image_entry(image))
    return entries


def _safe_source_filename(filename: str) -> str:
    base = Path(filename).name or "source.bin"
    suffix = Path(base).suffix
    stem = base[: -len(suffix)] if suffix else base
    # Strip leading dots so the result can never be ".", ".." or a hidden file
    # — joining it onto a directory must stay inside that directory.
    safe_stem = _safe_filename(stem, max_len=80).lstrip(".")
    if not safe_stem:
        safe_stem = "source"
    return f"{safe_stem}{suffix}" if suffix else safe_stem


def _persist_source_file(doc_dir: Path, filename: str, source_path: Path) -> dict[str, Any]:
    source_dir = doc_dir / "source"
    source_dir.mkdir(parents=True, exist_ok=True)
    safe_name = _safe_source_filename(filename)
    target = source_dir / safe_name
    # Stream copy + sha256 into a sibling .tmp file, then atomically rename.
    # Direct write to `target` would truncate an existing source file on disk
    # error during replace=true (manifest stays, source is corrupted).
    tmp = source_dir / (safe_name + ".tmp")
    hasher = hashlib.sha256()
    size = 0
    try:
        with open(source_path, "rb") as src, open(tmp, "wb") as dst:
            while chunk := src.read(1024 * 1024):
                dst.write(chunk)
                hasher.update(chunk)
                size += len(chunk)
        os.replace(tmp, target)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return {
        "kind": "upload",
        "filename": filename,
        "stored_filename": safe_name,
        "ref": f"source/{safe_name}",
        "sha256": hasher.hexdigest(),
        "size_bytes": size,
    }


def _load_ocr_sidecar_payload(doc_dir: Path, doc_id: str) -> dict[str, Any]:
    sidecar_path = doc_dir / OCR_BLOCKS_SIDECAR_PATH
    if not sidecar_path.exists():
        raise HTTPException(404, f"layout sidecar not found for {doc_id}")
    try:
        sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(500, f"layout sidecar unreadable for {doc_id}: {exc}") from exc
    if not isinstance(sidecar, dict):
        raise HTTPException(500, f"layout sidecar unreadable for {doc_id}")
    return sidecar


def _sidecar_page_summaries(sidecar: dict[str, Any]) -> list[dict[str, Any]]:
    pages = sidecar.get("pages") if isinstance(sidecar.get("pages"), list) else []
    summaries: list[dict[str, Any]] = []
    for page in pages:
        if not isinstance(page, dict):
            continue
        blocks = page.get("blocks") if isinstance(page.get("blocks"), list) else []
        summaries.append(
            {
                "page": int(page.get("page") or 0),
                "width": int(page.get("width") or 0),
                "height": int(page.get("height") or 0),
                "block_count": len(blocks),
            }
        )
    return summaries


def _load_tables_sidecar(doc_dir: Path) -> list[dict[str, Any]]:
    tables_path = doc_dir / "tables.json"
    if not tables_path.exists():
        return []
    try:
        tables = json.loads(tables_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(500, f"tables sidecar unreadable: {exc}") from exc
    if not isinstance(tables, list):
        raise HTTPException(500, "tables sidecar unreadable")
    return [table for table in tables if isinstance(table, dict)]


def _resolve_table_json_path(doc_dir: Path, rel_path: str) -> Path | None:
    if not isinstance(rel_path, str):
        return None
    raw_path = Path(rel_path)
    if raw_path.is_absolute() or raw_path.suffix != ".json" or ".." in raw_path.parts:
        return None
    tables_dir = (doc_dir / "tables").resolve()
    path = (doc_dir / raw_path).resolve()
    try:
        path.relative_to(tables_dir)
    except ValueError:
        return None
    return path


def _strip_section_storage_wrapper(raw: str) -> str:
    body = raw
    body = re.sub(
        r"^# .*\n\n\*\*(?:章节|Section) .*?\n\n",
        "",
        body,
        count=1,
        flags=re.S,
    )
    body = re.sub(
        r"^\*\*(?:摘要|Summary)\*\*: .*?\n\n---\n\n",
        "",
        body,
        count=1,
        flags=re.S,
    )
    return body.strip()


def _backfill_manifest_tags(docs_dir: Path) -> dict[str, int]:
    """Patch legacy per-doc manifest.json files that predate the tags-in-manifest fix.

    Reads `tags` from doc-index.json and writes them into each manifest that is
    missing the field. Idempotent: manifests that already carry a `tags` list
    are skipped.
    """
    stats = {"checked": 0, "patched": 0, "skipped": 0, "missing_index": 0, "errors": 0}
    if not docs_dir.exists():
        return stats

    tags_by_id: dict[str, list[str]] = {}
    for entry in _load_doc_index(docs_dir):
        entry_id = entry.get("id")
        if not isinstance(entry_id, str):
            continue
        raw = entry.get("tags")
        if isinstance(raw, list):
            tags_by_id[entry_id] = [str(t) for t in raw]

    for manifest_path in docs_dir.rglob("manifest.json"):
        # Skip visual-debug sidecar manifests
        if VISUAL_DEBUG_ARTIFACT_DIR.split("/")[0] in manifest_path.parts:
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            stats["errors"] += 1
            continue
        if not isinstance(manifest, dict) or not isinstance(manifest.get("doc_id"), str):
            continue
        stats["checked"] += 1
        if isinstance(manifest.get("tags"), list):
            stats["skipped"] += 1
            continue
        doc_id = manifest["doc_id"]
        tags = tags_by_id.get(doc_id)
        if tags is None:
            stats["missing_index"] += 1
            tags = []
        manifest["tags"] = tags
        try:
            tmp_path = manifest_path.with_suffix(".tmp")
            tmp_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.replace(tmp_path, manifest_path)
            stats["patched"] += 1
        except Exception:
            stats["errors"] += 1
    return stats


def _load_parsed_document_from_storage(docs_dir: Path, doc_id: str) -> tuple[ParsedDocument, dict[str, Any], dict[str, Any]]:
    doc_dir = _resolve_doc_dir(docs_dir, doc_id)
    manifest_path = doc_dir / "manifest.json"
    if not manifest_path.exists():
        raise HTTPException(404, t("doc_not_found", doc_id=doc_id))

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    sections_meta = manifest.get("sections")
    if not isinstance(sections_meta, list):
        raise HTTPException(500, f"manifest missing sections for {doc_id}")

    sections: list[Section] = []
    for sec in sorted(
        (item for item in sections_meta if isinstance(item, dict)),
        key=lambda item: int(item.get("index", 0)),
    ):
        rel_path = sec.get("file")
        section_path = _resolve_manifest_section_path(doc_dir, rel_path)
        if not section_path or not section_path.exists():
            raise HTTPException(500, f"section file missing for {doc_id}: {rel_path}")

        raw = section_path.read_text(encoding="utf-8")
        lines = raw.splitlines()
        title = str(sec.get("title") or "")
        text = _strip_section_storage_wrapper(raw)
        if lines and lines[0].startswith("#"):
            title = lines[0].lstrip("#").strip() or title

        sections.append(
            Section(
                index=int(sec.get("index", len(sections) + 1)),
                title=title or f"Section {len(sections) + 1}",
                level=1,
                text=text,
                page_range=str(sec.get("page_range") or ""),
                sid=str(sec.get("sid") or ""),
                image_refs=[
                    str(value)
                    for value in sec.get("image_refs", [])
                    if isinstance(value, str)
                ],
            )
        )

    parsed = ParsedDocument(
        filename=str(manifest.get("filename") or doc_id),
        file_type=str(manifest.get("file_type") or "pdf"),
        total_pages=int((manifest.get("parse_metadata") or {}).get("total_pages") or 0),
        pages=[],
        sections=sections,
        ocr_page_count=int((manifest.get("parse_metadata") or {}).get("ocr_page_count") or 0),
        table_count=0,
        metadata=dict(manifest.get("parse_metadata") or {}),
    )

    if not parsed.total_pages:
        meta_path = doc_dir / ".meta.json"
        if meta_path.exists():
            raw_meta = json.loads(meta_path.read_text(encoding="utf-8"))
            parsed.total_pages = int(raw_meta.get("total_pages") or 0)
            parsed.ocr_page_count = int(raw_meta.get("ocr_page_count") or parsed.ocr_page_count)
            parsed.table_count = int(raw_meta.get("table_count") or 0)

    metadata = manifest.get("metadata") if isinstance(manifest.get("metadata"), dict) else {}
    source_record = manifest.get("source_file") if isinstance(manifest.get("source_file"), dict) else {}
    return parsed, metadata, source_record


def _filter_documents(
    documents: list[dict[str, Any]],
    *,
    file_type: str | None = None,
    content_type: str | None = None,
    tags: str | None = None,
    metadata_filters: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    filtered = documents
    if file_type:
        filtered = [d for d in filtered if d.get("file_type") == file_type]
    if content_type:
        normalized_content_type = _normalize_content_type(content_type)
        filtered = [
            d
            for d in filtered
            if _normalize_content_type(d.get("content_type") or "General") == normalized_content_type
        ]
    if tags:
        tag_list = [t.strip() for t in tags.split(",") if t.strip()]
        filtered = [d for d in filtered if any(t in (d.get("tags") or []) for t in tag_list)]
    if metadata_filters:
        filtered = [
            d
            for d in filtered
            if _matches_metadata_filters(d.get("metadata") or {}, metadata_filters)
        ]
    return filtered


def _resolve_manifest_section_path(doc_dir: Path, rel_path: str) -> Path | None:
    if not isinstance(rel_path, str):
        return None
    raw_path = Path(rel_path)
    if raw_path.is_absolute() or raw_path.suffix != ".md":
        return None
    sections_dir = (doc_dir / "sections").resolve()
    section_path = (doc_dir / raw_path).resolve()
    try:
        section_path.relative_to(sections_dir)
    except ValueError:
        return None
    return section_path


def _load_section_records(docs_dir: Path, doc_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    _validate_doc_id(doc_id)
    doc_dir = _resolve_doc_dir(docs_dir, doc_id)
    manifest_path = doc_dir / "manifest.json"
    if not manifest_path.exists():
        raise HTTPException(404, t("doc_not_found", doc_id=doc_id))
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise HTTPException(500, f"manifest unreadable for {doc_id}: {exc}") from exc
    sections_meta = manifest.get("sections")
    if not isinstance(sections_meta, list):
        raise HTTPException(500, f"manifest missing sections for {doc_id}")

    records: list[dict[str, Any]] = []
    for sec in sorted(
        (item for item in sections_meta if isinstance(item, dict)),
        key=lambda item: int(item.get("index", item.get("order", 0)) or 0),
    ):
        section_path = _resolve_manifest_section_path(doc_dir, sec.get("file", ""))
        if not section_path or not section_path.exists():
            continue
        raw = section_path.read_text(encoding="utf-8")
        text = _strip_section_storage_wrapper(raw)
        page_start = sec.get("page_start")
        page_end = sec.get("page_end")
        if page_start is None and page_end is None:
            page_start, page_end = _page_bounds(sec.get("page_range"))
        token_estimate = int(sec.get("token_estimate") or _estimate_tokens(text))
        record = {
            **sec,
            "doc_id": doc_id,
            "text": text,
            "page_start": page_start,
            "page_end": page_end,
            "char_count": len(text),
            "token_estimate": token_estimate,
        }
        records.append(record)
    return manifest, records


def _make_chunk(
    doc_id: str,
    index: int,
    records: list[dict[str, Any]],
    text: str,
    *,
    include_text: bool,
) -> dict[str, Any]:
    section_ids = [str(r.get("sid") or "") for r in records if r.get("sid")]
    page_starts = [r.get("page_start") for r in records if isinstance(r.get("page_start"), int)]
    page_ends = [r.get("page_end") for r in records if isinstance(r.get("page_end"), int)]
    chunk = {
        "chunk_id": f"chunk-{index:04d}",
        "doc_id": doc_id,
        "index": index,
        "section_ids": section_ids,
        "title": " / ".join(str(r.get("title") or "") for r in records[:3]).strip(" / "),
        "page_start": min(page_starts) if page_starts else None,
        "page_end": max(page_ends) if page_ends else None,
        "char_count": len(text),
        "token_estimate": _estimate_tokens(text),
        "provenance": [
            {
                "doc_id": doc_id,
                "sid": r.get("sid"),
                "title": r.get("title"),
                "page_start": r.get("page_start"),
                "page_end": r.get("page_end"),
                "token_estimate": r.get("token_estimate"),
            }
            for r in records
        ],
    }
    if include_text:
        chunk["text"] = text
    return chunk


def _split_text_by_token_estimate(
    record: dict[str, Any],
    *,
    max_tokens: int,
    overlap_tokens: int,
    include_text: bool,
    start_index: int,
) -> list[dict[str, Any]]:
    text = str(record.get("text") or "")
    paragraphs = [p.strip() for p in re.split(r"\n{2,}", text) if p.strip()]
    if not paragraphs:
        paragraphs = [text]

    chunks: list[dict[str, Any]] = []
    current_parts: list[str] = []
    current_tokens = 0
    chunk_index = start_index
    for para in paragraphs:
        para_tokens = _estimate_tokens(para)
        if current_parts and current_tokens + para_tokens > max_tokens:
            chunk_text = "\n\n".join(current_parts).strip()
            chunks.append(
                _make_chunk(
                    str(record["doc_id"]),
                    chunk_index,
                    [record],
                    chunk_text,
                    include_text=include_text,
                )
            )
            chunk_index += 1
            if overlap_tokens:
                overlap_chars = max(0, int(overlap_tokens * 4))
                current_parts = [chunk_text[-overlap_chars:]] if overlap_chars else []
                current_tokens = _estimate_tokens(current_parts[0]) if current_parts else 0
            else:
                current_parts = []
                current_tokens = 0
        current_parts.append(para)
        current_tokens += para_tokens

    if current_parts:
        chunks.append(
            _make_chunk(
                str(record["doc_id"]),
                chunk_index,
                [record],
                "\n\n".join(current_parts).strip(),
                include_text=include_text,
            )
        )
    return chunks


def _chunk_sections(
    doc_id: str,
    records: list[dict[str, Any]],
    request: ChunkRequest,
) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    current_records: list[dict[str, Any]] = []
    current_parts: list[str] = []
    current_tokens = 0

    def flush() -> None:
        nonlocal current_records, current_parts, current_tokens
        if not current_records:
            return
        chunks.append(
            _make_chunk(
                doc_id,
                len(chunks) + 1,
                current_records,
                "\n\n".join(current_parts).strip(),
                include_text=request.include_text,
            )
        )
        current_records = []
        current_parts = []
        current_tokens = 0

    for record in records:
        text = str(record.get("text") or "")
        tokens = int(record.get("token_estimate") or _estimate_tokens(text))
        if tokens > request.max_tokens_per_chunk:
            flush()
            split_chunks = _split_text_by_token_estimate(
                record,
                max_tokens=request.max_tokens_per_chunk,
                overlap_tokens=request.overlap_tokens,
                include_text=request.include_text,
                start_index=len(chunks) + 1,
            )
            chunks.extend(split_chunks)
            continue

        can_merge = (
            request.merge_short_sections
            and current_records
            and current_tokens + tokens <= request.max_tokens_per_chunk
            and (current_tokens < request.merge_threshold_tokens or tokens < request.merge_threshold_tokens)
        )
        if not current_records or can_merge:
            current_records.append(record)
            current_parts.append(text)
            current_tokens += tokens
            continue

        flush()
        current_records.append(record)
        current_parts.append(text)
        current_tokens = tokens

    flush()
    return chunks


def _make_snippet(text: str, query: str, radius: int = 90) -> str:
    haystack = text.strip()
    if not haystack:
        return ""
    idx = haystack.lower().find(query.lower())
    if idx == -1:
        return haystack[: radius * 2].strip()
    start = max(0, idx - radius)
    end = min(len(haystack), idx + len(query) + radius)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(haystack) else ""
    return prefix + haystack[start:end].strip() + suffix


def _search_score(*parts: tuple[bool, float]) -> float:
    return sum(weight for matched, weight in parts if matched)


@app.get("/health")
async def health():
    return {
        "ok": True,
        "version": "3.0.0",
        "docs_dir": _mask_path(_get_docs_dir()),
        "supported_formats": SUPPORTED_FORMATS,
    }


@app.post("/parse", response_model=ParseResponse)
async def api_parse_doc(
    file: UploadFile = File(...),
    doc_id: str | None = Form(None),
    content_type: str = Form("General"),
    generate_summary: bool = Form(True),
    summary_mode: str | None = Form(None),
    document_profile: str | None = Form(None),
    field_ocr_config: str | None = Form(None),
    parse_mode: str | None = Form(None),
    id_strategy: str | None = Form(None),
    skip_ocr_pages: str | None = Form(None),
    force_ocr: bool = Form(False),
    ocr_pages: str | None = Form(None),
    extract_tables: bool = Form(True),
    extract_images: bool = Form(False),
    ocr_images: bool = Form(False),
    image_ocr_backend: str = Form("auto"),
    max_images: int = Form(200),
    max_ocr_images: int = Form(WORD_IMAGE_OCR_MAX_IMAGES),
    max_tables_per_page: int = Form(3),
    concurrency: int = Form(3),
    tags: str | None = Form(None),  # JSON array string: '["Q3","financial"]'
    metadata: str | None = Form(None),  # JSON object string
    replace: bool = Form(False),
):
    """Parse uploaded document (PDF/DOCX), return structured result."""
    if _parse_sem.locked():
        raise HTTPException(429, "too many concurrent parse requests")

    docs_dir = _get_docs_dir()
    filename = file.filename or "unknown"
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise HTTPException(422, t("unsupported_format", fmt=suffix))

    # Run cheap form validations before _resolve_doc_id so a 422 doesn't
    # advance the counter (or otherwise reserve an id) for a request that
    # was never going to succeed. Also fails fast before streaming the body.
    parsed_metadata = _parse_metadata_form(metadata)
    requested_content_type = _normalize_content_type(
        content_type or str(parsed_metadata.get("content_type") or "General")
    )
    selected_image_ocr_backend = (image_ocr_backend or "auto").strip().lower()
    if selected_image_ocr_backend not in {"auto", "local", "llm"}:
        raise HTTPException(422, "image_ocr_backend must be one of: auto, local, llm")
    # Validate a client-supplied parse_mode here (422); env/profile defaults are
    # resolved later and a bad one there is a 500 server-config error.
    client_parse_mode = str(parse_mode or parsed_metadata.get("parse_mode") or "").strip().lower()
    if client_parse_mode and client_parse_mode not in {"fast", "accurate", "full"}:
        raise HTTPException(422, "parse_mode must be one of: fast, accurate, full.")

    # Reject explicit-doc_id conflicts before streaming the body — the resolver
    # returns the input verbatim for explicit ids, so the existence check
    # doesn't need d_id. Catching this here means a conflicting upload can't
    # waste disk + `_upload_sem` writing a scratch file just to be 409'd.
    will_replace = bool(doc_id and _doc_exists_anywhere(docs_dir, doc_id))
    if will_replace and not replace:
        raise HTTPException(
            409,
            f"doc_id '{doc_id}' already exists. "
            f"Pass replace=true to overwrite, or omit doc_id to get a fresh one.",
        )
    dedup_status = "replaced" if will_replace else "miss"

    # Stream the upload into a scratch file so the in-memory buffer never
    # grows beyond one chunk; without this, requests queued on the per-doc
    # lock or _parse_sem would each pin MAX_UPLOAD_BYTES until parse ends.
    scratch_path: Path | None = None
    # Place the scratch tempfile on the docs volume rather than the system tmp
    # dir; in container/k8s setups /tmp is often a small tmpfs while docs_dir
    # is the durable mount where the upload would eventually land anyway.
    scratch_dir = docs_dir / ".upload-tmp"
    scratch_dir.mkdir(parents=True, exist_ok=True)
    async with _upload_sem:
        scratch_fd, scratch_path_str = tempfile.mkstemp(
            suffix=suffix, prefix="mantisfetch-upload-", dir=str(scratch_dir)
        )
        scratch_path = Path(scratch_path_str)
        total_size = 0
        upload_ok = False
        try:
            # Wrap the fd in a Python file object so `.write()` handles partial
            # writes internally — raw `os.write` may return short on some
            # filesystems and silently truncate.
            with os.fdopen(scratch_fd, "wb") as dst:
                while True:
                    chunk = await file.read(1024 * 1024)
                    if not chunk:
                        break
                    total_size += len(chunk)
                    if total_size > MAX_UPLOAD_BYTES:
                        raise HTTPException(
                            413,
                            f"file too large: {total_size} bytes (max {MAX_UPLOAD_BYTES})",
                        )
                    dst.write(chunk)
            upload_ok = True
        finally:
            # The outer try/finally below only catches errors raised after
            # upload completes. Clean up the scratch file here if the upload
            # itself failed (413, read error, etc.) so /tmp doesn't accumulate
            # `mantisfetch-upload-*` files from rejected requests.
            if not upload_ok:
                try:
                    scratch_path.unlink()
                except OSError:
                    pass
                scratch_path = None

    try:
        # The early-reject above was a snapshot before the upload started.
        # Re-check now so a burst that crowded in past the snapshot fails fast
        # instead of queueing scratch files against `_parse_sem`.
        if _parse_sem.locked():
            raise HTTPException(429, "too many concurrent parse requests")
        # Pre-validate the Word OCR image limit for .docx so the 422 fires
        # before _resolve_doc_id advances .counter (issue #67). The check
        # reads two XML files from the docx zip (~50ms) and only runs when
        # OCR was actually requested. .doc files skip this — converting them
        # to .docx for the count would cost 1-5s of LibreOffice startup, so
        # their (rare) counter gap is accepted.
        if suffix == ".docx" and extract_images and ocr_images and scratch_path is not None:
            # Mirror the 0..1000 clamp the in-lock path applies, so requests
            # with max_images=2000 don't get a 422 here that the lock would
            # let through after clamping (max_images -> 1000).
            early_max_images = max(0, min(int(max_images), 1000))
            early_max_ocr_images = max(0, min(int(max_ocr_images), 1000))
            early_embedded = _count_word_embedded_image_references(scratch_path)
            early_requested = min(early_embedded, early_max_images)
            if early_requested > early_max_ocr_images:
                raise HTTPException(
                    422,
                    (
                        "word embedded image OCR refused: "
                        f"{early_requested} requested images exceeds "
                        f"max_ocr_images={early_max_ocr_images} "
                        f"(embedded_image_count={early_embedded}, max_images={early_max_images}). "
                        "Retry with ocr_images=false, a higher max_ocr_images value, "
                        "or a lower max_images value."
                    ),
                )
        # Atomically resolve the doc_id and reserve it via the per-doc lock dict.
        # Holding `_doc_id_parse_locks_guard` around resolve + insert means
        # concurrent same-explicit-id requests serialize, and concurrent
        # source_filename uploads can't both pick the same id (the second sees
        # the first's reservation via `_next_filename_doc_id`'s
        # `in _doc_id_parse_locks` check and rolls to the next candidate).
        async with _doc_id_parse_locks_guard:
            d_id = _resolve_doc_id(docs_dir, filename, doc_id, id_strategy)
            d_id_lock = _doc_id_parse_locks.get(d_id)
            if d_id_lock is None:
                d_id_lock = asyncio.Lock()
                _doc_id_parse_locks[d_id] = d_id_lock

        # Lock outside _parse_sem so waiters don't burn a parse slot — otherwise
        # unrelated documents get 429'd while one same-id queue drains.
        async with d_id_lock, _parse_sem:
            t0 = time.time()
            # Guard against silent overwrite when the caller pins an explicit
            # Re-check existence inside d_id_lock to close the TOCTOU race
            # between the early check (before upload) and this point: two
            # concurrent same-explicit-id requests both saw the id as free
            # before either had written a manifest, then one acquired the
            # lock and wrote — the second must not silently overwrite.
            if doc_id:
                exists_now = _doc_exists_anywhere(docs_dir, d_id)
                if exists_now and not replace:
                    raise HTTPException(
                        409,
                        f"doc_id '{doc_id}' already exists. "
                        f"Pass replace=true to overwrite, or omit doc_id to get a fresh one.",
                    )
                if exists_now and not will_replace:
                    will_replace = True
                    dedup_status = "replaced"
            if will_replace:
                # Preserve the existing doc's content_type so replace=true can't
                # leave orphans in a different category directory. The caller's
                # content_type is silently overridden because they already
                # asked to replace this specific doc.
                existing_content_type = _doc_content_type(docs_dir, doc_id)
                if requested_content_type != existing_content_type:
                    logger.info(
                        "replace=true: overriding requested content_type '%s' with existing '%s' for doc_id %s",
                        requested_content_type, existing_content_type, doc_id,
                    )
                selected_content_type = existing_content_type
                parsed_metadata["content_type"] = selected_content_type
            else:
                selected_content_type = requested_content_type
                parsed_metadata.setdefault("content_type", selected_content_type)
            # Effective parse mode (incl. env fallback) — also drives summary-mode
            # selection, so it must reflect env. A bad *client* parse_mode is
            # already rejected with 422 in the early form validation above.
            requested_parse_mode = (
                str(parse_mode or parsed_metadata.get("parse_mode") or "").strip()
                or os.environ.get("MANTISFETCH_PDF_PARSE_MODE", "").strip()
                or None
            )
            field_ocr_profile = (
                str(document_profile or parsed_metadata.get("document_profile") or "").strip()
                or str(parsed_metadata.get("field_ocr_profile") or "").strip()
                or os.environ.get("MANTISFETCH_FIELD_OCR_PROFILE", "").strip()
                or None
            )
            if field_ocr_profile:
                canonical_profile = _DOCUMENT_PROFILE_ALIASES.get(field_ocr_profile, field_ocr_profile)
                if canonical_profile != field_ocr_profile:
                    field_ocr_profile = canonical_profile
                    if parsed_metadata.get("document_profile"):
                        parsed_metadata["document_profile"] = canonical_profile
            requested_field_ocr_config = (
                str(field_ocr_config or parsed_metadata.get("field_ocr_config") or "").strip()
                or os.environ.get("MANTISFETCH_FIELD_OCR_CONFIG", "").strip()
                or None
            )
            requested_summary_mode = (
                str(summary_mode or parsed_metadata.get("summary_mode") or "").strip()
                or None
            )
            for key, value in {
                "summary_mode": requested_summary_mode,
                "document_profile": field_ocr_profile,
                "field_ocr_config": requested_field_ocr_config,
                "parse_mode": requested_parse_mode,
                "id_strategy": id_strategy,
                "skip_ocr_pages": skip_ocr_pages,
                "extract_images": str(bool(extract_images)).lower() if extract_images else "",
                "ocr_images": str(bool(ocr_images)).lower() if ocr_images else "",
                "image_ocr_backend": image_ocr_backend if ocr_images else "",
                "max_images": str(max_images) if extract_images else "",
                "max_ocr_images": str(max_ocr_images) if ocr_images else "",
            }.items():
                if value:
                    parsed_metadata.setdefault(key, value)
            max_images = max(0, min(int(max_images), 1000))
            max_ocr_images = max(0, min(int(max_ocr_images), 1000))
            manual_blank_pages_spec = (
                _metadata_page_range_spec(skip_ocr_pages)
                or _metadata_page_range_spec(parsed_metadata.get("skip_ocr_pages"))
                or _metadata_page_range_spec(parsed_metadata.get("blank_pages"))
                or _metadata_page_range_spec(parsed_metadata.get("near_blank_pages"))
                or _metadata_page_range_spec(parsed_metadata.get("manual_blank_pages"))
            )

            profile = _load_document_profile(field_ocr_profile, requested_field_ocr_config)
            summary_mode = _resolve_summary_mode(
                profile=profile,
                parse_mode=requested_parse_mode,
                generate_summary=generate_summary,
                requested_mode=requested_summary_mode,
            )

            # Parse tags
            parsed_tags: list[str] = []
            if tags:
                try:
                    parsed_tags = json.loads(tags)
                except json.JSONDecodeError:
                    parsed_tags = [t.strip() for t in tags.split(",") if t.strip()]

            try:
                doc_storage_dir = _doc_storage_dir(docs_dir, d_id, selected_content_type)
                tmp_dir = doc_storage_dir / ".tmp"
                tmp_dir.mkdir(parents=True, exist_ok=True)
                # Sanitize to a basename: the raw multipart filename is
                # attacker-controlled and must never be joined onto a path
                # (e.g. "../../../etc/x.pdf" would escape the scratch dir).
                tmp_path = tmp_dir / _safe_source_filename(filename)
                shutil.move(str(scratch_path), str(tmp_path))
                scratch_path = None
            except HTTPException:
                raise
            except Exception as e:
                raise HTTPException(500, t("file_save_failed", err=str(e)))

            # Parse
            try:
                loop = asyncio.get_event_loop()
                if suffix == ".pdf":
                    should_prewarm_local_ocr = False
                    if PREWARM_LOCAL_OCR:
                        try:
                            should_prewarm_local_ocr = _should_prewarm_local_ocr_for_pdf(
                                tmp_path,
                                profile=profile,
                                parse_mode=requested_parse_mode,
                                force_ocr=force_ocr,
                                ocr_pages_spec=ocr_pages,
                                manual_blank_pages_spec=manual_blank_pages_spec,
                                ocr_threshold=OCR_THRESHOLD,
                            )
                        except Exception as exc:
                            logger.warning("Local OCR prewarm planning skipped before parse: %s", exc)
                    if should_prewarm_local_ocr:
                        try:
                            with _local_ocr_worker_lock:
                                _get_local_ocr_worker()
                            logger.info("Local OCR worker prewarmed before PDF parse")
                        except Exception as exc:
                            logger.warning("Local OCR worker prewarm skipped before parse: %s", exc)
                    parsed = await loop.run_in_executor(
                        None,
                        lambda: parse_pdf(
                            tmp_path,
                            force_ocr=force_ocr,
                            ocr_threshold=OCR_THRESHOLD,
                            ocr_pages_spec=ocr_pages,
                            extract_tables=extract_tables,
                            max_tables_per_page=max_tables_per_page,
                            concurrency=concurrency,
                            cache_dir=doc_storage_dir,
                            field_ocr_profile=field_ocr_profile,
                            field_ocr_config=requested_field_ocr_config,
                            parse_mode=requested_parse_mode,
                            manual_blank_pages_spec=manual_blank_pages_spec,
                        ),
                    )
                elif suffix in (".doc", ".docx"):
                    # LibreOffice conversion shells out and can take seconds —
                    # run it off the event loop.
                    word_path = (
                        await loop.run_in_executor(None, _convert_legacy_office, tmp_path, "docx")
                        if suffix == ".doc"
                        else tmp_path
                    )
                    if extract_images:
                        embedded_image_count = _count_word_embedded_image_references(word_path)
                        requested_ocr_image_count = min(embedded_image_count, max_images)
                        parsed_metadata.setdefault("embedded_image_count", embedded_image_count)
                        parsed_metadata.setdefault("requested_image_count", requested_ocr_image_count)
                        parsed_metadata.setdefault(
                            "image_inventory_truncated",
                            bool(embedded_image_count > requested_ocr_image_count),
                        )
                        if ocr_images:
                            parsed_metadata.setdefault(
                                "requested_ocr_image_count", requested_ocr_image_count
                            )
                        if ocr_images and requested_ocr_image_count > max_ocr_images:
                            raise HTTPException(
                                422,
                                (
                                    "word embedded image OCR refused: "
                                    f"{requested_ocr_image_count} requested images exceeds "
                                    f"max_ocr_images={max_ocr_images} "
                                    f"(embedded_image_count={embedded_image_count}, max_images={max_images}). "
                                    "Retry with ocr_images=false, a higher max_ocr_images value, "
                                    "or a lower max_images value."
                                ),
                            )
                    parsed = await loop.run_in_executor(
                        None,
                        lambda: parse_word(
                            word_path,
                            extract_tables=extract_tables,
                            profile=profile,
                            extract_images=extract_images,
                            ocr_images=ocr_images,
                            image_ocr_backend=selected_image_ocr_backend,
                            max_images=max_images,
                        ),
                    )
                elif suffix in (".xlsx", ".xls"):
                    parsed = await loop.run_in_executor(None, lambda: parse_xlsx(tmp_path))
                elif suffix == ".csv":
                    parsed = await loop.run_in_executor(None, lambda: parse_csv(tmp_path))
                elif suffix == ".ppt":
                    parsed = await loop.run_in_executor(
                        None, lambda: parse_generic(_convert_legacy_office(tmp_path, "pptx"), profile=profile)
                    )
                else:  # .pptx, .html, .htm, etc.
                    parsed = await loop.run_in_executor(None, lambda: parse_generic(tmp_path, profile=profile))
                # Persist the source while tmp_path still exists; the finally
                # below removes tmp_dir, and we no longer hold the bytes in
                # memory after the streaming upload.
                source_record = (
                    await loop.run_in_executor(
                        None, _persist_source_file, doc_storage_dir, filename, tmp_path
                    )
                    if STORE_SOURCE_FILES else {}
                )
            except HTTPException:
                raise
            except Exception as e:
                raise HTTPException(500, t("parse_failed", err=str(e)))
            finally:
                # Cleanup temp file
                try:
                    shutil.rmtree(tmp_dir, ignore_errors=True)
                except Exception:
                    pass

            parsed.filename = filename
            parsed.file_type = suffix.lstrip(".")
            if suffix in {".doc", ".ppt"}:
                parsed.metadata["converted_to"] = "docx" if suffix == ".doc" else "pptx"
            parsed_locale = _parsed_document_locale(parsed)

            # Summarize + write
            digest = _summary_placeholder_text("pending", locale=parsed_locale)
            try:
                if summary_mode == "sync":
                    _set_summary_metadata(parsed, mode="sync", status="running")
                    digest_text, brief_text, _ = await loop.run_in_executor(
                        None, lambda: generate_summaries(parsed, concurrency=concurrency)
                    )
                    digest = digest_text
                    _set_summary_metadata(parsed, mode="sync", status="completed")
                    await loop.run_in_executor(
                        None,
                        lambda: write_output(
                            d_id,
                            parsed,
                            digest_text,
                            brief_text,
                            docs_dir,
                            tags=parsed_tags,
                            source="upload",
                            original_path=str(filename),
                            metadata=parsed_metadata,
                            source_record=source_record,
                            content_type=selected_content_type,
                        ),
                    )
                else:
                    status = "disabled" if summary_mode == "off" else "pending"
                    _set_summary_metadata(parsed, mode=summary_mode, status=status)
                    await loop.run_in_executor(
                        None,
                        lambda: write_output_extract_only(
                            d_id,
                            parsed,
                            docs_dir,
                            tags=parsed_tags,
                            source="upload",
                            metadata=parsed_metadata,
                            source_record=source_record,
                            content_type=selected_content_type,
                        ),
                    )
                    if summary_mode == "defer":
                        worker = threading.Thread(
                            target=_generate_deferred_summary,
                            args=(
                                d_id,
                                parsed,
                                docs_dir,
                                concurrency,
                                parsed_tags,
                                parsed_metadata,
                                source_record,
                                selected_content_type,
                            ),
                            daemon=True,
                        )
                        worker.start()
                        logger.info("Deferred summary scheduled: %s", d_id)
            except Exception as e:
                raise HTTPException(500, t("write_failed", err=str(e)))

            elapsed = round(time.time() - t0, 2)
            return ParseResponse(
                doc_id=d_id,
                filename=parsed.filename,
                file_type=parsed.file_type,
                total_pages=parsed.total_pages,
                section_count=len(parsed.sections),
                table_count=parsed.table_count,
                image_count=len(parsed.images),
                ocr_page_count=parsed.ocr_page_count,
                digest=digest[:300],
                manifest_path=f"docs/{_doc_storage_rel_path(d_id, selected_content_type)}/manifest.json",
                processing_time_sec=elapsed,
                source_ref=source_record.get("ref"),
                content_type=selected_content_type,
                storage_path=_doc_storage_rel_path(d_id, selected_content_type),
                dedup=dedup_status,
            )
    finally:
        if scratch_path is not None and scratch_path.exists():
            try:
                scratch_path.unlink()
            except OSError:
                pass


# ---- Library query endpoints ----


@app.get("/library/search", response_model=SearchResponse)
async def library_search(
    request: Request,
    q: str | None = None,
    tags: str | None = None,
    file_type: str | None = None,
    content_type: str | None = None,
    limit: int = 20,
):
    """Search document library."""
    limit = max(1, min(limit, SEARCH_LIMIT_MAX))  # clamp: negative dropped results
    docs_dir = _get_docs_dir()
    metadata_filters = _metadata_filters_from_request(request)
    documents = _filter_documents(
        _load_doc_index(docs_dir),
        file_type=file_type,
        content_type=content_type,
        tags=tags,
        metadata_filters=metadata_filters,
    )

    if q:
        q_lower = q.lower()
        scored = []
        for d in documents:
            score = 0.0
            if q_lower in (d.get("filename") or "").lower():
                score += 2.0
            if q_lower in (d.get("digest") or "").lower():
                score += 1.0
            if q_lower in (d.get("source_filename") or "").lower():
                score += 1.0
            for tag in d.get("tags") or []:
                if q_lower in tag.lower():
                    score += 1.5
            for val in (d.get("metadata") or {}).values():
                if isinstance(val, list):
                    if any(q_lower in str(item).lower() for item in val):
                        score += 1.0
                elif q_lower in str(val).lower():
                    score += 1.0
            if score > 0:
                scored.append((d, score))
        scored.sort(key=lambda x: x[1], reverse=True)
        total = len(scored)  # true match count, before the page limit
        documents = [d for d, _ in scored[:limit]]
        scores = {d.get("id"): s for d, s in scored[:limit]}
    else:
        total = len(documents)
        documents = documents[:limit]
        scores = {}

    results = [
        SearchResult(
            doc_id=d.get("id", ""),
            filename=d.get("filename", ""),
            file_type=d.get("file_type", ""),
            content_type=d.get("content_type", "General"),
            storage_path=d.get("storage_path"),
            digest=d.get("digest", ""),
            tags=d.get("tags", []),
            source=d.get("source", "upload"),
            created_at=d.get("created_at"),
            score=scores.get(d.get("id"), 1.0),
            metadata=d.get("metadata") or {},
            source_ref=d.get("source_ref") or None,
            source_filename=d.get("source_filename") or None,
            source_available=bool(d.get("source_available")),
            summary_mode=d.get("summary_mode") or None,
            summary_status=d.get("summary_status") or None,
            summary_error_code=d.get("summary_error_code") or None,
        )
        for d in documents
    ]
    return SearchResponse(results=results, total=total)


@app.get("/library/search_text", response_model=SearchResponse)
async def library_search_text(
    request: Request,
    q: str,
    tags: str | None = None,
    file_type: str | None = None,
    content_type: str | None = None,
    doc_id: str | None = None,
    limit: int = 20,
    scope: str = "all",
):
    """Search full text and/or section text with snippets and page hints."""
    limit = max(1, min(limit, SEARCH_LIMIT_MAX))  # clamp: negative dropped results
    query = q.strip()
    if not query:
        raise HTTPException(422, "q is required")
    if doc_id:
        _validate_doc_id(doc_id)
    if scope not in {"all", "full", "section"}:
        raise HTTPException(422, "scope must be one of: all, full, section")

    docs_dir = _get_docs_dir()
    metadata_filters = _metadata_filters_from_request(request)
    documents = _filter_documents(
        _load_doc_index(docs_dir),
        file_type=file_type,
        content_type=content_type,
        tags=tags,
        metadata_filters=metadata_filters,
    )
    if doc_id:
        documents = [d for d in documents if d.get("id") == doc_id]
        if not documents:
            fallback_doc = _doc_entry_from_manifest(docs_dir, doc_id)
            if fallback_doc:
                documents = _filter_documents(
                    [fallback_doc],
                    file_type=file_type,
                    content_type=content_type,
                    tags=tags,
                    metadata_filters=metadata_filters,
                )

    results: list[SearchResult] = []
    for d in documents:
        current_doc_id = d.get("id", "")
        if not isinstance(current_doc_id, str) or not _DOC_ID_RE.match(current_doc_id):
            continue
        try:
            doc_dir = _resolve_doc_dir(docs_dir, current_doc_id)
        except HTTPException:
            continue
        manifest_path = doc_dir / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            continue

        if scope in {"all", "full"}:
            full_path = doc_dir / "full.md"
            if full_path.exists():
                full_text = full_path.read_text(encoding="utf-8")
                if query.lower() in full_text.lower():
                    results.append(
                        SearchResult(
                            doc_id=current_doc_id,
                            filename=d.get("filename", ""),
                            file_type=d.get("file_type", ""),
                            content_type=d.get("content_type", "General"),
                            storage_path=d.get("storage_path"),
                            digest=d.get("digest", ""),
                            tags=d.get("tags", []),
                            source=d.get("source", "upload"),
                            created_at=d.get("created_at"),
                            score=_search_score((True, 1.0)),
                            metadata=d.get("metadata") or {},
                            source_ref=d.get("source_ref") or None,
                            source_filename=d.get("source_filename") or None,
                            source_available=bool(d.get("source_available")),
                            summary_mode=d.get("summary_mode") or None,
                            summary_status=d.get("summary_status") or None,
                            summary_error_code=d.get("summary_error_code") or None,
                            snippet=_make_snippet(full_text, query),
                        )
                    )

        if scope in {"all", "section"}:
            for sec in manifest.get("sections", []):
                rel_path = sec.get("file")
                if not rel_path:
                    continue
                section_path = _resolve_manifest_section_path(doc_dir, rel_path)
                if not section_path:
                    continue
                if not section_path.exists():
                    continue
                section_text = section_path.read_text(encoding="utf-8")
                title = sec.get("title", "")
                title_hit = query.lower() in title.lower()
                text_hit = query.lower() in section_text.lower()
                if not (title_hit or text_hit):
                    continue
                page_start = sec.get("page_start")
                page_end = sec.get("page_end")
                if page_start is None and page_end is None:
                    page_start, page_end = _page_bounds(sec.get("page_range"))
                results.append(
                    SearchResult(
                        doc_id=current_doc_id,
                        filename=d.get("filename", ""),
                        file_type=d.get("file_type", ""),
                        content_type=d.get("content_type", "General"),
                        storage_path=d.get("storage_path"),
                        digest=d.get("digest", ""),
                        tags=d.get("tags", []),
                        source=d.get("source", "upload"),
                        created_at=d.get("created_at"),
                        score=_search_score((title_hit, 2.0), (text_hit, 1.5)),
                        metadata=d.get("metadata") or {},
                        source_ref=d.get("source_ref") or None,
                        source_filename=d.get("source_filename") or None,
                        source_available=bool(d.get("source_available")),
                        summary_mode=d.get("summary_mode") or None,
                        summary_status=d.get("summary_status") or None,
                        summary_error_code=d.get("summary_error_code") or None,
                        sid=sec.get("sid"),
                        section_title=title,
                        page_range=sec.get("page_range"),
                        page_start=page_start,
                        page_end=page_end,
                        snippet=_make_snippet(section_text if text_hit else title, query),
                    )
                )

    results.sort(key=lambda item: item.score, reverse=True)
    total = len(results)
    return SearchResponse(results=results[:limit], total=total)


@app.get("/library/{doc_id}/manifest")
async def get_manifest(doc_id: str):
    """Get document manifest."""
    _validate_doc_id(doc_id)
    p = _resolve_doc_dir(_get_docs_dir(), doc_id) / "manifest.json"
    if not p.exists():
        raise HTTPException(404, t("doc_not_found", doc_id=doc_id))
    return json.loads(p.read_text(encoding="utf-8"))


@app.get("/library/{doc_id}/sidecars")
async def discover_sidecars(doc_id: str):
    """Discover optional sidecars without returning large geometry payloads."""
    _validate_doc_id(doc_id)
    doc_dir = _resolve_doc_dir(_get_docs_dir(), doc_id)
    manifest_path = doc_dir / "manifest.json"
    if not manifest_path.exists():
        raise HTTPException(404, t("doc_not_found", doc_id=doc_id))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    layout = manifest.get("layout") if isinstance(manifest.get("layout"), dict) else {}
    sidecar_path = doc_dir / OCR_BLOCKS_SIDECAR_PATH
    layout_summary = {
        "available": sidecar_path.exists(),
        "path": OCR_BLOCKS_SIDECAR_PATH if sidecar_path.exists() else "",
        "coordinate_system": layout.get("coordinate_system") or OCR_BLOCKS_COORDINATE_SYSTEM,
        "version": int(layout.get("version") or OCR_BLOCKS_SIDECAR_VERSION),
        "pages_endpoint": f"/library/{doc_id}/layout/pages" if sidecar_path.exists() else "",
        "page_endpoint_template": f"/library/{doc_id}/layout/page/{{page}}" if sidecar_path.exists() else "",
    }
    if sidecar_path.exists():
        sidecar = _load_ocr_sidecar_payload(doc_dir, doc_id)
        page_summaries = _sidecar_page_summaries(sidecar)
        layout_summary["page_count"] = len(page_summaries)
        layout_summary["block_count"] = sum(page["block_count"] for page in page_summaries)
    else:
        layout_summary["page_count"] = 0
        layout_summary["block_count"] = 0

    tables = _load_tables_sidecar(doc_dir)
    table_summaries = [
        {
            "table_id": str(table.get("table_id") or ""),
            "page": table.get("page"),
            "row_count": table.get("row_count"),
            "column_count": table.get("column_count"),
            "source": table.get("source"),
            "file": table.get("file"),
            "json_file": table.get("json_file") or "",
            "bbox_available": bool(table.get("bbox")),
        }
        for table in tables
    ]
    return {
        "doc_id": doc_id,
        "layout": layout_summary,
        "tables": {
            "available": bool(tables),
            "path": "tables.json" if tables else "",
            "count": len(tables),
            "items": table_summaries,
            "json_endpoint_template": f"/library/{doc_id}/table/{{table_id}}/json",
        },
    }


@app.get("/library/{doc_id}/layout/pages")
async def list_layout_pages(doc_id: str):
    """List OCR layout pages and block counts without returning block geometry."""
    _validate_doc_id(doc_id)
    doc_dir = _resolve_doc_dir(_get_docs_dir(), doc_id)
    if not (doc_dir / "manifest.json").exists():
        raise HTTPException(404, t("doc_not_found", doc_id=doc_id))
    sidecar = _load_ocr_sidecar_payload(doc_dir, doc_id)
    return {
        "doc_id": doc_id,
        "coordinate_system": sidecar.get("coordinate_system") or OCR_BLOCKS_COORDINATE_SYSTEM,
        "version": int(sidecar.get("version") or OCR_BLOCKS_SIDECAR_VERSION),
        "pages": _sidecar_page_summaries(sidecar),
    }


@app.get("/library/{doc_id}/layout/page/{page_num}")
async def get_layout_page(doc_id: str, page_num: int):
    """Read OCR geometry for one page only."""
    _validate_doc_id(doc_id)
    if page_num < 1:
        raise HTTPException(422, "page_num must be a 1-based positive integer")
    doc_dir = _resolve_doc_dir(_get_docs_dir(), doc_id)
    if not (doc_dir / "manifest.json").exists():
        raise HTTPException(404, t("doc_not_found", doc_id=doc_id))
    sidecar = _load_ocr_sidecar_payload(doc_dir, doc_id)
    for page in sidecar.get("pages") or []:
        if isinstance(page, dict) and int(page.get("page") or 0) == page_num:
            return {
                "doc_id": doc_id,
                "coordinate_system": sidecar.get("coordinate_system") or OCR_BLOCKS_COORDINATE_SYSTEM,
                "version": int(sidecar.get("version") or OCR_BLOCKS_SIDECAR_VERSION),
                "page": page,
            }
    raise HTTPException(404, f"layout page not found: {page_num}")


@app.post("/library/{doc_id}/search_sections", response_model=SearchResponse)
async def search_sections(doc_id: str, request: SectionSearchRequest):
    """Search within one document's section files and return sid/page provenance."""
    query = request.q.strip()
    if not query:
        raise HTTPException(422, "q is required")

    docs_dir = _get_docs_dir()
    manifest, records = _load_section_records(docs_dir, doc_id)
    needle = query if request.case_sensitive else query.lower()
    results: list[SearchResult] = []
    for record in records:
        title = str(record.get("title") or "")
        text = str(record.get("text") or "")
        title_haystack = title if request.case_sensitive else title.lower()
        text_haystack = text if request.case_sensitive else text.lower()
        title_hit = needle in title_haystack
        text_hit = needle in text_haystack
        if not (title_hit or text_hit):
            continue
        results.append(
            SearchResult(
                doc_id=doc_id,
                filename=str(manifest.get("filename") or ""),
                file_type=str(manifest.get("file_type") or ""),
                content_type=str(manifest.get("content_type") or "General"),
                storage_path=manifest.get("storage_path") if isinstance(manifest.get("storage_path"), str) else None,
                digest="",
                tags=[],
                source=str(manifest.get("source") or "upload"),
                score=_search_score((title_hit, 2.0), (text_hit, 1.5)),
                metadata=manifest.get("metadata") if isinstance(manifest.get("metadata"), dict) else {},
                source_ref=(manifest.get("source_file") or {}).get("ref") if isinstance(manifest.get("source_file"), dict) else None,
                source_filename=(manifest.get("source_file") or {}).get("filename") if isinstance(manifest.get("source_file"), dict) else None,
                source_available=bool((manifest.get("source_file") or {}).get("ref")) if isinstance(manifest.get("source_file"), dict) else False,
                sid=record.get("sid"),
                section_title=title,
                page_range=record.get("page_range"),
                page_start=record.get("page_start"),
                page_end=record.get("page_end"),
                snippet=_make_snippet(text if text_hit else title, query),
                content=text if request.include_content else None,
            )
        )
    results.sort(key=lambda item: item.score, reverse=True)
    total = len(results)
    return SearchResponse(results=results[: request.limit], total=total)


@app.post("/library/{doc_id}/chunks")
async def chunk_document(doc_id: str, request: ChunkRequest):
    """Build generic section-boundary chunks for downstream skills."""
    docs_dir = _get_docs_dir()
    _, records = _load_section_records(docs_dir, doc_id)
    chunks = _chunk_sections(doc_id, records, request)
    return {
        "doc_id": doc_id,
        "chunk_count": len(chunks),
        "chunks": chunks,
        "config": request.model_dump() if hasattr(request, "model_dump") else request.dict(),
    }


@app.get("/library/{doc_id}/summary")
async def get_summary_status(doc_id: str):
    _validate_doc_id(doc_id)
    manifest_path = _resolve_doc_dir(_get_docs_dir(), doc_id) / "manifest.json"
    if not manifest_path.exists():
        raise HTTPException(404, t("doc_not_found", doc_id=doc_id))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    parse_metadata = manifest.get("parse_metadata") if isinstance(manifest.get("parse_metadata"), dict) else {}
    summary = parse_metadata.get("summary") if isinstance(parse_metadata.get("summary"), dict) else {}
    return {
        "doc_id": doc_id,
        "summary": summary,
        "paths": manifest.get("paths") or {},
    }


@app.post("/library/{doc_id}/summary")
async def retry_summary(doc_id: str, concurrency: int = 3, force: bool = False):
    _validate_doc_id(doc_id)
    docs_dir = _get_docs_dir()
    # Serialize on the per-doc_id lock (the same registry /parse uses) so the
    # load → status check → placeholder write is atomic w.r.t. a concurrent
    # parse/retry of the same doc — otherwise two callers both pass the
    # "running" check and double-schedule, or clobber each other's output.
    async with _optional_doc_id_lock(doc_id):
        parsed, metadata, source_record = _load_parsed_document_from_storage(docs_dir, doc_id)
        tags = _load_doc_tags(docs_dir, doc_id)
        content_type = _doc_content_type(docs_dir, doc_id)

        summary_meta = parsed.metadata.get("summary") if isinstance(parsed.metadata, dict) else {}
        current_status = summary_meta.get("status") if isinstance(summary_meta, dict) else None
        attempts = _current_summary_attempts(parsed)
        if current_status == "running" and not force:
            raise HTTPException(409, f"summary already running for {doc_id}")
        if attempts >= DEFERRED_SUMMARY_MAX_ATTEMPTS and not force:
            raise HTTPException(409, f"summary attempt limit reached for {doc_id}")

        # Claim the slot by marking running *under the lock* before starting the
        # worker. A concurrent retry then sees "running" and 409s, closing the
        # double-schedule window — without rejecting the legit "pending" state a
        # parse leaves behind (which must stay retryable).
        _set_summary_metadata(parsed, mode="defer", status="running", attempts=attempts)
        # preserve_extracted: parsed was reconstructed from storage and has no
        # tables/images/ocr_blocks, so regenerating would wipe them — keep the
        # existing on-disk artifacts (both here and in the worker's writes).
        write_output_extract_only(
            doc_id,
            parsed,
            docs_dir,
            tags=tags,
            source="upload",
            metadata=metadata,
            source_record=source_record,
            content_type=content_type,
            preserve_extracted=True,
            summary_placeholder=_summary_placeholder_text(
                "running", locale=_parsed_document_locale(parsed)
            ),
        )
        worker = threading.Thread(
            target=_generate_deferred_summary,
            args=(
                doc_id,
                parsed,
                docs_dir,
                concurrency,
                tags,
                metadata,
                source_record,
                content_type,
            ),
            kwargs={"preserve_extracted": True},
            daemon=True,
        )
        worker.start()
    logger.info("Deferred summary retry scheduled: %s", doc_id)
    return {
        "doc_id": doc_id,
        "scheduled": True,
        "summary": parsed.metadata.get("summary"),
        "limits": {
            "max_attempts": DEFERRED_SUMMARY_MAX_ATTEMPTS,
            "timeout_sec": DEFERRED_SUMMARY_TIMEOUT_SEC,
            "max_concurrent": DEFERRED_SUMMARY_MAX_CONCURRENT,
        },
    }


@app.get("/library/{doc_id}/digest")
async def get_digest(doc_id: str):
    """Get document digest (lowest token cost)."""
    _validate_doc_id(doc_id)
    p = _resolve_doc_dir(_get_docs_dir(), doc_id) / "digest.md"
    if not p.exists():
        raise HTTPException(404, t("digest_not_found", doc_id=doc_id))
    return {"doc_id": doc_id, "content": p.read_text(encoding="utf-8")}


@app.get("/library/{doc_id}/brief")
async def get_brief(doc_id: str):
    """Get document brief (medium token cost)."""
    _validate_doc_id(doc_id)
    p = _resolve_doc_dir(_get_docs_dir(), doc_id) / "brief.md"
    if not p.exists():
        raise HTTPException(404, t("brief_not_found", doc_id=doc_id))
    return {"doc_id": doc_id, "content": p.read_text(encoding="utf-8")}


@app.get("/library/{doc_id}/full")
async def get_full(doc_id: str):
    """Get full document text (high token cost, use sparingly)."""
    _validate_doc_id(doc_id)
    p = _resolve_doc_dir(_get_docs_dir(), doc_id) / "full.md"
    if not p.exists():
        raise HTTPException(404, t("full_not_found", doc_id=doc_id))
    return {"doc_id": doc_id, "content": p.read_text(encoding="utf-8")}


@app.get("/library/{doc_id}/section/{sid}")
async def get_section(doc_id: str, sid: str):
    """Read a single section by sid."""
    _validate_doc_id(doc_id)
    doc_dir = _resolve_doc_dir(_get_docs_dir(), doc_id)
    manifest_path = doc_dir / "manifest.json"
    if not manifest_path.exists():
        raise HTTPException(404, t("doc_not_found", doc_id=doc_id))

    # Match the sid EXACTLY against the manifest (the old substring scan of
    # filenames "NN-{sid}-{title}.md" matched the index prefix or title too,
    # returning the wrong section). The file path is resolved + bounds-checked.
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for sec in manifest.get("sections", []):
        if isinstance(sec, dict) and sec.get("sid") == sid:
            path = _resolve_manifest_section_path(doc_dir, sec.get("file"))
            if path and path.exists():
                return {"doc_id": doc_id, "sid": sid, "content": path.read_text(encoding="utf-8")}
            break

    raise HTTPException(404, t("section_not_found", sid=sid))


@app.post("/library/{doc_id}/sections/batch")
async def get_sections_batch(doc_id: str, request: SectionBatchRequest):
    """Read multiple sections by sid in one call — fewer round-trips than
    repeated /section/{sid} (matters for cross-host MCP clients). Returns the
    sections found plus any requested sids that didn't resolve."""
    if not request.sids:
        raise HTTPException(422, "sids must be a non-empty list")
    if len(request.sids) > 100:
        raise HTTPException(422, "sids accepts at most 100 ids")
    _validate_doc_id(doc_id)
    doc_dir = _resolve_doc_dir(_get_docs_dir(), doc_id)
    manifest_path = doc_dir / "manifest.json"
    if not manifest_path.exists():
        raise HTTPException(404, t("doc_not_found", doc_id=doc_id))

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    files_by_sid = {
        sec["sid"]: sec.get("file")
        for sec in manifest.get("sections", [])
        if isinstance(sec, dict) and sec.get("sid")
    }
    sections: list[dict[str, str]] = []
    missing: list[str] = []
    seen: set[str] = set()
    for sid in request.sids:
        if sid in seen:  # dedupe requested ids; don't read the same file twice
            continue
        seen.add(sid)
        file = files_by_sid.get(sid)
        path = _resolve_manifest_section_path(doc_dir, file) if file else None
        if path and path.exists():
            sections.append({"sid": sid, "content": path.read_text(encoding="utf-8")})
        else:
            missing.append(sid)
    return {"doc_id": doc_id, "sections": sections, "missing": missing}


@app.get("/library/{doc_id}/table/{table_id}")
async def get_table(doc_id: str, table_id: str):
    """Read a single table."""
    _validate_doc_id(doc_id)
    _validate_table_id(table_id)
    tables_dir = _resolve_doc_dir(_get_docs_dir(), doc_id) / "tables"
    if not tables_dir.exists():
        raise HTTPException(404, t("tables_dir_not_found", doc_id=doc_id))

    # table_id: "table-01" or "01"
    tid = table_id if table_id.startswith("table-") else f"table-{table_id}"
    p = tables_dir / f"{tid}.md"
    if not p.exists():
        raise HTTPException(404, t("table_not_found", table_id=table_id))
    return {"doc_id": doc_id, "table_id": table_id, "content": p.read_text(encoding="utf-8")}


@app.get("/library/{doc_id}/table/{table_id}/json")
async def get_table_json(doc_id: str, table_id: str):
    """Read structured JSON for one table when available."""
    _validate_doc_id(doc_id)
    _validate_table_id(table_id)
    doc_dir = _resolve_doc_dir(_get_docs_dir(), doc_id)
    if not (doc_dir / "manifest.json").exists():
        raise HTTPException(404, t("doc_not_found", doc_id=doc_id))
    tid = table_id if table_id.startswith("table-") else f"table-{table_id}"
    for table in _load_tables_sidecar(doc_dir):
        if table.get("table_id") != tid:
            continue
        json_file = str(table.get("json_file") or "")
        path = _resolve_table_json_path(doc_dir, json_file)
        if path is None or not path.exists():
            raise HTTPException(404, f"table JSON not found: {table_id}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise HTTPException(500, f"table JSON unreadable for {table_id}: {exc}") from exc
        return {"doc_id": doc_id, "table_id": tid, "table": payload}
    raise HTTPException(404, f"table JSON not found: {table_id}")


@app.get("/library/{doc_id}/images")
async def list_images(doc_id: str):
    """List embedded images extracted from a document."""
    _validate_doc_id(doc_id)
    doc_dir = _resolve_doc_dir(_get_docs_dir(), doc_id)
    manifest_path = doc_dir / "manifest.json"
    if not manifest_path.exists():
        raise HTTPException(404, t("doc_not_found", doc_id=doc_id))
    images_path = doc_dir / "images.json"
    if not images_path.exists():
        return {"doc_id": doc_id, "images": []}
    images = json.loads(images_path.read_text(encoding="utf-8"))
    if not isinstance(images, list):
        raise HTTPException(500, f"images metadata unreadable for {doc_id}")
    return {"doc_id": doc_id, "images": images}


@app.get("/library/{doc_id}/image/{image_id}")
async def get_image_record(doc_id: str, image_id: str):
    """Read one embedded image metadata record and OCR text when available."""
    _validate_doc_id(doc_id)
    normalized_id = _normalize_image_id(image_id)
    doc_dir = _resolve_doc_dir(_get_docs_dir(), doc_id)
    images_path = doc_dir / "images.json"
    if not images_path.exists():
        raise HTTPException(404, f"images not found for {doc_id}")
    images = json.loads(images_path.read_text(encoding="utf-8"))
    if not isinstance(images, list):
        raise HTTPException(500, f"images metadata unreadable for {doc_id}")
    for image in images:
        if not isinstance(image, dict) or image.get("image_id") != normalized_id:
            continue
        ocr = image.get("ocr") if isinstance(image.get("ocr"), dict) else {}
        text_path = str(ocr.get("text_path") or "")
        if text_path:
            path = (doc_dir / text_path).resolve()
            doc_root = doc_dir.resolve()
            if path.is_relative_to(doc_root) and path.exists() and path.is_file():
                image = dict(image)
                image["ocr"] = dict(ocr)
                image["ocr"]["text"] = path.read_text(encoding="utf-8")
        return {"doc_id": doc_id, "image_id": normalized_id, "image": image}
    raise HTTPException(404, f"image not found: {image_id}")


@app.get("/library/{doc_id}/image/{image_id}/raw")
async def get_image_bytes(doc_id: str, image_id: str, variant: str = "rendered"):
    """Return the raw image bytes for one embedded image.

    The /image/{id} record endpoint only returns metadata + OCR text; this serves
    the actual file so cross-host tool code can do visual reads (e.g. stamp /
    signature recognition) without a shared filesystem. ``variant`` is
    ``rendered`` (normalized PNG, default; falls back to original) or ``original``.
    """
    _validate_doc_id(doc_id)
    if variant not in {"rendered", "original"}:
        raise HTTPException(422, "variant must be 'rendered' or 'original'")
    normalized_id = _normalize_image_id(image_id)
    doc_dir = _resolve_doc_dir(_get_docs_dir(), doc_id)
    images_path = doc_dir / "images.json"
    if not images_path.exists():
        raise HTTPException(404, f"images not found for {doc_id}")
    images = json.loads(images_path.read_text(encoding="utf-8"))
    if not isinstance(images, list):
        raise HTTPException(500, f"images metadata unreadable for {doc_id}")
    for image in images:
        if not isinstance(image, dict) or image.get("image_id") != normalized_id:
            continue
        media = image.get("media") if isinstance(image.get("media"), dict) else {}
        if variant == "original":
            rel = media.get("original_path") or ""
        else:  # rendered, fall back to original when no normalized render exists
            rel = media.get("rendered_path") or media.get("original_path") or ""
        if not rel:
            raise HTTPException(404, f"image bytes not available: {image_id}")
        # Resolve + containment-check (the path comes from images.json, not the URL).
        path = (doc_dir / rel).resolve()
        if not path.is_relative_to(doc_dir.resolve()) or not path.is_file():
            raise HTTPException(404, f"image bytes not found: {image_id}")
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return Response(content=path.read_bytes(), media_type=media_type)
    raise HTTPException(404, f"image not found: {image_id}")


@app.get("/library/{doc_id}/sections")
async def list_sections(doc_id: str):
    """List all sections from manifest."""
    _validate_doc_id(doc_id)
    manifest_path = _resolve_doc_dir(_get_docs_dir(), doc_id) / "manifest.json"
    if not manifest_path.exists():
        raise HTTPException(404, t("doc_not_found", doc_id=doc_id))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return {
        "doc_id": doc_id,
        "sections": manifest.get("sections", []),
    }


# ═══════════════════════════════════════════
# Startup
# ═══════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8090"))

    DEFAULT_DOCS_DIR.mkdir(parents=True, exist_ok=True)
    logger.info(f"MantisFetch DocReader API v3.0 starting: {host}:{port}")
    logger.info(f"Docs directory: {DEFAULT_DOCS_DIR}")

    uvicorn.run(app, host=host, port=port)
