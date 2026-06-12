"""Word (.docx) parsing: text via MarkItDown, embedded-image extraction via XML.

`parse_word` turns a .docx into a `ParsedDocument` — body text + tables come
from MarkItDown, sections from the shared splitter, and (optionally) embedded
images are pulled straight out of the OPC package by walking `word/document.xml`
and its relationship table (`_extract_word_embedded_images` and the `_word_*`
helpers). Images are rendered/OCR'd via the format-agnostic helpers in
`images.py` and anchored back to their nearest section.

Cross-module wiring:
- sectioning (`_is_heading`, `_split_sections`, `_normalize_heading_key`,
  `_strip_heading_markup`) and `ocr.tables._extract_markdown_table_blocks` are
  leaf imports.
- `_convert_to_markdown` / `_section_sid` live in the package `__init__`, and
  the embedded-image render/OCR/inventory helpers are facade-patched by tests
  (`monkeypatch.setattr(docreader, "_ocr_embedded_image", …)`), so both groups
  are reached via function-level relative imports off the facade — that both
  breaks the import cycle and lets the test patches take effect.
"""

from __future__ import annotations

import logging
import posixpath
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from .models import DocumentProfile, EmbeddedImage, PageContent, ParsedDocument, Section
from .ocr.tables import _extract_markdown_table_blocks
from .sectioning import (
    _is_heading,
    _normalize_heading_key,
    _split_sections,
    _strip_heading_markup,
)

logger = logging.getLogger("larkscout_docreader")

WORD_XML_NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "v": "urn:schemas-microsoft-com:vml",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
}

_IMAGE_MIME_BY_EXT = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".bmp": "image/bmp",
    ".gif": "image/gif",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".emf": "image/x-emf",
    ".wmf": "image/x-wmf",
}


def _word_rel_target_to_package_path(target: str) -> str:
    clean = target.replace("\\", "/").strip()
    if clean.startswith("/"):
        return posixpath.normpath(clean.lstrip("/"))
    return posixpath.normpath(posixpath.join("word", clean))


def _word_paragraph_text(paragraph: ET.Element) -> str:
    parts: list[str] = []
    for node in paragraph.findall(".//w:t", WORD_XML_NS):
        if node.text:
            parts.append(node.text)
    return "".join(parts).strip()


def _word_paragraph_style(paragraph: ET.Element) -> str:
    style = paragraph.find("./w:pPr/w:pStyle", WORD_XML_NS)
    if style is None:
        return ""
    return str(style.attrib.get(f"{{{WORD_XML_NS['w']}}}val") or "").strip()


def _word_heading_level(text: str, style: str) -> int:
    style_lower = style.lower()
    if any(token in style_lower for token in ("heading", "标题", "title")):
        match = re.search(r"([1-6])", style_lower)
        return min(int(match.group(1)), 3) if match else 1
    return _is_heading(text)


def _word_image_relationships(docx_path: Path) -> dict[str, str]:
    try:
        with zipfile.ZipFile(docx_path) as zf:
            rels_xml = zf.read("word/_rels/document.xml.rels")
    except Exception:
        return {}

    root = ET.fromstring(rels_xml)
    rels: dict[str, str] = {}
    for rel in root.findall("rel:Relationship", WORD_XML_NS):
        rel_id = str(rel.attrib.get("Id") or "")
        target = str(rel.attrib.get("Target") or "")
        rel_type = str(rel.attrib.get("Type") or "")
        target_mode = str(rel.attrib.get("TargetMode") or "")
        if not rel_id or not target:
            continue
        if target_mode.lower() == "external":
            # Linked (not embedded) images live outside the .docx package;
            # they cannot be read via zipfile and must not count toward
            # embedded-image limits.
            continue
        if "image" not in rel_type.lower() and not target.lower().startswith("media/"):
            continue
        rels[rel_id] = _word_rel_target_to_package_path(target)
    return rels


def _word_paragraph_image_rel_ids(paragraph: ET.Element) -> list[str]:
    rel_ids: list[str] = []
    for node in paragraph.findall(".//a:blip", WORD_XML_NS):
        rel_id = str(node.attrib.get(f"{{{WORD_XML_NS['r']}}}embed") or "").strip()
        if rel_id and rel_id not in rel_ids:
            rel_ids.append(rel_id)
    for node in paragraph.findall(".//v:imagedata", WORD_XML_NS):
        rel_id = str(node.attrib.get(f"{{{WORD_XML_NS['r']}}}id") or "").strip()
        if rel_id and rel_id not in rel_ids:
            rel_ids.append(rel_id)
    return rel_ids


