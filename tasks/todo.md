# LarkScout Scanned Table / OCR Geometry Execution Checklist

Date: 2026-05-04

## Scope

Implement the next LarkScout pass for scanned contracts, invoices, quotations, and similar enterprise PDFs. The goal is to improve low-level document structure fidelity, especially OCR geometry and tables, without adding business semantics to core.

Reference project to study: https://github.com/opendataloader-project/opendataloader-pdf

Use OpenDataLoader as a design reference for element-level bounding boxes, Markdown+JSON dual output, reading order, table structure, and visual debug output. Do not make it the immediate production backend.

## Execution Checklist

### P0.1 Define Layout Sidecar Contract

- [x] Task: Define the stable `ocr_blocks.json` schema and manifest metadata shape.

说明:
Create a low-level OCR geometry sidecar contract that can support table reconstruction, evidence lookup, and region re-recognition. Keep it generic: text, bbox, confidence, page size, source, and stable block IDs. Do not include business labels such as invoice number, contract amount, buyer, seller, or payment terms.

AC:
- [x] `ocr_blocks.json` has `version`, `doc_id`, and `pages`.
- [x] Each page has `page`, `width`, `height`, and `blocks`.
- [x] Each block has stable `block_id`, `text`, `bbox`, `confidence`, `source`, and ordering metadata.
- [x] Coordinate system is explicitly documented as image pixels or PDF points.
- [x] Manifest stores only path, availability, version, and coordinate system.
- [x] Default digest/brief/section APIs do not inline OCR blocks.

### P0.2 Capture PaddleOCR Geometry

- [x] Task: Preserve PaddleOCR line/block coordinates and confidence during local OCR.

说明:
Current scan OCR mostly produces text for downstream sectioning. Extend the OCR path so raw OCR geometry is normalized and written to the sidecar. This is the foundation for table reconstruction and page/region evidence.

AC:
- [x] Local scan OCR writes `ocr_blocks.json` for pages that run OCR.
- [x] Text content in generated sections remains compatible with current behavior.
- [x] Raw OCR cache files remain usable and are not treated as the structured source of truth.
- [x] Blank or skipped OCR pages are represented clearly or omitted with manifest metadata.
- [x] Existing scanned contract tests still pass.

### P0.3 Add Manifest Layout Metadata

- [x] Task: Add manifest `layout` metadata for OCR block sidecars.

说明:
Expose sidecar availability without increasing token usage in normal APIs. Consumers should be able to discover whether geometry exists and where it is stored.

AC:
- [x] Manifest includes layout availability for scan OCR outputs.
- [x] Manifest remains backward compatible for old documents without layout metadata.
- [x] API responses that expose manifest do not include the full block payload.
- [x] Unit tests cover documents with and without `ocr_blocks.json`.

### P0.4 Table Metadata Compatibility Layer

- [x] Task: Add generic table metadata while preserving current Markdown table behavior.

说明:
LarkScout should continue serving existing table Markdown, but each table should gain stable metadata such as page range, row count, column count, source, and continuation links when detectable.

AC:
- [x] Existing `/library/{doc_id}/table/{table_id}` behavior is preserved.
- [x] Table records include `table_id`, `page_start`, `page_end`, `row_count`, `column_count`, and `source`.
- [x] Metadata supports `continued_from` and `continued_to`.
- [x] Table body is not duplicated back into normal section text.
- [x] Existing table-related tests still pass.

### P0.5 Markdown Table Row/Column Counting

- [x] Task: Implement reliable row and column counting for existing Markdown tables.

说明:
Before reconstructing scanned tables, strengthen metadata for tables already represented as Markdown. This gives a low-risk compatibility layer and better baseline metrics.

AC:
- [x] Markdown tables with separator rows are counted correctly.
- [x] Empty cells and uneven rows are handled conservatively.
- [x] Header row detection is captured where obvious.
- [x] Unit tests include normal, empty-cell, and uneven-row tables.

### P1.1 Scanned Table Candidate Detection

