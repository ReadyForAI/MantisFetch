#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["markitdown[pdf,docx,pptx,xlsx,xls]", "pymupdf", "google-genai", "Pillow", "fastapi", "uvicorn", "python-multipart", "paddleocr", "paddlepaddle"]
# ///

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import io
import json
import logging
import math
import os
import posixpath
import re
import selectors
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import weakref
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeoutError
from dataclasses import dataclass, field
from datetime import UTC, datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

from i18n import init_locale, prompt_for_locale, t, tmpl_for_locale

init_locale()

logger = logging.getLogger("larkscout_docreader")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

# ═══════════════════════════════════════════
# Config
# ═══════════════════════════════════════════
MAX_PARSE_ROWS = int(os.environ.get("LARKSCOUT_MAX_PARSE_ROWS", "100000"))
_MAX_CONCURRENT_PARSE = int(os.environ.get("LARKSCOUT_MAX_CONCURRENT_PARSE", "2"))
_parse_sem = asyncio.Semaphore(_MAX_CONCURRENT_PARSE)

# Bound concurrent upload reads so a burst of large requests can't allocate
# unbounded memory before any parse slot is acquired. The doc_id reservation
# and `_parse_sem` are deliberately downstream — this gate covers only the
# upload-buffer footprint.
_MAX_CONCURRENT_UPLOAD = int(
    os.environ.get("LARKSCOUT_MAX_CONCURRENT_UPLOAD", str(_MAX_CONCURRENT_PARSE))
)
_upload_sem = asyncio.Semaphore(_MAX_CONCURRENT_UPLOAD)

# Per-doc_id locks serialize concurrent /doc/parse requests that pin the same
# explicit doc_id, so the existence check + write reservation can't race past
# each other when _MAX_CONCURRENT_PARSE > 1.
#
# WeakValueDictionary so entries vanish once no request still references the
# Lock — long-running servers receiving high-cardinality explicit ids would
# otherwise leak one Lock per id forever. While requests are queued on a
# lock their `async with lock:` frame keeps it alive.
_doc_id_parse_locks: "weakref.WeakValueDictionary[str, asyncio.Lock]" = weakref.WeakValueDictionary()
_doc_id_parse_locks_guard = asyncio.Lock()


@contextlib.asynccontextmanager
async def _optional_doc_id_lock(doc_id: str | None):
    """Hold a per-doc_id lock for the duration of the parse when doc_id is pinned."""
    if not doc_id:
        yield
        return
    async with _doc_id_parse_locks_guard:
        lock = _doc_id_parse_locks.get(doc_id)
        if lock is None:
            lock = asyncio.Lock()
            _doc_id_parse_locks[doc_id] = lock
    async with lock:
        yield

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
DOCUMENT_PROFILE_CONFIG_DIR = Path(__file__).resolve().parents[2] / "configs" / "document_profiles"
FIELD_OCR_CONFIG_DIR = Path(__file__).resolve().parents[2] / "configs" / "field_profiles"
# Backward-compat: tender_cn was renamed to bid_cn to match the Bid storage directory.
_DOCUMENT_PROFILE_ALIASES = {"tender_cn": "bid_cn"}
OCR_BLOCKS_SIDECAR_VERSION = 1
OCR_BLOCKS_SIDECAR_PATH = "ocr_blocks.json"
OCR_BLOCKS_COORDINATE_SYSTEM = "image_pixels"
CROP_ARTIFACT_DIR = "derived/crops"
REGION_OCR_ARTIFACT_DIR = "derived/region_ocr"
VISUAL_DEBUG_ARTIFACT_DIR = "derived/debug"

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
    timeout = int(os.environ.get("LARKSCOUT_OFFICE_CONVERT_TIMEOUT_SEC", "120"))
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


def _count_markdown_tables(text: str) -> int:
    """Count distinct Markdown tables by counting separator rows (| --- | --- |)."""
    return len(re.findall(r"^\|[\s\-:|]+\|$", text, re.MULTILINE))


_TABLE_CELL_SEP_RE = re.compile(r"^[\s]*:?-+:?[\s]*$")
_TABLE_FENCE_RE = re.compile(r"^(`{3,}|~{3,})")
_TABLE_ROW_RE = re.compile(r"^\|.*\|$")


def _is_markdown_table_separator(stripped: str) -> bool:
    """Cell-aware separator check: every cell must look like `:?-+:?`.

    Rejects whole-row matches like `|  | - |  |` where only one cell has a
    dash. Also rejects all-empty-cell rows.
    """
    if not stripped.startswith("|") or not stripped.endswith("|"):
        return False
    cells = stripped[1:-1].split("|")
    if not cells:
        return False
    has_dash = False
    for cell in cells:
        if not _TABLE_CELL_SEP_RE.match(cell):
            return False
        if "-" in cell:
            has_dash = True
    return has_dash


def _extract_markdown_table_blocks(text: str) -> list[str]:
    """Extract contiguous Markdown table blocks anchored on a separator row.

    A block is the run of consecutive lines whose stripped form starts and ends
    with `|`, surrounding a `| --- |` separator. Returns the joined block text
    for each table. Used to populate PageContent.tables for MarkItDown-derived
    formats (docx/pptx/html/etc.) so the table-sidecar writer has structured
    input.

    Lines inside fenced code blocks (``` or ~~~) and 4-space (or tab) indented
    code blocks are skipped so example tables embedded in code samples don't
    produce spurious entries. Fence markers must match the active opener type:
    a `~~~` inside an open ``` block doesn't close it. Back-to-back tables
    with no blank-line gap are split on the second separator row.
    """
    lines = text.splitlines()
    fence_marker: str | None = None
    eligible: list[bool] = []
    for line in lines:
        # CommonMark: code blocks of 4+ leading spaces or a tab are not
        # parsed as tables. Test the raw line — strip() would erase the cue.
        if not fence_marker and (line.startswith("    ") or line.startswith("\t")):
            eligible.append(False)
            continue
        stripped = line.strip()
        fence_match = _TABLE_FENCE_RE.match(stripped)
        if fence_match:
            marker = fence_match.group(1)[:3]  # collapse longer runs to ``` / ~~~
            if fence_marker is None:
                fence_marker = marker
            elif marker == fence_marker:
                fence_marker = None
            eligible.append(False)
            continue
        eligible.append(fence_marker is None)

    blocks: list[str] = []
    used: set[int] = set()
    for i, line in enumerate(lines):
        if i in used or not eligible[i] or not _is_markdown_table_separator(line.strip()):
            continue
        start = i
        while (
            start > 0
            and eligible[start - 1]
            and _TABLE_ROW_RE.match(lines[start - 1].strip())
            and (start - 1) not in used
        ):
            start -= 1
        end = i
        while end + 1 < len(lines) and eligible[end + 1] and _TABLE_ROW_RE.match(lines[end + 1].strip()):
            # Don't consume the next table's header row: if the line after
            # `end+1` is a separator, then `end+1` belongs to the next table.
            if (
                end + 2 < len(lines)
                and eligible[end + 2]
                and _is_markdown_table_separator(lines[end + 2].strip())
            ):
                break
            end += 1
        # Require at least a header + separator + one data row. A bare
        # separator with nothing around it isn't a real table.
        if end - start < 2:
            used.add(i)
            continue
        used.update(range(start, end + 1))
        blocks.append("\n".join(lines[start : end + 1]))
    return blocks


def _markdown_table_dimensions(table_md: str) -> dict[str, Any]:
    rows: list[list[str]] = []
    separator_indexes: set[int] = set()
    for line in table_md.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or not stripped.endswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        row_index = len(rows)
        rows.append(cells)
        if re.fullmatch(r"\|[\s\-:|]+\|", stripped):
            separator_indexes.add(row_index)

    content_rows = [row for idx, row in enumerate(rows) if idx not in separator_indexes]
    header_rows = 1 if rows and 1 in separator_indexes else 0
    return {
        "row_count": len(content_rows),
        "column_count": max((len(row) for row in content_rows), default=0),
        "header_rows": header_rows,
        "has_header": bool(header_rows),
    }


def _median(values: list[float], default: float = 0.0) -> float:
    if not values:
        return default
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def _cluster_ocr_blocks_into_rows(
    blocks: list[OCRTextBlock],
    y_tolerance: float,
) -> list[list[OCRTextBlock]]:
    rows: list[list[OCRTextBlock]] = []
    row_centers: list[float] = []
    for block in sorted(blocks, key=lambda b: ((b.bbox[1] + b.bbox[3]) / 2, b.bbox[0])):
        center_y = (block.bbox[1] + block.bbox[3]) / 2
        matched_index: int | None = None
        for idx, row_center in enumerate(row_centers):
            if abs(center_y - row_center) <= y_tolerance:
                matched_index = idx
                break
        if matched_index is None:
            rows.append([block])
            row_centers.append(center_y)
            continue
        rows[matched_index].append(block)
        row_centers[matched_index] = (
            row_centers[matched_index] * (len(rows[matched_index]) - 1) + center_y
        ) / len(rows[matched_index])
    return [sorted(row, key=lambda b: b.bbox[0]) for row in rows]


def _count_x_clusters(blocks: list[OCRTextBlock], x_tolerance: float) -> int:
    clusters: list[float] = []
    for block in sorted(blocks, key=lambda b: b.bbox[0]):
        x0 = block.bbox[0]
        for idx, cluster_x in enumerate(clusters):
            if abs(x0 - cluster_x) <= x_tolerance:
                clusters[idx] = (cluster_x + x0) / 2
                break
        else:
            clusters.append(x0)
    return len(clusters)


def _detect_table_candidates_from_ocr_blocks(
    sidecar: OCRBlocksSidecar,
    *,
    min_rows: int = 2,
    min_columns: int = 2,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for page in sidecar.pages:
        blocks = [
            block
            for block in page.blocks
            if block.text.strip() and (block.bbox[2] > block.bbox[0]) and (block.bbox[3] > block.bbox[1])
        ]
        if len(blocks) < min_rows * min_columns:
            continue
        heights = [block.bbox[3] - block.bbox[1] for block in blocks]
        widths = [block.bbox[2] - block.bbox[0] for block in blocks]
        rows = _cluster_ocr_blocks_into_rows(blocks, y_tolerance=max(8.0, _median(heights, 12.0) * 0.75))
        valid_rows = [row for row in rows if len(row) >= min_columns]
        if len(valid_rows) < min_rows:
            continue
        candidate_blocks = [block for row in valid_rows for block in row]
        column_count = _count_x_clusters(
            candidate_blocks,
            x_tolerance=max(12.0, _median(widths, 40.0) * 0.35),
        )
        if column_count < min_columns:
            continue
        xs0 = [block.bbox[0] for block in candidate_blocks]
        ys0 = [block.bbox[1] for block in candidate_blocks]
        xs1 = [block.bbox[2] for block in candidate_blocks]
        ys1 = [block.bbox[3] for block in candidate_blocks]
        avg_confidence = sum(block.confidence for block in candidate_blocks) / len(candidate_blocks)
        candidates.append(
            {
                "candidate_id": f"p{page.page}-tc{len(candidates) + 1:04d}",
                "page": page.page,
                "bbox": [min(xs0), min(ys0), max(xs1), max(ys1)],
                "row_count": len(valid_rows),
                "column_count": column_count,
                "confidence": round(avg_confidence, 4),
                "source": "ocr_geometry",
                "ocr_block_refs": [block.block_id for block in candidate_blocks],
            }
        )
    return candidates


def _bbox_union(bboxes: list[tuple[float, float, float, float]]) -> list[float]:
    return [
        min(b[0] for b in bboxes),
        min(b[1] for b in bboxes),
        max(b[2] for b in bboxes),
        max(b[3] for b in bboxes),
    ]


def _column_centers_for_blocks(blocks: list[OCRTextBlock], x_tolerance: float) -> list[float]:
    centers: list[float] = []
    for block in sorted(blocks, key=lambda b: b.bbox[0]):
        x0 = block.bbox[0]
        for idx, center in enumerate(centers):
            if abs(x0 - center) <= x_tolerance:
                centers[idx] = (center + x0) / 2
                break
        else:
            centers.append(x0)
    return centers


def _assign_column(block: OCRTextBlock, centers: list[float]) -> int:
    if not centers:
        return 1
    distances = [abs(block.bbox[0] - center) for center in centers]
    return distances.index(min(distances)) + 1


def _reconstruct_table_from_candidate(
    sidecar: OCRBlocksSidecar,
    candidate: dict[str, Any],
    table_id: str,
) -> dict[str, Any]:
    page_num = int(candidate["page"])
    page = next((p for p in sidecar.pages if p.page == page_num), None)
    if page is None:
        raise ValueError(f"candidate page not found in OCR blocks: {page_num}")
    refs = set(candidate.get("ocr_block_refs") or [])
    blocks = [block for block in page.blocks if block.block_id in refs]
    if not blocks:
        raise ValueError(f"candidate has no matching OCR blocks: {candidate.get('candidate_id')}")

    heights = [block.bbox[3] - block.bbox[1] for block in blocks]
    widths = [block.bbox[2] - block.bbox[0] for block in blocks]
    rows = _cluster_ocr_blocks_into_rows(
        blocks,
        y_tolerance=max(8.0, _median(heights, 12.0) * 0.75),
    )
    centers = _column_centers_for_blocks(blocks, x_tolerance=max(12.0, _median(widths, 40.0) * 0.35))
    structured_rows: list[dict[str, Any]] = []
    for row_index, row_blocks in enumerate(rows, 1):
        grouped: dict[int, list[OCRTextBlock]] = {}
        for block in row_blocks:
            grouped.setdefault(_assign_column(block, centers), []).append(block)
        cells: list[dict[str, Any]] = []
        for column in sorted(grouped):
            cell_blocks = sorted(grouped[column], key=lambda b: (b.bbox[1], b.bbox[0]))
            text = "\n".join(block.text for block in cell_blocks).strip()
            confidence = sum(block.confidence for block in cell_blocks) / len(cell_blocks)
            cells.append(
                {
                    "row": row_index,
                    "column": column,
                    "text": text,
                    "bbox": _bbox_union([block.bbox for block in cell_blocks]),
                    "rowspan": 1,
                    "colspan": 1,
                    "confidence": round(confidence, 4),
                    "ocr_block_refs": [block.block_id for block in cell_blocks],
                }
            )
        structured_rows.append({"row_index": row_index, "cells": cells})

    return {
        "table_id": table_id,
        "page": page_num,
        "page_width": page.width,
        "page_height": page.height,
        "bbox": candidate["bbox"],
        "source": "ocr_geometry",
        "row_count": len(structured_rows),
        "column_count": len(centers),
        "continued_from": None,
        "continued_to": None,
        "candidate_id": candidate.get("candidate_id"),
        "rows": structured_rows,
    }


def _markdown_from_structured_table(table: dict[str, Any]) -> str:
    column_count = int(table.get("column_count") or 0)
    rows = table.get("rows") if isinstance(table.get("rows"), list) else []
    if column_count <= 0 or not rows:
        return ""
    markdown_rows: list[list[str]] = []
    for row in rows:
        values = [""] * column_count
        for cell in row.get("cells") or []:
            column = int(cell.get("column") or 0)
            if 1 <= column <= column_count:
                values[column - 1] = str(cell.get("text") or "").replace("\n", " ")
        markdown_rows.append(values)
    header = markdown_rows[0]
    separator = ["---"] * column_count
    body = markdown_rows[1:]
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(separator) + " |",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in body)
    return "\n".join(lines)


def _normalize_cell_text_for_match(text: Any) -> str:
    return "".join(str(text or "").lower().split())


def _first_row_texts(table: dict[str, Any]) -> list[str]:
    rows = table.get("rows") if isinstance(table.get("rows"), list) else []
    if not rows:
        return []
    column_count = int(table.get("column_count") or 0)
    values = [""] * column_count
    for cell in rows[0].get("cells") or []:
        column = int(cell.get("column") or 0)
        if 1 <= column <= column_count:
            values[column - 1] = _normalize_cell_text_for_match(cell.get("text"))
    return values


