"""YOLO UI-component detection + Readability.js loading for the browser service.

Lazy-initialized vision subsystem: `_load_readability_js` / `_init_yolo` are
called once at startup and flip the module-level READABILITY_* / YOLO_* state;
`yolo_detect_ui_components` runs ONNX inference (letterbox -> decode -> NMS) to
find clickable UI boxes as a vision fallback for action extraction.

The READABILITY_*/YOLO_* globals are reassigned at startup, so readers in the
package __init__ access them as `vision.<NAME>` (submodule attribute) to see the
post-init values — a plain facade re-export would capture the pre-init snapshot.
"""

from __future__ import annotations

import json
import os
from io import BytesIO
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

BASE_DIR = Path(__file__).resolve().parent


# ---- Readability.js local file ----
READABILITY_JS_PATH = Path(os.getenv("READABILITY_JS_PATH", str(BASE_DIR / "readability.js")))
READABILITY_JS: str | None = None
READABILITY_AVAILABLE = False

# ---- YOLO (onnxruntime) ----
YOLO_ONNX_PATH = os.getenv("YOLO_ONNX_PATH", "")
YOLO_INPUT_SIZE = int(os.getenv("YOLO_INPUT_SIZE", "640"))
YOLO_CLASS_MAP_JSON = os.getenv(
    "YOLO_CLASS_MAP_JSON",
    '{"0":"button","1":"textbox","2":"checkbox","3":"link","4":"combobox"}',
)
try:
    YOLO_CLASS_MAP = {int(k): v for k, v in json.loads(YOLO_CLASS_MAP_JSON).items()}
except Exception:
    YOLO_CLASS_MAP = {0: "button", 1: "textbox"}

YOLO_ENABLED = False
YOLO_SESSION = None
YOLO_INPUT_NAME = None
YOLO_OUTPUT_NAMES = None


def _load_readability_js():
    global READABILITY_JS, READABILITY_AVAILABLE
    try:
        READABILITY_JS = READABILITY_JS_PATH.read_text(encoding="utf-8")
        READABILITY_AVAILABLE = True
    except Exception:
        READABILITY_JS = None
        READABILITY_AVAILABLE = False


# ============================================================
# YOLO init + decode helpers (onnxruntime)
# ============================================================
def _init_yolo():
    global YOLO_ENABLED, YOLO_SESSION, YOLO_INPUT_NAME, YOLO_OUTPUT_NAMES
    if not YOLO_ONNX_PATH:
        YOLO_ENABLED = False
        return
    try:
        import onnxruntime as ort

        YOLO_SESSION = ort.InferenceSession(YOLO_ONNX_PATH, providers=["CPUExecutionProvider"])
        YOLO_INPUT_NAME = YOLO_SESSION.get_inputs()[0].name
        YOLO_OUTPUT_NAMES = [o.name for o in YOLO_SESSION.get_outputs()]
        YOLO_ENABLED = True
    except Exception:
        YOLO_ENABLED = False
        YOLO_SESSION = None


def _letterbox(img: Image.Image, new_size: int = 640, color=(114, 114, 114)):
    w, h = img.size
    r = min(new_size / w, new_size / h)
    nw, nh = int(round(w * r)), int(round(h * r))
    img_resized = img.resize((nw, nh), Image.BILINEAR)

    canvas = Image.new("RGB", (new_size, new_size), color)
    pad_w = (new_size - nw) // 2
    pad_h = (new_size - nh) // 2
    canvas.paste(img_resized, (pad_w, pad_h))

    arr = np.asarray(canvas).astype(np.float32) / 255.0
    arr = np.transpose(arr, (2, 0, 1))
    arr = np.expand_dims(arr, 0)
    return arr, r, (pad_w, pad_h)


def _nms_xyxy(boxes: np.ndarray, scores: np.ndarray, iou_thresh: float) -> list[int]:
    x1, y1, x2, y2 = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
    areas = (x2 - x1 + 1) * (y2 - y1 + 1)
    order = scores.argsort()[::-1]
    keep = []

    while order.size > 0:
        i = order[0]
        keep.append(int(i))
        if order.size == 1:
            break
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])

        w = np.maximum(0.0, xx2 - xx1 + 1)
        h = np.maximum(0.0, yy2 - yy1 + 1)
        inter = w * h
        iou = inter / (areas[i] + areas[order[1:]] - inter + 1e-6)

        inds = np.where(iou <= iou_thresh)[0]
        order = order[inds + 1]

    return keep


def _decode_yolov8_like(out: np.ndarray, conf_thresh: float):
    if out.ndim == 3:
        out = out[0]
    pred = out.T
    boxes = pred[:, :4]
    cls_scores = pred[:, 4:]
    class_ids = np.argmax(cls_scores, axis=1)
    scores = cls_scores[np.arange(cls_scores.shape[0]), class_ids]

    mask = scores >= conf_thresh
    boxes, scores, class_ids = boxes[mask], scores[mask], class_ids[mask]

    cx, cy, w, h = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
    xyxy = np.stack([cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2], axis=1)
    return xyxy, scores, class_ids


def yolo_detect_ui_components(
    image_bytes: bytes,
    conf_thresh: float,
    iou_thresh: float,
    max_boxes: int,
) -> list[dict[str, Any]]:
    if not YOLO_ENABLED or YOLO_SESSION is None:
        return []

    img = Image.open(BytesIO(image_bytes)).convert("RGB")
    inp, ratio, (pad_w, pad_h) = _letterbox(img, YOLO_INPUT_SIZE)

    outputs = YOLO_SESSION.run(YOLO_OUTPUT_NAMES, {YOLO_INPUT_NAME: inp})
    xyxy, scores, class_ids = _decode_yolov8_like(outputs[0], conf_thresh=conf_thresh)
    if xyxy.size == 0:
        return []

    keep = _nms_xyxy(xyxy, scores, iou_thresh=iou_thresh)[:max_boxes]

    w0, h0 = img.size
    dets = []
    for i in keep:
        x1, y1, x2, y2 = xyxy[i]
        x1 = float(np.clip((x1 - pad_w) / ratio, 0, w0 - 1))
        y1 = float(np.clip((y1 - pad_h) / ratio, 0, h0 - 1))
        x2 = float(np.clip((x2 - pad_w) / ratio, 0, w0 - 1))
        y2 = float(np.clip((y2 - pad_h) / ratio, 0, h0 - 1))

        cid = int(class_ids[i])
        dets.append(
            {
                "bbox": [x1, y1, x2, y2],
                "class_id": cid,
                "type": YOLO_CLASS_MAP.get(cid, f"class_{cid}"),
                "score": float(scores[i]),
            }
        )
    return dets