def _word_image_context_text(
    paragraph_texts: list[str],
    paragraph_index: int,
    *,
    before: int = 4,
    after: int = 3,
    max_chars: int = 1200,
) -> str:
    start = max(0, paragraph_index - before)
    end = min(len(paragraph_texts), paragraph_index + after + 1)
    parts = [text for text in paragraph_texts[start:end] if text]
    return "\n".join(parts)[:max_chars]


def _count_word_embedded_image_references(filepath: Path) -> int:
    """Count embedded Word image references that would be processed for image OCR."""
    if filepath.suffix.lower() != ".docx":
        return 0
    try:
        with zipfile.ZipFile(filepath) as zf:
            document_xml = zf.read("word/document.xml")
        rels = _word_image_relationships(filepath)
        root = ET.fromstring(document_xml)
    except Exception as exc:
        logger.warning("Word embedded image count failed for %s: %s", filepath.name, exc)
        return 0

    count = 0
    for paragraph in root.findall(".//w:p", WORD_XML_NS):
        for rel_id in _word_paragraph_image_rel_ids(paragraph):
            if rel_id in rels:
                count += 1
    return count


def _anchor_word_images_to_sections(images: list[EmbeddedImage], sections: list[Section]) -> None:
    if not images or not sections:
        return
    for image in images:
        heading_key = _normalize_heading_key(image.near_heading)
        paragraph_key = _normalize_heading_key(image.paragraph_text)
        selected: Section | None = None
        for sec in sections:
            title_key = _normalize_heading_key(sec.title)
            text_key = _normalize_heading_key(sec.text[:1500])
            if heading_key and (heading_key == title_key or heading_key in text_key):
                selected = sec
                break
            if paragraph_key and paragraph_key in text_key:
                selected = sec
                break
        if selected:
            image.anchor_sid = selected.sid
            image.section_title = selected.title
            if image.image_id not in selected.image_refs:
                selected.image_refs.append(image.image_id)


def _extract_word_embedded_images(
    filepath: Path,
    *,
    sections: list[Section],
    ocr_images: bool = False,
    image_ocr_backend: str = "auto",
    max_images: int = 200,
) -> list[EmbeddedImage]:
    # Facade imports: render/OCR/inventory helpers are patched by tests via
    # `monkeypatch.setattr(docreader, "_ocr_embedded_image", …)`, so they must be
    # looked up off the facade at call time (not bound from .images at import).
    from . import (
        _ocr_embedded_image,
        _populate_embedded_image_inventory,
        _render_embedded_image,
    )

    if filepath.suffix.lower() != ".docx":
        return []
    limit = max(0, int(max_images or 0))
    if limit == 0:
        return []
    try:
        with zipfile.ZipFile(filepath) as zf:
            document_xml = zf.read("word/document.xml")
            rels = _word_image_relationships(filepath)
            root = ET.fromstring(document_xml)
            paragraphs = root.findall(".//w:p", WORD_XML_NS)
            paragraph_texts = [_word_paragraph_text(paragraph) for paragraph in paragraphs]
            images: list[EmbeddedImage] = []
            current_heading = ""

            for paragraph_index, paragraph in enumerate(paragraphs, 1):
                paragraph_text = paragraph_texts[paragraph_index - 1]
                heading_level = _word_heading_level(paragraph_text, _word_paragraph_style(paragraph))
                if paragraph_text and heading_level > 0:
                    current_heading = _strip_heading_markup(paragraph_text)

                for rel_id in _word_paragraph_image_rel_ids(paragraph):
                    media_path = rels.get(rel_id)
                    if not media_path:
                        continue
                    if len(images) >= limit:
                        break
                    try:
                        original_bytes = zf.read(media_path)
                    except KeyError:
                        continue
                    image_id = f"IMG-{len(images) + 1:03d}"
                    original_ext = Path(media_path).suffix.lower()
                    image = EmbeddedImage(
                        image_id=image_id,
                        order=len(images) + 1,
                        media_path=media_path,
                        relationship_id=rel_id,
                        paragraph_index=paragraph_index,
                        paragraph_text=paragraph_text,
                        context_text=_word_image_context_text(
                            paragraph_texts, paragraph_index - 1
                        ),
                        near_heading=current_heading,
                        original_ext=original_ext,
                        original_type=_IMAGE_MIME_BY_EXT.get(
                            original_ext, "application/octet-stream"
                        ),
                        original_bytes=original_bytes,
                    )
                    try:
                        rendered, rendered_ext, render_status = _render_embedded_image(
                            image.original_bytes, image.original_ext
                        )
                        image.rendered_bytes = rendered
                        image.rendered_ext = rendered_ext
                        image.rendered_type = _IMAGE_MIME_BY_EXT.get(rendered_ext, "image/png")
                        image.render_status = render_status
                    except Exception as exc:
                        image.render_status = "failed"
                        image.render_error = str(exc)

                    image.ocr_enabled = bool(ocr_images)
                    if ocr_images:
                        text, used_backend, status, error = _ocr_embedded_image(
                            image, image_ocr_backend
                        )
                        image.ocr_backend = used_backend
                        image.ocr_status = status
                        image.ocr_text = text
                        image.ocr_error = error
                    images.append(image)
                if len(images) >= limit:
                    break
    except Exception as exc:
        logger.warning("Word embedded image extraction failed for %s: %s", filepath.name, exc)
        return []

    _anchor_word_images_to_sections(images, sections)
    for image in images:
        _populate_embedded_image_inventory(image)
    return images