- [x] Task: Detect table-like regions from OCR block geometry.

说明:
Use generic layout signals such as repeated x positions, aligned y bands, dense numeric/text grids, and optional line/border evidence. Keep the detector document-agnostic.

AC:
- [x] Detector returns candidate table regions with page, bbox, confidence, and block refs.
- [x] Candidate detection does not create business-specific table labels.
- [x] Non-table paragraphs and section headings are not over-classified in basic samples.
- [x] Detection output can be disabled or kept sidecar-only.

### P1.2 Reconstruct Rows and Columns

- [x] Task: Build first-pass scanned table reconstruction from OCR blocks.

说明:
Cluster OCR blocks into rows by y overlap and infer columns from x alignment. Preserve source block references for every cell so downstream Skills can inspect evidence.

AC:
- [x] Structured table JSON is written under `tables/table-xx.json`.
- [x] Each cell has row, column, text, bbox, confidence, and OCR block refs.
- [x] Generated Markdown is available for low-token LLM usage.
- [x] Multi-line cells are merged when geometry strongly supports it.
- [x] Unit tests cover row clustering and column inference.

### P1.3 Cross-Page Table Continuation

- [x] Task: Add heuristic continuation links for tables split across pages.

说明:
Enterprise contracts and quotations often split line-item tables across pages. Detect likely continuation using adjacent page positions, compatible column structure, repeated headers, and missing/continued titles.

AC:
- [x] Table metadata can link `continued_from` and `continued_to`.
- [x] Continuation logic is conservative and avoids merging unrelated tables.
- [x] Tests include a positive multi-page sample and a negative adjacent-table sample.

### P1.4 Region Crop Export

- [x] Task: Implement page and bbox crop export for inspection and downstream Skills.

说明:
Skills need a low-level way to inspect or reprocess a document area without core deciding what the region means. Crops should be traceable to source document, page, bbox, and render settings.

AC:
- [x] API or internal helper can export a crop by `doc_id`, `page`, and `bbox`.
- [x] Crop metadata records source page, bbox, coordinate system, DPI, and output path.
- [x] Invalid bbox/page inputs return clear errors.
- [x] Crop files are stored separately from canonical parse outputs or clearly marked as derived artifacts.

### P1.5 Region Re-OCR

- [x] Task: Add targeted re-OCR by page and bbox.

说明:
When a field, table cell, or blurred area is weak, Skills should be able to request re-recognition of a specific region. LarkScout should provide the generic operation and traceable output only.

AC:
- [x] Re-OCR accepts page+bbox and OCR backend parameters.
- [x] Output is stored as a separate rerun artifact with source refs.
- [x] Existing document outputs are not silently overwritten.
- [x] Re-OCR result includes text, bbox, confidence where available, and backend metadata.
- [x] Tests cover successful rerun and invalid region handling.

### P1.6 Visual Debug Artifact

- [x] Task: Add optional annotated visual debug output for OCR blocks and tables.

说明:
Borrow the OpenDataLoader idea of visual structure verification. A developer should be able to see detected blocks, table regions, and cell boxes overlaid on page images.

AC:
- [x] Debug output is opt-in.
- [x] Annotated output marks OCR blocks and table regions distinctly.
- [x] Debug artifact location is recorded in metadata when generated.
- [x] Debug output is not returned by default APIs.

### P1.7 API Discovery Endpoints

- [x] Task: Expose low-token discovery for layout and table sidecars.

说明:
Consumers need to discover available sidecars and request specific artifacts without pulling large payloads by default.

AC:
- [x] Existing digest, brief, full, sections, section, table, and manifest APIs remain compatible.
- [x] New layout/table sidecar access is explicit and bounded.
- [x] Large geometry payloads require targeted calls or local file access.
- [x] Response sizes remain stable for default endpoints.

### P2.1 OpenDataLoader Comparative Spike

- [x] Task: Run a limited comparison against OpenDataLoader on selected PDFs.

