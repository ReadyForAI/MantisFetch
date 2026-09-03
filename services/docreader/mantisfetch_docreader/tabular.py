"""Tabular and generic MarkItDown-backed parsers: XLSX, CSV, and the catch-all.

These three parsers all lean on `_convert_to_markdown` (MarkItDown) and then
shape the result into a `ParsedDocument`:

- `parse_xlsx` — split the workbook markdown by `## sheet` headings into one
  section per sheet,
- `parse_csv` — single-section, single-table passthrough,
- `parse_generic` — any other MarkItDown-supported format (PPTX, HTML, …),
  sectioned via the shared splitter.

`_split_sections` (sectioning) and `_extract_markdown_table_blocks` (ocr.tables)
are leaf imports. `_convert_to_markdown` / `_section_sid` / `MAX_PARSE_ROWS` live in
the package `__init__`, so they are reached via function-level relative imports
to break the import cycle (the facade imports this module early); this also lets
tests that patch `docreader._convert_to_markdown` take effect.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from .models import DocumentProfile, PageContent, ParsedDocument, Section
from .ocr.tables import _extract_markdown_table_blocks
from .sectioning import _split_sections

logger = logging.getLogger("mantisfetch_docreader")


def parse_xlsx(filepath: Path) -> ParsedDocument:
    """Parse an XLSX workbook via MarkItDown."""
    from . import MAX_PARSE_ROWS, _convert_to_markdown, _section_sid

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


def parse_csv(filepath: Path) -> ParsedDocument:
    """Parse a CSV file via MarkItDown."""
    from . import _convert_to_markdown, _section_sid

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


#: MarkItDown marks each slide it emits. The count is the deck's, not an
#: estimate from how much text the slides happened to contain.
_SLIDE_MARKER_RE = re.compile(r"^<!--\s*Slide number:\s*\d+\s*-->", re.MULTILINE)


def parse_generic(
    filepath: Path, profile: DocumentProfile | None = None, extract_tables: bool = True
) -> ParsedDocument:
    """Parse any MarkItDown-supported format (PPTX, HTML, etc.)."""
    from . import _convert_to_markdown, _section_sid

    ext = filepath.suffix.lower()
    file_type = ext.lstrip(".")
    logger.info(f"Parsing {file_type.upper()}: {filepath.name}")
    markdown_text = _convert_to_markdown(filepath)
    logger.info(f"MarkItDown extraction complete: {len(markdown_text)} chars")

    # A deck knows how many slides it has. Dividing the markdown by 3000 does
    # not: a 2-slide deck reported total_pages 1, and page citations all read
    # p.1-1 however long the deck ran.
    slide_count = len(_SLIDE_MARKER_RE.findall(markdown_text))
    est_pages = slide_count or max(1, len(markdown_text) // 3000)

    # The tables were being counted and then dropped. `_count_markdown_tables`
    # found the one in an HTML page, the response reported table_count 0, and
    # no sidecar was written — because the count never became content and the
    # sidecar writer reads PageContent.tables, which stayed empty. DOCX, XLSX
    # and CSV all populate it; this is the path that did not.
    table_blocks = _extract_markdown_table_blocks(markdown_text) if extract_tables else []
    pages = [PageContent(page_num=1, text=markdown_text, tables=table_blocks)]
    sections = _split_sections(pages, section_policy=profile.section_policy if profile else None)
    for sec in sections:
        sec.sid = _section_sid(sec.title, sec.text)

    table_count = len(table_blocks)

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