def parse_word(
    filepath: Path,
    extract_tables: bool = True,
    profile: DocumentProfile | None = None,
    extract_images: bool = False,
    ocr_images: bool = False,
    image_ocr_backend: str = "auto",
    max_images: int = 200,
) -> ParsedDocument:
    # _convert_to_markdown / _section_sid live in the package __init__ (imported
    # before this module is defined); function-level import breaks the cycle and
    # lets the test patch on docreader._convert_to_markdown take effect.
    from . import _convert_to_markdown, _section_sid

    logger.info(f"Parsing Word: {filepath.name}")
    source_size_bytes = filepath.stat().st_size
    markdown_text = _convert_to_markdown(filepath)
    logger.info(f"MarkItDown extraction complete: {len(markdown_text)} chars")

    est_pages = max(1, len(markdown_text) // 3000)
    table_blocks = _extract_markdown_table_blocks(markdown_text) if extract_tables else []
    pages = [PageContent(page_num=1, text=markdown_text, tables=table_blocks)]
    sections = _split_sections(pages, section_policy=profile.section_policy if profile else None)
    for sec in sections:
        sec.sid = _section_sid(sec.title, sec.text)

    table_count = len(table_blocks) if extract_tables else 0
    embedded_image_count = _count_word_embedded_image_references(filepath) if extract_images else 0
    images = (
        _extract_word_embedded_images(
            filepath,
            sections=sections,
            ocr_images=ocr_images,
            image_ocr_backend=image_ocr_backend,
            max_images=max_images,
        )
        if extract_images
        else []
    )

    logger.info(
        f"Parse complete: {len(sections)} sections, ~{est_pages} pages, "
        f"{table_count} tables, {len(images)} images"
    )
    return ParsedDocument(
        filename=filepath.name,
        file_type=filepath.suffix.lower().lstrip(".") or "docx",
        total_pages=est_pages,
        pages=pages,
        sections=sections,
        table_count=table_count,
        images=images,
        extract_tables=extract_tables,
        metadata={
            "document_profile": profile.name if profile else None,
            "source_file": {"size_bytes": source_size_bytes},
            "word_images": {
                "extract_enabled": bool(extract_images),
                "ocr_enabled": bool(ocr_images),
                "ocr_backend": image_ocr_backend if ocr_images else "",
                "embedded_image_count": embedded_image_count,
                "max_images": max(0, int(max_images or 0)),
                "extracted": len(images),
                "truncated": bool(extract_images and embedded_image_count > len(images)),
                "render_ok": sum(1 for image in images if image.render_status == "ok"),
                "render_failed": sum(1 for image in images if image.render_status == "failed"),
                "ocr_ok": sum(1 for image in images if image.ocr_status == "ok"),
                "ocr_failed": sum(1 for image in images if image.ocr_status == "failed"),
            },
        },
    )