def _same_table_header(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_texts = _first_row_texts(left)
    right_texts = _first_row_texts(right)
    if len(left_texts) < 2 or len(left_texts) != len(right_texts):
        return False
    comparable = [(a, b) for a, b in zip(left_texts, right_texts) if a and b]
    if len(comparable) < 2:
        return False
    matches = sum(1 for a, b in comparable if a == b)
    return matches >= 2 and matches / len(comparable) >= 0.67


def _bbox_horizontal_overlap_ratio(left: list[float], right: list[float]) -> float:
    left_width = max(0.0, left[2] - left[0])
    right_width = max(0.0, right[2] - right[0])
    if left_width <= 0 or right_width <= 0:
        return 0.0
    overlap = max(0.0, min(left[2], right[2]) - max(left[0], right[0]))
    return overlap / min(left_width, right_width)


def _near_page_bottom(table: dict[str, Any]) -> bool:
    bbox = table.get("bbox") or []
    page_height = float(table.get("page_height") or 0)
    return len(bbox) == 4 and page_height > 0 and float(bbox[3]) >= page_height * 0.72


def _near_page_top(table: dict[str, Any]) -> bool:
    bbox = table.get("bbox") or []
    page_height = float(table.get("page_height") or 0)
    return len(bbox) == 4 and page_height > 0 and float(bbox[1]) <= page_height * 0.28


def _should_link_continued_tables(
    left_entry: dict[str, Any],
    left_table: dict[str, Any],
    right_entry: dict[str, Any],
    right_table: dict[str, Any],
) -> bool:
    if int(right_entry.get("page_start") or 0) != int(left_entry.get("page_end") or 0) + 1:
        return False
    if int(left_entry.get("column_count") or 0) < 2:
        return False
    if int(left_entry.get("column_count") or 0) != int(right_entry.get("column_count") or 0):
        return False
    left_bbox = left_entry.get("bbox")
    right_bbox = right_entry.get("bbox")
    if not (isinstance(left_bbox, list) and isinstance(right_bbox, list) and len(left_bbox) == 4 and len(right_bbox) == 4):
        return False
    min_width = max(1.0, min(float(left_bbox[2] - left_bbox[0]), float(right_bbox[2] - right_bbox[0])))
    edge_tolerance = max(24.0, min_width * 0.15)
    if abs(float(left_bbox[0]) - float(right_bbox[0])) > edge_tolerance:
        return False
    if abs(float(left_bbox[2]) - float(right_bbox[2])) > edge_tolerance:
        return False
    if _bbox_horizontal_overlap_ratio(left_bbox, right_bbox) < 0.75:
        return False
    return _same_table_header(left_table, right_table) or (_near_page_bottom(left_table) and _near_page_top(right_table))


def _apply_table_continuation_links(
    entries: list[tuple[dict[str, Any], dict[str, Any], str]],
) -> None:
    ordered = sorted(entries, key=lambda item: (int(item[0].get("page_start") or 0), int(item[0].get("index") or 0)))
    for (left_entry, left_table, _left_md), (right_entry, right_table, _right_md) in zip(ordered, ordered[1:]):
        if _should_link_continued_tables(left_entry, left_table, right_entry, right_table):
            left_entry["continued_to"] = right_entry["table_id"]
            right_entry["continued_from"] = left_entry["table_id"]
            left_table["continued_to"] = right_entry["table_id"]
            right_table["continued_from"] = left_entry["table_id"]


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
# Data structures
# ═══════════════════════════════════════════


@dataclass
class PageContent:
    """Single page content."""

    page_num: int
    text: str
    is_ocr: bool = False
    tables: list[str] = field(default_factory=list)
    tables_in_text: bool = False


@dataclass(frozen=True)
class OCRTextBlock:
    """Normalized OCR text block geometry for layout sidecars."""

    block_id: str
    text: str
    bbox: tuple[float, float, float, float]
    confidence: float = 0.0
    source: str = "local_ocr"
    line_index: int = 0
    order: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "block_id": self.block_id,
            "text": self.text,
            "bbox": _normalize_layout_bbox(self.bbox),
            "confidence": float(self.confidence),
            "source": self.source,
            "line_index": int(self.line_index),
            "order": int(self.order),
        }


@dataclass(frozen=True)
class OCRPageBlocks:
    """OCR geometry for one rendered document page."""

    page: int
    width: int
    height: int
    blocks: tuple[OCRTextBlock, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "page": int(self.page),
            "width": int(self.width),
            "height": int(self.height),
            "blocks": [block.to_dict() for block in self.blocks],
        }


@dataclass(frozen=True)
class OCRBlocksSidecar:
    """Versioned OCR geometry sidecar contract."""

    doc_id: str
    pages: tuple[OCRPageBlocks, ...] = ()
    version: int = OCR_BLOCKS_SIDECAR_VERSION
    coordinate_system: str = OCR_BLOCKS_COORDINATE_SYSTEM

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": int(self.version),
            "doc_id": self.doc_id,
            "coordinate_system": self.coordinate_system,
            "pages": [page.to_dict() for page in self.pages],
        }


@dataclass
class Section:
    """Document section."""

    index: int
    title: str
    level: int  # heading level 1-3
    text: str
    page_range: str  # "p.5-12"
    summary: str = ""
    sid: str = ""  # stable ID
    image_refs: list[str] = field(default_factory=list)


@dataclass
class EmbeddedImage:
    """Embedded document image with generic anchor and OCR metadata."""

    image_id: str
    order: int
    media_path: str
    relationship_id: str
    paragraph_index: int
    paragraph_text: str = ""
    context_text: str = ""
    near_heading: str = ""
    anchor_sid: str = ""
    section_title: str = ""
    original_ext: str = ""
    original_type: str = ""
    original_bytes: bytes = b""
    original_size_bytes: int = 0
    original_sha256: str = ""
    rendered_ext: str = ""
    rendered_type: str = ""
    rendered_bytes: bytes = b""
    rendered_size_bytes: int = 0
    rendered_sha256: str = ""
    width: int = 0
    height: int = 0
    aspect_ratio: float = 0.0
    average_hash: str = ""
    context_keywords: list[str] = field(default_factory=list)
    inventory_hints: list[str] = field(default_factory=list)
    render_status: str = "not_rendered"
    render_error: str = ""
    ocr_enabled: bool = False
    ocr_backend: str = ""
    ocr_status: str = "not_requested"
    ocr_text: str = ""
    ocr_error: str = ""


@dataclass
class ParsedDocument:
    """Parsed document result."""

    filename: str
    file_type: str  # "pdf" | "docx"
    total_pages: int
    pages: list[PageContent]
    sections: list[Section]
    ocr_page_count: int = 0
    table_count: int = 0
    images: list[EmbeddedImage] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    ocr_blocks: OCRBlocksSidecar | None = None
    extract_tables: bool = True


@dataclass(frozen=True)
class FieldCrop:
    x0: float
    y0: float
    x1: float
    y1: float


@dataclass(frozen=True)
class FieldGroup:
    id: str
    aliases: tuple[str, ...] = ()
    page_scope: tuple[int, ...] = ()
    crop: FieldCrop | None = None
    start_alias: str | None = None
    end_alias: str | None = None
    replace_mode: str = "block_between_aliases"


@dataclass(frozen=True)
class FieldRule:
    id: str
    aliases: tuple[str, ...] = ()
    pattern: str | None = None
    page_scope: tuple[int, ...] = ()


@dataclass(frozen=True)
class ClassificationPolicy:
    required_terms: tuple[str, ...] = ()


@dataclass(frozen=True)
class QualityPolicy:
    sparse_text_chars: int = 40
    usable_text_chars: int = 120
    scan_page_ratio: float = 0.85
    mixed_page_ratio: float = 0.2


@dataclass(frozen=True)
class UpgradePolicy:
    default_mode: str = "accurate"
    local_ocr_backend: str = "paddleocr"
    region_llm_modes: tuple[str, ...] = ("accurate", "full")
    full_llm_modes: tuple[str, ...] = ("full",)
    proofread_modes: tuple[str, ...] = ("full",)


@dataclass(frozen=True)
class TablePolicy:
    prefer_markitdown: bool = True


@dataclass(frozen=True)
class CachePolicy:
    page_ocr: bool = True
    region_ocr: bool = True


@dataclass(frozen=True)
class ProcessingPolicy:
    large_file_threshold_mb: int = 50
    local_ocr_render_scale: float = 2.0
    llm_ocr_render_scale: float = 3.0
    max_local_ocr_pixels: int = 4_000_000
    max_llm_ocr_pixels: int = 8_000_000
    min_ocr_render_scale: float = 1.25


@dataclass(frozen=True)
class SummaryPolicy:
    default_mode: str = "sync"
    async_modes: tuple[str, ...] = ()
    sync_modes: tuple[str, ...] = ("full",)


@dataclass(frozen=True)
class SectionPolicy:
    toc_max_level: int = 2
    suppress_arabic_clause_headings_when_formal_chinese: bool = False
    formal_chinese_min_headings: int = 4


@dataclass(frozen=True)
class DocumentProfile:
    name: str
    classification: ClassificationPolicy = ClassificationPolicy()
    quality_policy: QualityPolicy = QualityPolicy()
    upgrade_policy: UpgradePolicy = UpgradePolicy()
    table_policy: TablePolicy = TablePolicy()
    cache_policy: CachePolicy = CachePolicy()
    processing_policy: ProcessingPolicy = ProcessingPolicy()
    summary_policy: SummaryPolicy = SummaryPolicy()
    section_policy: SectionPolicy = SectionPolicy()
    groups: tuple[FieldGroup, ...] = ()
    fields: tuple[FieldRule, ...] = ()


# ═══════════════════════════════════════════
# LLM provider wrapper
# ═══════════════════════════════════════════


def gemini_ocr(image_bytes: bytes, page_num: int, *, proofread: bool | None = None) -> str:
    """OCR a single page image via the active LLM provider."""
    from providers import get_provider

    try:
        return get_provider().ocr(image_bytes, page_num, proofread=proofread)
    except Exception as exc:
        logger.warning("OCR unavailable for page %d: %s", page_num, exc)
        return t("ocr_failed", page=page_num)


def _is_ocr_failed_text(text: str | None) -> bool:
    if not text:
        return False
    value = text.strip()
    return value.startswith("[OCR failed") or value.startswith("[OCR 失败")


def gemini_summarize(text: str, summarize_prompt: str, max_retries: int = 2) -> str:
    """Generate summary via the active LLM provider."""
    from providers import get_provider

    with _summary_llm_lock:
        now = time.monotonic()
        wait_sec = _summary_llm_next_allowed_at - now
        if wait_sec > 0:
            time.sleep(wait_sec)
        try:
            return get_provider().summarize(text, summarize_prompt, max_retries=max_retries)
        finally:
            _set_next_summary_llm_allowed_at()


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

OCR_THRESHOLD = 50
OCR_RENDER_SCALE = float(os.environ.get("LARKSCOUT_OCR_RENDER_SCALE", "3.0"))
FIELD_OCR_RENDER_SCALE = float(os.environ.get("LARKSCOUT_FIELD_OCR_RENDER_SCALE", "4.0"))
LOCAL_OCR_RENDER_SCALE = float(os.environ.get("LARKSCOUT_LOCAL_OCR_RENDER_SCALE", "2.0"))
LOCAL_OCR_CONCURRENCY = max(1, int(os.environ.get("LARKSCOUT_LOCAL_OCR_CONCURRENCY", "1")))
DEFERRED_SUMMARY_MAX_CONCURRENT = max(
    1,
    int(os.environ.get("LARKSCOUT_DEFERRED_SUMMARY_MAX_CONCURRENT", "1")),
)
DEFERRED_SUMMARY_TIMEOUT_SEC = max(
    10.0,
    float(os.environ.get("LARKSCOUT_DEFERRED_SUMMARY_TIMEOUT_SEC", "180")),
)
DEFERRED_SUMMARY_MAX_ATTEMPTS = max(
    1,
    int(os.environ.get("LARKSCOUT_DEFERRED_SUMMARY_MAX_ATTEMPTS", "3")),
)
WORD_IMAGE_OCR_MAX_IMAGES = max(
    0,
    int(os.environ.get("LARKSCOUT_WORD_IMAGE_OCR_MAX_IMAGES", "80")),
)
SUMMARY_BATCH_CONCURRENCY = max(
    1,
    int(os.environ.get("LARKSCOUT_SUMMARY_BATCH_CONCURRENCY", "1")),
)
SUMMARY_REQUEST_MIN_INTERVAL_SEC = max(
    0.0,
    float(os.environ.get("LARKSCOUT_SUMMARY_REQUEST_MIN_INTERVAL_SEC", "2.0")),
)
SUMMARY_SECTION_DETAIL_LIMIT = max(
    1,
    int(os.environ.get("LARKSCOUT_SUMMARY_SECTION_DETAIL_LIMIT", "10")),
)
SUMMARY_BRIEF_SECTION_EXCERPT_CHARS = max(
    200,
    int(os.environ.get("LARKSCOUT_SUMMARY_BRIEF_SECTION_EXCERPT_CHARS", "1200")),
)
SUMMARY_BRIEF_MAX_INPUT_CHARS = max(
    4000,
    int(os.environ.get("LARKSCOUT_SUMMARY_BRIEF_MAX_INPUT_CHARS", "32000")),
)
_summary_llm_lock = threading.Lock()
_summary_llm_next_allowed_at = 0.0


def _set_next_summary_llm_allowed_at() -> None:
    global _summary_llm_next_allowed_at
    _summary_llm_next_allowed_at = (
        time.monotonic() + SUMMARY_REQUEST_MIN_INTERVAL_SEC
    )
_TABLE_HEADER_TERMS = {
    "序号",
    "名称",
    "售卖模式",
    "内容描述",
    "计价单位",
    "数量",
    "税率",
    "含税单价",
    "含税金额",
    "服务类型/服务项",
    "服务描述",
}
_TABLE_FOOTER_TERMS = ("小计", "合计", "大写人民币")
_COMPANY_NAME_RE = re.compile(r"[A-Za-z0-9\u4e00-\u9fff()（）·]+(?:股份有限公司|有限责任公司|有限公司)")
_UPPER_AMOUNT_RE = re.compile(
    r"(¥\s*[\d,]+(?:\.\d+)?)\s*[（(]\s*大写[：:]\s*人民币\s*([零〇一二三四五六七八九十百千万亿壹贰叁肆伍陆柒捌玖拾佰仟萬億元角分整正]+)\s*[)）]"
)


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
        useful = sum(1 for c in text if c.isalnum() or "\u4e00" <= c <= "\u9fff")
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


def _ocr_cache_path(doc_dir: Path, page_num: int) -> Path:
    cache_dir = doc_dir / ".cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / f"ocr_p{page_num:04d}.txt"


def _ocr_cache_variant_path(doc_dir: Path, key: str) -> Path:
    cache_dir = doc_dir / ".cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", key).strip("-") or "cache"
    return cache_dir / safe


def _ocr_cache_key(image_bytes: bytes) -> str:
    return hashlib.sha1(image_bytes).hexdigest()[:16]


_local_ocr_worker: subprocess.Popen[str] | None = None
_local_ocr_worker_lock = threading.Lock()
_local_ocr_worker_ready = threading.Event()
_local_ocr_worker_initializing = threading.Event()
_local_ocr_disabled_until = 0.0
_deferred_summary_sem = threading.BoundedSemaphore(DEFERRED_SUMMARY_MAX_CONCURRENT)
DEFERRED_SUMMARY_LOCAL_OCR_WAIT_SEC = float(
    os.environ.get("LARKSCOUT_DEFERRED_SUMMARY_LOCAL_OCR_WAIT_SEC", "30")
)
LOCAL_OCR_WORKER_STARTUP_TIMEOUT_SEC = float(
    os.environ.get("LARKSCOUT_LOCAL_OCR_WORKER_STARTUP_TIMEOUT_SEC", "180")
)
LOCAL_OCR_WORKER_REQUEST_TIMEOUT_SEC = float(
    os.environ.get("LARKSCOUT_LOCAL_OCR_WORKER_REQUEST_TIMEOUT_SEC", "180")
)
LOCAL_OCR_CIRCUIT_BREAKER_SEC = float(
    os.environ.get("LARKSCOUT_LOCAL_OCR_CIRCUIT_BREAKER_SEC", "120")
)


def _local_ocr_worker_command() -> list[str]:
    raw = os.environ.get("LARKSCOUT_LOCAL_OCR_WORKER_CMD", "").strip()
    if raw:
        return shlex.split(raw)
    worker = Path(__file__).with_name("paddle_ocr_worker.py")
    return [sys.executable, str(worker)]


def _drain_local_ocr_worker_stderr(proc: subprocess.Popen[str]) -> None:
    assert proc.stderr is not None
    for line in proc.stderr:
        value = line.rstrip()
        if value:
            logger.info("[local-ocr-worker] %s", value)


def _read_local_ocr_worker_message(proc: subprocess.Popen[str], timeout: float) -> dict[str, Any]:
    if proc.stdout is None:
        raise RuntimeError("local OCR worker stdout is unavailable")
    deadline = time.monotonic() + max(timeout, 0.1)
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ)
    try:
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(f"local OCR worker exited with code {proc.returncode}")
            remaining = max(deadline - time.monotonic(), 0.1)
            events = selector.select(timeout=remaining)
            if not events:
                continue
            line = proc.stdout.readline()
            if not line:
                raise RuntimeError(f"local OCR worker closed stdout with code {proc.poll()}")
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("Ignoring non-JSON local OCR worker output: %s", line.rstrip())
                continue
            if isinstance(message, dict):
                return message
        raise TimeoutError(f"local OCR worker timed out after {timeout:.1f}s")
    finally:
        selector.close()


def _mark_local_ocr_worker_unhealthy(reason: str) -> None:
    global _local_ocr_disabled_until
    _local_ocr_disabled_until = time.monotonic() + max(LOCAL_OCR_CIRCUIT_BREAKER_SEC, 0)
    logger.warning("Local OCR worker marked unhealthy: %s", reason)


def _stop_local_ocr_worker() -> None:
    global _local_ocr_worker
    proc = _local_ocr_worker
    _local_ocr_worker = None
    _local_ocr_worker_ready.clear()
    if not proc:
        return
    try:
        if proc.stdin:
            proc.stdin.close()
    except Exception:
        pass
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def _get_local_ocr_worker() -> subprocess.Popen[str]:
    global _local_ocr_worker
    if _local_ocr_disabled_until > time.monotonic():
        raise RuntimeError("local OCR worker is temporarily disabled after a crash")
    if _local_ocr_worker is not None and _local_ocr_worker.poll() is None:
        return _local_ocr_worker
    if _local_ocr_worker is not None:
        _stop_local_ocr_worker()

    _local_ocr_worker_initializing.set()
    try:
        cmd = _local_ocr_worker_command()
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        _local_ocr_worker = proc
        threading.Thread(
            target=_drain_local_ocr_worker_stderr,
            args=(proc,),
            daemon=True,
        ).start()
        message = _read_local_ocr_worker_message(
            proc, timeout=LOCAL_OCR_WORKER_STARTUP_TIMEOUT_SEC
        )
        if message.get("type") != "ready":
            error = message.get("error") or message
            _stop_local_ocr_worker()
            raise RuntimeError(f"local OCR worker startup failed: {error}")
        _local_ocr_worker_ready.set()
        return proc
    except Exception as exc:
        _stop_local_ocr_worker()
        _mark_local_ocr_worker_unhealthy(str(exc))
        raise
    finally:
        _local_ocr_worker_initializing.clear()


