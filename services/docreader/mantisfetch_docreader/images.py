"""Embedded-image processing: render, hash, inventory, and OCR.

Operates on EmbeddedImage objects and raw image bytes — raster/vector → PNG
rendering, dimensions, average-hash dedup signal, context-keyword/inventory
hints, and per-image OCR. PIL is imported lazily inside the functions, matching
the rest of the package. The docx-specific image *extraction* (the _word_*
helpers and _extract_word_embedded_images) stays with the word parser; this is
the format-agnostic image-processing leaf.

_ocr_embedded_image pulls _cleanup_ocr_text from the package via a function-level
relative import (it stays in __init__, coupled to the table/heading classifiers).
"""

from __future__ import annotations

import hashlib
import io
import shutil
import subprocess
import tempfile
from pathlib import Path

from .models import EmbeddedImage
from .ocr.engines import _is_ocr_failed_text, gemini_ocr, local_ocr

_RASTER_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tif", ".tiff"}
_VECTOR_IMAGE_EXTENSIONS = {".emf", ".wmf"}

_IMAGE_CONTEXT_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("business_license", ("营业执照", "统一社会信用代码", "business license")),
    ("id_card", ("身份证", "居民身份证", "identity card", "id card")),
    ("education_certificate", ("学历", "毕业证", "毕业证书", "学信网", "电子注册备案表")),
    ("degree_certificate", ("学位证", "学位证书")),
    (
        "personnel_certificate",
        ("项目经理证书", "人员证书", "人员资质", "PMP", "信息系统项目管理师", "系统集成项目管理工程师", "软考"),
    ),
    ("certificate", ("证书", "资质", "认证")),
    ("contract_copy", ("合同复印件", "合同案例", "类似案例", "业绩证明", "协议复印件")),
    ("financial_statement", ("财务报表", "审计报告", "资产负债表", "利润表", "现金流量表")),
    ("product_screenshot", ("产品截图", "系统截图", "功能截图", "界面截图", "截图")),
    ("seal_or_signature", ("签字", "签章", "盖章", "公章", "印章")),
)

def _render_raster_image_to_png(image_bytes: bytes) -> tuple[bytes, str]:
    from PIL import Image

    with Image.open(io.BytesIO(image_bytes)) as img:
        if img.mode not in {"RGB", "RGBA"}:
            img = img.convert("RGB")
        out = io.BytesIO()
        img.save(out, format="PNG")
        return out.getvalue(), "ok"


