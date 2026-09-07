"""Vendor profiles for OpenAI-compatible providers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class VendorProfile:
    """Resolved defaults for an OpenAI-compatible vendor."""

    name: str
    base_url: str
    # Kept as fields, and left None for every registered vendor. A profile knows
    # how to *talk to* a vendor — base URL, image encoding, body quirks — and
    # that is stable knowledge. Which model to talk to is not: every name here
    # had a shelf life, and the one that expired first (gemini-2.5-flash, in the
    # provider rather than here) failed a fresh deployment on its first document.
    # A caller that wants one passes it; nothing invents one.
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
    ),
    "zhipu": VendorProfile(
        name="zhipu",
        base_url="https://open.bigmodel.cn/api/paas/v4",
    ),
    "kimi": VendorProfile(
        name="kimi",
        base_url="https://api.moonshot.cn/v1",
    ),
    "aliyun": VendorProfile(
        name="aliyun",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
    ),
    "volcengine": VendorProfile(
        name="volcengine",
        base_url="https://ark.cn-beijing.volces.com/api/v3",
    ),
}


def get_vendor_profile(name: str | None, *, fallback_base_url: str | None = None) -> VendorProfile:
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
    raise RuntimeError(f"unknown MANTISFETCH_LLM_VENDOR {name!r}; must be one of: {allowed}")
