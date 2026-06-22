---
name: mantisfetch-docreader
description: Long document parsing and reading HTTP API. Use when you need to read, analyze, or summarize PDF, Office, HTML, CSV, text/JSON/XML files. Supports file upload parsing, three-tier summaries (digest/brief/full), on-demand section loading, table extraction, metadata persistence, source file references, and document library search via HTTP API. Outputs doc-index v2 format, sharing a unified index with mantisfetch-browser web capture results. Serves as the document parsing engine for the MantisFetch open-source data collection platform.
triggers:
  - "read document"
  - "parse document"
  - "analyze this PDF"
  - "this Word file"
  - "document summary"
  - "extract content"
  - "cross-document"
  - "consolidate documents"
  - "upload document"
  - "document library search"
  - ".pdf"
  - ".doc"
  - ".docx"
  - ".ppt"
  - ".pptx"
  - ".xls"
  - ".xlsx"
  - ".csv"
  - ".html"
  - ".txt"
  - ".json"
  - ".jsonl"
  - ".xml"
---

# SKILL: MantisFetch DocReader (Document Parsing HTTP API)

## 1. Purpose

Use for: document analysis, cross-document consolidation, research report extraction, financial data collection, document review, meeting minutes processing.

---

## 2. Service Dependency

- Base URL: `http://127.0.0.1:9898/doc/`
- If the server is started with TLS (`MANTISFETCH_TLS_CERTFILE` + `MANTISFETCH_TLS_KEYFILE`), use `https://` instead.
- Agents that connect over the Model Context Protocol can use the same capabilities as MCP tools (`doc_parse`, `doc_digest`, `doc_brief`, `doc_section`, …) — see the [mantisfetch-mcp](./mantisfetch-mcp-SKILL.md) skill.

---

## 3. Agent Execution Strategy (Low-Token Rules — Must Follow)

### 3.1 Three-Tier Loading Rules

| Tier | Endpoint                                     | Token Cost | When to Use                              |
| ---- | -------------------------------------------- | ---------- | ---------------------------------------- |
| L1   | `GET /doc/library/{doc_id}/digest`           | ~200       | When a document is mentioned; quick topic overview |
| L2   | `GET /doc/library/{doc_id}/brief`            | ~1500      | When you need key points per section     |
| L3   | `GET /doc/library/{doc_id}/section/{sid}`    | On-demand  | When you need the original text of a specific section |
| L4   | `GET /doc/library/{doc_id}/full`             | Full       | **Almost never used** — only in extreme cases |

**Never inject full text into context. Use section/{sid} to load specific sections on demand.**

### 3.2 Golden Workflow

```
POST /doc/parse (upload file)
↓
Returns doc_id + digest (summary already included — no extra call needed)
↓
Need more detail → GET /doc/library/{doc_id}/brief
↓
Need a section's original text → GET /doc/library/{doc_id}/sections (get section list)
                               → GET /doc/library/{doc_id}/section/{sid}
↓
Need table data → GET /doc/library/{doc_id}/table/{table_id}
```

### 3.3 Cross-Document Consolidation

When consolidating multiple documents:

1. Read the digest for all relevant documents (~200 tokens each)
2. Identify the dimensions needing cross-comparison
3. Load relevant sections from each document on demand
4. Synthesize analysis and produce a consolidated report

```
Total context cost:
  3 × digest         = ~600 tokens
  + 4 sections on-demand = ~4000 tokens
  ────────────────────────────────
  Total                ≈ 4600 tokens

vs. injecting 3 full documents:  ≈ 180,000 tokens
Savings: 97%
```

### 3.4 Document Library Search

```
GET /doc/library/search?q=revenue&tags=financial&file_type=pdf&metadata.customer=ACME
↓
Returns matching doc_id list + digest previews
↓
Load specific documents' brief or section on demand
```

**Prohibited behaviors:**

- Requesting full directly (wastes tokens)
- Reading brief without checking digest first (assess need first)
- Iterating all documents without using search (use search)

---

## 4. API Reference

> All requests use `Content-Type: application/json` (query endpoints) or `multipart/form-data` (upload endpoints)

### 4.1 Health Check

- `GET /doc/health`

Response example:

```json
{
  "ok": true,
  "version": "1.0.0",
  "docs_dir": "~/.mantisfetch/docs",
  "supported_formats": ["pdf", "doc", "docx", "ppt", "pptx", "xls", "xlsx", "csv", "html", "htm", "txt", "text", "json", "jsonl", "xml"]
}
```