说明:
Use this as a learning spike, not a backend migration. Compare element JSON, bbox conventions, table output, Markdown fidelity, and debug artifacts against LarkScout outputs.

AC:
- [x] Comparison uses at least one scanned contract, one invoice-like table, and one text-rich PDF.
- [x] Notes identify reusable data-model ideas and non-reusable dependency/runtime choices.
- [x] Findings are documented in `docs/` without changing production defaults.
- [x] No new required Java/OpenDataLoader runtime dependency is introduced by this spike.

### P2.2 Performance Guardrails

- [x] Task: Add performance and payload-size checks for sidecar generation.

说明:
OCR geometry can become large. The implementation must preserve LarkScout's low-token default behavior and avoid large response payload regressions.

AC:
- [x] Default API payload sizes do not materially increase.
- [x] Sidecar generation cost is measured on representative scanned PDFs.
- [x] Large documents avoid inlining geometry in manifest or section outputs.
- [x] Tests or scripts capture basic size/performance metrics.

### P2.3 Real Document Validation Batch

- [x] Task: Validate on known real scanned contract samples.

说明:
Use existing local samples such as NBS220667, NBS220952, NBS230310, and NBS250523 to verify that geometry and table output improves practical downstream extraction.

AC:
- [x] Reparse selected samples and confirm OCR/page counts remain sane.
- [x] Confirm blank-page behavior still works.
- [x] Confirm table sidecars exist where table-like regions are present.
- [x] Confirm section text quality does not regress on known corrected OCR noise cases.
- [x] Save sample findings and remaining gaps in `docs/`.

Execution:
- [x] Inventory available local samples and note substitutions for missing suggested IDs.
- [x] Run a bounded real-document validation batch in `/private/tmp` without mutating the production docs library.
- [x] Capture per-document page, OCR, layout sidecar, table sidecar, blank-page, and text-quality observations.
- [x] Add focused regression tests for the validation/reporting checks.
- [x] Document findings and remaining gaps under `docs/`.

## Review Checklist

- [x] Root cause addressed: table failures are tackled via geometry and structure, not business-field hacks.
- [x] Simplicity checked: sidecars are explicit and low-token APIs remain stable.
- [x] Compatibility checked: existing APIs and tests continue to pass.
- [x] Elegance checked: data model is generic enough for contracts, invoices, quotations, and statements.
- [x] Verification complete: unit, regression, sample-document, and payload-size checks are run before marking done.

## Review Notes

### P0.1 Layout Sidecar Contract

- Branch: `task/p0-1-layout-sidecar-contract`
- Implementation:
  - Added normalized OCR geometry dataclasses and manifest discovery helper.
  - Added `docs/layout-sidecar-contract.md`.
  - Added `tests/test_layout_sidecar_contract.py`.
- Verification:
  - `.venv/bin/pytest tests/test_layout_sidecar_contract.py -q`: 5 passed.
  - `.venv/bin/pytest tests/test_schema_consistency.py tests/test_library_endpoints.py -q`: 48 passed.
  - `.venv/bin/pytest`: 216 passed, 15 skipped.

### P0.2 Capture PaddleOCR Geometry

- Branch: `task/p0-2-capture-paddleocr-geometry`
- Implementation:
  - Extended the isolated PaddleOCR worker to return text blocks with bbox/confidence.
  - Added `local_ocr_with_layout` while keeping existing `local_ocr` text behavior compatible.
  - Threaded local OCR page blocks through PDF parsing into `ParsedDocument.ocr_blocks`.
  - Wrote `ocr_blocks.json` and low-token manifest `layout` metadata during output persistence.
  - Reset stale `ocr_blocks.json` on document rewrite.
