"""Document-profile loading and profile-driven field extraction.

A *document profile* is a JSON config (under `configs/document_profiles/` or
`configs/field_profiles/`) that declares how to classify a document and which
fields to pull out of it — alias-anchored regions to re-OCR, regex field rules,
quality gates, and per-stage render/cache policy. This module owns:

- `_load_document_profile` — parse a profile JSON into a `DocumentProfile`,
- the blob/field helpers (`_page_blob`, `_extract_profile_fields`, …),
- `_apply_field_focused_ocr` — crop the declared regions, re-OCR them, and
  splice the result back into the page text.

OCR primitives (`gemini_ocr`, the OCR cache) come from `ocr.engines`; the
generic OCR-text cleanup/table helpers and the shared render-scale constants
live in the package `__init__` and are pulled in via function-level relative
imports to avoid an import cycle (the facade imports this module early).
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from .models import (
    CachePolicy,
    ClassificationPolicy,
    DocumentProfile,
    FieldCrop,
    FieldGroup,
    FieldRule,
    PageContent,
    ProcessingPolicy,
    QualityPolicy,
    SectionPolicy,
    SummaryPolicy,
    TablePolicy,
    UpgradePolicy,
)
from .ocr.engines import _ocr_cache_key, _ocr_cache_variant_path, gemini_ocr

logger = logging.getLogger("larkscout_docreader")

DOCUMENT_PROFILE_CONFIG_DIR = Path(__file__).resolve().parents[3] / "configs" / "document_profiles"
FIELD_OCR_CONFIG_DIR = Path(__file__).resolve().parents[3] / "configs" / "field_profiles"
FIELD_OCR_RENDER_SCALE = float(os.environ.get("LARKSCOUT_FIELD_OCR_RENDER_SCALE", "4.0"))
_DOCUMENT_PROFILE_ALIASES = {"tender_cn": "bid_cn"}


def _resolve_profile_config_path(requested: str) -> Path | None:
    """Resolve a profile name to a JSON file inside an allowed config dir.

    The directory part of ``requested`` is discarded (basename only) and the
    resolved file must stay within a config dir, so attacker-supplied "../" or
    absolute paths (the profile/config form fields are public on /parse) cannot
    escape to read arbitrary local files (LFI).
    """
    name = Path(requested).name
    if not name or name in {".", ".."}:
        return None
    if not name.endswith(".json"):
        name = f"{name}.json"
    for base in (DOCUMENT_PROFILE_CONFIG_DIR, FIELD_OCR_CONFIG_DIR):
        candidate = (base / name).resolve()
        try:
            candidate.relative_to(base.resolve())
        except ValueError:
            continue
        if candidate.exists():
            return candidate
    return None


def _load_document_profile(profile_name: str | None, config_path: str | None) -> DocumentProfile | None:
    from . import LOCAL_OCR_RENDER_SCALE, OCR_RENDER_SCALE

    selected = (profile_name or "").strip()
    custom = (config_path or "").strip()
    if not selected and not custom:
        return None

    selected = _DOCUMENT_PROFILE_ALIASES.get(selected, selected)

    # Both inputs are public /parse form values; treat them as opaque profile
    # names confined to the config dirs (never arbitrary paths) to prevent LFI.
    requested = custom or selected
    path = _resolve_profile_config_path(requested)
    if path is None:
        raise RuntimeError(f"field OCR config not found: {requested!r}")

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid field OCR config JSON: {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise RuntimeError(f"field OCR config must be a JSON object: {path}")

    classification_raw = raw.get("classification") if isinstance(raw.get("classification"), dict) else {}
    quality_raw = raw.get("quality_policy") if isinstance(raw.get("quality_policy"), dict) else {}
    upgrade_raw = raw.get("upgrade_policy") if isinstance(raw.get("upgrade_policy"), dict) else {}
    table_raw = raw.get("table_policy") if isinstance(raw.get("table_policy"), dict) else {}
    cache_raw = raw.get("cache_policy") if isinstance(raw.get("cache_policy"), dict) else {}
    processing_raw = raw.get("processing_policy") if isinstance(raw.get("processing_policy"), dict) else {}
    summary_raw = raw.get("summary_policy") if isinstance(raw.get("summary_policy"), dict) else {}
    section_raw = raw.get("section_policy") if isinstance(raw.get("section_policy"), dict) else {}

    groups: list[FieldGroup] = []
    for item in raw.get("groups", []):
        if not isinstance(item, dict):
            continue
        crop_raw = item.get("crop") or {}
        crop = None
        if isinstance(crop_raw, dict):
            try:
                crop = FieldCrop(
                    x0=float(crop_raw["x0"]),
                    y0=float(crop_raw["y0"]),
                    x1=float(crop_raw["x1"]),
                    y1=float(crop_raw["y1"]),
                )
            except (KeyError, TypeError, ValueError):
                crop = None
        groups.append(
            FieldGroup(
                id=str(item.get("id") or f"group_{len(groups)+1}"),
                aliases=tuple(str(v) for v in item.get("aliases", []) if str(v).strip()),
                page_scope=tuple(int(v) for v in item.get("page_scope", []) if isinstance(v, int)),
                crop=crop,
                start_alias=str(item.get("start_alias")).strip() if item.get("start_alias") else None,
                end_alias=str(item.get("end_alias")).strip() if item.get("end_alias") else None,
                replace_mode=str(item.get("replace_mode") or "block_between_aliases"),
            )
        )

    fields: list[FieldRule] = []
    for item in raw.get("fields", []):
        if not isinstance(item, dict):
            continue
        pattern = item.get("pattern")
        fields.append(
            FieldRule(
                id=str(item.get("id") or f"field_{len(fields)+1}"),
                aliases=tuple(str(v) for v in item.get("aliases", []) if str(v).strip()),
                pattern=str(pattern) if pattern else None,
                page_scope=tuple(int(v) for v in item.get("page_scope", []) if isinstance(v, int)),
            )
        )

    return DocumentProfile(
        name=str(raw.get("profile") or selected or path.stem),
        classification=ClassificationPolicy(
            required_terms=tuple(
                str(v) for v in classification_raw.get("required_terms", []) if str(v).strip()
            )
        ),
        quality_policy=QualityPolicy(
            sparse_text_chars=max(0, int(quality_raw.get("sparse_text_chars", 40))),
            usable_text_chars=max(1, int(quality_raw.get("usable_text_chars", 120))),
            scan_page_ratio=float(quality_raw.get("scan_page_ratio", 0.85)),
            mixed_page_ratio=float(quality_raw.get("mixed_page_ratio", 0.2)),
        ),
        upgrade_policy=UpgradePolicy(
            default_mode=str(upgrade_raw.get("default_mode") or "accurate").strip().lower(),
            local_ocr_backend=str(upgrade_raw.get("local_ocr_backend") or "paddleocr").strip().lower(),
            region_llm_modes=tuple(
                str(v).strip().lower()
                for v in upgrade_raw.get("region_llm_modes", ["accurate", "full"])
                if str(v).strip()
            ),
            full_llm_modes=tuple(
                str(v).strip().lower()
                for v in upgrade_raw.get("full_llm_modes", ["full"])
                if str(v).strip()
            ),
            proofread_modes=tuple(
                str(v).strip().lower()
                for v in upgrade_raw.get("proofread_modes", ["full"])
                if str(v).strip()
            ),
        ),
        table_policy=TablePolicy(
            prefer_markitdown=bool(table_raw.get("prefer_markitdown", True))
        ),
        cache_policy=CachePolicy(
            page_ocr=bool(cache_raw.get("page_ocr", True)),
            region_ocr=bool(cache_raw.get("region_ocr", True)),
        ),
        processing_policy=ProcessingPolicy(
            large_file_threshold_mb=max(1, int(processing_raw.get("large_file_threshold_mb", 50))),
            local_ocr_render_scale=max(
                0.5,
                float(processing_raw.get("local_ocr_render_scale", LOCAL_OCR_RENDER_SCALE)),
            ),
            llm_ocr_render_scale=max(
                0.5,
                float(processing_raw.get("llm_ocr_render_scale", OCR_RENDER_SCALE)),
            ),
            max_local_ocr_pixels=max(
                500_000,
                int(processing_raw.get("max_local_ocr_pixels", 4_000_000)),
            ),
            max_llm_ocr_pixels=max(
                500_000,
                int(processing_raw.get("max_llm_ocr_pixels", 8_000_000)),
            ),
            min_ocr_render_scale=max(
                0.5,
                float(processing_raw.get("min_ocr_render_scale", 1.25)),
            ),
        ),
        summary_policy=SummaryPolicy(
            default_mode=str(summary_raw.get("default_mode") or "sync").strip().lower(),
            async_modes=tuple(
                str(v).strip().lower()
                for v in summary_raw.get("async_modes", [])
                if str(v).strip()
            ),
            sync_modes=tuple(
                str(v).strip().lower()
                for v in summary_raw.get("sync_modes", ["full"])
                if str(v).strip()
            ),
        ),
        section_policy=SectionPolicy(
            toc_max_level=max(1, int(section_raw.get("toc_max_level", 2))),
            suppress_arabic_clause_headings_when_formal_chinese=bool(
                section_raw.get("suppress_arabic_clause_headings_when_formal_chinese", False)
            ),
            formal_chinese_min_headings=max(
                1,
                int(section_raw.get("formal_chinese_min_headings", 4)),
            ),
        ),
        groups=tuple(groups),
        fields=tuple(fields),
    )


def _page_blob(page: PageContent) -> str:
    if page.tables_in_text:
        return page.text.strip()

    parts = [page.text.strip()] if page.text.strip() else []
    parts.extend(table.strip() for table in page.tables if table.strip())
    return "\n\n".join(parts).strip()


def _set_page_blob(page: PageContent, text: str) -> None:
    from . import _extract_tables_from_ocr_text

    body, tables = _extract_tables_from_ocr_text(text, page.page_num, page.page_num)
    page.text = body
    page.tables = tables
    page.tables_in_text = bool(tables)


def _blob_has_alias(text: str, aliases: tuple[str, ...]) -> bool:
    return any(alias and alias in text for alias in aliases)


def _field_value_quality(field_id: str, value: str) -> tuple[bool, str]:
    from . import _looks_like_bracket_noise

    value = value.strip()
    if not value:
        return False, "empty"
    if re.search(r"[぀-ヿ]", value):
        return False, "kana_noise"
    if _looks_like_bracket_noise(value):
        return False, "bracket_noise"

    normalized = re.sub(r"\s+", "", value)
    if field_id == "contract_no":
        if normalized in {"甲", "乙", "合同", "合同编号", "方"}:
            return False, "label_only"
        if len(normalized) < 4 or not re.search(r"\d", normalized):
            return False, "too_short_or_no_digit"
    elif field_id in {"party_a_name", "party_b_name", "customer_name"}:
        if len(normalized) < 4:
            return False, "too_short"
        if not re.search(r"(公司|中心|银行|基金|学校|医院|政府|委员会|研究院|事务所|集团)", normalized):
            return False, "not_org_like"
    elif field_id.endswith("_phone"):
        if len(re.sub(r"\D", "", normalized)) < 7:
            return False, "not_phone_like"
    elif field_id.endswith("_account"):
        if len(re.sub(r"\D", "", normalized)) < 6:
            return False, "not_account_like"
    return True, ""


def _source_filename_contract_no(source_filename: str | None) -> str | None:
    stem = Path(source_filename or "").stem.strip()
    if re.fullmatch(r"[A-Za-z]{2,10}\d{4,20}", stem):
        return stem
    return None


def _normalize_cover_label_lines(blob: str) -> str:
    text = blob.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"(?m)^甲\s*\n\s*方\s*[：:]", "甲方：", text)
    text = re.sub(r"(?m)^乙\s*\n\s*方\s*[：:]", "乙方：", text)
    return text


def _replace_blob_segment(text: str, group: FieldGroup, replacement: str) -> str:
    if group.replace_mode == "replace_entire_page":
        return replacement.strip()

    start = -1
    matched_start = ""
    if group.start_alias:
        idx = text.find(group.start_alias)
        if idx >= 0:
            start, matched_start = idx, group.start_alias
    if start < 0:
        candidates = [(text.find(a), a) for a in group.aliases if a and text.find(a) >= 0]
        if candidates:
            start, matched_start = min(candidates, key=lambda c: c[0])
    if start < 0:
        return text

    end = len(text)
    if group.end_alias:
        # Search past the matched start marker. matched_start (not start_alias,
        # which may be None when the start came from `aliases`) gives its length.
        found = text.find(group.end_alias, start + len(matched_start))
        if found >= 0:
            end = found
    return (text[:start].rstrip() + "\n\n" + replacement.strip() + "\n\n" + text[end:].lstrip()).strip()


def _prepend_source_contract_no_if_missing(text: str, source_filename: str | None) -> str:
    contract_no = _source_filename_contract_no(source_filename)
    if not contract_no:
        return text.strip()
    normalized = re.sub(r"\s+", "", text)
    if contract_no in normalized:
        return text.strip()
    return f"{contract_no}\n{text.strip()}".strip()


def _extract_profile_fields(
    pages: list[PageContent],
    profile: DocumentProfile,
    *,
    source_filename: str | None = None,
) -> dict[str, Any]:
    extracted: dict[str, Any] = {}
    for field_rule in profile.fields:
        for page in pages:
            if field_rule.page_scope and page.page_num not in field_rule.page_scope:
                continue
            blob = _normalize_cover_label_lines(_page_blob(page))
            if field_rule.aliases and not _blob_has_alias(blob, field_rule.aliases):
                continue
            if field_rule.pattern:
                match = re.search(field_rule.pattern, blob, flags=re.MULTILINE)
                if not match:
                    continue
                value = (match.group(1) if match.groups() else match.group(0)).strip()
            else:
                value = next((alias for alias in field_rule.aliases if alias in blob), "").strip()
            if value:
                valid, reason = _field_value_quality(field_rule.id, value)
                if not valid:
                    logger.info(
                        "Discarded low-confidence field %s on page %d: %r (%s)",
                        field_rule.id,
                        page.page_num,
                        value,
                        reason,
                    )
                    continue
                extracted[field_rule.id] = {
                    "value": value,
                    "page": page.page_num,
                    "source": "profile_regex",
                }
                break
    if "contract_no" not in extracted:
        fallback_contract_no = _source_filename_contract_no(source_filename)
        if fallback_contract_no:
            extracted["contract_no"] = {
                "value": fallback_contract_no,
                "page": 1,
                "source": "source_filename",
            }
    return extracted


def _apply_field_focused_ocr(
    filepath: Path,
    pages: list[PageContent],
    profile: DocumentProfile,
    cache_dir: Path | None = None,
    proofread: bool = True,
) -> dict[str, Any]:
    import fitz

    from . import _cleanup_ocr_text, _normalize_document_text

    applied_groups: list[dict[str, Any]] = []
    doc = fitz.open(str(filepath))
    try:
        for group in profile.groups:
            if not group.crop:
                continue
            for page in pages:
                if group.page_scope and page.page_num not in group.page_scope:
                    continue
                blob = _page_blob(page)
                if group.aliases and not _blob_has_alias(blob, group.aliases):
                    continue

                fitz_page = doc[page.page_num - 1]
                rect = fitz_page.rect
                clip = fitz.Rect(
                    rect.x0 + rect.width * group.crop.x0,
                    rect.y0 + rect.height * group.crop.y0,
                    rect.x0 + rect.width * group.crop.x1,
                    rect.y0 + rect.height * group.crop.y1,
                )
                pix = fitz_page.get_pixmap(matrix=fitz.Matrix(FIELD_OCR_RENDER_SCALE, FIELD_OCR_RENDER_SCALE), clip=clip)
                img_bytes = pix.tobytes("png")
                region_text = ""
                if cache_dir and profile.cache_policy.region_ocr:
                    ck = _ocr_cache_key(img_bytes)
                    cache_path = _ocr_cache_variant_path(
                        cache_dir,
                        f"ocr_region_p{page.page_num:04d}_{group.id}.{ck}.txt",
                    )
                    if cache_path.exists():
                        region_text = cache_path.read_text(encoding="utf-8").strip()
                if not region_text:
                    region_text = gemini_ocr(img_bytes, page.page_num, proofread=proofread).strip()
                    if cache_dir and profile.cache_policy.region_ocr and region_text:
                        cache_path = _ocr_cache_variant_path(
                            cache_dir,
                            f"ocr_region_p{page.page_num:04d}_{group.id}.{_ocr_cache_key(img_bytes)}.txt",
                        )
                        cache_path.write_text(region_text, encoding="utf-8")
                if not region_text or region_text.startswith("["):
                    continue
                region_text = _cleanup_ocr_text(region_text, source_filename=filepath.name)
                if page.page_num == 1 and group.replace_mode == "replace_entire_page":
                    region_text = _prepend_source_contract_no_if_missing(region_text, filepath.name)

                replace_source = page.text.strip()
                replaced = _replace_blob_segment(replace_source, group, region_text)
                if replaced != replace_source:
                    page.tables = []
                    _set_page_blob(page, replaced)
                    applied_groups.append({"group_id": group.id, "page": page.page_num})
    finally:
        doc.close()

    _normalize_document_text(pages)
    return {
        "profile": profile.name,
        "applied_groups": applied_groups,
        "extracted_fields": _extract_profile_fields(pages, profile, source_filename=filepath.name),
    }
