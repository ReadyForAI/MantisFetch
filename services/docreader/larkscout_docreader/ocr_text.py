"""OCR / PDF text post-processing: noise cleanup, table detection and extraction.

After a page's text is obtained (native PDF text or OCR output), this module
turns it into clean body text plus structured tables:

- noise cleanup — drop page-number footers, bracket artifacts, fix common OCR
  mis-reads (`_cleanup_ocr_text`, `_remove_footer_page_number`, …),
- table detection — markdown-pipe rows and plain Chinese tabular rows
  (`_looks_like_markdown_table_row`, `_looks_like_plain_table_row`, …),
- table extraction — pull tables out of OCR text (`_extract_tables_from_ocr_text`)
  or off a native PDF page via PyMuPDF (`_extract_pdf_page_tables`,
  `_strip_text_in_table_bboxes`),
- whole-document normalization (`_normalize_document_text`).

`_cleanup_extracted_text_noise` / `_normalize_amount_phrases` come from the
text_utils leaf. `_source_filename_contract_no` (profiles) and `_is_heading`
(sectioning) are pulled in via function-level relative imports to break the
import cycle — the facade imports this module early and those modules call
back into helpers defined here.
"""

from __future__ import annotations

import logging
import math
import re
from typing import Any

from .models import PageContent
from .text_utils import _cleanup_extracted_text_noise, _normalize_amount_phrases

logger = logging.getLogger("larkscout_docreader")

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


# Running header/footer stripping: only act on multi-page docs, only inspect the
# top/bottom few lines of each page, and only drop a line when it repeats across
# at least half the pages — so body text is never touched.
_HF_MIN_PAGES = 4
_HF_EDGE_LINES = 2
_HF_RATIO = 0.5


def _norm_edge_line(line: str, page_num: int, total_pages: int) -> str:
    """Normalise an edge line for cross-page comparison.

    Whitespace is dropped. A digits-only line collapses to one sentinel ONLY
    when its value tracks the page index (a real page number, with a small
    offset tolerated for cover / front-matter pages). A standalone numeric
    heading or list marker like ``1`` / ``2`` whose value does not match the
    page index stays distinct, so body numbering is never mistaken for a
    running header/footer. Alphanumeric body lines (``item1`` / ``item2``)
    also stay distinct.
    """
    compact = re.sub(r"\s+", "", line)
    if compact.isdigit():
        value = int(compact)
        if 1 <= value <= total_pages and abs(value - page_num) <= 2:
            return "\x00page-number"
    return compact


def _strip_repeated_headers_footers(page_texts: dict[int, str], total_pages: int) -> dict[int, str]:
    """Drop running headers/footers that repeat across most pages.

    Native PDF text keeps the running header/footer on every page (e.g. a
    company-name banner, or ``<title> - page N``). ``_cleanup_ocr_text``'s
    per-page rules miss these because they are identical prose, not a
    page-number pattern. Here we detect lines that recur at the top or bottom
    edge of a majority of pages (after normalising digits, so page numbers
    collapse) and remove only those edge lines, leaving body text intact.
    """
    if total_pages < _HF_MIN_PAGES:
        return page_texts

    top_counts: dict[str, int] = {}
    bottom_counts: dict[str, int] = {}
    for pn in range(1, total_pages + 1):
        nonblank = [ln for ln in (x.strip() for x in page_texts.get(pn, "").split("\n")) if ln]
        for ln in nonblank[:_HF_EDGE_LINES]:
            key = _norm_edge_line(ln, pn, total_pages)
            top_counts[key] = top_counts.get(key, 0) + 1
        for ln in nonblank[-_HF_EDGE_LINES:]:
            key = _norm_edge_line(ln, pn, total_pages)
            bottom_counts[key] = bottom_counts.get(key, 0) + 1

    threshold = max(2, math.ceil(total_pages * _HF_RATIO))
    top_templates = {k for k, c in top_counts.items() if k and c >= threshold}
    bottom_templates = {k for k, c in bottom_counts.items() if k and c >= threshold}
    if not top_templates and not bottom_templates:
        return page_texts

    result: dict[int, str] = dict(page_texts)
    for pn in range(1, total_pages + 1):
        lines = page_texts.get(pn, "").split("\n")
        drop: set[int] = set()
        seen = 0
        for i, ln in enumerate(lines):
            stripped = ln.strip()
            if not stripped:
                continue
            seen += 1
            if seen > _HF_EDGE_LINES:
                break
            if _norm_edge_line(stripped, pn, total_pages) in top_templates:
                drop.add(i)
            else:
                break
        seen = 0
        for i in range(len(lines) - 1, -1, -1):
            stripped = lines[i].strip()
            if not stripped:
                continue
            seen += 1
            if seen > _HF_EDGE_LINES:
                break
            if _norm_edge_line(stripped, pn, total_pages) in bottom_templates:
                drop.add(i)
            else:
                break
        if drop:
            result[pn] = "\n".join(ln for i, ln in enumerate(lines) if i not in drop)
    return result


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
    cjk_count = sum(1 for ch in compact if "一" <= ch <= "鿿")
    if ascii_count >= 6 and ascii_count >= cjk_count * 2:
        return True
    return bool(re.fullmatch(r"[A-Za-z0-9_\-\[\]]{5,}", compact))