- Verification:
  - `.venv/bin/pytest tests/test_layout_sidecar_contract.py tests/test_robustness.py::TestPDFParse::test_local_ocr_uses_isolated_worker tests/test_robustness.py::TestPDFParse::test_local_ocr_worker_crash_does_not_crash_parent -q`: 12 passed.
  - `.venv/bin/pytest tests/test_schema_consistency.py tests/test_library_endpoints.py -q`: 48 passed.
  - `.venv/bin/pytest tests/test_word_embedded_images.py -q`: 6 passed.
  - `.venv/bin/pytest`: 221 passed, 15 skipped.

### P0.3 Manifest Layout Metadata

- Branch: `task/p0-3-manifest-layout-metadata`
- Implementation:
  - Added explicit coverage for manifest `layout.available=false` when no OCR geometry sidecar exists.
  - Confirmed legacy manifest endpoint compatibility remains covered by library endpoint fixtures without `layout`.
- Verification:
  - `.venv/bin/pytest tests/test_layout_sidecar_contract.py tests/test_schema_consistency.py tests/test_library_endpoints.py -q`: 59 passed.
  - `.venv/bin/pytest tests/test_word_embedded_images.py -q`: 6 passed.
  - `.venv/bin/pytest`: 222 passed, 15 skipped.

### P0.4 Table Metadata Compatibility Layer

- Branch: `task/p0-4-table-metadata-compat`
- Implementation:
  - Added generic table metadata fields while preserving Markdown table files and existing table endpoint behavior.
  - Added conservative Markdown table dimension helper for row/column/header metadata.
  - Added `source`, `continued_from`, and `continued_to` fields to table records.
- Verification:
  - `.venv/bin/pytest tests/test_table_metadata.py tests/test_library_endpoints.py tests/test_robustness.py::TestPDFParse::test_extract_tables_from_ocr_text_keeps_table_complete tests/test_robustness.py::TestPDFParse::test_split_sections_does_not_treat_table_rows_as_headings -q`: 40 passed.
  - `.venv/bin/pytest tests/test_schema_consistency.py -q`: 14 passed.
  - `.venv/bin/pytest`: 226 passed, 15 skipped.

### P0.5 Markdown Table Row/Column Counting

- Branch: `task/p0-5-markdown-table-counting`
- Implementation:
  - Added explicit `has_header` metadata.
  - Added separator/alignment/no-header/separator-only edge case tests.
  - Kept row count conservative: counts content rows including header, excludes separator rows.
- Verification:
  - `.venv/bin/pytest tests/test_table_metadata.py tests/test_schema_consistency.py -q`: 21 passed.
  - `.venv/bin/pytest tests/test_library_endpoints.py -q`: 34 passed.
  - `.venv/bin/pytest`: 229 passed, 15 skipped.

### P1.1 Scanned Table Candidate Detection

- Branch: `task/p1-1-scanned-table-candidates`
- Implementation:
  - Added a conservative OCR geometry detector that clusters blocks into rows and x-position columns.
  - Detector returns generic candidates with page, bbox, row/column counts, confidence, source, and OCR block refs.
  - Detector is helper-only and sidecar-compatible; it is not wired into default APIs.
- Verification:
  - `.venv/bin/pytest tests/test_table_candidates.py tests/test_table_metadata.py -q`: 10 passed.
  - `.venv/bin/pytest tests/test_layout_sidecar_contract.py -q`: 11 passed.
  - `.venv/bin/pytest`: 232 passed, 15 skipped.

### P1.2 Reconstruct Rows and Columns

- Branch: `task/p1-2-reconstruct-scanned-tables`
- Implementation:
  - Added reconstruction from OCR geometry candidates into structured table JSON.
  - Generated Markdown table files from reconstructed rows/cells.
  - Added `json_file`, bbox, and OCR block refs to layout-derived table entries.
  - Integrated layout-derived table sidecars into existing `_write_tables` output while keeping Markdown table API compatible.
- Verification:
  - `.venv/bin/pytest tests/test_table_candidates.py tests/test_table_metadata.py -q`: 12 passed.
  - `.venv/bin/pytest tests/test_library_endpoints.py tests/test_schema_consistency.py -q`: 48 passed.
  - `.venv/bin/pytest`: 234 passed, 15 skipped.

