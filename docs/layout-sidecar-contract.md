# Layout Sidecar Contract

Date: 2026-05-04

## Purpose

LarkScout layout sidecars store low-level OCR geometry for scanned PDFs and image-like pages. The sidecar is intended for table reconstruction, source evidence lookup, region crop export, and targeted region re-recognition.

Core must keep this layer generic. It must not add business semantics such as invoice number, contract amount, buyer, seller, payment terms, or material type.

## OCR Blocks Sidecar

Canonical path:

```text
ocr_blocks.json
```

Coordinate system:

```text
image_pixels
```

Bounding boxes use `[x0, y0, x1, y1]` in rendered page image pixels, with origin at the top-left of the rendered page image.

Schema:

```json
{
  "version": 1,
  "doc_id": "DOC-001",
  "coordinate_system": "image_pixels",
  "pages": [
    {
      "page": 1,
      "width": 2480,
      "height": 3508,
      "blocks": [
        {
          "block_id": "p1-b0001",
          "text": "example",
          "bbox": [100.0, 220.0, 680.0, 260.0],
          "confidence": 0.94,
          "source": "local_ocr",
          "line_index": 12,
          "order": 12
        }
      ]
    }
  ]
}
```

## Manifest Discovery

Manifest metadata must stay low-token and must not inline OCR block payloads.

Shape:

```json
{
  "layout": {
    "available": true,
    "ocr_blocks_path": "ocr_blocks.json",
    "version": 1,
    "coordinate_system": "image_pixels"
  }
}
```

For documents without OCR geometry, `available` is false and `ocr_blocks_path` is an empty string.

## Compatibility Rules

- Existing digest, brief, full, sections, section, table, and manifest APIs must remain compatible.
- Default APIs must not inline `pages` or `blocks` from `ocr_blocks.json`.
- Business-specific extraction belongs in Skills, not this contract.
- Future table sidecars should reference `block_id` values rather than duplicating all geometry.

