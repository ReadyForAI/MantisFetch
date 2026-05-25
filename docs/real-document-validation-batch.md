# Real Document Validation Batch

Date: 2026-05-04

## Scope

Suggested IDs in the plan were `NBS220667`, `NBS220952`, `NBS230310`, and `NBS250523`. Only `NBS250523` exists in the local `/Users/grace/.larkscout/docs` library during this run. Available substitutes used for validation:

- `NBS250523`: scanned contract sample, 9 pages
- `NBS260336`: scanned table/invoice-like sample, 5 pages
- `NBS250932`: existing scanned contract baseline, 18 pages, not reparsed in this bounded pass

All new validation outputs were written to `/private/tmp/larkscout-real-validation` or `/private/tmp/larkscout-sidecar-metrics`; the production docs library was not mutated.

## Commands

Focused checks:

```bash
.venv/bin/pytest tests/test_real_doc_validation.py -q
python3 scripts/real_doc_validation.py /private/tmp/larkscout-real-validation VAL-NBS250523 VAL-NBS260336-BLANK5 --expect-table VAL-NBS260336-BLANK5
python3 scripts/sidecar_metrics.py /private/tmp/larkscout-real-validation VAL-NBS250523 VAL-NBS260336-BLANK5
```

Baseline library check:

```bash
python3 scripts/real_doc_validation.py /Users/grace/.larkscout/docs NBS250523 NBS250932 NBS260336 --expect-table NBS260336
```

## Reparse Results

| Output doc | Source | Parse time | Pages | OCR pages | OCR sidecar pages | OCR blocks | Structured table JSON | Blank pages | Default geometry inline | Noise hits |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- | --- |
| `VAL-NBS250523` | `NBS250523.pdf` | 61.462s | 9 | 9 | 9 | 230 | 4 | none | false | none |
| `VAL-NBS260336-BLANK5` | `NBS260336.pdf` | 70.706s | 5 | 4 | 4 | 305 | 4 | `5` | false | none |
| `METRIC-NBS260336-P1` | `NBS260336.pdf` | 68.118s | 5 | 5 | 4 | 254 | 4 | none | false | none |

Notes:

- `VAL-NBS260336-BLANK5` used `manual_blank_pages=5`; page 5 appears in `blank_pages`, `near_blank_pages`, and `manual_blank_pages`, and is absent from OCR targets.
- Structured tables are sidecar JSON files under `tables/*.json`. They were generated for the table-like validation outputs.
- `table_count` in the final stored output includes layout-derived tables even when text-only OCR extraction reported `0` Markdown tables during parsing.
- `METRIC-NBS260336-P1` intentionally includes one LLM OCR fallback caused by network connection failure; local PaddleOCR geometry still produced the measured sidecar pages.

## Existing Library Baseline

| Existing doc | Pages | OCR pages | OCR sidecar | Structured table JSON | Validation status |
| --- | ---: | ---: | --- | ---: | --- |
| `NBS250523` | 9 | 9 | absent | 0 | pass for pre-sidecar baseline |
| `NBS250932` | 18 | 18 | absent | 0 | pass for pre-sidecar baseline |
| `NBS260336` | 5 | 5 | absent | 0 | expected gap: table-like sample lacks structured JSON sidecar until reparsed |

The old library outputs remain low-token and text-quality checks did not find known OCR cleanup regressions. They were parsed before the OCR geometry sidecar rollout, so absence of `ocr_blocks.json` is expected.

## AC Assessment

- OCR/page counts are sane for both reparsed samples.
- Blank-page handling is preserved under a real scanned PDF with manual blank-page override.
- Table-like reparsed outputs produce structured table JSON sidecars.
- Known OCR cleanup noise patterns were not found in `full.md`.
- Default manifest and section payloads do not inline OCR geometry.

## Remaining Gaps

- `NBS220667`, `NBS220952`, and `NBS230310` were not present locally, so they were not validated.
- `NBS250932` should be included in a longer future reparse batch because it is an 18-page scanned contract and would provide a broader geometry/table stress case.
- Natural blank-page validation is still needed; this run verifies the manual blank-page path on a real PDF.