### P1.3 Cross-Page Table Continuation

- Branch: `task/p1-3-cross-page-continuation`
- Implementation:
  - Added conservative continuation helpers for adjacent layout-derived tables.
  - Linked `continued_from` and `continued_to` in table metadata and structured table JSON.
  - Required compatible column count, horizontal alignment, and either repeated headers or page-edge continuation positioning.
- Verification:
  - `.venv/bin/pytest tests/test_table_candidates.py tests/test_table_metadata.py -q`: 14 passed.
  - `.venv/bin/pytest tests/test_library_endpoints.py tests/test_schema_consistency.py -q`: 48 passed.
  - `.venv/bin/pytest`: 236 passed, 15 skipped.

### P1.4 Region Crop Export

- Branch: `task/p1-4-region-crop-export`
- Implementation:
  - Added `export_pdf_region_crop` for source-backed PDF page+bbox crop export.
  - Stored crop PNGs and metadata under `derived/crops/` to keep canonical parse outputs separate.
  - Supported both `page_points` and OCR sidecar `image_pixels` coordinate systems.
  - Added clear errors for invalid pages, bbox geometry, DPI, missing source files, and unavailable OCR dimensions.
- Verification:
  - `.venv/bin/pytest tests/test_region_crop_export.py -q`: 4 passed.
  - `.venv/bin/pytest tests/test_library_endpoints.py tests/test_schema_consistency.py -q`: 48 passed.
  - `.venv/bin/pytest`: 240 passed, 15 skipped.

### P1.5 Region Re-OCR

- Branch: `task/p1-5-region-reocr`
- Implementation:
  - Added `rerun_region_ocr` on top of derived PDF crop export.
  - Stored rerun text and metadata under `derived/region_ocr/` without touching canonical parse outputs.
  - Included source refs, crop refs, backend metadata, recognized text, OCR blocks, and average confidence where available.
  - Supported local PaddleOCR and LLM/Gemini backend selection.
- Verification:
  - `.venv/bin/pytest tests/test_region_crop_export.py -q`: 6 passed.
  - `.venv/bin/pytest tests/test_library_endpoints.py tests/test_schema_consistency.py -q`: 48 passed.
  - `.venv/bin/pytest`: 242 passed, 15 skipped.

### P1.6 Visual Debug Artifact

- Branch: `task/p1-6-visual-debug-artifact`
- Implementation:
  - Added opt-in `generate_visual_debug_artifacts` for annotated page PNGs.
  - Marked OCR blocks with blue rectangles and table regions with translucent orange rectangles.
  - Wrote debug images and `derived/debug/manifest.json` with artifact locations, legend, options, and page counts.
  - Kept default document manifest/API output unchanged unless the helper is explicitly invoked.
- Verification:
  - `.venv/bin/pytest tests/test_region_crop_export.py -q`: 7 passed.
  - `.venv/bin/pytest tests/test_library_endpoints.py tests/test_schema_consistency.py -q`: 48 passed.
  - `.venv/bin/pytest`: 243 passed, 15 skipped.

### P1.7 API Discovery Endpoints

- Branch: `task/p1-7-sidecar-discovery-api`
- Implementation:
  - Added `GET /library/{doc_id}/sidecars` for low-token sidecar discovery.
  - Added bounded layout endpoints for page summaries and one-page OCR geometry retrieval.
  - Added explicit structured table JSON endpoint without changing Markdown table access.
  - Kept default manifest, digest, brief, full, section, and table responses compatible.
- Verification:
  - `.venv/bin/pytest tests/test_library_endpoints.py::TestLibrarySidecars -q`: 2 passed.
  - `.venv/bin/pytest tests/test_library_endpoints.py tests/test_schema_consistency.py -q`: 50 passed.
  - `.venv/bin/pytest`: 245 passed, 15 skipped.

### P2.1 OpenDataLoader Comparative Spike

