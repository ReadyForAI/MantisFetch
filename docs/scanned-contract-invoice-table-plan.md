# Scanned Contract / Invoice Table Extraction Plan

Date: 2026-05-04

## Positioning

LarkScout core should stay a low-level document ingestion and reading layer for enterprise customers.

It should not implement business semantics such as contract amount, invoice number, buyer/seller, or payment terms. Those belong in Skills. LarkScout should provide accurate, structured, low-token document facts that Skills can consume.

The near-term priority is not a broad parse-quality or audit framework. The priority is to improve recognition accuracy and structural fidelity for scanned contracts, invoices, quotations, and similar enterprise documents, especially tables.

## Reference Project

Use OpenDataLoader PDF as a reference project for layout-aware PDF extraction:

- Repository: https://github.com/opendataloader-project/opendataloader-pdf
- Relevant ideas to study:
  - element-level JSON with semantic type, page number, and bounding boxes
  - Markdown plus JSON dual output for LLM/RAG usage and source citation
  - reading-order analysis such as XY-Cut-style layout ordering
  - table detection using border analysis and text clustering
  - annotated PDF or visual debug output for verifying detected structures
  - local deterministic mode plus hybrid routing for complex/scanned pages
  - hidden/off-page/invisible text filtering as a later safety reference

Do not treat OpenDataLoader as the immediate production backend. For the next LarkScout pass, borrow the data-model and verification ideas while keeping the current priority on native OCR geometry, table sidecars, and low-token API compatibility.

## Priorities

### P0: Table Structure Enhancement

Improve table preservation and table metadata for scanned contracts, invoices, quotations, statements, and line-item lists.

Keep the existing Markdown table output, but add structured table metadata and optional JSON sidecars.

Target metadata:

```json
{
  "table_id": "table-01",
  "page_start": 5,
  "page_end": 5,
  "row_count": 12,
  "column_count": 6,
  "source": "markdown|ocr|layout",
  "continued_from": null,
  "continued_to": "table-02"
}
```

First-stage goals:

- Preserve row and column relationships more reliably.
- Detect header rows where possible.
- Keep cross-page table relationships when detectable.
- Avoid mixing table body back into normal section text.
- Preserve existing `/library/{doc_id}/table/{table_id}` behavior.

Do not add business labels such as "invoice detail table" or "contract payment table" in core.

### P0: OCR Blocks With Coordinates

Add a sidecar for OCR text blocks with coordinates and confidence. This is required for table reconstruction, region re-OCR, and evidence lookup, but should not be returned in default low-token APIs.

Candidate file:

```text
docs/DOC-xxx/ocr_blocks.json
```

Candidate structure:

```json
{
  "version": 1,
  "doc_id": "DOC-001",
  "pages": [
    {
      "page": 1,
      "width": 2480,
      "height": 3508,
      "blocks": [
        {
          "text": "example",
          "bbox": [100, 220, 680, 260],
          "confidence": 0.94,
          "source": "local_ocr",
          "line_index": 12
        }
      ]
    }
  ]
}
```

Manifest should only store the path and availability, not inline all blocks:

```json
{
  "layout": {
    "ocr_blocks_path": "ocr_blocks.json",
    "available": true,
    "coordinate_system": "image_pixels"
  }
}
```

### P1: Scanned Table Reconstruction

Use OCR block geometry to reconstruct tables in scanned PDFs.

Generic, non-business goals:

- Cluster OCR blocks into rows using y coordinates.
- Infer columns from x coordinates.
- Detect likely header rows.
- Keep multi-line cell text together where possible.
- Emit Markdown plus structured JSON.
- Preserve each cell's source OCR block references when available.

Candidate sidecar:

```text
docs/DOC-xxx/tables/table-01.json
```

Candidate structure:

```json
{
  "table_id": "table-01",
  "page": 5,
  "rows": [
    {
      "row_index": 1,
      "cells": [
        {
          "row": 1,
          "column": 1,
          "text": "item",
          "bbox": [100, 200, 300, 240],
          "rowspan": 1,
          "colspan": 1,
          "confidence": 0.9,
          "ocr_block_refs": ["p5-b12"]
        }
      ]
    }
  ]
}
```

### P1: Region Re-Recognition / Cropping

Support targeted re-recognition for details that were not recognized cleanly.

Useful generic capabilities:

- Re-OCR by `page + bbox`.
- Re-extract a table by `table_id`.
- Export a page/region crop for inspection or downstream Skill processing.
- Keep rerun output separate and traceable.

This should remain a low-level document operation. Skills decide which region matters.

## Deferred

The following are useful, but should not be first:

- Broad `parse_quality` framework.
- Hidden/off-page/tiny text safety statistics.
- OpenDataLoader as a production backend.
- Tagged PDF / PDF accessibility features.
- Business semantic extraction in core.

## Test Plan

Unit tests:

- OCR block sidecar writing.
- Manifest path/availability for OCR blocks.
- Row/column counting for Markdown tables.
- Table metadata compatibility.
- Basic table JSON generation.

Regression tests:

- Existing digest, brief, full, sections, section, table, manifest APIs remain compatible.
- Existing Word image inventory tests remain compatible.
- Existing scan-contract OCR tests still pass.

Sample document tests:

- Scanned contract with normal text pages.
- Scanned contract with quotation/payment tables.
- Scanned invoice or invoice-like table.
- Multi-page quotation/line-item table.
- Mixed text PDF with no OCR blocks.

Performance constraints:

- Do not inline large OCR/layout payloads in default API responses.
- Do not materially increase token usage on the default digest/brief/section path.
- Sidecar generation should be optional or cheap enough for normal scan parsing.

## Guiding Principle

Enterprise customers care most about accurate detail extraction, structure preservation, and system integration. For the next implementation pass, prioritize table structure and OCR geometry over broad quality reporting.