def _ocr_page_blocks_from_worker_response(
    page_num: int,
    response: dict[str, Any],
    source: str,
) -> OCRPageBlocks | None:
    raw_blocks = response.get("blocks")
    if not isinstance(raw_blocks, list):
        return None
    blocks: list[OCRTextBlock] = []
    for index, raw in enumerate(raw_blocks):
        if not isinstance(raw, dict):
            continue
        text = str(raw.get("text") or "").strip()
        if not text:
            continue
        try:
            bbox = tuple(_normalize_layout_bbox(raw.get("bbox") or [0, 0, 0, 0]))
        except (TypeError, ValueError):
            bbox = (0.0, 0.0, 0.0, 0.0)
        order = int(raw.get("order") if raw.get("order") is not None else index)
        line_index = int(raw.get("line_index") if raw.get("line_index") is not None else order)
        blocks.append(
            OCRTextBlock(
                block_id=f"p{page_num}-b{len(blocks) + 1:04d}",
                text=text,
                bbox=bbox,  # type: ignore[arg-type]
                confidence=float(raw.get("confidence") or 0.0),
                source=source,
                line_index=line_index,
                order=order,
            )
        )
    width = int(response.get("width") or 0)
    height = int(response.get("height") or 0)
    return OCRPageBlocks(page=page_num, width=width, height=height, blocks=tuple(blocks))


def local_ocr_with_layout(
    image_bytes: bytes,
    page_num: int,
    backend: str,
) -> tuple[str, OCRPageBlocks | None]:
    name = (backend or "").strip().lower()
    if name in {"", "none"}:
        return "", None
    if name != "paddleocr":
        raise RuntimeError(f"unsupported local OCR backend: {backend}")
    with _local_ocr_worker_lock:
        try:
            proc = _get_local_ocr_worker()
            if proc.stdin is None:
                raise RuntimeError("local OCR worker stdin is unavailable")
            request = {
                "page_num": page_num,
                "image_b64": base64.b64encode(image_bytes).decode("ascii"),
            }
            proc.stdin.write(json.dumps(request) + "\n")
            proc.stdin.flush()
            response = _read_local_ocr_worker_message(
                proc, timeout=LOCAL_OCR_WORKER_REQUEST_TIMEOUT_SEC
            )
            if not response.get("ok"):
                logger.warning(
                    "Local OCR worker failed page %d via %s: %s",
                    page_num,
                    backend,
                    response.get("error") or response,
                )
                return t("ocr_failed", page=page_num), None
            text = str(response.get("text") or "").strip()
            page_blocks = _ocr_page_blocks_from_worker_response(
                page_num,
                response,
                source=f"local-{name}",
            )
            return text or t("ocr_failed", page=page_num), page_blocks
        except Exception as exc:
            _stop_local_ocr_worker()
            _mark_local_ocr_worker_unhealthy(str(exc))
            logger.warning("Local OCR unavailable for page %d via %s: %s", page_num, backend, exc)
            return t("ocr_failed", page=page_num), None


def local_ocr(image_bytes: bytes, page_num: int, backend: str) -> str:
    text, _page_blocks = local_ocr_with_layout(image_bytes, page_num, backend)
    return text


def _remove_footer_page_number(lines: list[str], page_num: int, total_pages: int) -> list[str]:
    cleaned = list(lines)
    if not cleaned:
        return cleaned
    candidate_numbers = {n for n in (page_num - 1, page_num, page_num + 1) if 0 < n <= total_pages}
    while cleaned:
        tail = cleaned[-1].strip()
        if tail.isdigit() and int(tail) in candidate_numbers and len(cleaned) >= 3:
            cleaned.pop()
            continue
        break
    return cleaned


def _looks_like_page_footer(line: str) -> bool:
    return bool(
        re.fullmatch(
            r"[-—_]*\s*第\s*\d+\s*[页頁]\s*(?:(?:[/／]\s*)?共\s*\d+\s*[页頁]?)?\s*[-—_]*",
            line.strip(),
        )
    )


def _looks_like_bracket_noise(line: str) -> bool:
    compact = re.sub(r"\s+", "", line.strip())
    if "[" not in compact and "]" not in compact:
        return False
    if len(compact) <= 5 and re.fullmatch(r"\[[A-Za-z0-9_]+\]?", compact):
        return True
    ascii_count = sum(1 for ch in compact if ch.isascii() and (ch.isalnum() or ch in "_-[]"))
    cjk_count = sum(1 for ch in compact if "\u4e00" <= ch <= "\u9fff")
    if ascii_count >= 6 and ascii_count >= cjk_count * 2:
        return True
    return bool(re.fullmatch(r"[A-Za-z0-9_\-\[\]]{5,}", compact))


def _cleanup_ocr_text(text: str, *, source_filename: str | None = None) -> str:
    lines = [line.strip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    cleaned: list[str] = []
    for idx, line in enumerate(lines):
        if not line:
            continue
        if _looks_like_bracket_noise(line):
            continue
        if _looks_like_page_footer(line):
            continue
        if line == "定作":
            prev_context = "\n".join(cleaned[-4:])
            next_line = lines[idx + 1].strip() if idx + 1 < len(lines) else ""
            if "合同签订地点" in prev_context or _looks_like_page_footer(next_line):
                continue
        cleaned.append(line)

    if len(cleaned) > 1 and cleaned[0].strip().lower() in {"preface"}:
        cleaned.pop(0)

    cleaned_text = "\n".join(cleaned)
    replacements = {
        "安装元成": "安装完成",
        "软件采贝": "软件采购",
        "合同采贝": "合同采购",
        "软件东统": "软件系统",
        "基调研云": "基调听云",
        "基调所元": "基调听云",
        "营通探针": "普通探针",
        "邮付申请": "邮件申请",
        "lava Agent": "Java Agent",
        "语吉探针": "语言探针",
        "则特殊开发部分应符\n合需求说明书": "则特殊开发部分应符合需求说明书",
    }
    for src, dst in replacements.items():
        cleaned_text = cleaned_text.replace(src, dst)

    source_contract_no = _source_filename_contract_no(source_filename)
    if source_contract_no:
        cleaned_lines = cleaned_text.splitlines()
        if cleaned_lines:
            leading = re.sub(r"\s+", "", cleaned_lines[0].strip())
            if re.fullmatch(r"[A-Za-z]{2,10}\d{4,20}", leading) and leading != source_contract_no:
                cleaned_lines[0] = source_contract_no
                cleaned_text = "\n".join(cleaned_lines)
    return cleaned_text.strip()


def _is_markdown_table_delimiter(line: str) -> bool:
    return bool(re.match(r"^\|?(?:\s*:?-+:?\s*\|)+\s*:?-+:?\s*\|?$", line.strip()))


def _looks_like_markdown_table_row(line: str) -> bool:
    line = line.strip()
    return line.count("|") >= 2 and len(line.replace("|", "").strip()) > 0


def _looks_like_plain_table_header(line: str) -> bool:
    line = line.strip()
    if not line:
        return False
    matches = sum(1 for term in _TABLE_HEADER_TERMS if term in line)
    return matches >= 3 or line.startswith("序号 ")


def _looks_like_plain_table_footer(line: str) -> bool:
    return any(term in line for term in _TABLE_FOOTER_TERMS)


def _looks_like_plain_table_row(line: str) -> bool:
    line = line.strip()
    if not line:
        return False
    if _looks_like_plain_table_header(line) or _looks_like_plain_table_footer(line):
        return True
    if re.match(r"^\d+\s+", line) and len(line) >= 20:
        if any(token in line for token in ("¥", "%", "套", "次", "年", "项", "个", "台", "PV")):
            return True
    if line in {"软件产品", "服务中心"}:
        return True
    return False


def _extract_tables_from_ocr_text(text: str, page_num: int, total_pages: int) -> tuple[str, list[str]]:
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    lines = [line.strip() for line in lines if line.strip()]
    lines = _remove_footer_page_number(lines, page_num, total_pages)

    body_parts: list[str] = []
    tables: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]

        if (
            i + 1 < len(lines)
            and _looks_like_markdown_table_row(line)
            and _is_markdown_table_delimiter(lines[i + 1])
        ):
            table_lines = [line, lines[i + 1]]
            i += 2
            while i < len(lines) and _looks_like_markdown_table_row(lines[i]):
                table_lines.append(lines[i])
                i += 1
            table_text = "\n".join(table_lines).strip()
            if len(table_lines) > 2:
                tables.append(table_text)
            else:
                body_parts.append(line)
            continue

        if _looks_like_plain_table_header(line):
            table_lines = [line]
            i += 1
            while i < len(lines):
                current = lines[i]
                if _is_heading(current) > 0 and not _looks_like_plain_table_row(current):
                    break
                if _looks_like_plain_table_row(current):
                    table_lines.append(current)
                    i += 1
                    continue
                break
            table_text = "\n".join(table_lines).strip()
            tables.append(table_text)
            continue

        body_parts.append(line)
        i += 1

    return "\n".join(part for part in body_parts if part).strip(), tables


def _amount_to_uppercase_rmb(amount_text: str) -> str | None:
    digits = amount_text.replace("¥", "").replace(",", "").strip()
    if not re.fullmatch(r"\d+(?:\.\d{1,2})?", digits):
        return None

    value = round(float(digits) + 1e-9, 2)
    integer = int(value)
    jiao = int((value * 10) % 10)
    fen = int(round(value * 100)) % 10

    digits_map = "零壹贰叁肆伍陆柒捌玖"
    small_units = ["", "拾", "佰", "仟"]
    large_units = ["", "万", "亿", "兆"]

    if integer == 0:
        integer_text = "零元"
    else:
        groups: list[int] = []
        while integer > 0:
            groups.append(integer % 10000)
            integer //= 10000

        parts: list[str] = []
        zero_between = False
        for idx in range(len(groups) - 1, -1, -1):
            group = groups[idx]
            if group == 0:
                zero_between = bool(parts)
                continue

            if zero_between or (parts and group < 1000):
                parts.append("零")
                zero_between = False

            group_digits: list[str] = []
            zero_inside = False
            for pos in range(3, -1, -1):
                divisor = 10**pos
                digit = group // divisor
                group %= divisor
                if digit == 0:
                    if group_digits:
                        zero_inside = True
                    continue
                if zero_inside:
                    group_digits.append("零")
                    zero_inside = False
                group_digits.append(digits_map[digit] + small_units[pos])

            parts.append("".join(group_digits) + large_units[idx])

        integer_text = "".join(parts) + "元"

    if jiao == 0 and fen == 0:
        return integer_text + "整"

    tail = ""
    if jiao > 0:
        tail += digits_map[jiao] + "角"
    elif fen > 0:
        tail += "零"
    if fen > 0:
        tail += digits_map[fen] + "分"
    return integer_text + tail


def _normalize_amount_phrases(text: str) -> str:
    def repl(match: re.Match[str]) -> str:
        amount_text = match.group(1)
        normalized_upper = _amount_to_uppercase_rmb(amount_text)
        if not normalized_upper:
            return match.group(0)
        return f"{amount_text}（大写：人民币{normalized_upper}）"

    return _UPPER_AMOUNT_RE.sub(repl, text)


def _looks_like_signature_watermark_line(line: str) -> bool:
    compact = re.sub(r"\s+", "", line.strip())
    if not compact:
        return False
    if set(compact) <= {"万", "翼", "签"} and "万翼" in compact:
        return True
    residue = compact
    for token in ("万翼签", "万翼", "翼签"):
        residue = residue.replace(token, "")
    return not residue and len(compact) >= 2


def _cleanup_extracted_text_noise(text: str) -> str:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    cleaned: list[str] = []
    for line in lines:
        stripped = line.strip()
        if _looks_like_signature_watermark_line(stripped):
            continue
        if stripped.upper() == "TINGYUN.COM":
            continue
        cleaned.append(re.sub(r"(\d+)\s*月/个", r"\1 元/个", line.rstrip()))
    return "\n".join(cleaned).strip()


def _collect_company_names(blocks: list[str]) -> list[str]:
    names: set[str] = set()
    for block in blocks:
        for match in _COMPANY_NAME_RE.findall(block):
            names.add(match.strip())
    return sorted(names)


def _split_company_name(name: str) -> tuple[str, str]:
    for suffix in ("股份有限公司", "有限责任公司", "有限公司"):
        if name.endswith(suffix):
            return name[: -len(suffix)], suffix
    return name, ""


def _build_company_name_replacements(blocks: list[str]) -> dict[str, str]:
    names = _collect_company_names(blocks)
    replacements: dict[str, str] = {}
    for name in names:
        stem, suffix = _split_company_name(name)
        best = name
        best_score = 1
        for other in names:
            if other == name:
                continue
            other_stem, other_suffix = _split_company_name(other)
            if not suffix or suffix != other_suffix:
                continue
            if len(stem) < 2 or len(other_stem) < 2:
                continue
            if stem[-2:] != other_stem[-2:]:
                continue
            score = SequenceMatcher(None, stem, other_stem).ratio()
            if score < 0.7:
                continue
            if len(other) > len(best):
                best = other
                best_score = score
        if best != name and best_score >= 0.7:
            replacements[name] = best
    return replacements


def _apply_company_name_replacements(text: str, replacements: dict[str, str]) -> str:
    for src, dst in sorted(replacements.items(), key=lambda item: len(item[0]), reverse=True):
        pattern = re.compile(
            rf"(?<![A-Za-z0-9\u4e00-\u9fff]){re.escape(src)}(?![A-Za-z0-9\u4e00-\u9fff])"
        )
        text = pattern.sub(dst, text)
    return text


def _normalize_document_text(pages: list[PageContent]) -> None:
    for page in pages:
        page.text = _cleanup_extracted_text_noise(_normalize_amount_phrases(page.text))
        page.tables = [
            _cleanup_extracted_text_noise(_normalize_amount_phrases(table))
            for table in page.tables
        ]


