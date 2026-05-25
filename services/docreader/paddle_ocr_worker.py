#!/usr/bin/env python3
"""Isolated PaddleOCR JSONL worker.

The parent process communicates over stdin/stdout using one JSON object per
line. All third-party stdout noise is redirected to stderr so protocol output
stays parseable.
"""

from __future__ import annotations

import base64
import importlib.metadata
import io
import json
import os
import sys
from typing import Any

_protocol_out: Any | None = None


def _setup_protocol_output() -> None:
    global _protocol_out
    if _protocol_out is None:
        _protocol_out = os.fdopen(os.dup(sys.stdout.fileno()), "w", buffering=1, encoding="utf-8")
        sys.stdout = sys.stderr


def _write(message: dict[str, Any]) -> None:
    if _protocol_out is None:
        _setup_protocol_output()
    _protocol_out.write(json.dumps(message, ensure_ascii=False) + "\n")
    _protocol_out.flush()


def _bbox_from_geometry(value: Any) -> list[float]:
    if value is None:
        return [0.0, 0.0, 0.0, 0.0]
    if hasattr(value, "tolist"):
        value = value.tolist()
    if isinstance(value, (list, tuple)) and len(value) == 4 and all(
        isinstance(v, (int, float)) for v in value
    ):
        x0, y0, x1, y1 = [float(v) for v in value]
        return [min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)]

    points: list[tuple[float, float]] = []

    def collect(obj: Any) -> None:
        if hasattr(obj, "tolist"):
            obj = obj.tolist()
        if (
            isinstance(obj, (list, tuple))
            and len(obj) >= 2
            and isinstance(obj[0], (int, float))
            and isinstance(obj[1], (int, float))
        ):
            points.append((float(obj[0]), float(obj[1])))
            return
        if isinstance(obj, (list, tuple)):
            for child in obj:
                collect(child)

    collect(value)
    if not points:
        return [0.0, 0.0, 0.0, 0.0]
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return [min(xs), min(ys), max(xs), max(ys)]


def _as_sequence(value: Any) -> list[Any]:
    if value is None:
        return []
    if hasattr(value, "tolist"):
        value = value.tolist()
    if isinstance(value, (list, tuple)):
        return list(value)
    return []


def _first_sequence(block: dict[str, Any], keys: tuple[str, ...]) -> list[Any]:
    for key in keys:
        values = _as_sequence(block.get(key))
        if values:
            return values
    return []


def _extract_paddle_ocr_blocks(result: Any) -> list[dict[str, Any]]:
    extracted: list[dict[str, Any]] = []
    blocks = result if isinstance(result, list) else [result]
    for block in blocks:
        if isinstance(block, dict):
            texts = _as_sequence(block.get("rec_texts"))
            scores = _first_sequence(block, ("rec_scores", "scores"))
            boxes = _first_sequence(block, ("rec_boxes", "rec_polys", "dt_polys", "boxes"))
            for index, text in enumerate(texts):
                value = str(text).strip()
                if value:
                    score = scores[index] if index < len(scores) else 0.0
                    box = boxes[index] if index < len(boxes) else None
                    extracted.append(
                        {
                            "text": value,
                            "bbox": _bbox_from_geometry(box),
                            "confidence": float(score or 0.0),
                            "line_index": len(extracted),
                            "order": len(extracted),
                        }
                    )
            continue
        if isinstance(block, list):
            for item in block:
                if not isinstance(item, (list, tuple)) or len(item) < 2:
                    continue
                box = item[0] if item else None
                payload = item[1]
                if isinstance(payload, (list, tuple)) and payload:
                    text = str(payload[0]).strip()
                    score = float(payload[1] or 0.0) if len(payload) > 1 else 0.0
                else:
                    text = str(payload).strip()
                    score = 0.0
                if text:
                    extracted.append(
                        {
                            "text": text,
                            "bbox": _bbox_from_geometry(box),
                            "confidence": score,
                            "line_index": len(extracted),
                            "order": len(extracted),
                        }
                    )
    return extracted


def _flatten_paddle_ocr_result(result: Any) -> str:
    lines: list[str] = []
    for block in _extract_paddle_ocr_blocks(result):
        value = str(block.get("text") or "").strip()
        if value:
            lines.append(value)
    return "\n".join(lines).strip()


def _build_engine():
    os.environ.setdefault("FLAGS_enable_pir_api", "0")
    os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")

    from paddleocr import PaddleOCR

    v2_kwargs: dict[str, Any] = {
        "use_angle_cls": False,
        "lang": os.environ.get("LARKSCOUT_LOCAL_OCR_LANG", "ch"),
        "show_log": False,
    }
    try:
        major = int(importlib.metadata.version("paddleocr").split(".", 1)[0])
    except Exception:
        major = 0
    if major and major < 3:
        return PaddleOCR(**v2_kwargs), "v2"

    v3_kwargs: dict[str, Any] = {
        "use_doc_orientation_classify": False,
        "use_doc_unwarping": False,
        "use_textline_orientation": False,
        "text_detection_model_name": os.environ.get(
            "LARKSCOUT_LOCAL_OCR_DET_MODEL", "PP-OCRv5_mobile_det"
        ),
        "text_recognition_model_name": os.environ.get(
            "LARKSCOUT_LOCAL_OCR_REC_MODEL", "PP-OCRv5_mobile_rec"
        ),
    }
    if os.environ.get("LARKSCOUT_LOCAL_OCR_ENABLE_HPI", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        v3_kwargs["enable_hpi"] = True
    device = os.environ.get("LARKSCOUT_LOCAL_OCR_DEVICE", "").strip()
    if device:
        v3_kwargs["device"] = device
    try:
        engine = PaddleOCR(**v3_kwargs)
    except TypeError:
        return PaddleOCR(**v2_kwargs), "v2"
    if hasattr(engine, "predict"):
        return engine, "v3"
    return PaddleOCR(**v2_kwargs), "v2"


def _predict(engine: Any, api_version: str, image_array: Any) -> Any:
    if api_version == "v3":
        return engine.predict(
            image_array,
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
        )
    return engine.ocr(image_array, cls=False)


def main() -> int:
    _setup_protocol_output()
    try:
        import numpy as np
        from PIL import Image

        engine, api_version = _build_engine()
    except BaseException as exc:
        _write({"type": "error", "error": f"{type(exc).__name__}: {exc}"})
        return 2

    _write({"type": "ready"})

    for line in sys.stdin:
        try:
            request = json.loads(line)
            page_num = int(request.get("page_num") or 0)
            image_bytes = base64.b64decode(str(request.get("image_b64") or ""), validate=True)
            image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
            result = _predict(engine, api_version, np.asarray(image))
            blocks = _extract_paddle_ocr_blocks(result)
            text = _flatten_paddle_ocr_result(result)
            _write(
                {
                    "ok": True,
                    "page_num": page_num,
                    "text": text,
                    "width": image.width,
                    "height": image.height,
                    "blocks": blocks,
                }
            )
        except BaseException as exc:
            _write(
                {
                    "ok": False,
                    "page_num": int(request.get("page_num") or 0) if "request" in locals() else 0,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