def _cleanup_ocr_text(text: str, *, source_filename: str | None = None) -> str:
    from . import _source_filename_contract_no

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


def _extract_pdf_page_tables(
    page: Any,
) -> tuple[list[str], list[tuple[float, float, float, float]]]:
    """Return (markdown_tables, bboxes). The two lists are kept aligned: a table is
    only recorded when both its markdown and bbox are usable, so callers can rely on
    bboxes to strip every table region surfaced via markdown."""
    try:
        finder = page.find_tables()
    except Exception as exc:
        logger.warning("find_tables failed on page %d: %s", page.number + 1, exc)
        return [], []
    tabs = finder.tables if hasattr(finder, "tables") else list(finder)
    out_md: list[str] = []
    out_bboxes: list[tuple[float, float, float, float]] = []
    for table in tabs:
        try:
            md = table.to_markdown()
        except Exception as exc:
            logger.warning("table.to_markdown failed on page %d: %s", page.number + 1, exc)
            continue
        if not md or not md.strip():
            continue
        bbox = getattr(table, "bbox", None)
        if bbox is None:
            logger.warning(
                "table missing bbox on page %d; skipping (cannot dedupe)", page.number + 1
            )
            continue
        try:
            bbox_tuple = (float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3]))
        except (TypeError, ValueError, IndexError):
            logger.warning("table bbox invalid on page %d; skipping", page.number + 1)
            continue
        out_md.append(md.strip())
        out_bboxes.append(bbox_tuple)
    return out_md, out_bboxes


def _strip_text_in_table_bboxes(
    page: Any, bboxes: list[tuple[float, float, float, float]]
) -> str | None:
    """Return the page text with table-region blocks removed, or None if the strip
    could not be performed (caller should leave raw text untouched and treat tables
    as already embedded)."""
    if not bboxes:
        return page.get_text("text").strip()
    import fitz

    rects = [fitz.Rect(*bbox) for bbox in bboxes]
    try:
        blocks = page.get_text("blocks")
    except Exception as exc:
        logger.warning("get_text(blocks) failed on page %d: %s", page.number + 1, exc)
        return None
    kept: list[str] = []
    for block in blocks:
        try:
            x0, y0, x1, y1, text = block[0], block[1], block[2], block[3], block[4]
        except (IndexError, TypeError):
            continue
        if not isinstance(text, str) or not text.strip():
            continue
        centroid = fitz.Point((x0 + x1) / 2.0, (y0 + y1) / 2.0)
        if any(centroid in r for r in rects):
            continue
        kept.append(text.rstrip())
    return "\n".join(part for part in kept if part).strip()


def _extract_tables_from_ocr_text(text: str, page_num: int, total_pages: int) -> tuple[str, list[str]]:
    from . import _is_heading

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


def _normalize_document_text(pages: list[PageContent]) -> None:
    for page in pages:
        page.text = _cleanup_extracted_text_noise(_normalize_amount_phrases(page.text))
        page.tables = [
            _cleanup_extracted_text_noise(_normalize_amount_phrases(table))
            for table in page.tables
        ]