Notes:
- `docs_dir` shows a masked path (`~` replaces the home directory) — this is intentional for security
- `supported_formats` includes PDF, Office, CSV, HTML, text, JSON, JSONL, and XML; `.doc` and `.ppt` are converted server-side to `.docx` / `.pptx` before parsing
- `.doc` / `.ppt` support requires LibreOffice/soffice on the server; the Docker image includes the conversion components by default
- Document parsing powered by [MarkItDown](https://github.com/microsoft/markitdown) (Microsoft)

### 4.2 Upload and Parse Document (Core)

- `POST /doc/parse`
- Content-Type: `multipart/form-data`

Request parameters:

| Parameter             | Type   | Default    | Description                                                                             |
| --------------------- | ------ | ---------- | --------------------------------------------------------------------------------------- |
| `file`                | File   | (required) | File to upload (.pdf, .doc/.docx, .ppt/.pptx, .xls/.xlsx, .csv, .html/.htm, .txt/.text, .json/.jsonl/.xml) |
| `doc_id`              | string | Auto-increment | Manually specify DOC-ID                                                             |
| `content_type`        | string | `General`  | Library category: `General`, `Contract`, `Bid`, or `Knowledge`                      |
| `generate_summary`    | bool   | `true`     | Whether to generate summaries (false = extract text only)                               |
| `summary_mode`        | string | null       | Summary mode: `sync` / `defer` / `off`. Use `defer` for large documents and business Skills |
| `document_profile`    | string | null       | Optional document profile name; pass only when the caller knows an available profile    |
| `parse_mode`          | string | `accurate` | PDF parsing intensity: `fast` (native text, minimal OCR) / `accurate` (default — native text + OCR for scan/mixed pages) / `full` (most thorough, full-page/region LLM OCR; highest cost). Applies to PDFs; server default comes from `MANTISFETCH_PDF_PARSE_MODE` |
| `replace`             | bool   | `false`    | When an explicit `doc_id` already exists, set `true` to overwrite it; otherwise the request returns `409` (see §4.2 notes)            |
| `id_strategy`         | string | null       | DOC-ID strategy: `counter` / `source_filename`                                         |
| `skip_ocr_pages`      | string | null       | Pages confirmed blank or unnecessary for OCR, e.g. `"30,104,106-108"`                  |
| `force_ocr`           | bool   | `false`    | Force LLM OCR on all pages. This is higher cost and should only be used when the caller explicitly needs visual re-recognition for the whole document |
| `ocr_pages`           | string | null       | Upgrade specific page ranges to LLM OCR, e.g. `"10-30"`; unspecified pages still follow the server's automatic plan |
| `extract_tables`      | bool   | `true`     | Whether to extract tables                                                               |
| `extract_images`      | bool   | `false`    | Whether to extract embedded Word images into `images.json` / `images/`; can be used for lightweight image inventory first |
| `ocr_images`          | bool   | `false`    | Whether to OCR extracted embedded Word images                                           |
| `image_ocr_backend`   | string | `auto`     | Image OCR backend: `auto` / `local` / `llm`; for large bid files prefer explicit `local` to avoid default LLM fallback |
| `max_images`          | int    | `200`      | Maximum embedded images to process per document                                         |
| `max_ocr_images`      | int    | `80`       | Maximum embedded Word images allowed for OCR when `ocr_images=true`; requests above this threshold return 422 |
| `max_tables_per_page` | int    | `3`        | Maximum tables to extract per page                                                      |
| `concurrency`         | int    | `3`        | OCR/summary concurrency                                                                 |
| `tags`                | string | null       | Tags — JSON array (`'["Q3","financial"]'`) or comma-separated (`"Q3,financial"`)        |
| `metadata`            | string | null       | Custom metadata (JSON object). Stored in manifest; shallow scalar fields are indexed.   |

Call example:

```bash
curl -X POST http://localhost:9898/doc/parse \
  -F "file=@report.pdf" \
  -F "content_type=General" \
  -F "generate_summary=true" \
  -F "extract_tables=true" \
  -F 'tags=["Q3","financial"]'
```

Callers do not need the Python SDK; they can call MantisFetch directly with `curl`. MantisFetch provides lower-level parsing, indexing, and source retention only. Business scenarios, business metadata fields, naming rules, and follow-up operations are owned by the upper-level caller.

Recommended ingestion for embedded Word images is to create a lightweight image inventory first, without OCR for every image. `images.json` includes image files, anchors, dimensions, hashes, context keywords, and candidate hints so downstream tools can select candidate evidence by business requirements:

```bash
curl -X POST http://localhost:9898/doc/parse \
  -F "file=@/path/to/document.docx" \
  -F "content_type=Bid" \
  -F "summary_mode=defer" \
  -F "extract_tables=true" \
  -F "extract_images=true" \
  -F "ocr_images=false" \
  -F "max_images=1000" \
  -F 'metadata={"display_name":"document.docx","source_system":"agent_upload"}'
```

If a caller really needs OCR during ingestion, explicitly limit the image count and prefer local OCR:

```bash
curl -X POST http://localhost:9898/doc/parse \
  -F "file=@/path/to/document.docx" \
  -F "content_type=Bid" \
  -F "summary_mode=defer" \
  -F "extract_images=true" \
  -F "ocr_images=true" \
  -F "image_ocr_backend=local" \
  -F "max_images=50" \
  -F "max_ocr_images=50"
```

When the requested OCR image count exceeds `max_ocr_images`, the service refuses image OCR. Large bid-file callers should use `ocr_images=false` to ingest text and the image inventory first, then let downstream tools select candidate images for targeted OCR or vision review.

This only outputs image source, nearby heading, section anchor, image files, inventory metadata, and optional OCR text. The upper-level caller owns all business interpretation and requirement checks.

Generic ingestion example with metadata:

```bash
curl -X POST http://localhost:9898/doc/parse \
  -F "file=@/path/to/document.pdf" \
  -F "content_type=Contract" \
  -F "summary_mode=defer" \
  -F "extract_tables=true" \
  -F 'metadata={"display_name":"document.pdf","source_system":"manual_upload"}'
```

If some pages are confirmed blank or do not need OCR, append:

```bash
-F "skip_ocr_pages=30,104,106,108,110,112"
```

Response example:

```json
{
  "doc_id": "DOC-010",
  "filename": "report.pdf",
  "file_type": "pdf",
  "content_type": "General",
  "storage_path": "General/DOC-010",
  "total_pages": 45,
  "section_count": 12,
  "table_count": 8,
  "ocr_page_count": 3,
  "digest": "Q3 revenue grew 15%, net profit up 23% YoY...",
  "manifest_path": "docs/General/DOC-010/manifest.json",
  "processing_time_sec": 23.5,
  "source_ref": "source/report.pdf"
}
```

**Key notes:**

- The returned `digest` field already contains the first 300 characters of the summary — Agent usually doesn't need an extra call to `/doc/library/{doc_id}/digest`
- `generate_summary=false` extracts text and tables only without calling LLM — faster but no summary
- `content_type` defaults to `General` when omitted; pass `Contract`, `Bid`, or `Knowledge` when the caller already knows the business category
- Passing an explicit `doc_id` that already exists returns `409` unless `replace=true`; omit `doc_id` to always get a fresh auto-incremented one. The conflict is checked before the body is streamed, so a rejected upload wastes no disk
- `metadata` should be a JSON object; nested objects are preserved in manifest, while shallow scalar fields are available for filtering in `/doc/library/search`
- `source_ref` points to the stored upload inside the document directory when `MANTISFETCH_STORE_SOURCE_FILES=true`
- Large files (100+ page PDFs) may take 30–60 seconds to parse — Agents should set a longer timeout

### 4.3 Search Document Library

- `GET /doc/library/search`

| Parameter   | Description                                         |
| ----------- | --------------------------------------------------- |
| `q`         | Keyword (searches filename, digest, tags, metadata summary) |
| `tags`      | Tag filter, comma-separated                         |
| `file_type` | File type filter (`pdf` / `docx` / `web`)           |
| `content_type` | Category filter: `General`, `Contract`, `Bid`, or `Knowledge` |
| `metadata.*`| Equality-style metadata filters, e.g. `metadata.customer=ACME` |
| `limit`     | Maximum results (default 20)                        |

Response example:

```json
{
  "results": [
    {
      "doc_id": "DOC-010",
      "filename": "Q3-report.pdf",
      "file_type": "pdf",
      "content_type": "General",
      "storage_path": "General/DOC-010",
      "digest": "Q3 revenue grew 15%...",
      "tags": ["Q3", "financial"],
      "source": "upload",
      "metadata": {"customer": "ACME", "category": "report"},
      "source_ref": "source/Q3-report.pdf",
      "source_filename": "Q3-report.pdf",
      "source_available": true,
      "score": 3.5
    }
  ],
  "total": 1
}
```

**Search matches both documents uploaded via DocReader and web pages captured via MantisFetch Browser.** The `source` field distinguishes origin: `"upload"` = file upload, `"web_capture"` = web capture.

### 4.4 Search Full Text / Sections

- `GET /doc/library/search_text`

| Parameter   | Description |
| ----------- | ----------- |
| `q`         | Required query string |
| `tags`      | Tag filter, comma-separated |
| `file_type` | File type filter |
| `content_type` | Category filter: `General`, `Contract`, `Bid`, or `Knowledge` |
| `doc_id`    | Restrict to one document |
| `scope`     | `all` / `full` / `section` (default `all`) |
| `limit`     | Maximum results (default 20) |
| `metadata.*`| Equality-style metadata filters |

Response example:

```json
{
  "results": [
    {
      "doc_id": "DOC-010",
      "filename": "Q3-report.pdf",
      "file_type": "pdf",
      "content_type": "General",
      "storage_path": "General/DOC-010",
      "digest": "Q3 revenue grew 15%...",
      "tags": ["Q3", "financial"],
      "source": "upload",
      "metadata": {"customer": "ACME"},
      "sid": "a3f8e1b902cd",
      "section_title": "Payment Terms",
      "page_range": "p.12-13",
      "page_start": 12,
      "page_end": 13,
      "snippet": "...payment terms require invoice submission within 30 days...",
      "score": 1.5
    }
  ],
  "total": 1
}
```

Use this endpoint when you need a snippet and page hint before reading a section in full.

### 4.5 Get Document Digest (Lowest Token Cost)

- `GET /doc/library/{doc_id}/digest`

Response: `{"doc_id": "DOC-010", "content": "# DOC-010: report.pdf\n\nQ3 revenue grew 15%..."}`

### 4.6 Get Document Brief (Medium Token Cost)

- `GET /doc/library/{doc_id}/brief`

Response: `{"doc_id": "DOC-010", "content": "# DOC-010: report.pdf · Brief\n\n..."}`

### 4.7 Get Document Full Text (High Token Cost — Use Sparingly)

- `GET /doc/library/{doc_id}/full`

Response: `{"doc_id": "DOC-010", "content": "# report.pdf\n\n..."}`

### 4.8 List Document Sections

- `GET /doc/library/{doc_id}/sections`

Response example:

```json
{
  "doc_id": "DOC-010",
  "sections": [
    {
      "sid": "a3f8e1b902cd",
      "index": 1,
      "title": "Executive Summary",
      "page_range": "p.1-3",
      "page_start": 1,
      "page_end": 3,
      "char_count": 2500,
      "summary_preview": "Q3 revenue grew 15%, net profit up 23% YoY..."
    },
    {
      "sid": "b7c2d4e5f612",
      "index": 2,
      "title": "Financial Analysis",
      "page_range": "p.4-15",
      "page_start": 4,
      "page_end": 15,
      "char_count": 12000,
      "summary_preview": "Revenue mix shifted, service revenue share rose to 42%..."
    }
  ]
}
```

**Agent should call this endpoint first to get the section list, then read specific sections by sid.**

### 4.9 Read Single Section

- `GET /doc/library/{doc_id}/section/{sid}`

Response: `{"doc_id": "DOC-010", "sid": "a3f8e1b902cd", "content": "# Executive Summary\n\n..."}`

### 4.10 Read Single Table

- `GET /doc/library/{doc_id}/table/{table_id}`

table_id format: `"01"` or `"table-01"`.

Response: `{"doc_id": "DOC-010", "table_id": "01", "content": "# Table 1 (Page 5)\n\n| ... |"}`

### 4.11 Read Embedded Word Image Results

- `GET /doc/library/{doc_id}/images`
- `GET /doc/library/{doc_id}/image/{image_id}` — metadata + OCR text (JSON)
- `GET /doc/library/{doc_id}/image/{image_id}/raw` — raw image **bytes** (`variant=rendered` default, or `original`); use for visual reads (stamp/signature recognition) that OCR text can't serve

Results exist only when `/doc/parse` was called with `extract_images=true`. `image_id` format: `"001"` or `"IMG-001"`.

### 4.12 Get Manifest

- `GET /doc/library/{doc_id}/manifest`

Returns the full manifest.json contents, including document structure, section list, image/table path information, metadata, source file reference, and provenance.

### 4.13 Search Within One Document's Sections

- `POST /doc/library/{doc_id}/search_sections`

Request body: `{"q": "payment terms", "case_sensitive": false, "include_content": false, "limit": 20}`

Searches only inside one document's section files (title + body) and returns the same result shape as `/doc/library/search_text`, with `sid` / `section_title` / `page_start` / `page_end` provenance and a `snippet`. Set `include_content=true` to also return each matched section's full text. Use this to locate the right `sid` before reading a section in full.

### 4.14 Read Structured Table JSON

- `GET /doc/library/{doc_id}/table/{table_id}/json`

Returns the table as structured JSON (when a JSON sidecar exists for it), in addition to the Markdown form from §4.10:

```json
{
  "doc_id": "DOC-010",
  "table_id": "table-01",
  "table": {
    "table_id": "table-01",
    "page": 5,
    "source": "ocr_geometry",
    "row_count": 4,
    "column_count": 3,
    "rows": [
      {
        "row_index": 1,
        "cells": [
          {"row": 1, "column": 1, "text": "Item", "rowspan": 1, "colspan": 2, "confidence": 0.97},
          {"row": 1, "column": 3, "text": "Total", "rowspan": 1, "colspan": 1, "confidence": 0.96}
        ]
      }
    ]
  }
}
```

**Cell fields:** `row`, `column` (1-based left-anchor), `text`, `rowspan`, `colspan`, `confidence`, plus `bbox` / `ocr_block_refs` for OCR-geometry tables.

- For tables reconstructed from scanned pages (`source="ocr_geometry"`), a merged header/total cell that spans columns now carries the **real `colspan`** (computed geometrically from the cell bbox vs. column centers), so downstream consumers get correct cell→column mapping. `rowspan` stays `1` (vertical merges need cell borders / a TSR model). The Markdown output (§4.10) is unchanged — the merged value already renders in its start column.
- Use the Markdown form (§4.10) for reading; use the JSON form when you need explicit cell geometry or merged-cell spans (e.g. contract/invoice field extraction).

### 4.15 Build Retrieval Chunks

- `POST /doc/library/{doc_id}/chunks`

Request body: `{"include_text": false}` (plus optional chunking config fields).

Returns section-boundary chunks for downstream RAG/retrieval pipelines: `{"doc_id", "chunk_count", "chunks": [...], "config": {...}}`. MantisFetch itself does not do retrieval — this produces generic chunks for an upper-level skill to embed/index. Set `include_text=true` to include chunk text.

### 4.16 Deferred Summary Status & Retry

These pair with `summary_mode=defer` at parse time (summary generated in the background).

- `GET /doc/library/{doc_id}/summary` — current summary status (poll this after a deferred parse):

```json
{"doc_id": "DOC-010", "summary": {"status": "running", "mode": "defer", "attempts": 1}, "paths": {...}}
```

`status` is one of `pending` / `running` / `done` / `failed`.

- `POST /doc/library/{doc_id}/summary?concurrency=3&force=false` — (re)schedule summary generation for a document parsed without one, or retry a failed one. Returns `{"doc_id", "scheduled": true, "summary": {...}, "limits": {...}}`. Returns `409` if a summary is already `running` or the per-doc attempt limit is reached (pass `force=true` to override).

### 4.17 Discover Sidecars & OCR Layout (Advanced)

For consumers that need OCR geometry (e.g. precise table/region positions):

- `GET /doc/library/{doc_id}/sidecars` — discover which optional sidecars exist (OCR layout, structured tables) and their endpoints, **without** returning large geometry payloads. Includes per-table summaries (`row_count`, `column_count`, `json_file`, `bbox_available`).
- `GET /doc/library/{doc_id}/layout/pages` — list OCR layout pages + block counts (no block geometry).
- `GET /doc/library/{doc_id}/layout/page/{page_num}` — full OCR geometry for one 1-based page.

Most agents never need these — prefer digest/brief/section/table. Use them only when cell-level coordinates matter.

### 4.18 Read Multiple Sections (Batch)

- `POST /doc/library/{doc_id}/sections/batch`

Request body: `{"sids": ["a3f8e1b902cd", "b7c2d4e5f612"]}` (1–100 sids).

Reads several sections in one request — fewer round-trips than repeated `/section/{sid}`, which matters for remote/MCP callers. Returns the sections found (in request order, de-duplicated) plus any sids that didn't resolve:

```json
{"doc_id": "DOC-010", "sections": [{"sid": "a3f8e1b902cd", "content": "# Executive Summary\n\n..."}], "missing": ["unknown_sid"]}
```

---

## 5. Document Library Structure

All parsed results are stored under `DOCS_DIR`:

```text
docs/
  ├─ doc-index.json              ← Global index (v2 format, shared with MantisFetch Browser)
  │
  ├─ General/
  │   └─ DOC-001/                ← Default categorized parsed results
  │       ├─ .meta.json
  │       ├─ manifest.json
  │       ├─ source/
  │       ├─ digest.md
  │       ├─ brief.md
  │       ├─ full.md
  │       ├─ sections/
  │       ├─ tables/
  │       ├─ images.json
  │       └─ images/
  │
  ├─ Contract/
  ├─ Bid/
  ├─ Knowledge/
  │
  ├─ DOC-001/                    ← Legacy flat parsed results remain readable
  │   ├─ .meta.json
  │   ├─ manifest.json           ← Contains provenance tracking
  │   ├─ source/                 ← Original uploaded file (when enabled)
  │   │   └─ original.pdf
  │   ├─ digest.md               ← ~200 tokens
  │   ├─ brief.md                ← ~1500 tokens
  │   ├─ full.md                 ← Full text
  │   ├─ sections/               ← Section slices
  │   │   ├─ 01-{sid}-{title}.md
  │   │   └─ 02-{sid}-{title}.md
  │   ├─ tables/                 ← Extracted tables
  │   │   ├─ table-01.md
  │   │   └─ table-02.md
  │   ├─ images.json             ← Embedded Word image anchors, files, and OCR metadata
  │   └─ images/                 ← Embedded Word originals, rendered images, and OCR text
  │       ├─ IMG-001.original.png
  │       ├─ IMG-001.png
  │       └─ IMG-001.ocr.txt
  │
  └─ WEB-001/                    ← Legacy flat web capture results remain readable
      └─ ...
```

New ingested content is stored under `General/`, `Contract/`, `Bid/`, or `Knowledge/`. Direct reads still use `doc_id`; the service resolves the directory by checking `storage_path` (then `content_type`) in `doc-index.json`, scanning the category subdirectories, and finally falling back to the legacy flat `${MANTISFETCH_DOCS_DIR}/<doc_id>` layout.

**doc-index.json v2 Key Fields:**

| Field          | Description                                     |
| -------------- | ----------------------------------------------- |
| `id`           | DOC-001 / WEB-001                               |
| `content_type` | `General`, `Contract`, `Bid`, or `Knowledge`    |
| `storage_path` | Relative document directory, e.g. `Contract/DOC-001` |
| `source`       | `"upload"` or `"web_capture"`                   |
| `tags`         | Tag array                                       |
| `metadata`     | Indexed scalar metadata copied from upload metadata |
| `source_ref`   | Relative path to stored upload under `source/`  |
| `content_hash` | SHA256 of content, used for deduplication and change detection |
| `digest`       | First 200 characters of the summary             |

---

## 6. Agent Call Templates

### 6.1 Single Document Analysis

```
POST /doc/parse (upload file)
↓
Returns doc_id + digest → determine if document is relevant
↓
GET /doc/library/{doc_id}/brief → understand key points per section
↓
GET /doc/library/{doc_id}/section/{target_sid} → deep read key sections
```

### 6.2 Cross-Document Comparison

```
POST /doc/parse (Document A) → doc_id_a
POST /doc/parse (Document B) → doc_id_b
↓
GET /doc/library/{doc_id_a}/digest + GET /doc/library/{doc_id_b}/digest
↓
Compare digests, identify dimensions needing cross-comparison
↓
GET /doc/library/{doc_id_a}/section/{relevant_sid}
GET /doc/library/{doc_id_b}/section/{relevant_sid}
↓
Synthesize analysis and produce comparison report
```

### 6.3 Document Library Search

```
GET /doc/library/search?q=Q3+revenue&tags=financial&metadata.customer=ACME
↓
Returns matching document list + digest previews
↓
Select target document → GET /doc/library/{doc_id}/brief
↓
Drill down as needed → GET /doc/library/{doc_id}/section/{sid}
```

Need page-level hint before loading a section:

```
GET /doc/library/search_text?q=payment+terms&doc_id=DOC-010&scope=section
↓
Returns snippet + sid + page_start/page_end
↓
GET /doc/library/{doc_id}/section/{sid}
```

### 6.4 Text-Only Extraction (No Summary Generation)

```
POST /doc/parse (generate_summary=false)
↓
Returns doc_id → text extracted, sections readable
↓
GET /doc/library/{doc_id}/sections → section list
GET /doc/library/{doc_id}/section/{sid} → read content
```

Use for: scenarios where the Agent performs its own analysis without needing LLM summaries, or to conserve LLM API calls.

---

## 7. Common Errors and Solutions

| Error                                              | Cause                          | Solution                                                                   |
| -------------------------------------------------- | ------------------------------ | -------------------------------------------------------------------------- |
| `422 unsupported format`                           | Uploaded non-supported file    | Check file format against `/doc/health` `supported_formats`               |
| `409 doc_id already exists`                         | Explicit `doc_id` collides with an existing doc | Pass `replace=true` to overwrite, or omit `doc_id` for a fresh one         |
| `409 summary already running` / `attempt limit reached` | Concurrent / repeated `POST .../summary` | Poll `GET .../summary` instead; pass `force=true` only if you must override |
| `429 too many concurrent requests`                 | Rate limit exceeded            | Wait and retry — server limits concurrent parse operations                 |
| `404 document not found`                           | Invalid doc_id or unparsed doc | Use search to confirm doc_id first                                         |
| `404 section not found`                            | Invalid sid                    | Call `/doc/library/{doc_id}/sections` first to get valid sid list           |
| `500 parse failed`                                 | Corrupted or encrypted PDF     | Prompt user to check the file                                              |
| `500 RuntimeError` about missing LLM credentials   | LLM provider credentials not configured | Check the active LLM provider settings and restart service        |
| Parsing takes too long                             | Large file + OCR               | Use `generate_summary=false` for fast extraction first, generate summary later |
| Table is empty                                     | Tables are images or complex layouts | First confirm text OCR was ingested; if critical table content is missing, retry only relevant pages with `ocr_pages`, or use `force_ocr=true` only when the extra cost is acceptable |
| OCR output looks like `No image provided`          | Vision model / image input mode mismatch | Check the active OCR model, vendor profile, and OCR image input mode before retrying |
| XLSX/CSV truncated warning in metadata             | File exceeds MAX_PARSE_ROWS    | Normal — large spreadsheets are truncated for safety; check `metadata.truncated` |

---

## 8. Recommended Default Parameters

**Parsing:**

- `generate_summary=true` (when summaries are needed)
- `extract_tables=true`
- `max_tables_per_page=3`
- `concurrency=3` (adjust based on upstream LLM/OCR quota)

**OCR:**

- Normal and scanned documents: don't pass `force_ocr` by default. The service auto-detects scanned pages and prioritizes local PaddleOCR
- Confirmed blank pages or pages that do not need OCR: pass `skip_ocr_pages` to avoid wasted processing time
- When only a few pages need higher-quality visual recognition, pass `ocr_pages="10-30"` to upgrade those pages to LLM OCR
- Use `force_ocr=true` only when the caller explicitly accepts the cost and latency of visual re-recognition for the whole document
- Local PaddleOCR runs in an isolated server-side worker process. Worker crashes do not crash the main service and do not automatically fall back to LLM OCR by default
- If OCR fails in a provider-specific way, first inspect the service's active OCR model / vendor configuration before blaming the document itself

---

## 9. Security and Compliance

- Temporary copies of uploaded files are automatically cleaned up after parsing
- Document library is physically isolated by `DOCS_DIR` directory
- Provenance tracking: Each document's manifest contains provenance (created_at, content_hash, source_ref when available)
- If `MANTISFETCH_STORE_SOURCE_FILES=true` (default), the original uploaded file is stored under each document's `source/` directory for later reference
