"""Table detection, reconstruction, and Markdown-table utilities.

Pure leaf module: operates on OCRTextBlock geometry (from models) and Markdown
text only. It must not import from the package __init__ (would create a
circular import). The PDF-page and OCR-text table extractors that depend on
locale/heading/footer helpers stay in the package for now.
"""

from __future__ import annotations

import re
from typing import Any

from ..models import OCRBlocksSidecar, OCRTextBlock


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
        # A real separator has dashes ("|---|---|"); require one so an
        # all-empty/all-blank row ("| | |") is treated as content, not a
        # header separator.
        if "-" in stripped and re.fullmatch(r"\|[\s\-:|]+\|", stripped):
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
