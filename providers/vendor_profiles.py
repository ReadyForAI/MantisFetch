"""Vendor profiles for OpenAI-compatible providers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class VendorProfile:
    """Resolved defaults for an OpenAI-compatible vendor."""

    name: str
    base_url: str
    default_text_model: str | None = None
    default_ocr_model: str | None = None
    supports_vision: bool = True
    image_input_mode: str = "data_url"
    extra_chat_body: dict[str, Any] = field(default_factory=dict)
    extra_ocr_body: dict[str, Any] = field(default_factory=dict)


_DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"

_VENDOR_PROFILES: dict[str, VendorProfile] = {
    "openai": VendorProfile(
        name="openai",
        base_url=_DEFAULT_OPENAI_BASE_URL,
        default_text_model="gpt-4o-mini",
    ),
    "zhipu": VendorProfile(
        name="zhipu",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        default_text_model="glm-5.1",
        default_ocr_model="glm-4.6v",
    ),
    "kimi": VendorProfile(
        name="kimi",
        base_url="https://api.moonshot.cn/v1",
        default_text_model="kimi-k2.6",
        default_ocr_model="kimi-k2.6",
    ),
    "aliyun": VendorProfile(
        name="aliyun",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        default_text_model="qwen-plus",
        default_ocr_model="qwen-vl-ocr",
    ),
    "volcengine": VendorProfile(
        name="volcengine",
        base_url="https://ark.cn-beijing.volces.com/api/v3",
    ),
}


def get_vendor_profile(
    name: str | None, *, fallback_base_url: str | None = None
) -> VendorProfile:
    """Return a vendor profile. Defaults to OpenAI only when unset.

    An unknown vendor name raises rather than silently sending requests to
    api.openai.com — unless ``fallback_base_url`` is given, in which case a
    generic OpenAI-compatible profile is synthesized against that URL. This
    lets the dual-slot scheme point a slot at any OpenAI-compatible endpoint
    without needing a registered profile, while the legacy single-provider
    path (no base_url override) still fails loudly on a typo'd vendor.
    """
    key = (name or "openai").strip().lower()
    profile = _VENDOR_PROFILES.get(key)
    if profile is not None:
        return profile
    if fallback_base_url:
        return VendorProfile(name=key, base_url=fallback_base_url.rstrip("/"))
    allowed = ", ".join(sorted(_VENDOR_PROFILES))
    raise RuntimeError(
        f"unknown MANTISFETCH_LLM_VENDOR {name!r}; must be one of: {allowed}"
    )
