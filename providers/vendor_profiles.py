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


def get_vendor_profile(name: str | None) -> VendorProfile:
    """Return a vendor profile. Defaults to OpenAI only when unset; an unknown
    vendor name raises rather than silently sending requests to api.openai.com."""
    key = (name or "openai").strip().lower()
    profile = _VENDOR_PROFILES.get(key)
    if profile is None:
        allowed = ", ".join(sorted(_VENDOR_PROFILES))
        raise RuntimeError(
            f"unknown MANTISFETCH_LLM_VENDOR {name!r}; must be one of: {allowed}"
        )
    return profile
