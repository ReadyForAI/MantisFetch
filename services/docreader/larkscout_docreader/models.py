"""Domain data structures for the document reader.

Pure leaf module: dataclasses for parsed-document content, OCR layout sidecars,
and document-profile policies, plus the two helpers they depend on (the OCR
sidecar contract constants and the bbox normalizer). Imported by the service
package's ``__init__`` and re-exported, so ``larkscout_docreader.Section`` etc.
resolve unchanged. Must not import from the package ``__init__`` (would create a
circular import).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ── OCR blocks sidecar contract ──────────────────────────────
OCR_BLOCKS_SIDECAR_VERSION = 1
OCR_BLOCKS_SIDECAR_PATH = "ocr_blocks.json"
OCR_BLOCKS_COORDINATE_SYSTEM = "image_pixels"


def _normalize_layout_bbox(bbox: tuple[float, float, float, float] | list[float]) -> list[float]:
    """Normalize a bbox to [x0, y0, x1, y1] floats and reject malformed geometry."""
    if len(bbox) != 4:
        raise ValueError("layout bbox must contain exactly four coordinates")
    normalized = [float(v) for v in bbox]
    x0, y0, x1, y1 = normalized
    if x1 < x0 or y1 < y0:
        raise ValueError("layout bbox must be ordered as [x0, y0, x1, y1]")
    return normalized


# ═══════════════════════════════════════════
# Data structures
# ═══════════════════════════════════════════


@dataclass
class PageContent:
    """Single page content."""

    page_num: int
    text: str
    is_ocr: bool = False
    tables: list[str] = field(default_factory=list)
    tables_in_text: bool = False


@dataclass(frozen=True)
class OCRTextBlock:
    """Normalized OCR text block geometry for layout sidecars."""

    block_id: str
    text: str
    bbox: tuple[float, float, float, float]
    confidence: float = 0.0
    source: str = "local_ocr"
    line_index: int = 0
    order: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "block_id": self.block_id,
            "text": self.text,
            "bbox": _normalize_layout_bbox(self.bbox),
            "confidence": float(self.confidence),
            "source": self.source,
            "line_index": int(self.line_index),
            "order": int(self.order),
        }


@dataclass(frozen=True)
class OCRPageBlocks:
    """OCR geometry for one rendered document page."""

    page: int
    width: int
    height: int
    blocks: tuple[OCRTextBlock, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "page": int(self.page),
            "width": int(self.width),
            "height": int(self.height),
            "blocks": [block.to_dict() for block in self.blocks],
        }


@dataclass(frozen=True)
class OCRBlocksSidecar:
    """Versioned OCR geometry sidecar contract."""

    doc_id: str
    pages: tuple[OCRPageBlocks, ...] = ()
    version: int = OCR_BLOCKS_SIDECAR_VERSION
    coordinate_system: str = OCR_BLOCKS_COORDINATE_SYSTEM

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": int(self.version),
            "doc_id": self.doc_id,
            "coordinate_system": self.coordinate_system,
            "pages": [page.to_dict() for page in self.pages],
        }


@dataclass
class Section:
    """Document section."""

    index: int
    title: str
    level: int  # heading level 1-3
    text: str
    page_range: str  # "p.5-12"
    summary: str = ""
    sid: str = ""  # stable ID
    image_refs: list[str] = field(default_factory=list)


@dataclass
class EmbeddedImage:
    """Embedded document image with generic anchor and OCR metadata."""

    image_id: str
    order: int
    media_path: str
    relationship_id: str
    paragraph_index: int
    paragraph_text: str = ""
    context_text: str = ""
    near_heading: str = ""
    anchor_sid: str = ""
    section_title: str = ""
    original_ext: str = ""
    original_type: str = ""
    original_bytes: bytes = b""
    original_size_bytes: int = 0
    original_sha256: str = ""
    rendered_ext: str = ""
    rendered_type: str = ""
    rendered_bytes: bytes = b""
    rendered_size_bytes: int = 0
    rendered_sha256: str = ""
    width: int = 0
    height: int = 0
    aspect_ratio: float = 0.0
    average_hash: str = ""
    context_keywords: list[str] = field(default_factory=list)
    inventory_hints: list[str] = field(default_factory=list)
    render_status: str = "not_rendered"
    render_error: str = ""
    ocr_enabled: bool = False
    ocr_backend: str = ""
    ocr_status: str = "not_requested"
    ocr_text: str = ""
    ocr_error: str = ""


@dataclass
class ParsedDocument:
    """Parsed document result."""

    filename: str
    file_type: str  # "pdf" | "docx"
    total_pages: int
    pages: list[PageContent]
    sections: list[Section]
    ocr_page_count: int = 0
    table_count: int = 0
    images: list[EmbeddedImage] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    ocr_blocks: OCRBlocksSidecar | None = None
    extract_tables: bool = True


@dataclass(frozen=True)
class FieldCrop:
    x0: float
    y0: float
    x1: float
    y1: float


@dataclass(frozen=True)
class FieldGroup:
    id: str
    aliases: tuple[str, ...] = ()
    page_scope: tuple[int, ...] = ()
    crop: FieldCrop | None = None
    start_alias: str | None = None
    end_alias: str | None = None
    replace_mode: str = "block_between_aliases"


@dataclass(frozen=True)
class FieldRule:
    id: str
    aliases: tuple[str, ...] = ()
    pattern: str | None = None
    page_scope: tuple[int, ...] = ()


@dataclass(frozen=True)
class ClassificationPolicy:
    required_terms: tuple[str, ...] = ()


@dataclass(frozen=True)
class QualityPolicy:
    sparse_text_chars: int = 40
    usable_text_chars: int = 120
    scan_page_ratio: float = 0.85
    mixed_page_ratio: float = 0.2


@dataclass(frozen=True)
class UpgradePolicy:
    default_mode: str = "accurate"
    local_ocr_backend: str = "paddleocr"
    region_llm_modes: tuple[str, ...] = ("accurate", "full")
    full_llm_modes: tuple[str, ...] = ("full",)
    proofread_modes: tuple[str, ...] = ("full",)


@dataclass(frozen=True)
class TablePolicy:
    prefer_markitdown: bool = True


@dataclass(frozen=True)
class CachePolicy:
    page_ocr: bool = True
    region_ocr: bool = True


@dataclass(frozen=True)
class ProcessingPolicy:
    large_file_threshold_mb: int = 50
    local_ocr_render_scale: float = 2.0
    llm_ocr_render_scale: float = 3.0
    max_local_ocr_pixels: int = 4_000_000
    max_llm_ocr_pixels: int = 8_000_000
    min_ocr_render_scale: float = 1.25


@dataclass(frozen=True)
class SummaryPolicy:
    default_mode: str = "sync"
    async_modes: tuple[str, ...] = ()
    sync_modes: tuple[str, ...] = ("full",)


@dataclass(frozen=True)
class SectionPolicy:
    toc_max_level: int = 2
    suppress_arabic_clause_headings_when_formal_chinese: bool = False
    formal_chinese_min_headings: int = 4


@dataclass(frozen=True)
class DocumentProfile:
    name: str
    classification: ClassificationPolicy = ClassificationPolicy()
    quality_policy: QualityPolicy = QualityPolicy()
    upgrade_policy: UpgradePolicy = UpgradePolicy()
    table_policy: TablePolicy = TablePolicy()
    cache_policy: CachePolicy = CachePolicy()
    processing_policy: ProcessingPolicy = ProcessingPolicy()
    summary_policy: SummaryPolicy = SummaryPolicy()
    section_policy: SectionPolicy = SectionPolicy()
    groups: tuple[FieldGroup, ...] = ()
    fields: tuple[FieldRule, ...] = ()
