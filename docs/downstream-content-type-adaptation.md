# MantisFetch Content Type Adaptation for Downstream Apps

Date: 2026-05-10

Audience:
- `contract-manage` developers
- `bid-manage` developers

## Summary

MantisFetch now supports categorized document-library storage. New ingestion calls can specify `content_type`; MantisFetch stores the document under a category directory and records the path in `doc-index.json` and `manifest.json`.

Supported values:

```text
General
Contract
Bid
Knowledge
```

Default:

```text
General
```

New physical layout:

```text
${MANTISFETCH_DOCS_DIR}/General/<doc_id>
${MANTISFETCH_DOCS_DIR}/Contract/<doc_id>
${MANTISFETCH_DOCS_DIR}/Bid/<doc_id>
${MANTISFETCH_DOCS_DIR}/Knowledge/<doc_id>
```

Legacy flat documents remain readable:

```text
${MANTISFETCH_DOCS_DIR}/<doc_id>
```

## Shared Contract

### Ingestion API

`POST /doc/parse` accepts multipart form field:

```text
content_type=General|Contract|Bid|Knowledge
```

Values are case-insensitive on input (`"bid"`, `"BID"`, and `"Bid"` are all accepted), but always persisted as the title-case form shown above. Unknown values return HTTP 422.

`POST /web/capture` accepts JSON field:

```json
{
  "content_type": "Knowledge"
}
```

Responses now include:

```json
{
  "content_type": "Contract",
  "storage_path": "Contract/DOC-001",
  "manifest_path": "docs/Contract/DOC-001/manifest.json"
}
```

### Index and Manifest

`doc-index.json`, `.meta.json`, and `manifest.json` include:

```json
{
  "content_type": "Contract",
  "storage_path": "Contract/DOC-001"
}
```

Downstream apps must stop assuming:

```text
doc_dir = docs_dir / doc_id
```

Preferred resolution:

1. Load `doc-index.json`.
2. Find entry where `id == doc_id`.
3. If `storage_path` exists, use `docs_dir / storage_path`.
4. Otherwise, try category locations in this order: `General`, `Contract`, `Bid`, `Knowledge`.
5. Finally fall back to legacy `docs_dir / doc_id`.

Direct MantisFetch HTTP reads are unchanged:

```http
GET /doc/library/{doc_id}/manifest
GET /doc/library/{doc_id}/digest
GET /doc/library/{doc_id}/brief
GET /doc/library/{doc_id}/sections
GET /doc/library/{doc_id}/section/{sid}
GET /doc/library/{doc_id}/table/{table_id}
GET /doc/library/{doc_id}/images
```

Search endpoints can filter by category:

```http
GET /doc/library/search?content_type=Contract
GET /doc/library/search_text?q=payment&content_type=Contract
```

If an app already reads by `doc_id`, no query change is required. Add `content_type` only for browsing or searching one business library.

## Contract Manage Adaptation

Use category:

```text
Contract
```

### Ingestion

When a contract file is uploaded through the contract-management flow, call MantisFetch with:

```bash
curl -sS --fail-with-body -X POST http://127.0.0.1:9898/doc/parse \
  -F "file=@/path/to/contract.pdf" \
  -F "content_type=Contract" \
  -F "summary_mode=defer" \
  -F "extract_tables=true" \
  -F 'tags=["合同"]' \
  -F 'metadata={"app":"contract-manage","source_role":"contract","display_name":"contract.pdf"}'
```

If `contract-manage` still only receives `docs_dir + doc_id`, its CLI arguments can stay unchanged. The internal document-store resolver must map `doc_id` to the categorized document directory.

### File Access

Update `contract_tools/doc_store.py` or equivalent path resolver:

```text
DOC-001 -> docs/Contract/DOC-001
DOC-001 -> docs/DOC-001            # fallback for legacy data
```

Derived artifacts should be written under the resolved document directory:

```text
docs/Contract/DOC-001/contracts/
```

not unconditionally under:

```text
docs/DOC-001/contracts/
```