def _load_document_profile(profile_name: str | None, config_path: str | None) -> DocumentProfile | None:
    selected = (profile_name or "").strip()
    custom = (config_path or "").strip()
    if not selected and not custom:
        return None

    selected = _DOCUMENT_PROFILE_ALIASES.get(selected, selected)

    if custom:
        path = Path(custom).expanduser()
    else:
        path = DOCUMENT_PROFILE_CONFIG_DIR / f"{selected}.json"
        if not path.exists():
            path = FIELD_OCR_CONFIG_DIR / f"{selected}.json"

    if not path.exists():
        raise RuntimeError(f"field OCR config not found: {path}")

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid field OCR config JSON: {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise RuntimeError(f"field OCR config must be a JSON object: {path}")

    classification_raw = raw.get("classification") if isinstance(raw.get("classification"), dict) else {}
    quality_raw = raw.get("quality_policy") if isinstance(raw.get("quality_policy"), dict) else {}
    upgrade_raw = raw.get("upgrade_policy") if isinstance(raw.get("upgrade_policy"), dict) else {}
    table_raw = raw.get("table_policy") if isinstance(raw.get("table_policy"), dict) else {}
    cache_raw = raw.get("cache_policy") if isinstance(raw.get("cache_policy"), dict) else {}
    processing_raw = raw.get("processing_policy") if isinstance(raw.get("processing_policy"), dict) else {}
    summary_raw = raw.get("summary_policy") if isinstance(raw.get("summary_policy"), dict) else {}
    section_raw = raw.get("section_policy") if isinstance(raw.get("section_policy"), dict) else {}

    groups: list[FieldGroup] = []
    for item in raw.get("groups", []):
        if not isinstance(item, dict):
            continue
        crop_raw = item.get("crop") or {}
        crop = None
        if isinstance(crop_raw, dict):
            try:
                crop = FieldCrop(
                    x0=float(crop_raw["x0"]),
                    y0=float(crop_raw["y0"]),
                    x1=float(crop_raw["x1"]),
                    y1=float(crop_raw["y1"]),
                )
            except (KeyError, TypeError, ValueError):
                crop = None
        groups.append(
            FieldGroup(
                id=str(item.get("id") or f"group_{len(groups)+1}"),
                aliases=tuple(str(v) for v in item.get("aliases", []) if str(v).strip()),
                page_scope=tuple(int(v) for v in item.get("page_scope", []) if isinstance(v, int)),
                crop=crop,
                start_alias=str(item.get("start_alias")).strip() if item.get("start_alias") else None,
                end_alias=str(item.get("end_alias")).strip() if item.get("end_alias") else None,
                replace_mode=str(item.get("replace_mode") or "block_between_aliases"),
            )
        )

    fields: list[FieldRule] = []
    for item in raw.get("fields", []):
        if not isinstance(item, dict):
            continue
        pattern = item.get("pattern")
        fields.append(
            FieldRule(
                id=str(item.get("id") or f"field_{len(fields)+1}"),
                aliases=tuple(str(v) for v in item.get("aliases", []) if str(v).strip()),
                pattern=str(pattern) if pattern else None,
                page_scope=tuple(int(v) for v in item.get("page_scope", []) if isinstance(v, int)),
            )
        )

    return DocumentProfile(
        name=str(raw.get("profile") or selected or path.stem),
        classification=ClassificationPolicy(
            required_terms=tuple(
                str(v) for v in classification_raw.get("required_terms", []) if str(v).strip()
            )
        ),
        quality_policy=QualityPolicy(
            sparse_text_chars=max(0, int(quality_raw.get("sparse_text_chars", 40))),
            usable_text_chars=max(1, int(quality_raw.get("usable_text_chars", 120))),
            scan_page_ratio=float(quality_raw.get("scan_page_ratio", 0.85)),
            mixed_page_ratio=float(quality_raw.get("mixed_page_ratio", 0.2)),
        ),
        upgrade_policy=UpgradePolicy(
            default_mode=str(upgrade_raw.get("default_mode") or "accurate").strip().lower(),
            local_ocr_backend=str(upgrade_raw.get("local_ocr_backend") or "paddleocr").strip().lower(),
            region_llm_modes=tuple(
                str(v).strip().lower()
                for v in upgrade_raw.get("region_llm_modes", ["accurate", "full"])
                if str(v).strip()
            ),
            full_llm_modes=tuple(
                str(v).strip().lower()
                for v in upgrade_raw.get("full_llm_modes", ["full"])
                if str(v).strip()
            ),
            proofread_modes=tuple(
                str(v).strip().lower()
                for v in upgrade_raw.get("proofread_modes", ["full"])
                if str(v).strip()
            ),
        ),
        table_policy=TablePolicy(
            prefer_markitdown=bool(table_raw.get("prefer_markitdown", True))
        ),
        cache_policy=CachePolicy(
            page_ocr=bool(cache_raw.get("page_ocr", True)),
            region_ocr=bool(cache_raw.get("region_ocr", True)),
        ),
        processing_policy=ProcessingPolicy(
            large_file_threshold_mb=max(1, int(processing_raw.get("large_file_threshold_mb", 50))),
            local_ocr_render_scale=max(
                0.5,
                float(processing_raw.get("local_ocr_render_scale", LOCAL_OCR_RENDER_SCALE)),
            ),
            llm_ocr_render_scale=max(
                0.5,
                float(processing_raw.get("llm_ocr_render_scale", OCR_RENDER_SCALE)),
            ),
            max_local_ocr_pixels=max(
                500_000,
                int(processing_raw.get("max_local_ocr_pixels", 4_000_000)),
            ),
            max_llm_ocr_pixels=max(
                500_000,
                int(processing_raw.get("max_llm_ocr_pixels", 8_000_000)),
            ),
            min_ocr_render_scale=max(
                0.5,
                float(processing_raw.get("min_ocr_render_scale", 1.25)),
            ),
        ),
        summary_policy=SummaryPolicy(
            default_mode=str(summary_raw.get("default_mode") or "sync").strip().lower(),
            async_modes=tuple(
                str(v).strip().lower()
                for v in summary_raw.get("async_modes", [])
                if str(v).strip()
            ),
            sync_modes=tuple(
                str(v).strip().lower()
                for v in summary_raw.get("sync_modes", ["full"])
                if str(v).strip()
            ),
        ),
        section_policy=SectionPolicy(
            toc_max_level=max(1, int(section_raw.get("toc_max_level", 2))),
            suppress_arabic_clause_headings_when_formal_chinese=bool(
                section_raw.get("suppress_arabic_clause_headings_when_formal_chinese", False)
            ),
            formal_chinese_min_headings=max(
                1,
                int(section_raw.get("formal_chinese_min_headings", 4)),
            ),
        ),
        groups=tuple(groups),
        fields=tuple(fields),
    )


def _page_blob(page: PageContent) -> str:
    if page.tables_in_text:
        return page.text.strip()

    parts = [page.text.strip()] if page.text.strip() else []
    parts.extend(table.strip() for table in page.tables if table.strip())
    return "\n\n".join(parts).strip()


def _set_page_blob(page: PageContent, text: str) -> None:
    body, tables = _extract_tables_from_ocr_text(text, page.page_num, page.page_num)
    page.text = body
    page.tables = tables
    page.tables_in_text = bool(tables)


def _blob_has_alias(text: str, aliases: tuple[str, ...]) -> bool:
    return any(alias and alias in text for alias in aliases)


def _field_value_quality(field_id: str, value: str) -> tuple[bool, str]:
    value = value.strip()
    if not value:
        return False, "empty"
    if re.search(r"[\u3040-\u30ff]", value):
        return False, "kana_noise"
    if _looks_like_bracket_noise(value):
        return False, "bracket_noise"

    normalized = re.sub(r"\s+", "", value)
    if field_id == "contract_no":
        if normalized in {"甲", "乙", "合同", "合同编号", "方"}:
            return False, "label_only"
        if len(normalized) < 4 or not re.search(r"\d", normalized):
            return False, "too_short_or_no_digit"
    elif field_id in {"party_a_name", "party_b_name", "customer_name"}:
        if len(normalized) < 4:
            return False, "too_short"
        if not re.search(r"(公司|中心|银行|基金|学校|医院|政府|委员会|研究院|事务所|集团)", normalized):
            return False, "not_org_like"
    elif field_id.endswith("_phone"):
        if len(re.sub(r"\D", "", normalized)) < 7:
            return False, "not_phone_like"
    elif field_id.endswith("_account"):
        if len(re.sub(r"\D", "", normalized)) < 6:
            return False, "not_account_like"
    return True, ""


def _source_filename_contract_no(source_filename: str | None) -> str | None:
    stem = Path(source_filename or "").stem.strip()
    if re.fullmatch(r"[A-Za-z]{2,10}\d{4,20}", stem):
        return stem
    return None


def _normalize_cover_label_lines(blob: str) -> str:
    text = blob.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"(?m)^甲\s*\n\s*方\s*[：:]", "甲方：", text)
    text = re.sub(r"(?m)^乙\s*\n\s*方\s*[：:]", "乙方：", text)
    return text


def _replace_blob_segment(text: str, group: FieldGroup, replacement: str) -> str:
    if group.replace_mode == "replace_entire_page":
        return replacement.strip()

    start = -1
    if group.start_alias:
        start = text.find(group.start_alias)
    if start < 0:
        starts = [text.find(alias) for alias in group.aliases if alias and text.find(alias) >= 0]
        start = min(starts) if starts else -1
    if start < 0:
        return text

    end = len(text)
    if group.end_alias:
        found = text.find(group.end_alias, start + len(group.start_alias))
        if found >= 0:
            end = found
    return (text[:start].rstrip() + "\n\n" + replacement.strip() + "\n\n" + text[end:].lstrip()).strip()


def _prepend_source_contract_no_if_missing(text: str, source_filename: str | None) -> str:
    contract_no = _source_filename_contract_no(source_filename)
    if not contract_no:
        return text.strip()
    normalized = re.sub(r"\s+", "", text)
    if contract_no in normalized:
        return text.strip()
    return f"{contract_no}\n{text.strip()}".strip()


def _extract_profile_fields(
    pages: list[PageContent],
    profile: DocumentProfile,
    *,
    source_filename: str | None = None,
) -> dict[str, Any]:
    extracted: dict[str, Any] = {}
    for field_rule in profile.fields:
        for page in pages:
            if field_rule.page_scope and page.page_num not in field_rule.page_scope:
                continue
            blob = _normalize_cover_label_lines(_page_blob(page))
            if field_rule.aliases and not _blob_has_alias(blob, field_rule.aliases):
                continue
            if field_rule.pattern:
                match = re.search(field_rule.pattern, blob, flags=re.MULTILINE)
                if not match:
                    continue
                value = (match.group(1) if match.groups() else match.group(0)).strip()
            else:
                value = next((alias for alias in field_rule.aliases if alias in blob), "").strip()
            if value:
                valid, reason = _field_value_quality(field_rule.id, value)
                if not valid:
                    logger.info(
                        "Discarded low-confidence field %s on page %d: %r (%s)",
                        field_rule.id,
                        page.page_num,
                        value,
                        reason,
                    )
                    continue
                extracted[field_rule.id] = {
                    "value": value,
                    "page": page.page_num,
                    "source": "profile_regex",
                }
                break
    if "contract_no" not in extracted:
        fallback_contract_no = _source_filename_contract_no(source_filename)
        if fallback_contract_no:
            extracted["contract_no"] = {
                "value": fallback_contract_no,
                "page": 1,
                "source": "source_filename",
            }
    return extracted


def _apply_field_focused_ocr(
    filepath: Path,
    pages: list[PageContent],
    profile: DocumentProfile,
    cache_dir: Path | None = None,
    proofread: bool = True,
) -> dict[str, Any]:
    import fitz

    applied_groups: list[dict[str, Any]] = []
    doc = fitz.open(str(filepath))
    try:
        for group in profile.groups:
            if not group.crop:
                continue
            for page in pages:
                if group.page_scope and page.page_num not in group.page_scope:
                    continue
                blob = _page_blob(page)
                if group.aliases and not _blob_has_alias(blob, group.aliases):
                    continue

                fitz_page = doc[page.page_num - 1]
                rect = fitz_page.rect
                clip = fitz.Rect(
                    rect.x0 + rect.width * group.crop.x0,
                    rect.y0 + rect.height * group.crop.y0,
                    rect.x0 + rect.width * group.crop.x1,
                    rect.y0 + rect.height * group.crop.y1,
                )
                pix = fitz_page.get_pixmap(matrix=fitz.Matrix(FIELD_OCR_RENDER_SCALE, FIELD_OCR_RENDER_SCALE), clip=clip)
                img_bytes = pix.tobytes("png")
                region_text = ""
                if cache_dir and profile.cache_policy.region_ocr:
                    ck = _ocr_cache_key(img_bytes)
                    cache_path = _ocr_cache_variant_path(
                        cache_dir,
                        f"ocr_region_p{page.page_num:04d}_{group.id}.{ck}.txt",
                    )
                    if cache_path.exists():
                        region_text = cache_path.read_text(encoding="utf-8").strip()
                if not region_text:
                    region_text = gemini_ocr(img_bytes, page.page_num, proofread=proofread).strip()
                    if cache_dir and profile.cache_policy.region_ocr and region_text:
                        cache_path = _ocr_cache_variant_path(
                            cache_dir,
                            f"ocr_region_p{page.page_num:04d}_{group.id}.{_ocr_cache_key(img_bytes)}.txt",
                        )
                        cache_path.write_text(region_text, encoding="utf-8")
                if not region_text or region_text.startswith("["):
                    continue
                region_text = _cleanup_ocr_text(region_text, source_filename=filepath.name)
                if page.page_num == 1 and group.replace_mode == "replace_entire_page":
                    region_text = _prepend_source_contract_no_if_missing(region_text, filepath.name)

                replace_source = page.text.strip()
                replaced = _replace_blob_segment(replace_source, group, region_text)
                if replaced != replace_source:
                    page.tables = []
                    _set_page_blob(page, replaced)
                    applied_groups.append({"group_id": group.id, "page": page.page_num})
    finally:
        doc.close()

    _normalize_document_text(pages)
    return {
        "profile": profile.name,
        "applied_groups": applied_groups,
        "extracted_fields": _extract_profile_fields(pages, profile, source_filename=filepath.name),
    }


# ═══════════════════════════════════════════
# Section stable ID
# ═══════════════════════════════════════════


def _section_sid(title: str, text: str) -> str:
    raw = (title + text[:200]).encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:12]


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


def _resolve_summary_mode(
    *,
    profile: DocumentProfile | None,
    parse_mode: str | None,
    generate_summary: bool,
    requested_mode: str | None,
) -> str:
    if not generate_summary:
        return "off"

    mode = (requested_mode or "").strip().lower()
    if not mode:
        mode = os.environ.get("LARKSCOUT_SUMMARY_MODE", "").strip().lower()

    if mode in {"off", "sync", "defer"}:
        return mode

    selected_parse_mode = (parse_mode or "").strip().lower()
    if profile:
        if selected_parse_mode and selected_parse_mode in profile.summary_policy.async_modes:
            return "defer"
        if selected_parse_mode and selected_parse_mode in profile.summary_policy.sync_modes:
            return "sync"
        if profile.summary_policy.default_mode in {"off", "sync", "defer"}:
            return profile.summary_policy.default_mode

    return "sync"


def _set_summary_metadata(
    parsed: ParsedDocument,
    *,
    mode: str,
    status: str,
    error: str | None = None,
    error_code: str | None = None,
    attempts: int | None = None,
) -> None:
    metadata = parsed.metadata if isinstance(parsed.metadata, dict) else {}
    existing = metadata.get("summary") if isinstance(metadata.get("summary"), dict) else {}
    metadata["summary"] = {
        "mode": mode,
        "status": status,
        "updated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "attempts": int(attempts if attempts is not None else existing.get("attempts", 0)),
    }
    if status == "running":
        metadata["summary"]["started_at"] = metadata["summary"]["updated_at"]
    elif existing.get("started_at"):
        metadata["summary"]["started_at"] = existing.get("started_at")
    if status in {"completed", "failed"}:
        metadata["summary"]["finished_at"] = metadata["summary"]["updated_at"]
    if error:
        metadata["summary"]["error"] = error
    if error_code:
        metadata["summary"]["error_code"] = error_code
    parsed.metadata = metadata


def _summary_placeholder_text(
    status: str, error: str | None = None, locale: str | None = None
) -> str:
    output_locale = "zh" if str(locale or "").lower().startswith("zh") else "en"
    if status == "running":
        return "(摘要生成中)" if output_locale == "zh" else "(Summary running)"
    if status == "failed":
        if error:
            if output_locale == "zh":
                return f"(摘要生成失败: {error})"
            return f"(Summary failed: {error})"
        return "(摘要生成失败)" if output_locale == "zh" else "(Summary failed)"
    return "(摘要待生成)" if output_locale == "zh" else "(Summary pending)"


def _current_summary_attempts(parsed: ParsedDocument) -> int:
    metadata = parsed.metadata if isinstance(parsed.metadata, dict) else {}
    summary = metadata.get("summary") if isinstance(metadata.get("summary"), dict) else {}
    try:
        return int(summary.get("attempts", 0))
    except (TypeError, ValueError):
        return 0


def _classify_summary_error(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, FuturesTimeoutError):
        return "timeout", f"summary timed out after {int(DEFERRED_SUMMARY_TIMEOUT_SEC)}s"

    text = str(exc).strip() or exc.__class__.__name__
    lower = text.lower()
    if "attempt limit" in lower:
        return "attempt_limit", text
    if "429" in text or "rate limit" in lower or "速率限制" in text:
        return "rate_limit", "upstream rate limit"
    if "timeout" in lower or "timed out" in lower:
        return "timeout", text
    return "provider_error", text


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


# ═══════════════════════════════════════════
# PDF parsing
# ═══════════════════════════════════════════


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
    markdown_text = ""
    try:
        markdown_text = _convert_to_markdown(filepath)
        logger.info(f"MarkItDown extraction complete: {len(markdown_text)} chars")
    except RuntimeError as exc:
        logger.warning("MarkItDown extraction failed for %s: %s", filepath.name, exc)

    # Open with fitz for page count, TOC, and OCR rendering
    doc = fitz.open(str(filepath))
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

    for i, page in enumerate(doc):
        page_num = i + 1
        text = page.get_text("text").strip()
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

    assessment = _assess_contract_quality(markdown_text, page_signals, profile)
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
        scale, render_pixels, capped = _resolve_ocr_render_scale(
            page,
            requested_scale=requested_scale,
            max_pixels=max_pixels,
            min_scale=processing_policy.min_ocr_render_scale,
        )
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
                    cp = _ocr_cache_path(cache_dir, pn)
                    ck = _ocr_cache_key(img_b)
                    ck_path = cp.with_suffix(f".{ck}.txt")
                    ck_path.write_text(result, encoding="utf-8")

    pages: list[PageContent] = []
    ocr_table_count = 0
    ocr_count = len(local_ocr_set | llm_ocr_set)
    for page_num in range(1, total_pages + 1):
        raw_text = page_texts.get(page_num, "")
        page_text = raw_text
        page_tables: list[str] = []
        enhanced = llm_ocr_results.get(page_num) or local_ocr_results.get(page_num)
        if enhanced:
            page_text = _cleanup_ocr_text(_usable_page_text(raw_text, enhanced))
            if extract_tables:
                page_text, page_tables = _extract_tables_from_ocr_text(page_text, page_num, total_pages)
                ocr_table_count += len(page_tables)
        pages.append(
            PageContent(
                page_num=page_num,
                text=page_text.strip(),
                is_ocr=page_num in (local_ocr_set | llm_ocr_set),
                tables=page_tables,
                tables_in_text=bool(page_tables),
            )
        )

    if llm_ocr_results:
        logger.info(f"LLM OCR pages: {sorted(llm_ocr_results)}")
    if local_ocr_results:
        logger.info(f"Local OCR pages: {sorted(local_ocr_results)}")

    if profile and not assessment.get("is_contract"):
        combined_text = "\n".join(page.text for page in pages if page.text)
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

    # Count tables in Markdown output
    if extract_tables:
        table_count = _count_markdown_tables(markdown_text) if (markdown_text and (not profile or profile.table_policy.prefer_markitdown)) else ocr_table_count
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


# ═══════════════════════════════════════════
# Word parsing
# ═══════════════════════════════════════════

WORD_XML_NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "v": "urn:schemas-microsoft-com:vml",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
}

_RASTER_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tif", ".tiff"}
_VECTOR_IMAGE_EXTENSIONS = {".emf", ".wmf"}
_IMAGE_MIME_BY_EXT = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".bmp": "image/bmp",
    ".gif": "image/gif",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".emf": "image/x-emf",
    ".wmf": "image/x-wmf",
}

_IMAGE_CONTEXT_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("business_license", ("营业执照", "统一社会信用代码", "business license")),
    ("id_card", ("身份证", "居民身份证", "identity card", "id card")),
    ("education_certificate", ("学历", "毕业证", "毕业证书", "学信网", "电子注册备案表")),
    ("degree_certificate", ("学位证", "学位证书")),
    (
        "personnel_certificate",
        ("项目经理证书", "人员证书", "人员资质", "PMP", "信息系统项目管理师", "系统集成项目管理工程师", "软考"),
    ),
    ("certificate", ("证书", "资质", "认证")),
    ("contract_copy", ("合同复印件", "合同案例", "类似案例", "业绩证明", "协议复印件")),
    ("financial_statement", ("财务报表", "审计报告", "资产负债表", "利润表", "现金流量表")),
    ("product_screenshot", ("产品截图", "系统截图", "功能截图", "界面截图", "截图")),
    ("seal_or_signature", ("签字", "签章", "盖章", "公章", "印章")),
)


def _word_rel_target_to_package_path(target: str) -> str:
    clean = target.replace("\\", "/").strip()
    if clean.startswith("/"):
        return posixpath.normpath(clean.lstrip("/"))
    return posixpath.normpath(posixpath.join("word", clean))


