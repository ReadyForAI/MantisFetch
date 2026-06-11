"""Document sectioning: split parsed page text into a hierarchy of Sections.

Heading detection, table-of-contents handling, and the post-split tidy-up
(merge short sections, demote TOC-stub sections, renumber). Operates on
PageContent and produces Section objects for the section tier of the
three-level loading model.

Four low-level helpers stay in the package (they are shared with the parse
pipeline): _detect_text_locale, _page_bounds, and the two table-row
classifiers. They are pulled in via function-level relative imports
(`from . import ...`) to avoid an import cycle — the package __init__ imports
this module before it finishes defining those helpers, so a module-level
import would fail; at call time the package is fully loaded.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from i18n import tmpl_for_locale

from .models import PageContent, Section, SectionPolicy

logger = logging.getLogger("larkscout_docreader")

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
    from . import _looks_like_markdown_table_row, _looks_like_plain_table_row

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
    from . import _detect_text_locale

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
    from . import _page_bounds

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
    from . import _page_bounds

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


_CHAPTER_TITLE_LINE_RE = re.compile(
    r"^第[一二三四五六七八九十\d]+[章节部分篇]\s*[、:：]?\s*\S"
)


def _looks_like_toc_stub_body(text: str) -> bool:
    """True if a section's whole body is just one chapter-title-shaped line.

    Source DOCX files sometimes have transitional/cover pages where a Heading-1
    style is applied to a line whose only following content is the *next*
    chapter's title (e.g. ``## 第一章 供应商须知`` followed solely by
    ``第二章 应答文件格式``). Detecting this exact body shape lets us merge the
    stub into the preceding section without disturbing legitimate short
    sections (e.g. ``六、保函`` with body ``无``).
    """
    body = text.strip()
    if not body:
        return True
    if len(body.splitlines()) != 1:
        return False
    return bool(_CHAPTER_TITLE_LINE_RE.match(body))


def _demote_toc_stub_sections(
    sections: list[Section], *, max_body_chars: int = 30
) -> list[Section]:
    """Merge sections whose body is empty or solely another chapter-title line.

    These stubs are typically TOC-like transitional artifacts in the source
    docx, not real section boundaries. Append the stub title (and any
    chapter-title body line) to the preceding section as plain text.
    """
    from . import _page_bounds

    if len(sections) < 2:
        return sections
    result: list[Section] = []
    for sec in sections:
        body = sec.text.strip()
        if (
            result
            and len(body) <= max_body_chars
            and _looks_like_toc_stub_body(body)
        ):
            previous = result[-1]
            parts = [previous.text.rstrip(), sec.title.strip()]
            if body:
                parts.append(body)
            previous.text = "\n".join(part for part in parts if part).strip()
            start, _ = _page_bounds(previous.page_range)
            _, end = _page_bounds(sec.page_range)
            if start and end:
                previous.page_range = f"p.{start}-{end}"
            continue
        result.append(sec)
    return _renumber_sections(result)


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


_MARKDOWN_HEADING_RE = re.compile(r"^(#{1,6})\s+\S")


def _markdown_heading_level(text: str) -> int:
    m = _MARKDOWN_HEADING_RE.match(text.strip())
    return len(m.group(1)) if m else 0


def _detect_markdown_section_level(pages: list[PageContent]) -> int | None:
    """Return the shallowest markdown heading level used across pages, or None."""
    min_level: int | None = None
    for page in pages:
        for raw in page.text.splitlines():
            lvl = _markdown_heading_level(raw)
            if lvl == 0:
                continue
            if min_level is None or lvl < min_level:
                min_level = lvl
                if min_level == 1:
                    return 1
    return min_level


def _looks_like_polluted_heading_text(text: str) -> bool:
    """Detect lines wrongly classified as headings (either via an ``## `` marker
    applied by the source docx, or via legacy heuristics) that are actually
    paragraph body. A real Chinese business / bid-doc heading essentially never
    ends in 句号; other clause terminators (冒号/分号) are only suspicious past
    a length threshold to avoid downgrading legitimate "6.2 ... 内容:"
    enumeration headings.
    """
    stripped = _strip_heading_markup(text)
    if not stripped:
        return False
    if stripped.endswith("。"):
        return True
    if len(stripped) > 30 and stripped.endswith(("：", ":", "；", ";")):
        return True
    return False


def _split_sections(
    pages: list[PageContent], section_policy: SectionPolicy | None = None
) -> list[Section]:
    from . import _detect_text_locale

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
    md_section_level = None if ocr_mode else _detect_markdown_section_level(pages)

    for page in pages:
        page_has_body = False
        page_tables_attached = False
        for line in page.text.split("\n"):
            line = line.strip()
            if not line:
                continue
            md_level = _markdown_heading_level(line)
            if md_section_level is not None and md_level > 0:
                if md_level == md_section_level and not _looks_like_polluted_heading_text(line):
                    heading_level = md_level
                else:
                    heading_level = 0
            else:
                heading_level = _is_heading(line, ocr_mode=ocr_mode)
                if suppress_arabic_clause_headings and _is_arabic_numbered_heading_candidate(line):
                    heading_level = 0
                if heading_level > 0 and _looks_like_polluted_heading_text(line):
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
    if not ocr_mode:
        sections = _demote_toc_stub_sections(sections)
    if ocr_mode:
        sections = _merge_short_ocr_sections(sections)
    else:
        sections = _merge_short_sections(sections)
    return _renumber_sections(_promote_parent_sections_to_first_child(sections))