### Query

No change for direct reads by `doc_id`.

For contract-library browsing or discovery, add:

```http
content_type=Contract
```

Example:

```http
GET /doc/library/search?q=付款&content_type=Contract
```

### Required Tests

- A categorized fixture: `docs/Contract/DOC-900/manifest.json`.
- A legacy fixture: `docs/DOC-901/manifest.json`.
- `status`, `inspect`, `prepare`, `review`, `export`, and any clause/search command should work for both fixtures.
- Verify generated artifacts are placed under the resolved doc directory.

## Bid Manage Adaptation

Use category:

```text
Bid
```

### Ingestion

For tender documents and Word bid files that should become official review inputs, call MantisFetch with:

```bash
curl -sS --fail-with-body -X POST http://127.0.0.1:9898/doc/parse \
  -F "file=@/path/to/bid.docx" \
  -F "content_type=Bid" \
  -F "document_profile=bid_cn" \
  -F "summary_mode=defer" \
  -F "extract_tables=true" \
  -F "extract_images=true" \
  -F "ocr_images=false" \
  -F "max_images=1000" \
  -F "id_strategy=source_filename" \
  -F 'tags=["投标文件"]' \
  -F 'metadata={"app":"bid-manage","source_role":"bid_file","display_name":"bid.docx"}'
```

For tender files, keep the same category and distinguish role in metadata:

```bash
-F "content_type=Bid" \
-F 'metadata={"app":"bid-manage","source_role":"tender","display_name":"tender.docx"}'
```

Keep the existing PDF bid-file boundary:

- PDF bid files / final exported PDFs are not ingested by default.
- Only ingest them when the user explicitly asks for archival/search use and accepts OCR/text extraction limitations.
- If explicitly archived, still use `content_type=Bid`, and set metadata such as `source_role=archival_pdf`.

### File Access

Update the Bid Manager MantisFetch document loader/path resolver:

```text
v2-1101-3 -> docs/Bid/v2-1101-3
v2-1101-3 -> docs/v2-1101-3       # fallback for legacy data
```

Any per-document outputs should use the resolved document directory as their base. Do not assume `docs_dir / doc_id`.

### Image Inventory

The inventory-first guidance is unchanged, with `content_type=Bid` added:

```bash
-F "content_type=Bid" \
-F "extract_images=true" \
-F "ocr_images=false" \
-F "max_images=1000"
```

Targeted OCR after candidate selection is unchanged, also adding `content_type=Bid` only if a new parse is performed:

```bash
-F "content_type=Bid" \
-F "ocr_images=true" \
-F "image_ocr_backend=local" \
-F "max_images=50" \
-F "max_ocr_images=50"
```

### Query

No change for direct reads by `doc_id`.

For tender/bid discovery, add:

```http
content_type=Bid
```

Examples:

```http
GET /doc/library/search?q=保证金&content_type=Bid
GET /doc/library/search_text?q=学历证明&content_type=Bid
```

### Required Tests

- A categorized tender fixture: `docs/Bid/TENDER-900/manifest.json`.
- A categorized bid fixture with image inventory: `docs/Bid/BID-900/images.json`.
- A legacy fixture: `docs/v2-1101-3/manifest.json`.
- `status`, `tender-analyze`, `tender-materials`, `review`, `review-materials`, and `review-material-images` should resolve both categorized and legacy documents.
- Verify image inventory/OCR loaders read from the resolved document directory.

## Compatibility Notes

- Existing `doc_id` values remain valid.
- Existing flat document libraries do not require migration.
- New MantisFetch writes go to categorized directories when `content_type` is supplied.
- If callers omit `content_type`, new documents go to `General`.
- Business apps should set `content_type` explicitly to avoid mixed libraries:
  - `contract-manage`: `Contract`
  - `bid-manage`: `Bid`
- Do not encode business semantics into MantisFetch metadata beyond caller-owned tags/metadata. MantisFetch remains the parsing, OCR, sidecar, and document-library layer.
