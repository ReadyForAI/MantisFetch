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
    # Reported, not enforced. /capture rejects an error page because it is about
    # to store it; a session may legitimately want to land on a 404 and act from
    # there, so the caller decides. None when the navigation reported no response
    # (e.g. same-document).
    http_status: int | None = None


class SectionBudget(BaseModel):
    """Section/table limits for one distill, separate from the request's own.

    The request fields below are a *display* budget: they keep what comes back to
    the model small. Capture needs a *storage* budget, which is the opposite
    concern — what lands on disk is read later, section by section, under a
    budget applied at read time. Binding both to one set of numbers is why the
    library only ever held a clipped preview.

    Zero means unlimited. Every limit has to be listed here: leaving one at its
    display default silently becomes the new ceiling. In particular max_sections
    caps text and tables together, so a heading-rich page with a small
    max_sections fills the budget with prose and stores no tables at all.
    """

    max_sections: int = Field(default=0, ge=0)
    max_section_chars: int = Field(default=0, ge=0)
    total_text_budget_chars: int = Field(default=0, ge=0)
    total_output_budget_chars: int = Field(default=0, ge=0)
    max_table_rows: int = Field(default=2000, ge=1)
    max_tables: int = Field(default=50, ge=1)


# What /web/capture persists with: no clipping of body text, and enough table
# rows that a 223-row table survives whole.
CAPTURE_PERSIST_BUDGET = SectionBudget()


class DistillRequest(BaseModel):
    session_id: str
    # "html" parses page.content() in-process instead of running an extractor
    # inside the page; see extract.py for why capture uses it.
    distill_mode: Literal["simple", "readability", "auto", "html"] = "auto"
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
    force_refresh: bool = False
    # Provenance/metadata written verbatim into the capture's manifest + doc-index
    # (scalar values are index-filterable via ?metadata.<key>=). Used by
    # /web/search_and_capture to stamp source=web_search + search_* fields. NOT
    # part of the dedup cache key — a cache hit keeps the existing doc's metadata
    # (first-touch provenance).
    metadata: dict[str, Any] | None = None
    # "off" (default): the digest is a fast local snippet (title + section
    # previews). "defer": after persisting, generate an LLM digest + brief in the
    # background (three-tier parity with /doc) — opt-in because it spends tokens.
    summary_mode: Literal["off", "defer"] = "off"


class CaptureResponse(BaseModel):
    """Response from POST /capture."""

    doc_id: str
    content_type: str = "General"
    storage_path: str = ""
    digest: str
    section_count: int
    table_count: int
    reused: bool = False
    cache_age_hours: float | None = None
    # The URL the content actually came from (after redirects) and the status it
    # was served with, so a caller can tell a real article from a soft error page
    # without reading the body. None on a reused response: the cached entry
    # records the final URL but predates status capture.
    final_url: str | None = None
    http_status: int | None = None
    # "pending" when summary_mode="defer" scheduled an LLM digest/brief; poll
    # /doc/library/{doc_id}/summary for progress. None otherwise.
    summary_status: str | None = None


class SearchRequest(BaseModel):
    """Request body for POST /search (pure web search)."""

    query: str
    max_results: int | None = None  # None → server default (MANTISFETCH_SEARCH_MAX_RESULTS)
    lang: str = DEFAULT_LANG
    freshness: str | None = None  # "day" | "week" | "month" | None
    provider: str | None = None  # None → default provider/chain; else target one addressable provider


class SearchHit(BaseModel):
    """One search result. title/snippet are untrusted content — the MCP layer wraps
    them at the injection boundary before they reach the model."""

    url: str
    title: str
    snippet: str
    published_at: str | None = None
    score: float | None = None
    provider: str


class SearchResponse(BaseModel):
    """Response from POST /search."""

    query: str
    provider: str
    results: list[SearchHit]
    searched_at: str


class SearchAndCaptureRequest(BaseModel):
    """Request body for POST /search_and_capture (search + capture the top N hits)."""

    query: str
    capture_top: int = 3  # clamped to [1, 3] server-side; hits are captured serially
    tags: list[str] = []
    content_type: str = "General"
    lang: str = DEFAULT_LANG
    freshness: str | None = None
    provider: str | None = None  # None → default provider/chain; else target one addressable provider


class CapturedItem(BaseModel):
    """One successfully captured search hit."""

    doc_id: str
    url: str
    title: str | None = None
    digest: str
    reused: bool
    rank: int


class SkippedItem(BaseModel):
    """A search hit that failed to capture (does not abort the batch)."""

    url: str
    reason: str
    rank: int


class SearchAndCaptureResponse(BaseModel):
    """Response from POST /search_and_capture."""

    query: str
    provider: str
    captured: list[CapturedItem]
    skipped: list[SkippedItem]
    searched_at: str