def _word_paragraph_text(paragraph: ET.Element) -> str:
    parts: list[str] = []
    for node in paragraph.findall(".//w:t", WORD_XML_NS):
        if node.text:
            parts.append(node.text)
    return "".join(parts).strip()


def _word_paragraph_style(paragraph: ET.Element) -> str:
    style = paragraph.find("./w:pPr/w:pStyle", WORD_XML_NS)
    if style is None:
        return ""
    return str(style.attrib.get(f"{{{WORD_XML_NS['w']}}}val") or "").strip()


def _word_heading_level(text: str, style: str) -> int:
    style_lower = style.lower()
    if any(token in style_lower for token in ("heading", "标题", "title")):
        match = re.search(r"([1-6])", style_lower)
        return min(int(match.group(1)), 3) if match else 1
    return _is_heading(text)


def _word_image_relationships(docx_path: Path) -> dict[str, str]:
    try:
        with zipfile.ZipFile(docx_path) as zf:
            rels_xml = zf.read("word/_rels/document.xml.rels")
    except Exception:
        return {}

    root = ET.fromstring(rels_xml)
    rels: dict[str, str] = {}
    for rel in root.findall("rel:Relationship", WORD_XML_NS):
        rel_id = str(rel.attrib.get("Id") or "")
        target = str(rel.attrib.get("Target") or "")
        rel_type = str(rel.attrib.get("Type") or "")
        target_mode = str(rel.attrib.get("TargetMode") or "")
        if not rel_id or not target:
            continue
        if target_mode.lower() == "external":
            # Linked (not embedded) images live outside the .docx package;
            # they cannot be read via zipfile and must not count toward
            # embedded-image limits.
            continue
        if "image" not in rel_type.lower() and not target.lower().startswith("media/"):
            continue
        rels[rel_id] = _word_rel_target_to_package_path(target)
    return rels


def _word_paragraph_image_rel_ids(paragraph: ET.Element) -> list[str]:
    rel_ids: list[str] = []
    for node in paragraph.findall(".//a:blip", WORD_XML_NS):
        rel_id = str(node.attrib.get(f"{{{WORD_XML_NS['r']}}}embed") or "").strip()
        if rel_id and rel_id not in rel_ids:
            rel_ids.append(rel_id)
    for node in paragraph.findall(".//v:imagedata", WORD_XML_NS):
        rel_id = str(node.attrib.get(f"{{{WORD_XML_NS['r']}}}id") or "").strip()
        if rel_id and rel_id not in rel_ids:
            rel_ids.append(rel_id)
    return rel_ids


def _word_image_context_text(
    paragraph_texts: list[str],
    paragraph_index: int,
    *,
    before: int = 4,
    after: int = 3,
    max_chars: int = 1200,
) -> str:
    start = max(0, paragraph_index - before)
    end = min(len(paragraph_texts), paragraph_index + after + 1)
    parts = [text for text in paragraph_texts[start:end] if text]
    return "\n".join(parts)[:max_chars]


def _count_word_embedded_image_references(filepath: Path) -> int:
    """Count embedded Word image references that would be processed for image OCR."""
    if filepath.suffix.lower() != ".docx":
        return 0
    try:
        with zipfile.ZipFile(filepath) as zf:
            document_xml = zf.read("word/document.xml")
        rels = _word_image_relationships(filepath)
        root = ET.fromstring(document_xml)
    except Exception as exc:
        logger.warning("Word embedded image count failed for %s: %s", filepath.name, exc)
        return 0

    count = 0
    for paragraph in root.findall(".//w:p", WORD_XML_NS):
        for rel_id in _word_paragraph_image_rel_ids(paragraph):
            if rel_id in rels:
                count += 1
    return count


def _render_raster_image_to_png(image_bytes: bytes) -> tuple[bytes, str]:
    from PIL import Image

    with Image.open(io.BytesIO(image_bytes)) as img:
        if img.mode not in {"RGB", "RGBA"}:
            img = img.convert("RGB")
        out = io.BytesIO()
        img.save(out, format="PNG")
        return out.getvalue(), "ok"


