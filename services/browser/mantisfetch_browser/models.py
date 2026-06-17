"""Pydantic request/response models for the browser HTTP API.

Plain BaseModel schemas for the /web endpoints (new_session, goto, distill,
read_sections, act, scroll, navigate, webmcp_*, capture, ...). Self-contained
leaf: only pydantic + typing + stdlib, no Session/playwright/__init__ references.
"""

from __future__ import annotations

import os
from typing import Any, Literal

from pydantic import BaseModel, Field

# Session/request model field defaults — evaluated at class-definition time, so
# they live here with the models; re-exported from the facade for the endpoints.
DEFAULT_UA = os.getenv(
    "UA",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
)
DEFAULT_LANG = "en-US"


class NewSessionRequest(BaseModel):
    lang: str = DEFAULT_LANG
    user_agent: str = DEFAULT_UA
    block_resources: bool = True
    viewport: dict[str, int] = Field(default_factory=lambda: {"width": 900, "height": 700})
    storage_state: dict[str, Any] | None = None


class NewSessionResponse(BaseModel):
    session_id: str


class GotoRequest(BaseModel):
    session_id: str
    url: str
    wait_until: Literal["domcontentloaded", "load", "networkidle"] = "domcontentloaded"
    timeout_ms: int = 25000


class GotoResponse(BaseModel):
    session_id: str
    url: str
    title: str | None = None


class DistillRequest(BaseModel):
    session_id: str
    distill_mode: Literal["simple", "readability", "auto"] = "auto"
    max_sections: int = Field(default=30, ge=1, le=60)
    max_section_chars: int = Field(default=1800, ge=200, le=8000)
    total_text_budget_chars: int = Field(default=12000, ge=1000, le=60000)
    include_actions: bool = True
    max_actions: int = Field(default=60, ge=1, le=250)
    total_output_budget_chars: int = Field(default=18000, ge=2000, le=120000)
    min_actions_to_keep: int = Field(default=8, ge=0, le=50)
    max_action_name_chars: int = Field(default=80, ge=10, le=200)
    max_selector_chars: int = Field(default=120, ge=20, le=500)
    include_diff: bool = True
    min_actions_before_fallback: int = Field(default=8, ge=0, le=200)
    enable_a11y_fallback: bool = True
    enable_vision_fallback: bool = False
    vision_max_boxes: int = Field(default=12, ge=0, le=50)
    vision_conf_thresh: float = Field(default=0.35, ge=0.0, le=1.0)
    vision_iou_thresh: float = Field(default=0.45, ge=0.0, le=1.0)
    # Table extraction params
    extract_tables: bool = True
    max_table_rows: int = Field(default=80, ge=10, le=500)
    max_tables: int = Field(default=20, ge=1, le=50)
    # Optional wait_for_selector before distill (SPA-friendly)
    wait_for_selector: str | None = None
    wait_for_timeout_ms: int = Field(default=5000, ge=500, le=30000)


class ActionDescriptor(BaseModel):
    aid: str
    role: str
    name: str
    strategy: dict[str, Any]
    actions: list[str]
    confidence: float = 0.8
    source: str = "dom"


class Section(BaseModel):
    sid: str
    h: str | None = None
    t: str
    type: Literal["text", "table"] = "text"
    table_meta: dict[str, Any] | None = None


class DistillResponse(BaseModel):
    url: str
    title: str | None = None
    content_hash: str
    sections: list[Section]
    actions: list[ActionDescriptor] = []
    meta: dict[str, Any] = {}


class ReadSectionsRequest(BaseModel):
    session_id: str
    section_ids: list[str] = Field(min_length=1)
    max_section_chars: int = Field(default=1800, ge=200, le=8000)


class ReadSectionsResponse(BaseModel):
    url: str
    title: str | None
    content_hash: str
    picked_sections: list[Section]
    available_section_ids: list[str]


class ActRequest(BaseModel):
    session_id: str
    aid: str
    action: Literal["click", "type", "select", "scroll_into_view", "invoke"]
    text: str | None = None
    value: str | None = None
    wait_until: Literal["domcontentloaded", "load", "networkidle"] = "domcontentloaded"
    timeout_ms: int = 25000
    return_top_sections: bool = True
    top_k_sections: int = Field(default=3, ge=1, le=10)


class ActResponse(BaseModel):
    url_before: str
    url_after: str
    title: str | None
    changed: dict[str, Any]
    top_sections: list[Section] = []
    actions_sample: list[ActionDescriptor] = []


# scroll / back / forward request models
class ScrollRequest(BaseModel):
    session_id: str
    direction: Literal["up", "down"] = "down"
    pixels: int = Field(default=600, ge=50, le=5000)


class NavigateRequest(BaseModel):
    session_id: str
    direction: Literal["back", "forward"] = "back"
    wait_until: Literal["domcontentloaded", "load", "networkidle"] = "domcontentloaded"
    timeout_ms: int = 15000


class NavigateResponse(BaseModel):
    url: str
    title: str | None = None


# close also uses Pydantic Model
class CloseSessionRequest(BaseModel):
    session_id: str


class ExportStorageRequest(BaseModel):
    session_id: str


# ============================================================
# ✅ WebMCP Models
# ============================================================
class WebMCPDiscoverRequest(BaseModel):
    session_id: str
    force_refresh: bool = False


class WebMCPToolDescriptor(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any] | None = None
    read_only: bool = False
    auto_submit: bool | None = None
    source: str = "webmcp"  # "webmcp_imperative" | "webmcp_declarative"


class WebMCPDiscoverResponse(BaseModel):
    session_id: str
    url: str
    webmcp_available: bool
    tools: list[WebMCPToolDescriptor] = []
    errors: list[str] = []


class WebMCPInvokeRequest(BaseModel):
    session_id: str
    tool_name: str
    params: dict[str, Any] = Field(default_factory=dict)
    timeout_ms: int = 30000


class WebMCPInvokeResponse(BaseModel):
    session_id: str
    tool_name: str
    success: bool
    result: Any | None = None
    error: str | None = None
    url_before: str
    url_after: str


class CaptureRequest(BaseModel):
    """Request body for POST /capture (one-shot web capture)."""

    url: str
    content_type: str = "General"
    tags: list[str] = []
    extract_tables: bool = True
    lang: str = DEFAULT_LANG
    timeout_ms: int = 25000


class CaptureResponse(BaseModel):
    """Response from POST /capture."""

    doc_id: str
    content_type: str = "General"
    storage_path: str = ""
    digest: str
    section_count: int
    table_count: int