- Branch: `task/p2-1-opendataloader-comparison-spike`
- Implementation:
  - Added `docs/opendataloader-comparative-spike.md`.
  - Selected one scanned contract, one invoice-like table sample, and one text-rich PDF from the local LarkScout document library.
  - Compared LarkScout outputs against OpenDataLoader's public README, schema, and options.
  - Recorded that OpenDataLoader runtime execution was blocked by missing Java and missing `opendataloader-pdf` package; no dependency was added.
- Verification:
  - `java -version`: failed, no Java Runtime available.
  - `python3 -m pip show opendataloader-pdf`: package not found.
  - `find /Users/grace/.larkscout/docs -maxdepth 3 -type f -name '*.pdf' -print`: local sample PDFs found.
  - `git diff --check`: passed.
  - `.venv/bin/pytest`: 245 passed, 15 skipped.

### P2.2 Performance Guardrails

- Branch: `task/p2-2-sidecar-performance-guardrails`
- Implementation:
  - Added `scripts/sidecar_metrics.py` for default payload and sidecar size metrics.
  - Added tests that detect default-payload geometry regressions.
  - Added `docs/sidecar-performance-guardrails.md` with representative local sample metrics.
  - Measured a real scanned PDF sidecar run in `/private/tmp/larkscout-sidecar-metrics`.
  - Recorded that the broader selected local documents are pre-sidecar outputs; P2.3 should reparse a wider batch.
- Verification:
  - `.venv/bin/pytest tests/test_sidecar_metrics.py -q`: 2 passed.
  - `python3 scripts/sidecar_metrics.py /Users/grace/.larkscout/docs NBS250932 NBS260336 DOC-020`: 3 docs measured, no default geometry inline.
  - `parse_pdf` + `write_output_extract_only` for `NBS260336.pdf`: 68.118s, 5 OCR pages, 4 local OCR sidecar pages, 254 OCR blocks, LLM page fell back after network connection error.
  - `python3 scripts/sidecar_metrics.py /private/tmp/larkscout-sidecar-metrics METRIC-NBS260336-P1`: OCR sidecar 95,690 bytes, default payload 45,652 bytes, no default geometry inline.
  - `.venv/bin/pytest`: 247 passed, 15 skipped.

### P2.3 Real Document Validation Batch

- Branch: `task/p2-3-real-document-validation-batch`
- Implementation:
  - Added `scripts/real_doc_validation.py` for repeatable real-output checks.
  - Added tests for sidecar/table success and blank/noise/default-payload regression detection.
  - Added `docs/real-document-validation-batch.md` with sample substitutions, commands, findings, and gaps.
  - Confirmed suggested IDs `NBS220667`, `NBS220952`, and `NBS230310` were not present locally; used `NBS250523`, `NBS260336`, and existing `NBS250932` baseline.
- Verification:
  - `.venv/bin/pytest tests/test_real_doc_validation.py -q`: 2 passed.
  - `python3 scripts/real_doc_validation.py /Users/grace/.larkscout/docs NBS250523 NBS250932 NBS260336 --expect-table NBS260336`: expected pre-sidecar gap for `NBS260336` structured table sidecar.
  - `parse_pdf` + `write_output_extract_only` for `NBS260336.pdf` with `manual_blank_pages=5`: 70.706s, 5 pages, 4 OCR pages, sidecar available.
  - `parse_pdf` + `write_output_extract_only` for `NBS250523.pdf`: 61.462s, 9 pages, 9 OCR pages, sidecar available.
  - `python3 scripts/real_doc_validation.py /private/tmp/larkscout-real-validation VAL-NBS250523 VAL-NBS260336-BLANK5 --expect-table VAL-NBS260336-BLANK5`: passed.
  - `python3 scripts/sidecar_metrics.py /private/tmp/larkscout-real-validation VAL-NBS250523 VAL-NBS260336-BLANK5`: OCR sidecars 201,825 bytes total, 535 OCR blocks total, no default geometry inline.
  - `.venv/bin/pytest`: 249 passed, 15 skipped.