def _convert_vector_image_to_png(image_bytes: bytes, original_ext: str) -> tuple[bytes, str]:
    binary = shutil.which("soffice") or shutil.which("libreoffice")
    if not binary:
        raise RuntimeError("office converter is not available for vector image conversion")
    with tempfile.TemporaryDirectory(prefix="mantisfetch-word-image-") as tmp:
        tmp_dir = Path(tmp)
        src = tmp_dir / f"image{original_ext}"
        src.write_bytes(image_bytes)
        cmd = [
            binary,
            "--headless",
            "--nologo",
            "--nofirststartwizard",
            "--convert-to",
            "png",
            "--outdir",
            str(tmp_dir),
            str(src),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
        candidates = sorted(tmp_dir.glob("*.png"))
        if proc.returncode != 0 or not candidates:
            details = (proc.stderr or proc.stdout or "").strip()
            raise RuntimeError(details or f"failed to convert {original_ext} to png")
        return candidates[0].read_bytes(), "ok"


def _render_embedded_image(image_bytes: bytes, original_ext: str) -> tuple[bytes, str, str]:
    ext = original_ext.lower()
    if ext in _RASTER_IMAGE_EXTENSIONS:
        rendered, status = _render_raster_image_to_png(image_bytes)
        return rendered, ".png", status
    if ext in _VECTOR_IMAGE_EXTENSIONS:
        rendered, status = _convert_vector_image_to_png(image_bytes, ext)
        return rendered, ".png", status
    raise RuntimeError(f"unsupported embedded image format: {ext or 'unknown'}")


def _image_dimensions(image_bytes: bytes) -> tuple[int, int]:
    if not image_bytes:
        return 0, 0
    try:
        from PIL import Image

        with Image.open(io.BytesIO(image_bytes)) as img:
            return int(img.width), int(img.height)
    except Exception:
        return 0, 0


def _image_average_hash(image_bytes: bytes, hash_size: int = 8) -> str:
    if not image_bytes:
        return ""
    try:
        from PIL import Image

        with Image.open(io.BytesIO(image_bytes)) as img:
            img = img.convert("L").resize((hash_size, hash_size))
            pixels = list(img.tobytes())
    except Exception:
        return ""
    if not pixels:
        return ""
    avg = sum(pixels) / len(pixels)
    bits = "".join("1" if px >= avg else "0" for px in pixels)
    return f"{int(bits, 2):0{hash_size * hash_size // 4}x}"


def _extract_image_context_keywords(*texts: str) -> list[str]:
    haystack = "\n".join(text for text in texts if text).lower()
    if not haystack:
        return []
    keywords: list[str] = []
    for key, aliases in _IMAGE_CONTEXT_KEYWORDS:
        if any(alias.lower() in haystack for alias in aliases):
            keywords.append(key)
    return keywords


def _inventory_hints_for_image(image: EmbeddedImage) -> list[str]:
    hints: list[str] = []
    area = image.width * image.height
    ratio = image.aspect_ratio
    keyword_set = set(image.context_keywords)

    if image.render_status == "failed":
        hints.append("render_failed")
    if image.render_status == "ok" and area > 0:
        if area < 20_000:
            hints.append("small_image")
        if image.width >= 900 and image.height >= 500 and 1.2 <= ratio <= 2.4:
            hints.append("screenshot_like")
        if image.height >= 900 and 0.55 <= ratio <= 1.15:
            hints.append("document_scan_like")

    for key in sorted(keyword_set):
        hints.append(f"context:{key}")
    if {"education_certificate", "degree_certificate"} & keyword_set:
        hints.append("personnel_material_candidate")
    if {"business_license", "id_card", "personnel_certificate", "certificate"} & keyword_set:
        hints.append("certificate_or_identity_candidate")
    if "contract_copy" in keyword_set:
        hints.append("case_contract_candidate")
    if "financial_statement" in keyword_set:
        hints.append("financial_material_candidate")
    if "product_screenshot" in keyword_set:
        hints.append("product_screenshot_candidate")
    return list(dict.fromkeys(hints))


def _populate_embedded_image_inventory(image: EmbeddedImage) -> None:
    image.original_size_bytes = len(image.original_bytes)
    image.original_sha256 = (
        hashlib.sha256(image.original_bytes).hexdigest() if image.original_bytes else ""
    )
    image.rendered_size_bytes = len(image.rendered_bytes)
    image.rendered_sha256 = (
        hashlib.sha256(image.rendered_bytes).hexdigest() if image.rendered_bytes else ""
    )
    image.width, image.height = _image_dimensions(image.rendered_bytes or image.original_bytes)
    image.aspect_ratio = round(image.width / image.height, 4) if image.height else 0.0
    image.average_hash = _image_average_hash(image.rendered_bytes or image.original_bytes)
    image.context_keywords = _extract_image_context_keywords(
        image.near_heading,
        image.paragraph_text,
        image.context_text,
        image.section_title,
    )
    image.inventory_hints = _inventory_hints_for_image(image)


def _ocr_embedded_image(image: EmbeddedImage, backend: str) -> tuple[str, str, str, str]:
    from . import _cleanup_ocr_text

    selected = (backend or "auto").strip().lower()
    if selected not in {"auto", "local", "llm"}:
        selected = "auto"
    if not image.rendered_bytes:
        return "", selected, "failed", "image was not rendered"

    if selected in {"auto", "local"}:
        text = local_ocr(image.rendered_bytes, image.order, "paddleocr")
        if text and not _is_ocr_failed_text(text):
            return _cleanup_ocr_text(text), "local-paddleocr", "ok", ""
        if selected == "local":
            return "", "local-paddleocr", "failed", text or "local OCR returned no text"

    text = gemini_ocr(image.rendered_bytes, image.order)
    if text and not _is_ocr_failed_text(text):
        return _cleanup_ocr_text(text), "llm", "ok", ""
    used_backend = "llm" if selected == "llm" else "auto"
    return "", used_backend, "failed", text or "LLM OCR returned no text"
