# Sidecar Performance Guardrails

Date: 2026-05-04

## Goal

OCR geometry and table sidecars can become large. Default LarkScout responses must stay low-token: `manifest`, `sections`, `digest`, `brief`, and Markdown table endpoints should not inline OCR block lists.

## Guardrail Script

Use:

```bash
python3 scripts/sidecar_metrics.py /Users/grace/.larkscout/docs NBS250932 NBS260336 DOC-020
```

The script reports:

- default payload file sizes for `manifest`, `digest`, `brief`, `full`, `sections`, and `tables`
- `ocr_blocks.json` bytes, page count, and block count
- derived artifact bytes for debug, crops, and region OCR
- whether manifest or sections contain large geometry payloads such as inline `blocks`
- collection time in milliseconds

## Baseline Library Run

Samples:

| Sample | Class | Default payload bytes | OCR sidecar bytes | Inline geometry in defaults |
| --- | --- | ---: | ---: | --- |
| `NBS250932` | scanned contract | 79,603 | 0 | false |
| `NBS260336` | invoice-like/table | 23,806 | 0 | false |
| `DOC-020` | text-rich PDF | 174,760 | 0 | false |

Aggregate:

- `doc_count`: 3
- default payload bytes: 278,199
- `ocr_blocks_bytes`: 0
- `ocr_block_count`: 0
- `large_geometry_in_default_payloads`: false

The selected local documents were parsed before the OCR geometry sidecar work and do not yet contain `ocr_blocks.json`. This baseline confirms older library outputs still keep defaults low-token; the scanned sidecar run below captures actual sidecar size for a reparse, and P2.3 should expand the same measurement across a wider real-document batch.

## Scanned Sidecar Run

Command:

```bash
python3 scripts/sidecar_metrics.py /private/tmp/larkscout-sidecar-metrics METRIC-NBS260336-P1
```

The measured document was generated from `NBS260336.pdf` on 2026-05-04 with local OCR geometry enabled. The parser ran in 68.118 seconds. One page attempted LLM OCR and fell back after a network connection error; four pages produced local PaddleOCR geometry.

Results:

| Sample | OCR sidecar bytes | OCR sidecar pages | OCR blocks | Default payload bytes | Structured table JSON bytes | Inline geometry in defaults |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| `METRIC-NBS260336-P1` | 95,690 | 4 | 254 | 45,652 | 43,490 | false |

This confirms the large geometry payload stays in `ocr_blocks.json`; default manifest and section outputs remain metadata-only for the scanned sidecar sample.

## Current Guardrail Status

- Default manifest and section outputs remain low-token for the sampled documents.
- P1.7 sidecar APIs keep geometry behind explicit endpoints.
- P1.6 visual debug, P1.5 region OCR, and P1.4 crops are derived artifacts, not default API payloads.
- The metrics script can flag regressions if future code accidentally inlines `blocks` into manifest or sections.

## Thresholds For Follow-Up

These are initial review thresholds, not hard product limits:

- `large_geometry_in_default_payloads` must remain `false`.
- `manifest` should contain sidecar paths and counts only, not page block arrays.
- `sections.json` should contain section metadata only, not OCR block arrays.
- `ocr_blocks.json` should remain a separate file and should be accessed through targeted APIs or local file access.
- For large scanned documents, record `ocr_blocks_bytes / page` and `ocr_block_count / page` during P2.3 validation.