def _convert_vector_image_to_png(image_bytes: bytes, original_ext: str) -> tuple[bytes, str]:
    binary = shutil.which("soffice") or shutil.which("libreoffice")
    if not binary:
        raise RuntimeError("office converter is not available for vector image conversion")
    with tempfile.TemporaryDirectory(prefix="larkscout-word-image-") as tmp:
        tmp_dir = Path(tmp)
        src = tmp_dir / f"image{original_ext}"
        src.write_bytes(image_bytes)
        cmd = [
            binary,
            "--headless",
            "--nologo",
            "--nofirststartwizard",
            "--convert-to",
            "png",
            "--outdir",
            str(tmp_dir),
            str(src),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
        candidates = sorted(tmp_dir.glob("*.png"))
        if proc.returncode != 0 or not candidates:
            details = (proc.stderr or proc.stdout or "").strip()
            raise RuntimeError(details or f"failed to convert {original_ext} to png")
        return candidates[0].read_bytes(), "ok"


def _render_embedded_image(image_bytes: bytes, original_ext: str) -> tuple[bytes, str, str]:
    ext = original_ext.lower()
    if ext in _RASTER_IMAGE_EXTENSIONS:
        rendered, status = _render_raster_image_to_png(image_bytes)
        return rendered, ".png", status
    if ext in _VECTOR_IMAGE_EXTENSIONS:
        rendered, status = _convert_vector_image_to_png(image_bytes, ext)
        return rendered, ".png", status
    raise RuntimeError(f"unsupported embedded image format: {ext or 'unknown'}")


def _image_dimensions(image_bytes: bytes) -> tuple[int, int]:
    if not image_bytes:
        return 0, 0
    try:
        from PIL import Image

        with Image.open(io.BytesIO(image_bytes)) as img:
            return int(img.width), int(img.height)
    except Exception:
        return 0, 0


def _image_average_hash(image_bytes: bytes, hash_size: int = 8) -> str:
    if not image_bytes:
        return ""
    try:
        from PIL import Image

        with Image.open(io.BytesIO(image_bytes)) as img:
            img = img.convert("L").resize((hash_size, hash_size))
            pixels = list(img.tobytes())
    except Exception:
        return ""
    if not pixels:
        return ""
    avg = sum(pixels) / len(pixels)
    bits = "".join("1" if px >= avg else "0" for px in pixels)
    return f"{int(bits, 2):0{hash_size * hash_size // 4}x}"


def _extract_image_context_keywords(*texts: str) -> list[str]:
    haystack = "\n".join(text for text in texts if text).lower()
    if not haystack:
        return []
    keywords: list[str] = []
    for key, aliases in _IMAGE_CONTEXT_KEYWORDS:
        if any(alias.lower() in haystack for alias in aliases):
            keywords.append(key)
    return keywords


def _inventory_hints_for_image(image: EmbeddedImage) -> list[str]:
    hints: list[str] = []
    area = image.width * image.height
    ratio = image.aspect_ratio
    keyword_set = set(image.context_keywords)

    if image.render_status == "failed":
        hints.append("render_failed")
    if image.render_status == "ok" and area > 0:
        if area < 20_000:
            hints.append("small_image")
        if image.width >= 900 and image.height >= 500 and 1.2 <= ratio <= 2.4:
            hints.append("screenshot_like")
        if image.height >= 900 and 0.55 <= ratio <= 1.15:
            hints.append("document_scan_like")

    for key in sorted(keyword_set):
        hints.append(f"context:{key}")
    if {"education_certificate", "degree_certificate"} & keyword_set:
        hints.append("personnel_material_candidate")
    if {"business_license", "id_card", "personnel_certificate", "certificate"} & keyword_set:
        hints.append("certificate_or_identity_candidate")
    if "contract_copy" in keyword_set:
        hints.append("case_contract_candidate")
    if "financial_statement" in keyword_set:
        hints.append("financial_material_candidate")
    if "product_screenshot" in keyword_set:
        hints.append("product_screenshot_candidate")
    return list(dict.fromkeys(hints))


def _populate_embedded_image_inventory(image: EmbeddedImage) -> None:
    image.original_size_bytes = len(image.original_bytes)
    image.original_sha256 = (
        hashlib.sha256(image.original_bytes).hexdigest() if image.original_bytes else ""
    )
    image.rendered_size_bytes = len(image.rendered_bytes)
    image.rendered_sha256 = (
        hashlib.sha256(image.rendered_bytes).hexdigest() if image.rendered_bytes else ""
    )
    image.width, image.height = _image_dimensions(image.rendered_bytes or image.original_bytes)
    image.aspect_ratio = round(image.width / image.height, 4) if image.height else 0.0
    image.average_hash = _image_average_hash(image.rendered_bytes or image.original_bytes)
    image.context_keywords = _extract_image_context_keywords(
        image.near_heading,
        image.paragraph_text,
        image.context_text,
        image.section_title,
    )
    image.inventory_hints = _inventory_hints_for_image(image)


def _ocr_embedded_image(image: EmbeddedImage, backend: str) -> tuple[str, str, str, str]:
    selected = (backend or "auto").strip().lower()
    if selected not in {"auto", "local", "llm"}:
        selected = "auto"
    if not image.rendered_bytes:
        return "", selected, "failed", "image was not rendered"

    if selected in {"auto", "local"}:
        text = local_ocr(image.rendered_bytes, image.order, "paddleocr")
        if text and not _is_ocr_failed_text(text):
            return _cleanup_ocr_text(text), "local-paddleocr", "ok", ""
        if selected == "local":
            return "", "local-paddleocr", "failed", text or "local OCR returned no text"

    text = gemini_ocr(image.rendered_bytes, image.order)
    if text and not _is_ocr_failed_text(text):
        return _cleanup_ocr_text(text), "llm", "ok", ""
    used_backend = "llm" if selected == "llm" else "auto"
    return "", used_backend, "failed", text or "LLM OCR returned no text"


def _anchor_word_images_to_sections(images: list[EmbeddedImage], sections: list[Section]) -> None:
    if not images or not sections:
        return
    for image in images:
        heading_key = _normalize_heading_key(image.near_heading)
        paragraph_key = _normalize_heading_key(image.paragraph_text)
        selected: Section | None = None
        for sec in sections:
            title_key = _normalize_heading_key(sec.title)
            text_key = _normalize_heading_key(sec.text[:1500])
            if heading_key and (heading_key == title_key or heading_key in text_key):
                selected = sec
                break
            if paragraph_key and paragraph_key in text_key:
                selected = sec
                break
        if selected:
            image.anchor_sid = selected.sid
            image.section_title = selected.title
            if image.image_id not in selected.image_refs:
                selected.image_refs.append(image.image_id)


def _extract_word_embedded_images(
    filepath: Path,
    *,
    sections: list[Section],
    ocr_images: bool = False,
    image_ocr_backend: str = "auto",
    max_images: int = 200,
) -> list[EmbeddedImage]:
    if filepath.suffix.lower() != ".docx":
        return []
    limit = max(0, int(max_images or 0))
    if limit == 0:
        return []
    try:
        with zipfile.ZipFile(filepath) as zf:
            document_xml = zf.read("word/document.xml")
            rels = _word_image_relationships(filepath)
            root = ET.fromstring(document_xml)
            paragraphs = root.findall(".//w:p", WORD_XML_NS)
            paragraph_texts = [_word_paragraph_text(paragraph) for paragraph in paragraphs]
            images: list[EmbeddedImage] = []
            current_heading = ""

            for paragraph_index, paragraph in enumerate(paragraphs, 1):
                paragraph_text = paragraph_texts[paragraph_index - 1]
                heading_level = _word_heading_level(paragraph_text, _word_paragraph_style(paragraph))
                if paragraph_text and heading_level > 0:
                    current_heading = _strip_heading_markup(paragraph_text)

                for rel_id in _word_paragraph_image_rel_ids(paragraph):
                    media_path = rels.get(rel_id)
                    if not media_path:
                        continue
                    if len(images) >= limit:
                        break
                    try:
                        original_bytes = zf.read(media_path)
                    except KeyError:
                        continue
                    image_id = f"IMG-{len(images) + 1:03d}"
                    original_ext = Path(media_path).suffix.lower()
                    image = EmbeddedImage(
                        image_id=image_id,
                        order=len(images) + 1,
                        media_path=media_path,
                        relationship_id=rel_id,
                        paragraph_index=paragraph_index,
                        paragraph_text=paragraph_text,
                        context_text=_word_image_context_text(
                            paragraph_texts, paragraph_index - 1
                        ),
                        near_heading=current_heading,
                        original_ext=original_ext,
                        original_type=_IMAGE_MIME_BY_EXT.get(
                            original_ext, "application/octet-stream"
                        ),
                        original_bytes=original_bytes,
                    )
                    try:
                        rendered, rendered_ext, render_status = _render_embedded_image(
                            image.original_bytes, image.original_ext
                        )
                        image.rendered_bytes = rendered
                        image.rendered_ext = rendered_ext
                        image.rendered_type = _IMAGE_MIME_BY_EXT.get(rendered_ext, "image/png")
                        image.render_status = render_status
                    except Exception as exc:
                        image.render_status = "failed"
                        image.render_error = str(exc)

                    image.ocr_enabled = bool(ocr_images)
                    if ocr_images:
                        text, used_backend, status, error = _ocr_embedded_image(
                            image, image_ocr_backend
                        )
                        image.ocr_backend = used_backend
                        image.ocr_status = status
                        image.ocr_text = text
                        image.ocr_error = error
                    images.append(image)
                if len(images) >= limit:
                    break
    except Exception as exc:
        logger.warning("Word embedded image extraction failed for %s: %s", filepath.name, exc)
        return []

    _anchor_word_images_to_sections(images, sections)
    for image in images:
        _populate_embedded_image_inventory(image)
    return images


def parse_word(
    filepath: Path,
    extract_tables: bool = True,
    profile: DocumentProfile | None = None,
    extract_images: bool = False,
    ocr_images: bool = False,
    image_ocr_backend: str = "auto",
    max_images: int = 200,
) -> ParsedDocument:
    logger.info(f"Parsing Word: {filepath.name}")
    source_size_bytes = filepath.stat().st_size
    markdown_text = _convert_to_markdown(filepath)
    logger.info(f"MarkItDown extraction complete: {len(markdown_text)} chars")

    est_pages = max(1, len(markdown_text) // 3000)
    table_blocks = _extract_markdown_table_blocks(markdown_text) if extract_tables else []
    pages = [PageContent(page_num=1, text=markdown_text, tables=table_blocks)]
    sections = _split_sections(pages, section_policy=profile.section_policy if profile else None)
    for sec in sections:
        sec.sid = _section_sid(sec.title, sec.text)

    table_count = len(table_blocks) if extract_tables else 0
    embedded_image_count = _count_word_embedded_image_references(filepath) if extract_images else 0
    images = (
        _extract_word_embedded_images(
            filepath,
            sections=sections,
            ocr_images=ocr_images,
            image_ocr_backend=image_ocr_backend,
            max_images=max_images,
        )
        if extract_images
        else []
    )

    logger.info(
        f"Parse complete: {len(sections)} sections, ~{est_pages} pages, "
        f"{table_count} tables, {len(images)} images"
    )
    return ParsedDocument(
        filename=filepath.name,
        file_type=filepath.suffix.lower().lstrip(".") or "docx",
        total_pages=est_pages,
        pages=pages,
        sections=sections,
        table_count=table_count,
        images=images,
        extract_tables=extract_tables,
        metadata={
            "document_profile": profile.name if profile else None,
            "source_file": {"size_bytes": source_size_bytes},
            "word_images": {
                "extract_enabled": bool(extract_images),
                "ocr_enabled": bool(ocr_images),
                "ocr_backend": image_ocr_backend if ocr_images else "",
                "embedded_image_count": embedded_image_count,
                "max_images": max(0, int(max_images or 0)),
                "extracted": len(images),
                "truncated": bool(extract_images and embedded_image_count > len(images)),
                "render_ok": sum(1 for image in images if image.render_status == "ok"),
                "render_failed": sum(1 for image in images if image.render_status == "failed"),
                "ocr_ok": sum(1 for image in images if image.ocr_status == "ok"),
                "ocr_failed": sum(1 for image in images if image.ocr_status == "failed"),
            },
        },
    )


# ═══════════════════════════════════════════
# XLSX parsing
# ═══════════════════════════════════════════


def parse_xlsx(filepath: Path) -> ParsedDocument:
    """Parse an XLSX workbook via MarkItDown."""
    logger.info(f"Parsing XLSX: {filepath.name}")
    markdown_text = _convert_to_markdown(filepath)
    logger.info(f"MarkItDown extraction complete: {len(markdown_text)} chars")

    # Split by sheet headers (MarkItDown uses "## Sheet: name" or similar)
    pages: list[PageContent] = []
    sections: list[Section] = []
    table_count = 0

    # Try to split by markdown headings for sheet-level sections
    sheet_blocks = re.split(r"^(##\s+.+)$", markdown_text, flags=re.MULTILINE)

    if len(sheet_blocks) > 1:
        idx = 0
        for i in range(1, len(sheet_blocks), 2):
            idx += 1
            title = sheet_blocks[i].lstrip("#").strip()
            text = sheet_blocks[i + 1].strip() if i + 1 < len(sheet_blocks) else ""
            if not text:
                continue
            page = PageContent(page_num=idx, text=text, tables=[text] if "| " in text else [])
            pages.append(page)
            if "| " in text:
                table_count += 1
            sid = _section_sid(title, text)
            sections.append(
                Section(
                    index=idx, title=title, level=1, text=text, page_range=f"sheet {idx}", sid=sid
                )
            )
    else:
        # Single block — treat as one section
        pages = [
            PageContent(
                page_num=1,
                text=markdown_text,
                tables=[markdown_text] if "| " in markdown_text else [],
            )
        ]
        if "| " in markdown_text:
            table_count = 1
        sid = _section_sid(filepath.stem, markdown_text)
        sections = (
            [
                Section(
                    index=1,
                    title=filepath.stem,
                    level=1,
                    text=markdown_text,
                    page_range="sheet 1",
                    sid=sid,
                )
            ]
            if markdown_text.strip()
            else []
        )

    # Size guard
    truncated = len(markdown_text) > MAX_PARSE_ROWS * 100  # rough char limit

    if truncated:
        logger.warning("XLSX output may be truncated (large file)")
    logger.info(f"XLSX parse complete: {len(sections)} sheets, {table_count} tables")
    result = ParsedDocument(
        filename=filepath.name,
        file_type=filepath.suffix.lower().lstrip(".") or "xlsx",
        total_pages=max(len(pages), 1),
        pages=pages,
        sections=sections,
        table_count=table_count,
    )
    if truncated:
        result.metadata["truncated"] = True
        result.metadata["max_rows"] = MAX_PARSE_ROWS
    return result


# ═══════════════════════════════════════════
# CSV parsing
# ═══════════════════════════════════════════


def parse_csv(filepath: Path) -> ParsedDocument:
    """Parse a CSV file via MarkItDown."""
    logger.info(f"Parsing CSV: {filepath.name}")
    markdown_text = _convert_to_markdown(filepath)
    logger.info(f"MarkItDown extraction complete: {len(markdown_text)} chars")

    stem = filepath.stem
    table_count = 1 if markdown_text.strip() else 0
    sid = _section_sid(stem, markdown_text)

    page = PageContent(
        page_num=1,
        text=markdown_text,
        tables=[markdown_text] if markdown_text.strip() else [],
    )
    section = Section(
        index=1,
        title=stem,
        level=1,
        text=markdown_text,
        page_range="sheet 1",
        sid=sid,
    )

    logger.info(f"CSV parse complete: {table_count} tables")
    return ParsedDocument(
        filename=filepath.name,
        file_type="csv",
        total_pages=1,
        pages=[page],
        sections=[section] if markdown_text.strip() else [],
        table_count=table_count,
    )


def parse_generic(filepath: Path, profile: DocumentProfile | None = None) -> ParsedDocument:
    """Parse any MarkItDown-supported format (PPTX, HTML, etc.)."""
    ext = filepath.suffix.lower()
    file_type = ext.lstrip(".")
    logger.info(f"Parsing {file_type.upper()}: {filepath.name}")
    markdown_text = _convert_to_markdown(filepath)
    logger.info(f"MarkItDown extraction complete: {len(markdown_text)} chars")

    est_pages = max(1, len(markdown_text) // 3000)
    pages = [PageContent(page_num=1, text=markdown_text)]
    sections = _split_sections(pages, section_policy=profile.section_policy if profile else None)
    for sec in sections:
        sec.sid = _section_sid(sec.title, sec.text)

    table_count = _count_markdown_tables(markdown_text)

    logger.info(f"Parse complete: {len(sections)} sections, ~{est_pages} pages")
    return ParsedDocument(
        filename=filepath.name,
        file_type=file_type,
        total_pages=est_pages,
        pages=pages,
        sections=sections,
        table_count=table_count,
        metadata={"document_profile": profile.name if profile else None},
    )


# ═══════════════════════════════════════════
# Section splitting
# ═══════════════════════════════════════════

HEADING_PATTERNS = [
    re.compile(r"^第[一二三四五六七八九十\d]+[章节部分篇]\s*[、:：]?\s*.+"),
    re.compile(r"^[（(]?[一二三四五六七八九十]+[）)]?[、.．]\s*.+"),
    re.compile(r"^\d{1,2}\s*[-－]\s*\d{1,2}\s*(?![-\d])\S.{1,}"),
    re.compile(r"^\d+(\.\d+)*[.、．)\s]\s*.{2,}"),
    re.compile(r"^(?=.{8,60}$)[A-Z][A-Za-z0-9/&()'-]*(?: [A-Z][A-Za-z0-9/&()'-]*){0,5}$"),
    re.compile(r"^[A-Z][A-Z\s]{5,}$"),
    re.compile(r"^(摘要|目录|引言|绪论|前言|导论|背景|概述|总结|结论|致谢|参考文献|附录|附件)$"),
]


def _looks_like_ocr_chrome_heading(text: str) -> bool:
    compact = re.sub(r"\s+", " ", text.strip()).upper()
    if compact in {
        "GF FUTURES",
        "GFF",
        "FUTURES",
        "TINGYUN.COM",
        "UTURE",
        "LF",
    }:
        return True
    if re.fullmatch(r"(?:GF\s*)?FUTURES?", compact):
        return True
    return False


def _looks_like_numeric_identifier_heading(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    if re.fullmatch(r"[0-9][0-9\s-]{5,}[0-9]", stripped):
        return True
    return False


def _looks_like_numeric_table_value(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    compact = re.sub(r"\s+", "", stripped)
    if re.match(r"^\d{4}年\d{1,2}月", compact):
        return True
    if re.match(r"^\d+(?:[,.，]\d+)*(?:\.\d+)?(?:元|月|年|日|个|%|％|探针)", compact):
        return True
    if re.match(r"^[￥¥]\d+(?:[,.，]\d+)*(?:\.\d+)?", compact):
        return True
    return False


def _numeric_heading_level(text: str) -> int:
    stripped = _strip_heading_markup(text)
    if re.match(r"^\d{1,2}(?:\.\d{1,2}){2,}(?:[.、．)）\s]|$)", stripped):
        return -1
    dotted = re.match(r"^(\d{1,2}\.\d{1,2})(?:[.、．)）\s]*)(.{2,})$", stripped)
    if dotted:
        title = dotted.group(2).strip()
        if len(title) > 48:
            return -1
        if len(title) > 12 and re.search(r"[，。；;]", title):
            return -1
        if title.endswith(("。", "；", ";")):
            return -1
        return 2
    top = re.match(r"^(\d{1,2})([.、．)）])\s*(.{2,})$", stripped)
    if top:
        delimiter = top.group(2)
        title = top.group(3).strip()
        if delimiter == "、" and len(title) > 12:
            return -1
        if len(title) > 24:
            return -1
        return 2 if delimiter == "、" else 1
    return 0


def _is_heading(text: str, *, ocr_mode: bool = False) -> int:
    text = _strip_heading_markup(text)
    if not text or len(text) > 100:
        return 0
    if _looks_like_numeric_identifier_heading(text):
        return 0
    if _looks_like_numeric_table_value(text):
        return 0
    if _looks_like_plain_table_row(text) or _looks_like_markdown_table_row(text):
        return 0
    numeric_level = _numeric_heading_level(text)
    if numeric_level < 0:
        return 0
    if numeric_level > 0:
        return numeric_level
    if ocr_mode:
        if _looks_like_ocr_chrome_heading(text):
            return 0
        # OCR output for scanned contracts often turns every numbered sub-clause
        # into a tiny section. Keep top-level clauses as boundaries and leave
        # nested clauses inside their parent section.
        if re.match(r"^\d+\.\d+(?:\.\d+)*[.、．)\s]?", text):
            return 0
    for i, pattern in enumerate(HEADING_PATTERNS):
        if pattern.match(text):
            return 1 if i < 2 else 2
    return 0


def _strip_heading_markup(text: str) -> str:
    stripped = text.strip()
    stripped = re.sub(r"^(?:#{1,6}\s*)", "", stripped)
    stripped = re.sub(r"^\*{1,3}(.+?)\*{1,3}$", r"\1", stripped)
    stripped = re.sub(r"^_{1,3}(.+?)_{1,3}$", r"\1", stripped)
    return stripped.strip()


def _toc_has_dense_same_page_entries(toc: list) -> bool:
    page_counts: dict[int, int] = {}
    for entry in toc:
        try:
            level, _title, page_num = entry
        except ValueError:
            continue
        if int(level) > 2:
            continue
        page_counts[int(page_num)] = page_counts.get(int(page_num), 0) + 1
    return any(count > 1 for count in page_counts.values())


def _normalize_heading_key(text: str) -> str:
    return re.sub(r"[\s.．、:：)）\-_]+", "", text).lower()


def _line_index_for_toc_title(lines: list[str], title: str, *, start_at: int = 0) -> int | None:
    wanted = _normalize_heading_key(title)
    if not wanted:
        return None
    for idx in range(max(start_at, 0), len(lines)):
        line_key = _normalize_heading_key(lines[idx])
        if not line_key:
            continue
        if line_key == wanted or line_key.startswith(wanted) or wanted.startswith(line_key):
            return idx
    return None


def _toc_chapter_prefix(title: str) -> str | None:
    match = re.match(r"^\s*(\d{1,2})(?:[.、．)]|\s)", title)
    return match.group(1) if match else None


def _toc_parent_for_child(child_title: str, parents: dict[str, str]) -> str | None:
    match = re.match(r"^\s*(\d{1,2})\.\d{1,2}", child_title)
    if not match:
        return None
    return parents.get(match.group(1))


def _prepare_toc_section_boundaries(
    toc: list, *, max_level: int = 2
) -> list[dict[str, Any]]:
    """Keep configured TOC boundaries and attach level-1 titles to their first child."""
    parents: dict[str, str] = {}
    boundaries: list[dict[str, Any]] = []
    seen: set[tuple[int, str]] = set()
    max_level = max(1, max_level)
    for entry in toc:
        try:
            level, title, page_num = entry
        except ValueError:
            continue
        level = int(level)
        page_num = int(page_num)
        title = str(title).strip()
        if not title:
            continue
        if level == 1 and max_level > 1:
            prefix = _toc_chapter_prefix(title)
            if prefix:
                parents[prefix] = title
            continue
        if level > max_level:
            continue
        key = (page_num, _normalize_heading_key(title))
        if key in seen:
            continue
        seen.add(key)
        boundaries.append(
            {
                "level": level,
                "title": title,
                "page": page_num,
                "parent": _toc_parent_for_child(title, parents),
            }
        )
    return boundaries


def _compact_toc_for_section_boundaries(toc: list, *, max_level: int = 2) -> list[list[Any]]:
    compact: list[list[Any]] = []
    seen_pages: set[int] = set()
    max_level = max(1, max_level)
    for idx, entry in enumerate(toc):
        try:
            level, title, page_num = entry
        except ValueError:
            continue
        level = int(level)
        page_num = int(page_num)
        if level > max_level:
            continue
        next_entry = toc[idx + 1] if idx + 1 < len(toc) else None
        if next_entry:
            try:
                next_level, _next_title, next_page = next_entry
            except ValueError:
                next_level, next_page = 0, -1
            if level == 1 and int(next_level) == 2 and int(next_page) == page_num:
                continue
        if page_num in seen_pages:
            continue
        seen_pages.add(page_num)
        compact.append([level, str(title), page_num])
    return compact


def _split_sections_from_toc(
    pages: list[PageContent],
    toc: list,
    section_policy: SectionPolicy | None = None,
) -> list[Section]:
    """Split sections using PDF TOC."""
    if not toc or not pages:
        return _split_sections(pages, section_policy=section_policy)
    policy = section_policy or SectionPolicy()
    boundaries = _prepare_toc_section_boundaries(toc, max_level=policy.toc_max_level)
    if len(boundaries) < 2:
        if _toc_has_dense_same_page_entries(toc):
            toc = _compact_toc_for_section_boundaries(toc, max_level=policy.toc_max_level)
            logger.info("PDF TOC compacted to %s page-level section boundaries", len(toc))
            if len(toc) < 2:
                return _split_sections(pages, section_policy=section_policy)
            boundaries = [
                {"level": int(level), "title": str(title), "page": int(page_num), "parent": None}
                for level, title, page_num in toc
            ]
        else:
            boundaries = [
                {"level": int(level), "title": str(title), "page": int(page_num), "parent": None}
                for level, title, page_num in toc
                if int(level) <= policy.toc_max_level
            ]
    if len(boundaries) < 2:
        return _split_sections(pages, section_policy=section_policy)

    page_texts: dict[int, str] = {}
    page_lines: dict[int, list[str]] = {}
    for p in pages:
        t = p.text
        if p.tables and not p.tables_in_text:
            t += "\n\n" + "\n\n".join(p.tables)
        page_texts[p.page_num] = t
        page_lines[p.page_num] = [line.strip() for line in t.splitlines() if line.strip()]

    max_page = max(p.page_num for p in pages)
    sections: list[Section] = []
    first_start_page = max(1, int(boundaries[0]["page"]))
    preface_parts = [
        page_texts[pn].strip()
        for pn in range(1, first_start_page)
        if page_texts.get(pn, "").strip()
    ]
    if preface_parts:
        title = "前言/目录" if _detect_text_locale("\n".join(preface_parts)) == "zh" else "Preface / TOC"
        sections.append(
            Section(
                index=1,
                title=title,
                level=1,
                text="\n\n".join(preface_parts).strip(),
                page_range=f"p.1-{first_start_page - 1}",
            )
        )

    for i, boundary in enumerate(boundaries):
        level = int(boundary["level"])
        title = str(boundary["title"])
        start_page = int(boundary["page"])
        next_boundary = boundaries[i + 1] if i + 1 < len(boundaries) else None
        next_page = int(next_boundary["page"]) if next_boundary else max_page + 1
        end_page = next_page - 1 if next_boundary else max_page
        end_page = max(end_page, start_page)
        text_parts: list[str] = []
        start_lines = page_lines.get(start_page, [])
        start_idx = _line_index_for_toc_title(start_lines, title) or 0
        if next_boundary and next_page == start_page:
            next_idx = _line_index_for_toc_title(
                start_lines, str(next_boundary["title"]), start_at=start_idx + 1
            )
            selected = start_lines[start_idx:next_idx] if next_idx is not None else start_lines[start_idx:]
            text_parts.append("\n".join(selected))
        else:
            if start_lines:
                text_parts.append("\n".join(start_lines[start_idx:]))
            for pn in range(start_page + 1, end_page + 1):
                if page_texts.get(pn, "").strip():
                    text_parts.append(page_texts[pn].strip())
        parent = boundary.get("parent")
        text = "\n\n".join(part.strip() for part in text_parts if part.strip()).strip()
        if parent and text and _normalize_heading_key(str(parent)) not in _normalize_heading_key(text[:200]):
            text = f"{parent}\n{text}"
        if not text:
            continue
        sections.append(
            Section(
                index=len(sections) + 1,
                title=title.strip(),
                level=min(level, 3),
                text=text,
                page_range=f"p.{start_page}-{end_page}",
            )
        )

    if len(sections) < 2:
        logger.warning("PDF TOC produced too few sections, falling back to regex split")
        return _split_sections(pages, section_policy=section_policy)
    return sections


def _renumber_sections(sections: list[Section]) -> list[Section]:
    for idx, sec in enumerate(sections, 1):
        sec.index = idx
    return sections


def _merge_short_ocr_sections(sections: list[Section], *, min_chars: int = 20) -> list[Section]:
    merged: list[Section] = []
    for sec in sections:
        if merged and len(sec.text.strip()) < min_chars:
            previous = merged[-1]
            parts = [previous.text.rstrip(), sec.title.strip(), sec.text.strip()]
            previous.text = "\n".join(part for part in parts if part).strip()
            start, _ = _page_bounds(previous.page_range)
            _, end = _page_bounds(sec.page_range)
            if start and end:
                previous.page_range = f"p.{start}-{end}"
            continue
        merged.append(sec)
    return _renumber_sections(merged)


def _merge_short_sections(sections: list[Section], *, min_chars: int = 80) -> list[Section]:
    if not sections:
        return sections
    short_count = sum(1 for sec in sections if len(sec.text.strip()) < min_chars)
    if short_count / max(len(sections), 1) < 0.35:
        return sections

    merged: list[Section] = []
    for sec in sections:
        if merged and sec.level > 1 and len(sec.text.strip()) < min_chars:
            previous = merged[-1]
            parts = [previous.text.rstrip(), sec.title.strip(), sec.text.strip()]
            previous.text = "\n".join(part for part in parts if part).strip()
            start, _ = _page_bounds(previous.page_range)
            _, end = _page_bounds(sec.page_range)
            if start and end:
                previous.page_range = f"p.{start}-{end}"
            continue
        merged.append(sec)
    return _renumber_sections(merged)


def _numeric_heading_prefix(text: str) -> str | None:
    stripped = text.strip()
    dotted = re.match(r"^(\d{1,2}(?:\.\d{1,2})*)", stripped)
    if dotted:
        return dotted.group(1)
    top = re.match(r"^(\d{1,2})[.、．)]", stripped)
    if top:
        return top.group(1)
    return None


def _promote_parent_sections_to_first_child(sections: list[Section]) -> list[Section]:
    for sec in sections:
        if _numeric_heading_level(sec.title) != 1:
            continue
        parent_prefix = _numeric_heading_prefix(sec.title)
        lines = [line for line in sec.text.splitlines() if line.strip()]
        if not lines:
            continue
        first_line = lines[0].strip()
        if _numeric_heading_level(first_line) != 2:
            continue
        child_prefix = _numeric_heading_prefix(first_line)
        if not parent_prefix or not child_prefix or child_prefix.split(".", 1)[0] != parent_prefix:
            continue
        sec.text = f"{sec.title}\n{sec.text}".strip()
        sec.title = first_line
        sec.level = 2
    return sections


def _split_leading_toc_lines(lines: list[str]) -> tuple[list[str], list[str]] | None:
    toc_idx = next(
        (
            idx
            for idx, line in enumerate(lines)
            if _normalize_heading_key(line) in {"目录", "目次"}
        ),
        None,
    )
    if toc_idx is None:
        return None
    first_heading_key: str | None = None
    first_heading_idx: int | None = None
    for idx in range(toc_idx + 1, len(lines)):
        line = lines[idx].strip()
        if _is_heading(line) <= 0:
            continue
        first_heading_key = _normalize_heading_key(_strip_heading_markup(line))
        first_heading_idx = idx
        break
    if not first_heading_key or first_heading_idx is None:
        return None
    for idx in range(first_heading_idx + 1, len(lines)):
        line = lines[idx].strip()
        if _normalize_heading_key(_strip_heading_markup(line)) == first_heading_key:
            return lines[:idx], lines[idx:]
    return None


def _prefers_formal_chinese_sectioning(
    pages: list[PageContent], *, min_headings: int = 4
) -> bool:
    formal_count = 0
    arabic_count = 0
    for page in pages[:5]:
        for raw_line in page.text.splitlines():
            line = _strip_heading_markup(raw_line)
            if not line or len(line) > 120:
                continue
            if re.match(r"^第[一二三四五六七八九十\d]+[章节部分篇]\s*[、:：]?\s*.+", line):
                formal_count += 1
            elif re.match(r"^[（(]?[一二三四五六七八九十]+[）)]?[、.．]\s*.+", line):
                formal_count += 1
            if re.match(r"^\d{1,2}(?:\.\d{1,2})?[.、．)）\s]\s*.{2,}", line):
                arabic_count += 1
    return formal_count >= min_headings and arabic_count >= formal_count


def _is_arabic_numbered_heading_candidate(text: str) -> bool:
    stripped = _strip_heading_markup(text)
    return bool(re.match(r"^\d{1,2}(?:\.\d{1,2})?[.、．)）\s]\s*.{2,}", stripped))


def _split_sections(
    pages: list[PageContent], section_policy: SectionPolicy | None = None
) -> list[Section]:
    sections: list[Section] = []
    policy = section_policy or SectionPolicy()
    split_locale = _detect_text_locale("\n".join(p.text[:1000] for p in pages[:3]))
    default_section_title = tmpl_for_locale(split_locale, "default_section_title")
    full_document_title = tmpl_for_locale(split_locale, "full_document_title")
    current_title = default_section_title
    current_level = 1
    current_lines: list[str] = []
    current_start_page = 1
    sec_index = 0
    if len(pages) == 1:
        original_lines = [line.strip() for line in pages[0].text.splitlines() if line.strip()]
        toc_split = _split_leading_toc_lines(original_lines)
        if toc_split:
            preface_lines, body_lines = toc_split
            sec_index = 1
            sections.append(
                Section(
                    index=sec_index,
                    title="前言/目录" if split_locale == "zh" else "Preface / TOC",
                    level=1,
                    text="\n".join(preface_lines).strip(),
                    page_range="p.1-1",
                )
            )
            pages = [PageContent(page_num=1, text="\n".join(body_lines))]
    ocr_mode = bool(pages) and (
        sum(1 for page in pages if page.is_ocr) / max(len(pages), 1)
    ) >= 0.8
    suppress_arabic_clause_headings = (
        not ocr_mode
        and policy.suppress_arabic_clause_headings_when_formal_chinese
        and _prefers_formal_chinese_sectioning(
            pages, min_headings=policy.formal_chinese_min_headings
        )
    )

    for page in pages:
        page_has_body = False
        page_tables_attached = False
        for line in page.text.split("\n"):
            line = line.strip()
            if not line:
                continue
            heading_level = _is_heading(line, ocr_mode=ocr_mode)
            if suppress_arabic_clause_headings and _is_arabic_numbered_heading_candidate(line):
                heading_level = 0
            heading_title = _strip_heading_markup(line)
            if heading_level > 0 and not current_lines and current_title == default_section_title:
                current_title = heading_title
                current_level = heading_level
                current_start_page = page.page_num
                continue
            if heading_level > 0 and current_lines:
                if page.tables and not page.tables_in_text and not page_tables_attached:
                    current_lines.extend(table.strip() for table in page.tables if table.strip())
                    page_tables_attached = True
                end_page = page.page_num if page_has_body else max(current_start_page, page.page_num - 1)
                sec_index += 1
                sections.append(
                    Section(
                        index=sec_index,
                        title=current_title,
                        level=current_level,
                        text="\n".join(current_lines),
                        page_range=f"p.{current_start_page}-{end_page}",
                    )
                )
                current_title = heading_title
                current_level = heading_level
                current_lines = []
                current_start_page = page.page_num
            else:
                current_lines.append(line)
                page_has_body = True
        if page.tables_in_text:
            continue
        if not page_tables_attached:
            for table in page.tables:
                value = table.strip()
                if value:
                    current_lines.append(value)
                    page_has_body = True

    if current_lines:
        sec_index += 1
        last_page = pages[-1].page_num if pages else 1
        sections.append(
            Section(
                index=sec_index,
                title=current_title,
                level=current_level,
                text="\n".join(current_lines),
                page_range=f"p.{current_start_page}-{last_page}",
            )
        )

    if len(sections) == 1 and len(pages) > 1 and sections[0].page_range != "p.1-1":
        page_sections: list[Section] = []
        for page in pages:
            text_parts = [page.text.strip()] if page.text.strip() else []
            if page.tables and not page.tables_in_text:
                text_parts.extend(table.strip() for table in page.tables if table.strip())
            page_text = "\n\n".join(text_parts).strip()
            if not page_text:
                continue
            page_sections.append(
                Section(
                    index=len(page_sections) + 1,
                    title=f"Page {page.page_num}",
                    level=1,
                    text=page_text,
                    page_range=f"p.{page.page_num}-{page.page_num}",
                )
            )
        if page_sections:
            return page_sections

    if not sections:
        full_text = "\n\n".join(p.text for p in pages)
        sections.append(
            Section(
                index=1,
                title=full_document_title,
                level=1,
                text=full_text,
                page_range=f"p.1-{pages[-1].page_num if pages else 1}",
            )
        )
    if ocr_mode:
        sections = _merge_short_ocr_sections(sections)
    else:
        sections = _merge_short_sections(sections)
    return _renumber_sections(_promote_parent_sections_to_first_child(sections))


# ═══════════════════════════════════════════
# Summary generation
# ═══════════════════════════════════════════

SUMMARY_MAX_CHARS = 500


def _summary_failed_text(text: str | None) -> bool:
    if not text:
        return True
    compact = text.strip().lower()
    return compact in {
        "[summary generation failed]",
        "summary generation failed",
    }


def _local_section_preview(sec: Section, limit: int = SUMMARY_MAX_CHARS) -> str:
    text = re.sub(r"\s+", " ", sec.text).strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def _sections_overview_from_text(sections: list[Section]) -> str:
    parts: list[str] = []
    total_chars = 0
    for sec in sections:
        excerpt = _local_section_preview(sec, SUMMARY_BRIEF_SECTION_EXCERPT_CHARS)
        part = f"## {sec.title} ({sec.page_range})\n{excerpt}".strip()
        if not part:
            continue
        if total_chars + len(part) > SUMMARY_BRIEF_MAX_INPUT_CHARS and parts:
            parts.append(
                f"\n[Truncated after {len(parts)} sections due to summary input budget]"
            )
            break
        parts.append(part)
        total_chars += len(part)
    return "\n\n".join(parts)


def _sections_overview_for_brief(sections: list[Section]) -> str:
    if len(sections) > 60:
        overview = _compress_sections_for_brief(sections)
    else:
        overview = "\n\n".join(
            f"## {sec.title} ({sec.page_range})\n{sec.summary[:SUMMARY_MAX_CHARS]}"
            for sec in sections
            if sec.summary and not _summary_failed_text(sec.summary)
        )
    if overview.strip():
        return overview
    return _sections_overview_from_text(sections)


def _should_skip_section_summaries(parsed: ParsedDocument) -> bool:
    return len(parsed.sections) > SUMMARY_SECTION_DETAIL_LIMIT


def generate_summaries(
    parsed: ParsedDocument, concurrency: int = 3, allow_single_fallback: bool = True
) -> tuple[str, str, list[Section]]:
    logger.info("Generating summaries...")
    summary_locale = _parsed_document_locale(parsed)

    if _should_skip_section_summaries(parsed):
        logger.info(
            "Skipping per-section summaries for long document: %s sections > limit %s",
            len(parsed.sections),
            SUMMARY_SECTION_DETAIL_LIMIT,
        )
        sections_overview = _sections_overview_from_text(parsed.sections)
    else:
        # Dynamic batching by token estimate
        BATCH_TOKEN_LIMIT = 10000
        batches: list[list[Section]] = []
        current_batch: list[Section] = []
        current_tokens = 0

        for sec in parsed.sections:
            sec_tokens = _estimate_tokens(sec.text) + _estimate_tokens(sec.title) + 20
            if current_tokens + sec_tokens > BATCH_TOKEN_LIMIT and current_batch:
                batches.append(current_batch)
                current_batch = []
                current_tokens = 0
            current_batch.append(sec)
            current_tokens += sec_tokens
        if current_batch:
            batches.append(current_batch)

        summary_workers = min(max(1, concurrency), SUMMARY_BATCH_CONCURRENCY, len(batches))
        if len(batches) > 1 and summary_workers > 1:
            logger.info(f"{len(batches)} batches, {summary_workers} summary workers")
            with ThreadPoolExecutor(max_workers=summary_workers) as pool:
                futures = {
                    pool.submit(_summarize_batch, batch, allow_single_fallback, summary_locale): batch
                    for batch in batches
                }
                for fut in as_completed(futures):
                    fut.result()
        else:
            for batch in batches:
                _summarize_batch(batch, allow_single_fallback, summary_locale)

        logger.info(f"{len(parsed.sections)} section summaries complete")
        sections_overview = _sections_overview_for_brief(parsed.sections)

    brief = gemini_summarize(
        f"Document: {parsed.filename}\nTotal pages: {parsed.total_pages}\n\n{sections_overview}",
        prompt_for_locale(summary_locale, "brief"),
    )
    if _summary_failed_text(brief):
        raise RuntimeError("upstream brief generation failed")
    logger.info("Brief generation complete")

    digest = gemini_summarize(
        f"Document: {parsed.filename}\n\nBriefing:\n{brief}",
        prompt_for_locale(summary_locale, "digest"),
    )
    if _summary_failed_text(digest):
        raise RuntimeError("upstream digest generation failed")
    logger.info("Digest generation complete")

    return digest, brief, parsed.sections


def _summarize_batch(
    sections: list[Section], allow_single_fallback: bool = True, summary_locale: str = "en"
):
    """Batch summarize with JSON output + single fallback."""
    n = len(sections)

    if n == 1:
        sec = sections[0]
        sec.summary = gemini_summarize(
            f"## {sec.title} ({sec.page_range})\n\n{sec.text}",
            prompt_for_locale(summary_locale, "section_summary"),
        )
        logger.info(f"Section {sec.index}: {sec.title[:30]}... done")
        return

    batch_text = ""
    for sec in sections:
        batch_text += f"\n\n## Section {sec.index}: {sec.title} ({sec.page_range})\n\n{sec.text}"

    result = gemini_summarize(batch_text, prompt_for_locale(summary_locale, "batch_summary", n=n))
    if _summary_failed_text(result):
        raise RuntimeError("upstream summary generation failed")

    # JSON parse
    parsed_ok = False
    try:
        clean = result.strip()
        if clean.startswith("```"):
            clean = re.sub(r"^```(?:json)?\s*", "", clean)
            clean = re.sub(r"\s*```$", "", clean)
        items = json.loads(clean)
        if isinstance(items, list) and len(items) >= n:
            for sec in sections:
                match = next((it for it in items if it.get("index") == sec.index), None)
                if match and match.get("summary"):
                    sec.summary = match["summary"]
                else:
                    sec.summary = t("summary_missing")
            parsed_ok = True
    except (json.JSONDecodeError, KeyError, TypeError):
        pass

    if parsed_ok:
        for sec in sections:
            logger.info(f"Section {sec.index}: {sec.title[:30]}... done")
        return

    # Fallback
    if not allow_single_fallback:
        logger.warning(
            "Batch JSON parse failed for %d sections; using local section previews",
            n,
        )
        for sec in sections:
            sec.summary = _local_section_preview(sec)
        return

    logger.warning(f"Batch JSON parse failed, falling back to single ({n} items)")
    for sec in sections:
        sec.summary = gemini_summarize(
            f"## {sec.title} ({sec.page_range})\n\n{sec.text}",
            prompt_for_locale(summary_locale, "section_summary"),
        )
        logger.info(f"Section {sec.index}: {sec.title[:30]}... done (single)")


def _compress_sections_for_brief(sections: list[Section]) -> str:
    groups = []
    for i in range(0, len(sections), 10):
        group = sections[i : i + 10]
        group_text = "; ".join(f"{s.title}: {s.summary[:150]}" for s in group if s.summary)
        groups.append(f"**Sections {group[0].index}-{group[-1].index}**: {group_text}")
    return "\n\n".join(groups)


# ═══════════════════════════════════════════
# Output file writing
# ═══════════════════════════════════════════


def _reset_generated_output_dirs(doc_dir: Path) -> None:
    for child in ("sections", "tables", "images"):
        path = doc_dir / child
        if path.exists():
            shutil.rmtree(path)
    for child in ("sections.json", "tables.json", "images.json", OCR_BLOCKS_SIDECAR_PATH):
        path = doc_dir / child
        if path.exists():
            path.unlink()


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
):
    normalized_content_type = _normalize_content_type(content_type) if content_type else None
    storage_path = _doc_storage_rel_path(doc_id, normalized_content_type)
    doc_dir = output_dir / storage_path
    sections_dir = doc_dir / "sections"
    doc_dir.mkdir(parents=True, exist_ok=True)
    _reset_generated_output_dirs(doc_dir)
    sections_dir.mkdir(exist_ok=True)
    output_locale = _parsed_document_locale(parsed)

    meta = {
        "doc_id": doc_id,
        "filename": parsed.filename,
        "file_type": parsed.file_type,
        "total_pages": parsed.total_pages,
        "section_count": len(parsed.sections),
        "ocr_page_count": parsed.ocr_page_count,
        "table_count": parsed.table_count,
        "image_count": len(parsed.images),
        "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "metadata": metadata or {},
        "parse_metadata": parsed.metadata or {},
        "source_file": source_record or {},
        "content_type": normalized_content_type or "General",
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

    table_entries = _write_tables(doc_dir, parsed)
    if table_entries:
        _write_json(doc_dir / "tables.json", table_entries)
        logger.info(f"tables/ ({len(table_entries)} files)")
    image_entries = _write_images(doc_dir, parsed)
    if image_entries:
        _write_json(doc_dir / "images.json", image_entries)
        logger.info(f"images/ ({len(image_entries)} files)")
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
        "total_pages": parsed.total_pages,
        "section_count": len(parsed.sections),
        "table_count": parsed.table_count,
        "image_count": len(parsed.images),
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
):
    normalized_content_type = _normalize_content_type(content_type) if content_type else None
    storage_path = _doc_storage_rel_path(doc_id, normalized_content_type)
    doc_dir = output_dir / storage_path
    sections_dir = doc_dir / "sections"
    doc_dir.mkdir(parents=True, exist_ok=True)
    _reset_generated_output_dirs(doc_dir)
    sections_dir.mkdir(exist_ok=True)
    output_locale = _parsed_document_locale(parsed)

    meta = {
        "doc_id": doc_id,
        "filename": parsed.filename,
        "file_type": parsed.file_type,
        "total_pages": parsed.total_pages,
        "section_count": len(parsed.sections),
        "ocr_page_count": parsed.ocr_page_count,
        "table_count": parsed.table_count,
        "image_count": len(parsed.images),
        "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "metadata": metadata or {},
        "parse_metadata": parsed.metadata or {},
        "source_file": source_record or {},
        "content_type": normalized_content_type or "General",
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
        "total_pages": parsed.total_pages,
        "section_count": len(parsed.sections),
        "table_count": parsed.table_count,
        "image_count": len(parsed.images),
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
# Document index
# ═══════════════════════════════════════════


def _update_doc_index(
    docs_dir: Path,
    meta: dict,
    digest: str,
    tags: list[str] | None = None,
    source: str = "upload",
    source_url: str | None = None,
    content_hash: str | None = None,
    metadata: dict[str, Any] | None = None,
    source_record: dict[str, Any] | None = None,
    content_type: str | None = None,
    storage_path: str | None = None,
):
    """Update doc-index.json with threading lock and atomic write."""
    with _doc_index_lock:
        index_path = docs_dir / "doc-index.json"
        if index_path.exists():
            try:
                with open(index_path, encoding="utf-8") as f:
                    index = json.load(f)
            except (json.JSONDecodeError, Exception):
                index = {"version": 2, "documents": []}
        else:
            index = {"version": 2, "documents": []}

        index["version"] = 2
        if not isinstance(index.get("documents"), list):
            index["documents"] = []
        index["documents"] = [d for d in index["documents"] if d.get("id") != meta["doc_id"]]
        normalized_content_type = _normalize_content_type(
            content_type or meta.get("content_type") or "General"
        )
        rel_storage_path = storage_path or meta.get("storage_path") or _doc_storage_rel_path(
            meta["doc_id"],
            normalized_content_type if content_type or meta.get("storage_path") else None,
        )

        entry: dict[str, Any] = {
            "id": meta["doc_id"],
            "filename": meta["filename"],
            "file_type": meta["file_type"],
            "content_type": normalized_content_type,
            "storage_path": rel_storage_path,
            "source": source,
            "source_url": source_url or "",
            "pages": meta["total_pages"],
            "sections": meta["section_count"],
            "ocr_pages": meta.get("ocr_page_count", 0),
            "tables": meta.get("table_count", 0),
            "digest": digest[:200],
            "digest_path": f"docs/{rel_storage_path}/digest.md",
            "tags": tags or [],
            "created_at": meta["created_at"],
            "content_hash": content_hash or "",
            "metadata": _indexable_metadata(metadata or meta.get("metadata") or {}),
            "source_ref": (source_record or meta.get("source_file") or {}).get("ref", ""),
            "source_filename": (source_record or meta.get("source_file") or {}).get("filename", ""),
            "source_sha256": (source_record or meta.get("source_file") or {}).get("sha256", ""),
            "source_available": bool((source_record or meta.get("source_file") or {}).get("ref")),
        }
        summary_meta = (
            meta.get("parse_metadata", {}).get("summary")
            if isinstance(meta.get("parse_metadata"), dict)
            else {}
        )
        if isinstance(summary_meta, dict):
            entry["summary_mode"] = summary_meta.get("mode")
            entry["summary_status"] = summary_meta.get("status")
            entry["summary_error_code"] = summary_meta.get("error_code")

        index["documents"].append(entry)
        index["last_updated"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        _write_json(index_path, index)


# ═══════════════════════════════════════════
# Utility functions
# ═══════════════════════════════════════════


def _write_text(path: Path, content: str):
    """Write text atomically via temp file + os.replace."""
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, path)


def _write_json(path: Path, data: dict):
    """Write JSON atomically via temp file + os.replace."""
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _normalize_layout_bbox(bbox: tuple[float, float, float, float] | list[float]) -> list[float]:
    """Normalize a bbox to [x0, y0, x1, y1] floats and reject malformed geometry."""
    if len(bbox) != 4:
        raise ValueError("layout bbox must contain exactly four coordinates")
    normalized = [float(v) for v in bbox]
    x0, y0, x1, y1 = normalized
    if x1 < x0 or y1 < y0:
        raise ValueError("layout bbox must be ordered as [x0, y0, x1, y1]")
    return normalized


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


def _write_bytes(path: Path, content: bytes):
    """Write bytes atomically via temp file + os.replace."""
    tmp = path.with_suffix(".tmp")
    with open(tmp, "wb") as f:
        f.write(content)
    os.replace(tmp, path)


def _safe_filename(title: str, max_len: int = 40) -> str:
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", title)
    safe = safe.strip().replace(" ", "-")
    return (safe[:max_len] if len(safe) > max_len else safe) or "untitled"


_doc_counter_lock = threading.Lock()
_doc_index_lock = threading.Lock()


def _next_doc_id(docs_dir: Path) -> str:
    with _doc_counter_lock:
        counter_path = docs_dir / ".counter"
        if counter_path.exists():
            try:
                counter = int(counter_path.read_text(encoding="utf-8").strip())
            except ValueError:
                counter = 1
        else:
            counter = 1
        doc_id = f"DOC-{counter:03d}"
        counter_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = counter_path.with_suffix(".tmp")
        tmp.write_text(str(counter + 1), encoding="utf-8")
        os.replace(tmp, counter_path)
        return doc_id


# ═══════════════════════════════════════════
# HTTP API（FastAPI）
# ═══════════════════════════════════════════

DEFAULT_DOCS_DIR = Path(
    os.environ.get(
        "LARKSCOUT_DOCS_DIR",
        os.path.expanduser("~/.larkscout/docs"),
    )
)

MAX_UPLOAD_BYTES = int(os.environ.get("LARKSCOUT_MAX_UPLOAD_MB", "200")) * 1024 * 1024
STORE_SOURCE_FILES = os.environ.get("LARKSCOUT_STORE_SOURCE_FILES", "true").lower() not in {
    "0",
    "false",
    "no",
}

_DOC_ID_RE = re.compile(r"^(?=.{1,80}$)(?=.*\d)[A-Za-z0-9](?:[A-Za-z0-9-]{0,78}[A-Za-z0-9])?$")
_TABLE_ID_RE = re.compile(r"^(table-)?\d+$")
_IMAGE_ID_RE = re.compile(r"^(IMG-)?\d{1,6}$", re.IGNORECASE)
CONTENT_TYPE_DIRS = ("General", "Contract", "Bid", "Knowledge")
_CONTENT_TYPE_ALIASES = {name.lower(): name for name in CONTENT_TYPE_DIRS}


def _validate_doc_id(doc_id: str) -> None:
    """Reject doc_id values that could cause path traversal."""
    if not _DOC_ID_RE.match(doc_id):
        raise HTTPException(400, f"invalid doc_id: {doc_id!r}")


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


def _get_docs_dir() -> Path:
    d = DEFAULT_DOCS_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _normalize_content_type(value: str | None) -> str:
    raw = (value or "General").strip()
    normalized = _CONTENT_TYPE_ALIASES.get(raw.lower())
    if not normalized:
        allowed = ", ".join(CONTENT_TYPE_DIRS)
        raise HTTPException(422, f"content_type must be one of: {allowed}")
    return normalized


def _doc_storage_rel_path(doc_id: str, content_type: str | None = None) -> str:
    if content_type is None:
        return doc_id
    return f"{_normalize_content_type(content_type)}/{doc_id}"


def _doc_storage_dir(docs_dir: Path, doc_id: str, content_type: str | None = None) -> Path:
    return docs_dir / _doc_storage_rel_path(doc_id, content_type)


def _resolve_index_storage_path(docs_dir: Path, storage_path: Any) -> Path | None:
    if not isinstance(storage_path, str) or not storage_path.strip():
        return None
    raw_path = Path(storage_path)
    if raw_path.is_absolute() or ".." in raw_path.parts:
        return None
    candidate = (docs_dir / raw_path).resolve()
    try:
        candidate.relative_to(docs_dir.resolve())
    except ValueError:
        return None
    return candidate


def _find_doc_index_entry(docs_dir: Path, doc_id: str) -> dict[str, Any] | None:
    for entry in _load_doc_index(docs_dir):
        if entry.get("id") == doc_id:
            return entry
    return None


def _resolve_doc_dir(docs_dir: Path, doc_id: str) -> Path:
    _validate_doc_id(doc_id)
    entry = _find_doc_index_entry(docs_dir, doc_id)
    if entry:
        indexed_path = _resolve_index_storage_path(docs_dir, entry.get("storage_path"))
        if indexed_path and (indexed_path / "manifest.json").exists():
            return indexed_path
        indexed_type = entry.get("content_type")
        if isinstance(indexed_type, str):
            typed_path = _doc_storage_dir(docs_dir, doc_id, indexed_type)
            if (typed_path / "manifest.json").exists():
                return typed_path

    for content_type in CONTENT_TYPE_DIRS:
        typed_path = _doc_storage_dir(docs_dir, doc_id, content_type)
        if (typed_path / "manifest.json").exists():
            return typed_path

    legacy_path = docs_dir / doc_id
    if (legacy_path / "manifest.json").exists():
        return legacy_path
    raise HTTPException(404, t("doc_not_found", doc_id=doc_id))


def _doc_exists_anywhere(docs_dir: Path, doc_id: str) -> bool:
    try:
        _resolve_doc_dir(docs_dir, doc_id)
        return True
    except HTTPException as exc:
        if exc.status_code == 404:
            return False
        raise


def _doc_content_type(docs_dir: Path, doc_id: str) -> str:
    # Derive from the on-disk doc directory first — that's the authoritative
    # location. doc-index.json may carry a stale content_type that points at a
    # directory that no longer holds the manifest, and trusting it would let
    # `replace=true` write the new artifacts under the wrong category dir
    # (orphaning the real files).
    try:
        doc_dir = _resolve_doc_dir(docs_dir, doc_id)
    except HTTPException:
        return "General"
    try:
        rel_parts = doc_dir.relative_to(docs_dir).parts
    except ValueError:
        rel_parts = ()
    if len(rel_parts) >= 2 and rel_parts[0] in CONTENT_TYPE_DIRS:
        return rel_parts[0]
    # Legacy flat layout (or unrecognized prefix): consult the manifest, then
    # the index, then default to General.
    manifest_path = doc_dir / "manifest.json"
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(manifest, dict) and isinstance(manifest.get("content_type"), str):
                return _normalize_content_type(manifest.get("content_type"))
        except Exception:
            pass
    entry = _find_doc_index_entry(docs_dir, doc_id)
    if entry and isinstance(entry.get("content_type"), str):
        try:
            return _normalize_content_type(entry.get("content_type"))
        except HTTPException:
            pass
    return "General"


def _doc_id_strategy(requested_strategy: str | None = None) -> str:
    strategy = (requested_strategy or os.environ.get("LARKSCOUT_DOC_ID_STRATEGY", "counter")).strip().lower()
    return strategy if strategy in {"counter", "source_filename"} else "counter"


def _sanitize_doc_id_candidate(value: str, max_len: int = 80) -> str:
    base = Path(value).name.strip()
    stem = Path(base).stem if Path(base).suffix else base
    normalized = re.sub(r"[\s._]+", "-", stem)
    sanitized = re.sub(r"[^A-Za-z0-9-]+", "", normalized)
    sanitized = re.sub(r"-{2,}", "-", sanitized).strip("-")
    return sanitized[:max_len]


def _next_filename_doc_id(docs_dir: Path, filename: str) -> str | None:
    base = _sanitize_doc_id_candidate(filename)
    if not base:
        return None
    candidate = base
    suffix = 2
    # `candidate in _doc_id_parse_locks` filters out ids that another concurrent
    # parse has reserved but not yet written a manifest for, preventing two
    # same-filename uploads from racing past this check and both choosing the
    # same id. Caller must hold `_doc_id_parse_locks_guard` so the check + the
    # subsequent insert in the dict are atomic.
    while _doc_exists_anywhere(docs_dir, candidate) or candidate in _doc_id_parse_locks:
        # Reserve room for "-<suffix>" inside the 80-char limit so we always
        # produce a candidate distinct from `base`. Without this, an 80-char
        # `base` that's already reserved would loop forever — `f"{base}-2"[:80]`
        # is just `base`, leaving the candidate unchanged.
        suffix_str = f"-{suffix}"
        head_len = max(1, 80 - len(suffix_str))
        next_candidate = (base[:head_len] + suffix_str).rstrip("-")
        if not next_candidate or next_candidate == candidate:
            return None
        candidate = next_candidate
        suffix += 1
        if suffix > 10000:
            return None
    return candidate if _DOC_ID_RE.match(candidate) else None


def _resolve_doc_id(
    docs_dir: Path,
    filename: str,
    requested_doc_id: str | None,
    requested_strategy: str | None = None,
) -> str:
    if requested_doc_id:
        _validate_doc_id(requested_doc_id)
        return requested_doc_id

    if _doc_id_strategy(requested_strategy) == "source_filename":
        filename_doc_id = _next_filename_doc_id(docs_dir, filename)
        if filename_doc_id:
            return filename_doc_id

    return _next_doc_id(docs_dir)


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


# ---- FastAPI app ----

app = FastAPI(title="Doc Reader API", version="3.0.0")
PREWARM_LOCAL_OCR = os.environ.get("LARKSCOUT_PREWARM_LOCAL_OCR", "true").strip().lower() not in {
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


def _indexable_metadata(value: dict[str, Any]) -> dict[str, Any]:
    """Keep only shallow scalar metadata in doc-index for cheap filtering."""
    out: dict[str, Any] = {}
    for key, raw in value.items():
        if not isinstance(key, str):
            continue
        if isinstance(raw, (str, int, float, bool)) or raw is None:
            out[key] = raw
        elif isinstance(raw, list) and all(
            isinstance(item, (str, int, float, bool)) or item is None for item in raw
        ):
            out[key] = raw[:20]
    return out


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


def _build_table_entries(parsed: ParsedDocument) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for i, (page_num, table_md) in enumerate(
        ((p.page_num, table) for p in parsed.pages for table in p.tables),
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
                "source": "ocr",
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
    safe_stem = _safe_filename(stem, max_len=80)
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

    dpi = int(dpi)
    if dpi < 36 or dpi > 600:
        raise HTTPException(422, "dpi must be between 36 and 600")
    source_path, _manifest, source_file = _resolve_doc_source_file(docs_dir, doc_id)
    doc_dir = _resolve_doc_dir(docs_dir, doc_id)
    ocr_pages = _load_ocr_debug_overlays(doc_dir) if include_ocr_blocks else {}
    table_pages = _load_table_debug_overlays(doc_dir) if include_tables else {}
    pages_to_render = sorted(set(ocr_pages) | set(table_pages))

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


def _load_doc_index(docs_dir: Path) -> list[dict[str, Any]]:
    index_path = docs_dir / "doc-index.json"
    if not index_path.exists():
        return []
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    documents = index.get("documents", [])
    return documents if isinstance(documents, list) else []


def _doc_entry_from_manifest(docs_dir: Path, doc_id: str) -> dict[str, Any] | None:
    try:
        doc_dir = _resolve_doc_dir(docs_dir, doc_id)
    except HTTPException:
        return None
    manifest_path = doc_dir / "manifest.json"
    if not manifest_path.exists():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(manifest, dict):
        return None

    meta: dict[str, Any] = {}
    meta_path = doc_dir / ".meta.json"
    if meta_path.exists():
        try:
            raw_meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if isinstance(raw_meta, dict):
                meta = raw_meta
        except Exception:
            meta = {}

    source_file = manifest.get("source_file") or meta.get("source_file") or {}
    provenance = manifest.get("provenance") or {}
    content_type = _normalize_content_type(manifest.get("content_type") or meta.get("content_type") or "General")
    storage_path = str(manifest.get("storage_path") or meta.get("storage_path") or doc_dir.relative_to(docs_dir))
    sections = manifest.get("sections") if isinstance(manifest.get("sections"), list) else []
    images = manifest.get("images") if isinstance(manifest.get("images"), list) else []
    parse_metadata = manifest.get("parse_metadata") if isinstance(manifest.get("parse_metadata"), dict) else {}
    summary_meta = parse_metadata.get("summary") if isinstance(parse_metadata.get("summary"), dict) else {}
    digest = ""
    digest_path = doc_dir / "digest.md"
    if digest_path.exists():
        try:
            digest = digest_path.read_text(encoding="utf-8")[:200]
        except Exception:
            digest = ""

    return {
        "id": doc_id,
        "filename": manifest.get("filename") or meta.get("filename") or "",
        "file_type": manifest.get("file_type") or meta.get("file_type") or "",
        "content_type": content_type,
        "storage_path": storage_path,
        "source": manifest.get("source") or provenance.get("source") or "upload",
        "source_url": provenance.get("source_url") or "",
        "pages": meta.get("total_pages", 0),
        "sections": len(sections),
        "ocr_pages": meta.get("ocr_page_count", 0),
        "tables": meta.get("table_count", 0),
        "images": len(images) if images else meta.get("image_count", 0),
        "digest": digest,
        "digest_path": f"docs/{storage_path}/digest.md",
        "tags": meta.get("tags", []),
        "created_at": provenance.get("created_at") or meta.get("created_at"),
        "content_hash": provenance.get("content_hash") or "",
        "metadata": _indexable_metadata(manifest.get("metadata") or meta.get("metadata") or {}),
        "source_ref": source_file.get("ref", ""),
        "source_filename": source_file.get("filename", ""),
        "source_sha256": source_file.get("sha256", ""),
        "source_available": bool(source_file.get("ref")),
        "summary_mode": summary_meta.get("mode"),
        "summary_status": summary_meta.get("status"),
        "summary_error_code": summary_meta.get("error_code"),
    }


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


def _load_doc_tags(docs_dir: Path, doc_id: str) -> list[str]:
    for entry in _load_doc_index(docs_dir):
        if entry.get("id") == doc_id:
            tags = entry.get("tags")
            if isinstance(tags, list):
                return [str(tag) for tag in tags]
            return []
    return []


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


def _mask_path(p: str | Path) -> str:
    """Replace home directory prefix with ~ to avoid exposing absolute paths."""
    s = str(p)
    home = os.path.expanduser("~")
    return s.replace(home, "~") if s.startswith(home) else s


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
            suffix=suffix, prefix="larkscout-upload-", dir=str(scratch_dir)
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
            # `larkscout-upload-*` files from rejected requests.
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
            requested_parse_mode = (
                str(parse_mode or parsed_metadata.get("parse_mode") or "").strip()
                or os.environ.get("LARKSCOUT_PDF_PARSE_MODE", "").strip()
                or None
            )
            field_ocr_profile = (
                str(document_profile or parsed_metadata.get("document_profile") or "").strip()
                or str(parsed_metadata.get("field_ocr_profile") or "").strip()
                or os.environ.get("LARKSCOUT_FIELD_OCR_PROFILE", "").strip()
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
                or os.environ.get("LARKSCOUT_FIELD_OCR_CONFIG", "").strip()
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
                "image_ocr_backend": image_ocr_backend if extract_images else "",
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
                tmp_path = tmp_dir / filename
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
                    word_path = _convert_legacy_office(tmp_path, "docx") if suffix == ".doc" else tmp_path
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
                    _persist_source_file(doc_storage_dir, filename, tmp_path)
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
        documents = [d for d, _ in scored[:limit]]
        scores = {d.get("id"): s for d, s in scored[:limit]}
    else:
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
    return SearchResponse(results=results, total=len(results))


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

    _set_summary_metadata(parsed, mode="defer", status="pending", attempts=attempts)
    write_output_extract_only(
        doc_id,
        parsed,
        docs_dir,
        tags=tags,
        source="upload",
        metadata=metadata,
        source_record=source_record,
        content_type=content_type,
        summary_placeholder=_summary_placeholder_text(
            "pending", locale=_parsed_document_locale(parsed)
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
    sections_dir = _resolve_doc_dir(_get_docs_dir(), doc_id) / "sections"
    if not sections_dir.exists():
        raise HTTPException(404, t("doc_not_found", doc_id=doc_id))

    # sid is in filename: 01-{sid}-{title}.md
    for f in sections_dir.iterdir():
        if f.is_file() and sid in f.name:
            return {"doc_id": doc_id, "sid": sid, "content": f.read_text(encoding="utf-8")}

    raise HTTPException(404, t("section_not_found", sid=sid))


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
    logger.info(f"LarkScout DocReader API v3.0 starting: {host}:{port}")
    logger.info(f"Docs directory: {DEFAULT_DOCS_DIR}")

    uvicorn.run(app, host=host, port=port)
