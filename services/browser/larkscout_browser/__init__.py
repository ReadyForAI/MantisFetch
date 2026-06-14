import asyncio
import json
import logging
import os
import re
import secrets
import threading
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from i18n import t
from larkscout_common.atomic import _write_text as _write_text_atomic
from larkscout_common.paths import _mask_path
from larkscout_common.storage import (
    _doc_storage_rel_path,
    _get_docs_dir,
    _normalize_content_type,
)

# URL validation (anti-SSRF). Re-exported so the goto/capture endpoints and
# test_security keep calling _validate_url off the package namespace.
# Browser session object + manager + the process-wide `sessions` singleton.
# Re-exported so endpoints keep calling sessions.get/put/... and test_concurrency
# can import Session/SessionManager off the package namespace.
# YOLO detection + Readability.js loading. Imported as `vision` so endpoints can
# read the startup-mutated vision.YOLO_ENABLED / vision.READABILITY_* state live;
# the functions are re-exported for bare calls.
from . import vision as vision

# Pydantic request/response models for the /web endpoints. Re-exported so the
# endpoint handlers keep referencing them off the package namespace.
from .models import (
    DEFAULT_LANG as DEFAULT_LANG,
)
from .models import (
    DEFAULT_UA as DEFAULT_UA,
)
from .models import (
    ActionDescriptor as ActionDescriptor,
)
from .models import (
    ActRequest as ActRequest,
)
from .models import (
    ActResponse as ActResponse,
)
from .models import (
    CaptureRequest as CaptureRequest,
)
from .models import (
    CaptureResponse as CaptureResponse,
)
from .models import (
    CloseSessionRequest as CloseSessionRequest,
)
from .models import (
    DistillRequest as DistillRequest,
)
from .models import (
    DistillResponse as DistillResponse,
)
from .models import (
    ExportStorageRequest as ExportStorageRequest,
)
from .models import (
    GotoRequest as GotoRequest,
)
from .models import (
    GotoResponse as GotoResponse,
)
from .models import (
    NavigateRequest as NavigateRequest,
)
from .models import (
    NavigateResponse as NavigateResponse,
)
from .models import (
    NewSessionRequest as NewSessionRequest,
)
from .models import (
    NewSessionResponse as NewSessionResponse,
)
from .models import (
    ReadSectionsRequest as ReadSectionsRequest,
)
from .models import (
    ReadSectionsResponse as ReadSectionsResponse,
)
from .models import (
    ScrollRequest as ScrollRequest,
)
from .models import (
    Section as Section,
)
from .models import (
    WebMCPDiscoverRequest as WebMCPDiscoverRequest,
)
from .models import (
    WebMCPDiscoverResponse as WebMCPDiscoverResponse,
)
from .models import (
    WebMCPInvokeRequest as WebMCPInvokeRequest,
)
from .models import (
    WebMCPInvokeResponse as WebMCPInvokeResponse,
)
from .models import (
    WebMCPToolDescriptor as WebMCPToolDescriptor,
)
from .ranking import (
    _actions_diff as _actions_diff,
)

# Distill output post-processing (stable ids, text utils, diffs, action budget).
# Re-exported so _distill / the extract helpers / endpoints keep calling these
# off the facade with no change.
from .ranking import (
    _aid as _aid,
)
from .ranking import (
    _apply_total_output_budget as _apply_total_output_budget,
)
from .ranking import (
    _clip as _clip,
)
from .ranking import (
    _dedup_actions as _dedup_actions,
)
from .ranking import (
    _estimate_action_chars as _estimate_action_chars,
)
from .ranking import (
    _estimate_meta_chars as _estimate_meta_chars,
)
from .ranking import (
    _hash_text as _hash_text,
)
from .ranking import (
    _make_stable_sid as _make_stable_sid,
)
from .ranking import (
    _normalize as _normalize,
)
from .ranking import (
    _pick_action_methods as _pick_action_methods,
)
from .ranking import (
    _rank_actions as _rank_actions,
)
from .ranking import (
    _sections_diff as _sections_diff,
)
from .ranking import (
    _smart_truncate as _smart_truncate,
)
from .ranking import (
    _trim_action_fields as _trim_action_fields,
)
from .security import (
    _ALLOWED_SCHEMES as _ALLOWED_SCHEMES,
)
from .security import (
    _url_allowed as _url_allowed,
)
from .security import (
    _validate_url as _validate_url,
)
from .session import (
    SESSION_MAXSIZE as SESSION_MAXSIZE,
)
from .session import (
    SESSION_TTL_SECONDS as SESSION_TTL_SECONDS,
)
from .session import (
    Session as Session,
)
from .session import (
    SessionManager as SessionManager,
)
from .session import (
    sessions as sessions,
)
from .vision import (
    _decode_yolov8_like as _decode_yolov8_like,
)
from .vision import (
    _init_yolo as _init_yolo,
)
from .vision import (
    _letterbox as _letterbox,
)
from .vision import (
    _load_readability_js as _load_readability_js,
)
from .vision import (
    _nms_xyxy as _nms_xyxy,
)
from .vision import (
    yolo_detect_ui_components as yolo_detect_ui_components,
)

logger = logging.getLogger("larkscout_browser")

# ============================================================
# Config
# ============================================================

# ---- Rate limiting (in-memory semaphores) ----
_MAX_CONCURRENT_CAPTURE = int(os.environ.get("LARKSCOUT_MAX_CONCURRENT_CAPTURE", "10"))
_MAX_CONCURRENT_SESSIONS = int(os.environ.get("LARKSCOUT_MAX_CONCURRENT_SESSIONS", "20"))
_capture_sem = asyncio.Semaphore(_MAX_CONCURRENT_CAPTURE)
_session_sem = asyncio.Semaphore(_MAX_CONCURRENT_SESSIONS)


# ============================================================
# Models
# ============================================================
# ============================================================
# Routing: block resources
# ============================================================
_BLOCKED_KEYWORDS = frozenset(
    [
        "doubleclick",
        "googletagmanager",
        "google-analytics",
        "facebook.com/tr",
        "segment.com",
    ]
)

_BLOCKED_RESOURCE_TYPES = frozenset(["image", "media", "font"])


async def _request_allowed(url: str) -> bool:
    """Run the full (DNS-resolving) SSRF check off the event loop."""
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(None, _url_allowed, url)
    except Exception:
        return False


async def _setup_routing(context: BrowserContext, block_resources: bool):
    async def route_handler(route) -> None:
        req = route.request
        # Anti-SSRF (defense in depth): block ANY request — navigations, their
        # redirects, popups, and subresources (fetch/XHR/script/etc.) — to
        # private/loopback/metadata targets at the network layer, even when the
        # literal pre-check passed (DNS rebinding, redirect/fetch to an internal
        # host). A public page issuing fetch("http://169.254.169.254/...") still
        # sends the request regardless of CORS, so every request is validated.
        # Always installed, not just when block_resources is set.
        if not await _request_allowed(req.url):
            return await route.abort("addressunreachable")

        if block_resources:
            if req.resource_type in _BLOCKED_RESOURCE_TYPES:
                return await route.abort()
            url_lower = req.url.lower()
            if any(x in url_lower for x in _BLOCKED_KEYWORDS):
                return await route.abort()

        return await route.continue_()

    await context.route("**/*", route_handler)


# ============================================================
# Distiller JS
# ============================================================
# Added text density detection for div/section/td (better SPA scraping)
DISTILL_SIMPLE_JS = r"""
(extractTables, maxTableRows) => {
  function visible(el) {
    const style = window.getComputedStyle(el);
    if (!style) return false;
    if (style.visibility === "hidden" || style.display === "none") return false;
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return false;
    return true;
  }

  const candidates = [
    document.querySelector("article"),
    document.querySelector("main"),
    document.querySelector('[role="main"]'),
    document.body
  ].filter(Boolean);

  let root = candidates[0];
  let bestLen = 0;
  for (const c of candidates) {
    const t = (c.innerText || "").trim();
    if (t.length > bestLen) { bestLen = t.length; root = c; }
  }

  const nodes = root.querySelectorAll("h1,h2,h3,p,li,blockquote,pre,code");
  const seen = new Set();
  const blocks = [];
  for (const n of nodes) {
    if (!visible(n)) continue;
    const txt = (n.innerText || "").replace(/\s+/g, " ").trim();
    if (!txt) continue;
    if (txt.length < 20 && !["h1","h2","h3"].includes(n.tagName.toLowerCase())) continue;
    if (seen.has(txt)) continue;
    seen.add(txt);
    blocks.push({ tag: n.tagName.toLowerCase(), text: txt });
    if (blocks.length > 1500) break;
  }

  if (blocks.length < 10) {
    const containers = root.querySelectorAll("div, section, td");
    for (const d of containers) {
      if (!visible(d)) continue;
      let directText = "";
      for (const child of d.childNodes) {
        if (child.nodeType === 3) { directText += child.textContent; }
      }
      directText = directText.replace(/\s+/g, " ").trim();
      if (directText.length > 80 && !seen.has(directText)) {
        seen.add(directText);
        blocks.push({ tag: "p", text: directText });
      }
      if (blocks.length > 1500) break;
    }
  }

  // Table extraction: <table> → Markdown + numeric column stats
  const tables = [];
  if (extractTables) {
    const tableEls = root.querySelectorAll("table");
    for (const tbl of tableEls) {
      if (!visible(tbl)) continue;
      const capEl = tbl.querySelector("caption");
      const caption = capEl ? (capEl.innerText || "").replace(/\s+/g, " ").trim() : "";
      let heading = caption;
      if (!heading) {
        let prev = tbl.previousElementSibling;
        for (let i = 0; i < 3 && prev; i++) {
          const tag = prev.tagName.toLowerCase();
          if (["h1","h2","h3","h4","h5","h6"].includes(tag)) {
            heading = (prev.innerText || "").replace(/\s+/g, " ").trim().slice(0, 120);
            break;
          }
          prev = prev.previousElementSibling;
        }
      }
      const allRows = [];
      for (const tr of tbl.querySelectorAll("tr")) {
        const cells = [];
        for (const cell of tr.querySelectorAll("th, td")) {
          let txt = (cell.innerText || "").replace(/\s+/g, " ").trim();
          txt = txt.replace(/\|/g, "¦").replace(/\n/g, " ");
          cells.push(txt);
        }
        if (cells.length > 0) allRows.push({ cells, isHeader: tr.querySelectorAll("th").length > 0 });
      }
      if (allRows.length < 1) continue;
      const totalRows = allRows.length;
      const totalCols = Math.max(...allRows.map(r => r.cells.length));
      for (const row of allRows) { while (row.cells.length < totalCols) row.cells.push(""); }
      const truncated = totalRows > maxTableRows;
      const displayRows = truncated ? allRows.slice(0, maxTableRows) : allRows;
      let headerRow = null;
      let dataRows = displayRows;
      if (displayRows.length > 0 && displayRows[0].isHeader) {
        headerRow = displayRows[0].cells;
        dataRows = displayRows.slice(1);
      } else if (displayRows.length > 1) {
        const firstRow = displayRows[0].cells;
        if (firstRow.every(c => c.length < 30 && c.length > 0)) {
          headerRow = firstRow;
          dataRows = displayRows.slice(1);
        }
      }
      let md = "";
      if (headerRow) {
        md += "| " + headerRow.join(" | ") + " |\n";
        md += "| " + headerRow.map(() => "---").join(" | ") + " |\n";
      } else {
        const ah = [];
        for (let i = 0; i < totalCols; i++) ah.push("Col_" + (i + 1));
        md += "| " + ah.join(" | ") + " |\n";
        md += "| " + ah.map(() => "---").join(" | ") + " |\n";
      }
      for (const row of dataRows) { md += "| " + row.cells.join(" | ") + " |\n"; }
      if (truncated) { md += "\n[... " + totalRows + " rows total, showing first " + maxTableRows + " ...]"; }
      const stats = {};
      if (headerRow && allRows.length > 3) {
        for (let ci = 0; ci < totalCols; ci++) {
          const nums = [];
          for (const row of allRows.slice(1)) {
            const v = parseFloat(row.cells[ci].replace(/[,$%¥€£]/g, ""));
            if (!isNaN(v)) nums.push(v);
          }
          if (nums.length > allRows.length * 0.5) {
            const colName = headerRow[ci] || ("Col_" + (ci + 1));
            const sum = nums.reduce((a, b) => a + b, 0);
            stats[colName] = { min: Math.min(...nums), max: Math.max(...nums), avg: Math.round(sum / nums.length * 100) / 100, count: nums.length };
          }
        }
      }
      tables.push({
        tag: "table", text: md.trim(),
        table_meta: { rows: totalRows, cols: totalCols, has_header: !!headerRow, truncated, caption: caption || null, heading: heading || null, stats: Object.keys(stats).length > 0 ? stats : null }
      });
      if (tables.length >= 20) break;
    }
  }

  const title = (document.title || "").trim() || null;
  const url = location.href;
  return { title, url, blocks, tables };
}
"""

READABILITY_EVAL = r"""
(maxChars) => {
  const doc = document.cloneNode(true);
  doc.querySelectorAll("script, style, noscript, iframe").forEach(n => n.remove());
  const reader = new Readability(doc);
  const parsed = reader.parse();
  if (!parsed) return null;

  let text = (parsed.textContent || "").replace(/\n{3,}/g, "\n\n").trim();
  if (text.length > maxChars) text = text.slice(0, maxChars);

  return {
    title: parsed.title || document.title || null,
    byline: parsed.byline || null,
    excerpt: parsed.excerpt || null,
    siteName: parsed.siteName || null,
    url: location.href,
    text
  };
}
"""

# Standalone table extraction JS for Readability mode (Readability strips tables)
EXTRACT_TABLES_JS = r"""
(maxTableRows, maxTables) => {
  function visible(el) {
    const style = window.getComputedStyle(el);
    if (!style) return false;
    if (style.visibility === "hidden" || style.display === "none") return false;
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return false;
    return true;
  }
  const tables = [];
  const tableEls = document.querySelectorAll("table");
  for (const tbl of tableEls) {
    if (!visible(tbl)) continue;
    const capEl = tbl.querySelector("caption");
    const caption = capEl ? (capEl.innerText || "").replace(/\s+/g, " ").trim() : "";
    let heading = caption;
    if (!heading) {
      let prev = tbl.previousElementSibling;
      for (let i = 0; i < 3 && prev; i++) {
        const tag = prev.tagName.toLowerCase();
        if (["h1","h2","h3","h4","h5","h6"].includes(tag)) {
          heading = (prev.innerText || "").replace(/\s+/g, " ").trim().slice(0, 120);
          break;
        }
        prev = prev.previousElementSibling;
      }
    }
    const allRows = [];
    for (const tr of tbl.querySelectorAll("tr")) {
      const cells = [];
      for (const cell of tr.querySelectorAll("th, td")) {
        let txt = (cell.innerText || "").replace(/\s+/g, " ").trim();
        txt = txt.replace(/\|/g, "¦").replace(/\n/g, " ");
        cells.push(txt);
      }
      if (cells.length > 0) allRows.push({ cells, isHeader: tr.querySelectorAll("th").length > 0 });
    }
    if (allRows.length < 1) continue;
    const totalRows = allRows.length;
    const totalCols = Math.max(...allRows.map(r => r.cells.length));
    for (const row of allRows) { while (row.cells.length < totalCols) row.cells.push(""); }
    const truncated = totalRows > maxTableRows;
    const displayRows = truncated ? allRows.slice(0, maxTableRows) : allRows;
    let headerRow = null;
    let dataRows = displayRows;
    if (displayRows.length > 0 && displayRows[0].isHeader) {
      headerRow = displayRows[0].cells;
      dataRows = displayRows.slice(1);
    } else if (displayRows.length > 1) {
      const firstRow = displayRows[0].cells;
      if (firstRow.every(c => c.length < 30 && c.length > 0)) {
        headerRow = firstRow;
        dataRows = displayRows.slice(1);
      }
    }
    let md = "";
    if (headerRow) {
      md += "| " + headerRow.join(" | ") + " |\n";
      md += "| " + headerRow.map(() => "---").join(" | ") + " |\n";
    } else {
      const ah = [];
      for (let i = 0; i < totalCols; i++) ah.push("Col_" + (i + 1));
      md += "| " + ah.join(" | ") + " |\n";
      md += "| " + ah.map(() => "---").join(" | ") + " |\n";
    }
    for (const row of dataRows) { md += "| " + row.cells.join(" | ") + " |\n"; }
    if (truncated) { md += "\n[... " + totalRows + " rows total, showing first " + maxTableRows + " ...]"; }
    const stats = {};
    if (headerRow && allRows.length > 3) {
      for (let ci = 0; ci < totalCols; ci++) {
        const nums = [];
        for (const row of allRows.slice(1)) {
          const v = parseFloat(row.cells[ci].replace(/[,$%¥€£]/g, ""));
          if (!isNaN(v)) nums.push(v);
        }
        if (nums.length > allRows.length * 0.5) {
          const colName = headerRow[ci] || ("Col_" + (ci + 1));
          const sum = nums.reduce((a, b) => a + b, 0);
          stats[colName] = { min: Math.min(...nums), max: Math.max(...nums), avg: Math.round(sum / nums.length * 100) / 100, count: nums.length };
        }
      }
    }
    tables.push({
      tag: "table", text: md.trim(),
      table_meta: { rows: totalRows, cols: totalCols, has_header: !!headerRow, truncated, caption: caption || null, heading: heading || null, stats: Object.keys(stats).length > 0 ? stats : null }
    });
    if (tables.length >= maxTables) break;
  }
  return tables;
}
"""

ACTIONS_DOM_JS = r"""
(maxActions) => {
  function visible(el) {
    const style = window.getComputedStyle(el);
    if (!style) return false;
    if (style.visibility === "hidden" || style.display === "none") return false;
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return false;
    return true;
  }

  function getName(el) {
    const aria = el.getAttribute("aria-label");
    if (aria && aria.trim()) return aria.trim().slice(0, 200);
    const title = el.getAttribute("title");
    if (title && title.trim()) return title.trim().slice(0, 200);
    const ph = el.getAttribute("placeholder");
    if (ph && ph.trim()) return ph.trim().slice(0, 200);
    const txt = (el.innerText || "").replace(/\s+/g, " ").trim();
    if (txt) return txt.slice(0, 200);
    if (el.value && typeof el.value === "string") return el.value.slice(0, 200);
    return "";
  }

  function roleOf(el) {
    const r = (el.getAttribute("role") || "").toLowerCase().trim();
    if (r) return r;
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute("type") || "").toLowerCase();
    if (tag === "a") return "link";
    if (tag === "button") return "button";
    if (tag === "input") {
      if (["button","submit","reset"].includes(type)) return "button";
      if (type === "checkbox") return "checkbox";
      if (type === "radio") return "radio";
      return "textbox";
    }
    if (tag === "textarea") return "textbox";
    if (tag === "select") return "combobox";
    return "generic";
  }

  function cssPath(el) {
    const id = el.getAttribute("id");
    if (id && /^[A-Za-z][A-Za-z0-9\-_:.]{1,60}$/.test(id)) return `#${CSS.escape(id)}`;
    const dt = el.getAttribute("data-testid") || el.getAttribute("data-test") || el.getAttribute("data-qa");
    if (dt && dt.length < 80) return `${el.tagName.toLowerCase()}[data-testid="${dt}"]`;
    const aria = el.getAttribute("aria-label");
    if (aria && aria.length < 80) return `${el.tagName.toLowerCase()}[aria-label="${aria.replace(/"/g,'\\"')}"]`;

    let p = el;
    let parts = [];
    for (let i=0;i<4 && p && p.nodeType===1 && p !== document.body;i++) {
      const tag = p.tagName.toLowerCase();
      const parent = p.parentElement;
      if (!parent) break;
      const siblings = Array.from(parent.children).filter(x => x.tagName === p.tagName);
      const idx = siblings.indexOf(p) + 1;
      parts.unshift(`${tag}:nth-of-type(${idx})`);
      p = parent;
    }
    return parts.length ? parts.join(" > ") : el.tagName.toLowerCase();
  }

  const selector = [
    "button", "a[href]", "input", "textarea", "select",
    "[role='button']", "[role='link']", "[role='textbox']",
    "[role='combobox']", "[onclick]", "[tabindex]"
  ].join(",");

  const all = Array.from(document.querySelectorAll(selector));
  const out = [];

  for (const el of all) {
    if (!visible(el)) continue;
    const role = roleOf(el);
    if (role === "generic") continue;

    const name = getName(el);
    const tag = el.tagName.toLowerCase();
    const isInputLike = ["input","textarea","select"].includes(tag) || ["textbox","combobox"].includes(role);
    if (!name && !isInputLike) continue;

    const actions = [];
    if (["button","link","checkbox","radio","menuitem","tab"].includes(role)) actions.push("click");
    if (role === "textbox") actions.push("type");
    if (role === "combobox") actions.push("select");
    actions.push("scroll_into_view");

    out.push({ role, name, strategy: { css: cssPath(el) }, actions });
    if (out.length >= maxActions) break;
  }
  return out;
}
"""

MAP_BOX_TO_ELEMENT = r"""
(cx, cy) => {
  const el = document.elementFromPoint(cx, cy);
  if (!el) return null;

  const interactive = el.closest(
    "button, a[href], input, textarea, select, [role='button'], [role='link'], [role='textbox'], [role='combobox'], [tabindex]"
  );
  const target = interactive || el;

  function nameOf(x) {
    const aria = x.getAttribute("aria-label");
    if (aria && aria.trim()) return aria.trim().slice(0,200);
    const title = x.getAttribute("title");
    if (title && title.trim()) return title.trim().slice(0,200);
    const ph = x.getAttribute("placeholder");
    if (ph && ph.trim()) return ph.trim().slice(0,200);
    const txt = (x.innerText || "").replace(/\s+/g, " ").trim();
    if (txt) return txt.slice(0,200);
    return "";
  }

  function roleOf(x) {
    const r = (x.getAttribute("role") || "").toLowerCase().trim();
    if (r) return r;
    const tag = x.tagName.toLowerCase();
    const type = (x.getAttribute("type") || "").toLowerCase();
    if (tag === "a") return "link";
    if (tag === "button") return "button";
    if (tag === "input") {
      if (["button","submit","reset"].includes(type)) return "button";
      if (type === "checkbox") return "checkbox";
      if (type === "radio") return "radio";
      return "textbox";
    }
    if (tag === "textarea") return "textbox";
    if (tag === "select") return "combobox";
    return tag;
  }

  function cssPath(x) {
    const id = x.getAttribute("id");
    if (id && /^[A-Za-z][A-Za-z0-9\-_:.]{1,60}$/.test(id)) return `#${CSS.escape(id)}`;
    const dt = x.getAttribute("data-testid") || x.getAttribute("data-test") || x.getAttribute("data-qa");
    if (dt && dt.length < 80) return `${x.tagName.toLowerCase()}[data-testid="${dt}"]`;
    const aria = x.getAttribute("aria-label");
    if (aria && aria.length < 80) return `${x.tagName.toLowerCase()}[aria-label="${aria.replace(/"/g,'\\"')}"]`;
    return x.tagName.toLowerCase();
  }

  return { role: roleOf(target), name: nameOf(target), css: cssPath(target) };
}
"""


# ============================================================
# ✅ WebMCP Discovery + Invocation JS
# ============================================================
WEBMCP_DISCOVER_JS = r"""
() => {
  const result = {
    available: false,
    imperative_tools: [],
    declarative_tools: [],
    errors: []
  };

  // ---- 1. Imperative API: navigator.modelContext ----
  try {
    const mc = navigator.modelContext;
    if (mc) {
      result.available = true;
      // Chrome 146+ exposes getTools() or .tools property
      let tools = [];
      if (typeof mc.getTools === 'function') {
        tools = mc.getTools();
      } else if (mc.tools && Array.isArray(mc.tools)) {
        tools = mc.tools;
      }
      for (const t of tools) {
        result.imperative_tools.push({
          name: t.name || "",
          description: t.description || "",
          inputSchema: t.inputSchema || null,
          readOnly: !!(t.annotations && t.annotations.readOnlyHint),
          source: "webmcp_imperative"
        });
      }
    }
  } catch(e) {
    result.errors.push("imperative: " + e.message);
  }

  // ---- 2. Declarative API: form[toolname] ----
  try {
    const forms = document.querySelectorAll("form[toolname]");
    for (const form of forms) {
      const toolName = form.getAttribute("toolname") || "";
      const toolDesc = form.getAttribute("tooldescription") || "";
      const autoSubmit = form.hasAttribute("toolautosubmit");
      if (!toolName) continue;

      const properties = {};
      const required = [];
      const fields = form.querySelectorAll("input, textarea, select");
      for (const f of fields) {
        const name = f.getAttribute("name");
        if (!name) continue;
        const paramDesc = f.getAttribute("toolparamdescription") || "";
        const tag = f.tagName.toLowerCase();
        const type = (f.getAttribute("type") || "text").toLowerCase();
        let fieldType = "string";
        if (type === "number" || type === "range") fieldType = "number";
        if (type === "checkbox") fieldType = "boolean";
        const prop = { type: fieldType };
        if (paramDesc) prop.description = paramDesc;
        if (f.getAttribute("placeholder")) {
          prop.description = (prop.description ? prop.description + " " : "") + "(e.g. " + f.getAttribute("placeholder") + ")";
        }
        if (tag === "select") {
          const opts = Array.from(f.querySelectorAll("option")).map(o => o.value).filter(v => v);
          if (opts.length > 0) prop.enum = opts;
        }
        properties[name] = prop;
        if (f.hasAttribute("required")) required.push(name);
      }

      result.declarative_tools.push({
        name: toolName,
        description: toolDesc,
        inputSchema: { type: "object", properties, required },
        autoSubmit,
        readOnly: false,
        source: "webmcp_declarative"
      });
    }
  } catch(e) {
    result.errors.push("declarative: " + e.message);
  }

  return result;
}
"""

WEBMCP_INVOKE_IMPERATIVE_JS = r"""
async (toolName, params) => {
  const mc = navigator.modelContext;
  if (!mc) throw new Error("modelContext not available");

  // Prefer invokeTool if browser exposes it
  if (typeof mc.invokeTool === 'function') {
    const result = await mc.invokeTool(toolName, params);
    return { success: true, result };
  }

  // Fallback: find tool's execute callback
  let tool = null;
  if (typeof mc.getTools === 'function') {
    tool = mc.getTools().find(t => t.name === toolName);
  } else if (mc.tools && Array.isArray(mc.tools)) {
    tool = mc.tools.find(t => t.name === toolName);
  }
  if (!tool) throw new Error("tool not found: " + toolName);
  if (typeof tool.execute !== 'function') throw new Error("tool has no execute: " + toolName);

  const result = await tool.execute(params);
  return { success: true, result };
}
"""

WEBMCP_INVOKE_DECLARATIVE_JS = r"""
async (toolName, params, autoSubmit) => {
  const form = document.querySelector('form[toolname="' + toolName + '"]');
  if (!form) throw new Error("form not found: " + toolName);

  // Fill form fields
  for (const [key, value] of Object.entries(params)) {
    const field = form.querySelector('[name="' + key + '"]');
    if (!field) continue;
    const tag = field.tagName.toLowerCase();
    const type = (field.getAttribute("type") || "text").toLowerCase();

    if (tag === "select") {
      field.value = value;
      field.dispatchEvent(new Event("change", { bubbles: true }));
    } else if (type === "checkbox") {
      field.checked = !!value;
      field.dispatchEvent(new Event("change", { bubbles: true }));
    } else if (type === "radio") {
      const radio = form.querySelector('[name="' + key + '"][value="' + value + '"]');
      if (radio) { radio.checked = true; radio.dispatchEvent(new Event("change", { bubbles: true })); }
    } else {
      field.value = value;
      field.dispatchEvent(new Event("input", { bubbles: true }));
      field.dispatchEvent(new Event("change", { bubbles: true }));
    }
  }

  if (autoSubmit) {
    form.requestSubmit();
    await new Promise(r => setTimeout(r, 500));
    return { success: true, submitted: true };
  }

  return { success: true, submitted: false, message: "Fields populated. autoSubmit not enabled." };
}
"""


# ============================================================
# Readability loader
# ============================================================
# ============================================================
# Distill: blocks -> stable sections
# ============================================================
def _blocks_to_sections_stable(
    blocks: list[dict[str, str]],
    max_sections: int,
    max_section_chars: int,
    total_budget: int,
    tables: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    # (heading, text, type, table_meta)
    sections_raw: list[tuple[str | None, str, str, dict | None]] = []
    cur_h: str | None = None
    cur: list[str] = []

    def flush() -> None:
        nonlocal cur_h, cur
        if not cur:
            return
        txt = _normalize("\n\n".join(cur))
        if not txt:
            cur = []
            return
        txt = _clip(txt, max_section_chars)
        sections_raw.append((cur_h, txt, "text", None))
        cur = []

    for b in blocks:
        tag = b.get("tag")
        text = (b.get("text") or "").strip()
        if not text:
            continue
        if tag in ("h1", "h2", "h3"):
            flush()
            cur_h = text[:120]
            continue
        cur.append(text)
        if len(cur) > 40:
            flush()
            cur_h = None
        if len(sections_raw) >= max_sections:
            break
    flush()

    # Tables appended as separate sections after text sections
    if tables:
        for tbl in tables:
            if len(sections_raw) >= max_sections:
                break
            tbl_text = (tbl.get("text") or "").strip()
            if not tbl_text:
                continue
            tbl_meta = tbl.get("table_meta") or {}
            tbl_heading = tbl_meta.get("heading") or tbl_meta.get("caption") or None
            if not tbl_heading:
                first_line = tbl_text.split("\n")[0].replace("|", " ").strip()
                if first_line:
                    tbl_heading = f"{t('table_prefix')} {_smart_truncate(first_line, 60)}"
                else:
                    tbl_heading = t("table_prefix")
            else:
                tbl_heading = f"{t('table_prefix')} {tbl_heading}"
            tbl_text_clipped = _clip(tbl_text, max_section_chars)
            sections_raw.append((tbl_heading, tbl_text_clipped, "table", tbl_meta))

    out: list[dict[str, Any]] = []
    used = 0
    seen = set()

    for h, body, sec_type, tbl_meta in sections_raw[:max_sections]:
        if used >= total_budget:
            break
        remain = total_budget - used
        if len(body) > remain:
            body = _clip(body, max(220, remain))

        sid = _make_stable_sid(h, body)
        if sid in seen:
            sid = sid + "_" + str(len(seen))
        seen.add(sid)

        # Auto-use first sentence as heading when empty
        effective_h = h
        if not effective_h:
            first_sentence = re.split(r"[.!?。！？\n]", body)[0].strip()
            if first_sentence and len(first_sentence) > 10:
                effective_h = _smart_truncate(first_sentence, 80)

        section: dict[str, Any] = {"sid": sid, "h": effective_h, "t": body, "type": sec_type}
        if sec_type == "table" and tbl_meta:
            section["table_meta"] = tbl_meta
        out.append(section)
        used += len(body)

    return out


# ============================================================
# Actions: DOM + A11y + Vision
# ============================================================
async def _extract_actions_dom(page: Page, max_actions: int) -> list[dict[str, Any]]:
    raw = await page.evaluate(ACTIONS_DOM_JS, max_actions)
    actions = []
    for ra in raw:
        role = (ra.get("role") or "").strip()
        name = (ra.get("name") or "").strip()
        css = (ra.get("strategy") or {}).get("css") or ""
        acts = ra.get("actions") or _pick_action_methods(role)

        if name and role in ("button", "link", "checkbox", "radio", "textbox", "combobox"):
            strategy = {"type": "role", "role": role, "name": name}
        else:
            strategy = {"type": "css", "selector": css}

        aid = _aid({"role": role, "name": name, "strategy": strategy})
        actions.append(
            {
                "aid": aid,
                "role": role,
                "name": name,
                "strategy": strategy,
                "actions": acts,
                "confidence": 0.8,
                "source": "dom",
            }
        )
    return actions


async def _extract_actions_a11y(page: Page, max_actions: int) -> tuple[list[dict[str, Any]], str]:
    # 1) Preferred: accessibility.snapshot
    try:
        acc = getattr(page, "accessibility", None)
        if acc is not None and hasattr(acc, "snapshot"):
            snap = await acc.snapshot(interesting_only=False)
            out: list[tuple[str, str]] = []

            def walk(node) -> None:
                if not node:
                    return
                role = (node.get("role") or "").lower()
                name = (node.get("name") or "").strip()
                if role in ("button", "link", "textbox", "combobox", "checkbox", "radio") and name:
                    out.append((role, name))
                for ch in node.get("children") or []:
                    walk(ch)

            walk(snap)

            seen = set()
            uniq: list[tuple[str, str]] = []
            for r, n in out:
                if (r, n) not in seen:
                    seen.add((r, n))
                    uniq.append((r, n))
                    if len(uniq) >= max_actions:
                        break

            actions: list[dict[str, Any]] = []
            for role, name in uniq:
                strategy = {"type": "role", "role": role, "name": name}
                aid = _aid({"role": role, "name": name, "strategy": strategy})
                actions.append(
                    {
                        "aid": aid,
                        "role": role,
                        "name": name,
                        "strategy": strategy,
                        "actions": _pick_action_methods(role),
                        "confidence": 0.85,
                        "source": "a11y",
                    }
                )
            return actions, "accessibility.snapshot"
    except Exception:
        pass

    # 2) Fallback: aria snapshot
    try:
        snap_text = await page.locator("body").aria_snapshot()
    except Exception:
        return [], "unavailable"

    roles = {"button", "link", "textbox", "combobox", "checkbox", "radio"}
    out2: list[tuple[str, str]] = []
    line_re = re.compile(r'^\s*-\s*([A-Za-z0-9_-]+)\s+"(.*)"\s*$', re.M)

    for mm in line_re.finditer(snap_text or ""):
        role = (mm.group(1) or "").strip().lower()
        name = (mm.group(2) or "").strip()
        if role in roles and name:
            out2.append((role, name.replace(r"\"", '"')))
        if len(out2) >= max_actions * 3:
            break

    seen = set()
    uniq2: list[tuple[str, str]] = []
    for r, n in out2:
        if (r, n) not in seen:
            seen.add((r, n))
            uniq2.append((r, n))
            if len(uniq2) >= max_actions:
                break

    actions2: list[dict[str, Any]] = []
    for role, name in uniq2:
        strategy = {"type": "role", "role": role, "name": name}
        aid = _aid({"role": role, "name": name, "strategy": strategy})
        actions2.append(
            {
                "aid": aid,
                "role": role,
                "name": name,
                "strategy": strategy,
                "actions": _pick_action_methods(role),
                "confidence": 0.82,
                "source": "a11y",
            }
        )
    return actions2, "aria_snapshot"


async def _extract_actions_vision(page: Page, req: DistillRequest) -> list[dict[str, Any]]:
    if not vision.YOLO_ENABLED or not req.enable_vision_fallback:
        return []

    try:
        img_bytes = await page.screenshot(full_page=False)
    except Exception:
        return []

    dets = yolo_detect_ui_components(
        image_bytes=img_bytes,
        conf_thresh=req.vision_conf_thresh,
        iou_thresh=req.vision_iou_thresh,
        max_boxes=req.vision_max_boxes,
    )
    if not dets:
        return []

    actions: list[dict[str, Any]] = []
    for d in dets:
        x1, y1, x2, y2 = d["bbox"]
        cx, cy = (x1 + x2) / 2, (y1 + y2) / 2

        info = await page.evaluate(MAP_BOX_TO_ELEMENT, cx, cy)
        if not info:
            continue

        role = (info.get("role") or "").strip().lower()
        name = (info.get("name") or "").strip()
        css = (info.get("css") or "").strip()

        if d.get("type") == "textbox" and role in ("div", "span", "generic", ""):
            role = "textbox"
        if not role:
            role = "button"

        if name and role in ("button", "link", "checkbox", "radio", "textbox", "combobox"):
            strategy = {"type": "role", "role": role, "name": name}
        else:
            if not css:
                continue
            strategy = {"type": "css", "selector": css}

        aid = _aid({"role": role, "name": name, "strategy": strategy})
        actions.append(
            {
                "aid": aid,
                "role": role,
                "name": name,
                "strategy": strategy,
                "actions": _pick_action_methods(role),
                "confidence": float(d.get("score", 0.6)),
                "source": "vision",
            }
        )
    return actions


# ============================================================
# ✅ WebMCP: discover + invoke
# ============================================================
async def _discover_webmcp_tools(session: Session, force: bool = False) -> dict[str, Any]:
    """Discover WebMCP tools on page (imperative + declarative), cached per session."""
    if not force and session.webmcp_tools is not None:
        return {"available": session.webmcp_available, "tools": session.webmcp_tools, "errors": []}

    try:
        raw = await session.page.evaluate(WEBMCP_DISCOVER_JS)
    except Exception as e:
        logger.warning("webmcp discover failed: %s", e)
        session.webmcp_available = False
        session.webmcp_tools = []
        return {"available": False, "tools": [], "errors": [str(e)]}

    available = raw.get("available", False)
    all_tools: list[dict[str, Any]] = []

    for tool in raw.get("imperative_tools", []):
        all_tools.append(
            {
                "name": tool.get("name", ""),
                "description": tool.get("description", ""),
                "input_schema": tool.get("inputSchema"),
                "read_only": tool.get("readOnly", False),
                "source": "webmcp_imperative",
            }
        )

    for tool in raw.get("declarative_tools", []):
        all_tools.append(
            {
                "name": tool.get("name", ""),
                "description": tool.get("description", ""),
                "input_schema": tool.get("inputSchema"),
                "read_only": tool.get("readOnly", False),
                "auto_submit": tool.get("autoSubmit", False),
                "source": "webmcp_declarative",
            }
        )
        if not available:
            available = True

    session.webmcp_available = available
    session.webmcp_tools = all_tools
    return {"available": available, "tools": all_tools, "errors": raw.get("errors", [])}


async def _invoke_webmcp_tool(
    session: Session, tool_name: str, params: dict[str, Any], timeout_ms: int = 30000
) -> dict[str, Any]:
    """Invoke a WebMCP tool (auto-detect imperative vs declarative)."""
    if session.webmcp_tools is None:
        await _discover_webmcp_tools(session)

    tool = next((t for t in (session.webmcp_tools or []) if t["name"] == tool_name), None)
    if not tool:
        return {"success": False, "error": f"tool not found: {tool_name}"}

    url_before = session.page.url
    try:
        if tool["source"] == "webmcp_imperative":
            result = await session.page.evaluate(WEBMCP_INVOKE_IMPERATIVE_JS, tool_name, params)
        elif tool["source"] == "webmcp_declarative":
            auto_submit = tool.get("auto_submit", False)
            result = await session.page.evaluate(
                WEBMCP_INVOKE_DECLARATIVE_JS, tool_name, params, auto_submit
            )
        else:
            return {"success": False, "error": f"unknown source: {tool['source']}"}

        try:
            await session.page.wait_for_load_state(
                "domcontentloaded", timeout=min(timeout_ms, 5000)
            )
        except Exception:
            pass
        await _maybe_switch_to_new_page(session)

        return {
            "success": True,
            "result": result,
            "url_before": url_before,
            "url_after": session.page.url,
        }
    except Exception as e:
        return {
            "success": False,
            "error": f"{type(e).__name__}: {e}",
            "url_before": url_before,
            "url_after": session.page.url,
        }


# ============================================================
# Distill core
# ============================================================
async def _distill(session: Session, req: DistillRequest) -> dict[str, Any]:
    page = session.page
    url = page.url
    title = None

    # optional wait_for_selector (SPA-friendly)
    if req.wait_for_selector:
        try:
            await page.wait_for_selector(req.wait_for_selector, timeout=req.wait_for_timeout_ms)
        except Exception:
            pass  # best-effort, continue on timeout

    mode = req.distill_mode
    if mode == "auto":
        mode = "readability" if vision.READABILITY_AVAILABLE else "simple"

    blocks: list[dict[str, str]] = []
    readability_meta = {}
    extracted_tables: list[dict[str, Any]] = []

    if mode == "readability":
        if not vision.READABILITY_AVAILABLE or not vision.READABILITY_JS:
            mode = "simple"
        else:
            # avoid re-injecting Readability.js
            already = await page.evaluate("typeof Readability !== 'undefined'")
            if not already:
                await page.add_script_tag(content=vision.READABILITY_JS)

            data = await page.evaluate(READABILITY_EVAL, 40000)
            if not data or not (data.get("text") or "").strip():
                mode = "simple"
            else:
                title = data.get("title") or await page.title()
                url = data.get("url") or page.url
                readability_meta = {
                    "byline": data.get("byline"),
                    "excerpt": data.get("excerpt"),
                    "siteName": data.get("siteName"),
                }
                paras = [p.strip() for p in (data.get("text") or "").split("\n\n") if p.strip()]
                for p in paras[:2000]:
                    blocks.append({"tag": "p", "text": p})

                # Table extraction: Readability strips <table>, extract from original DOM
                if req.extract_tables:
                    try:
                        extracted_tables = (
                            await page.evaluate(
                                EXTRACT_TABLES_JS, req.max_table_rows, req.max_tables
                            )
                            or []
                        )
                    except Exception:
                        extracted_tables = []

    if mode == "simple":
        dist = await page.evaluate(DISTILL_SIMPLE_JS, req.extract_tables, req.max_table_rows)
        blocks = dist.get("blocks") or []
        extracted_tables = dist.get("tables") or []
        url = dist.get("url") or page.url
        title = dist.get("title") or await page.title()

    sections = _blocks_to_sections_stable(
        blocks=blocks,
        max_sections=req.max_sections,
        max_section_chars=req.max_section_chars,
        total_budget=req.total_text_budget_chars,
        tables=extracted_tables if req.extract_tables else None,
    )

    joined = "\n\n".join(s["t"] for s in sections)
    content_hash = _hash_text((title or "") + "\n" + (url or "") + "\n" + joined)

    a11y_attempted = False
    a11y_mode = None
    a11y_error = None
    webmcp_result: dict[str, Any] = {"available": False, "tools": [], "errors": []}

    actions: list[dict[str, Any]] = []
    if req.include_actions:
        # WebMCP: prefer structured tools (highest confidence)
        webmcp_result = await _discover_webmcp_tools(session)
        for wt in webmcp_result.get("tools", []):
            aid = _aid({"webmcp": wt["name"], "source": wt["source"]})
            actions.append(
                {
                    "aid": aid,
                    "role": "webmcp_tool",
                    "name": f"[WebMCP] {wt['name']}: {(wt.get('description') or '')[:80]}",
                    "strategy": {
                        "type": "webmcp",
                        "tool_name": wt["name"],
                        "source": wt["source"],
                        "input_schema": wt.get("input_schema"),
                    },
                    "actions": ["invoke"],
                    "confidence": 0.95,
                    "source": wt["source"],
                }
            )

        # Original DOM extraction
        actions.extend(await _extract_actions_dom(page, max_actions=req.max_actions))

        if req.enable_a11y_fallback and len(actions) < req.min_actions_before_fallback:
            a11y_attempted = True
            try:
                a11y_actions, a11y_mode = await _extract_actions_a11y(
                    page, max_actions=req.max_actions
                )
                actions.extend(a11y_actions)
            except Exception as e:
                a11y_error = f"{type(e).__name__}: {e}"

        if req.enable_vision_fallback and len(actions) < req.min_actions_before_fallback:
            actions.extend(await _extract_actions_vision(page, req))

        actions = _dedup_actions(actions)

    # Table stats
    table_sections = [s for s in sections if s.get("type") == "table"]

    meta = {
        "mode": mode,
        "readability_available": vision.READABILITY_AVAILABLE,
        "yolo_enabled": vision.YOLO_ENABLED,
        "a11y": {
            "attempted": a11y_attempted,
            "mode": a11y_mode,
            "error": (a11y_error[:200] if a11y_error else None),
        },
        # WebMCP: meta info
        "webmcp": {
            "available": webmcp_result.get("available", False),
            "tools_count": len(webmcp_result.get("tools", [])),
            "errors": webmcp_result.get("errors", []),
        },
        "blocks_count": len(blocks),
        "sections_count": len(sections),
        "table_sections_count": len(table_sections),
        "tables_extracted": len(extracted_tables),
        "actions_count_raw": len(actions),
        "readability": readability_meta,
    }

    sections, actions, meta = _apply_total_output_budget(
        sections=sections,
        actions=actions,
        meta=meta,
        total_budget=req.total_output_budget_chars,
        min_actions_to_keep=req.min_actions_to_keep,
        name_max=req.max_action_name_chars,
        selector_max=req.max_selector_chars,
    )

    meta["actions_count"] = len(actions)
    meta["sections_count"] = len(sections)
    meta["budget"] = {
        "total_output_budget_chars": req.total_output_budget_chars,
        "total_text_budget_chars": req.total_text_budget_chars,
        "max_actions": req.max_actions,
        "min_actions_to_keep": req.min_actions_to_keep,
    }

    session.last_distill = {
        "url": url,
        "title": title,
        "content_hash": content_hash,
        "sections": sections,
        "actions": actions,
        "meta": meta,
    }
    session.action_map = {a["aid"]: a for a in actions}
    return session.last_distill


async def _ensure_session(session_id: str) -> Session:
    sess = await sessions.get(session_id)
    if not sess or sess.closed:
        raise HTTPException(404, "session not found or expired")
    return sess


# ============================================================
# Action executor
# ============================================================
async def _locate(page: Page, strategy: dict[str, Any]):
    stype = strategy.get("type")
    if stype == "role":
        return page.get_by_role(strategy["role"], name=strategy.get("name") or "").first
    if stype == "css":
        sel = strategy.get("selector") or ""
        if not sel:
            raise RuntimeError("empty css selector")
        return page.locator(sel).first
    raise RuntimeError(f"unknown strategy type: {stype}")


# detect popup/new tab, switch to latest page
async def _maybe_switch_to_new_page(sess: Session):
    pages = sess.context.pages
    if len(pages) > 1 and pages[-1] != sess.page:
        sess.page = pages[-1]
        logger.info("switched to new tab: %s", sess.page.url)


# ============================================================
# lifespan replaces deprecated on_event
# ============================================================
_pw = None
_browser: Browser | None = None


# ============================================================
# Document library helpers (shared with docreader)
# ============================================================


_web_counter_lock = threading.Lock()
_web_index_lock = threading.Lock()


def _next_web_doc_id(docs_dir: Path) -> str:
    """Allocate the next WEB-xxx doc ID using a file-based counter."""
    with _web_counter_lock:
        counter_path = docs_dir / ".web_counter"
        counter = 1
        if counter_path.exists():
            try:
                counter = int(counter_path.read_text(encoding="utf-8").strip())
            except ValueError:
                counter = 1
        doc_id = f"WEB-{counter:03d}"
        tmp = counter_path.with_suffix(".tmp")
        tmp.write_text(str(counter + 1), encoding="utf-8")
        os.replace(tmp, counter_path)
        return doc_id


def _build_web_digest(
    title: str | None, sections: list[dict[str, Any]], max_chars: int = 600
) -> str:
    """Build a short digest from page title and section headings/snippets."""
    parts: list[str] = []
    if title:
        parts.append(f"# {title}")
    for sec in sections:
        if sec.get("type") == "table":
            continue
        h = sec.get("h") or ""
        snippet = (sec.get("t") or "")[:120]
        parts.append(f"## {h}\n{snippet}" if h else snippet)
        if sum(len(p) for p in parts) >= max_chars:
            break
    return "\n\n".join(parts)[:max_chars]


def _safe_heading(h: str | None, max_len: int = 40) -> str:
    """Sanitize a heading for use in filenames."""
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", h or "").strip().replace(" ", "-")
    return (safe[:max_len] if len(safe) > max_len else safe) or "section"


def _build_manifest_sections(
    text_sections: list[dict[str, Any]],
    table_sections: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build unified manifest section entries for a web capture."""
    result: list[dict[str, Any]] = []
    for i, s in enumerate(text_sections, 1):
        sid = s.get("sid", f"s_{i:03d}")
        h = s.get("h", "")
        safe_h = _safe_heading(h)
        result.append({
            "sid": sid, "index": i, "title": h,
            "char_count": len(s.get("t", "")), "type": "text",
            "file": f"sections/{i:02d}-{sid}-{safe_h}.md",
        })
    for i, s in enumerate(table_sections, 1):
        result.append({
            "sid": s.get("sid", f"t_{i:03d}"),
            "index": len(text_sections) + i,
            "title": s.get("h", f"Table {i}"),
            "char_count": len(s.get("t", "")), "type": "table",
            "file": f"tables/table-{i:02d}.md",
        })
    return result


def _persist_web_capture(
    doc_id: str,
    url: str,
    title: str | None,
    sections: list[dict[str, Any]],
    digest: str,
    tags: list[str],
    content_hash: str,
    docs_dir: Path,
    content_type: str = "General",
) -> None:
    """Write a web capture to the document library and update doc-index.json."""
    normalized_content_type = _normalize_content_type(content_type)
    storage_path = _doc_storage_rel_path(doc_id, normalized_content_type)
    doc_dir = docs_dir / storage_path
    sections_dir = doc_dir / "sections"
    tables_dir = doc_dir / "tables"
    doc_dir.mkdir(parents=True, exist_ok=True)
    sections_dir.mkdir(exist_ok=True)

    now_str = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    text_sections = [s for s in sections if s.get("type") != "table"]
    table_sections = [s for s in sections if s.get("type") == "table"]

    # digest.md
    _write_text_atomic(doc_dir / "digest.md", f"# {doc_id}: {title or url}\n\n{digest}\n")

    # sections/
    for i, sec in enumerate(text_sections, 1):
        sid = sec.get("sid", f"s_{i:03d}")
        h = sec.get("h") or ""
        body = sec.get("t", "")
        safe_h = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", h).strip().replace(" ", "-")[:40] or "section"
        fname = f"{i:02d}-{sid}-{safe_h}.md"
        header = f"## {h}\n\n" if h else ""
        _write_text_atomic(sections_dir / fname, f"{header}{body}\n")

    # tables/
    if table_sections:
        tables_dir.mkdir(exist_ok=True)
        for i, tbl in enumerate(table_sections, 1):
            h = tbl.get("h") or f"Table {i}"
            body = tbl.get("t", "")
            meta = tbl.get("table_meta") or {}
            meta_comment = f"\n<!-- table_meta: {json.dumps(meta)} -->\n" if meta else ""
            _write_text_atomic(tables_dir / f"table-{i:02d}.md", f"# {h}\n\n{body}\n{meta_comment}")

    # manifest.json
    manifest: dict[str, Any] = {
        "doc_id": doc_id,
        "filename": title or url,
        "file_type": "web_capture",
        "source": "web_capture",
        "content_type": normalized_content_type,
        "storage_path": storage_path,
        "tags": list(tags) if tags else [],
        "paths": {
            "digest": "digest.md",
            "sections_dir": "sections/",
            **({"tables_dir": "tables/"} if table_sections else {}),
        },
        "sections": _build_manifest_sections(text_sections, table_sections),
        "provenance": {
            "source": "web_capture",
            "source_url": url,
            "created_at": now_str,
            "content_hash": content_hash,
        },
    }
    _write_text_atomic(
        doc_dir / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2)
    )

    # doc-index.json (v2, shared with docreader) — locked + atomic write
    with _web_index_lock:
        index_path = docs_dir / "doc-index.json"
        if index_path.exists():
            try:
                with open(index_path, encoding="utf-8") as f:
                    index: dict[str, Any] = json.load(f)
            except Exception:
                index = {"version": 2, "documents": []}
        else:
            index = {"version": 2, "documents": []}

        index["version"] = 2
        if not isinstance(index.get("documents"), list):
            index["documents"] = []
        index["documents"] = [d for d in index["documents"] if d.get("id") != doc_id]
        index["documents"].append({
            "id": doc_id,
            "filename": title or url,
            "file_type": "web_capture",
            "content_type": normalized_content_type,
            "storage_path": storage_path,
            "source": "web_capture",
            "source_url": url,
            "pages": 1,
            "sections": len(text_sections),
            "ocr_pages": 0,
            "tables": len(table_sections),
            "digest": digest[:200],
            "digest_path": f"docs/{storage_path}/digest.md",
            "tags": tags,
            "created_at": now_str,
            "content_hash": content_hash,
        })
        index["last_updated"] = now_str
        tmp_path = index_path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp_path, index_path)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    global _pw, _browser
    _load_readability_js()
    _init_yolo()

    _pw = await async_playwright().start()
    try:
        _browser = await _pw.chromium.launch(
            headless=True,
            args=[
                "--disable-gpu",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-background-networking",
                "--disable-default-apps",
                "--disable-extensions",
                # WebMCP: enable Chrome 146+ WebMCP features
                "--enable-features=WebMCP",
            ],
        )
    except Exception:
        await _pw.stop()
        _pw = None
        raise

    # background cleanup task
    async def cleanup_loop() -> None:
        while True:
            await asyncio.sleep(60)
            try:
                await sessions.cleanup()
            except Exception:
                logger.exception("cleanup error")

    cleanup_task = asyncio.create_task(cleanup_loop())

    yield  # ---- app running ----

    cleanup_task.cancel()
    await sessions.close_all()

    if _browser:
        await _browser.close()
    if _pw:
        await _pw.stop()


app = FastAPI(
    title="Agent Browser Service (Playwright + WebMCP)", version="0.6.0", lifespan=lifespan
)


# ============================================================
# Routes
# ============================================================
@app.get("/health")
async def health() -> dict:
    return {
        "ok": True,
        "sessions": len(sessions),
        "readability_available": vision.READABILITY_AVAILABLE,
        "readability_js_path": _mask_path(vision.READABILITY_JS_PATH),
        "yolo_enabled": vision.YOLO_ENABLED,
        "yolo_onnx_path": _mask_path(vision.YOLO_ONNX_PATH) if vision.YOLO_ONNX_PATH else None,
        "yolo_input_size": vision.YOLO_INPUT_SIZE,
        "webmcp_support": True,
    }


@app.post("/session/new", response_model=NewSessionResponse)
async def new_session(req: NewSessionRequest) -> NewSessionResponse:
    if _session_sem.locked():
        raise HTTPException(429, "too many concurrent session creations")
    async with _session_sem:
        if not _browser:
            raise HTTPException(500, "browser not ready")

        context = await _browser.new_context(
            user_agent=req.user_agent,
            locale=req.lang,
            viewport=req.viewport,
            storage_state=req.storage_state,
            extra_http_headers={"Accept-Language": f"{req.lang},en;q=0.9"},
        )
        await _setup_routing(context, req.block_resources)
        page = await context.new_page()

        # secrets.token_hex replaces sha1(time) to avoid collision
        sid = "s_" + secrets.token_hex(8)
        sess = Session(context=context, page=page, lang=req.lang)
        await sessions.put(sid, sess)
        return NewSessionResponse(session_id=sid)


@app.post("/session/goto", response_model=GotoResponse)
async def goto(req: GotoRequest) -> GotoResponse:
    _validate_url(req.url)
    sess = await _ensure_session(req.session_id)
    async with sess.lock:  # concurrency lock
        try:
            await sess.page.goto(req.url, wait_until=req.wait_until, timeout=req.timeout_ms)
        except Exception as e:
            raise HTTPException(502, f"goto failed: {e}")
        # WebMCP: new page needs tool re-discovery
        sess.webmcp_tools = None
        sess.webmcp_available = False
        title = None
        try:
            title = await sess.page.title()
        except Exception:
            pass
        return GotoResponse(session_id=req.session_id, url=sess.page.url, title=title)


@app.post("/session/distill", response_model=DistillResponse)
async def distill(req: DistillRequest) -> DistillResponse:
    sess = await _ensure_session(req.session_id)
    async with sess.lock:  # concurrency lock
        old = sess.last_distill if req.include_diff else None

        try:
            out = await _distill(sess, req)
        except Exception as e:
            raise HTTPException(500, f"distill failed: {e}")

        if req.include_diff and old:
            out["meta"]["diff"] = {
                "url_changed": old.get("url") != out.get("url"),
                "hash_changed": old.get("content_hash") != out.get("content_hash"),
                **_sections_diff(old.get("sections", []), out.get("sections", [])),
                **_actions_diff(old.get("actions", []), out.get("actions", [])),
            }
        elif req.include_diff and not old:
            out["meta"]["diff"] = {"note": "no_previous_snapshot"}

        return DistillResponse(
            url=out["url"],
            title=out["title"],
            content_hash=out["content_hash"],
            sections=[Section(**s) for s in out["sections"]],
            actions=[ActionDescriptor(**a) for a in out["actions"]] if req.include_actions else [],
            meta=out["meta"],
        )


@app.post("/session/read_sections", response_model=ReadSectionsResponse)
async def read_sections(req: ReadSectionsRequest) -> ReadSectionsResponse:
    sess = await _ensure_session(req.session_id)
    async with sess.lock:  # concurrency lock
        if not sess.last_distill:
            await _distill(sess, DistillRequest(session_id=req.session_id, include_actions=False))

        out = sess.last_distill
        sec_map = {s["sid"]: s for s in out["sections"]}
        picked = []
        for sid in req.section_ids:
            s = sec_map.get(sid)
            if not s:
                continue
            picked.append(
                {"sid": sid, "h": s.get("h"), "t": _clip(s.get("t", ""), req.max_section_chars)}
            )

        avail = [s["sid"] for s in out["sections"][:60]]

        return ReadSectionsResponse(
            url=out["url"],
            title=out["title"],
            content_hash=out["content_hash"],
            picked_sections=[Section(**s) for s in picked],
            available_section_ids=avail,
        )


@app.post("/session/act", response_model=ActResponse)
async def act(req: ActRequest) -> ActResponse:
    sess = await _ensure_session(req.session_id)
    async with sess.lock:  # concurrency lock
        if not sess.last_distill:
            await _distill(sess, DistillRequest(session_id=req.session_id))

        before = sess.last_distill
        before_url = sess.page.url
        before_hash = before["content_hash"]
        before_sections = before["sections"]
        before_actions_n = len(before["actions"])

        ad = (sess.action_map or {}).get(req.aid)
        if not ad:
            await _distill(sess, DistillRequest(session_id=req.session_id))
            ad = (sess.action_map or {}).get(req.aid)
        if not ad:
            raise HTTPException(404, "aid not found")

        try:
            # WebMCP: route to invoke instead of DOM action
            if ad.get("strategy", {}).get("type") == "webmcp":
                tool_name = ad.get("strategy", {}).get("tool_name")
                if not tool_name:
                    raise HTTPException(400, "webmcp action missing tool_name")
                params = {}
                if req.text:
                    try:
                        params = json.loads(req.text)
                    except Exception:
                        params = {"input": req.text}
                invoke_result = await _invoke_webmcp_tool(
                    sess, tool_name, params, timeout_ms=req.timeout_ms
                )
                if not invoke_result.get("success"):
                    raise HTTPException(500, f"webmcp invoke failed: {invoke_result.get('error')}")
            else:
                # Original DOM action path
                locator = await _locate(sess.page, ad["strategy"])

                # best-effort wait for element visibility
                try:
                    await locator.wait_for(state="visible", timeout=3000)
                except Exception:
                    pass

                if req.action == "click":
                    await locator.click(timeout=req.timeout_ms)
                elif req.action == "type":
                    if req.text is None:
                        raise HTTPException(422, "type requires text")
                    await locator.fill(req.text, timeout=req.timeout_ms)
                elif req.action == "select":
                    if req.value is None:
                        raise HTTPException(422, "select requires value")
                    await locator.select_option(req.value, timeout=req.timeout_ms)
                elif req.action == "scroll_into_view":
                    await locator.scroll_into_view_if_needed(timeout=req.timeout_ms)
                else:
                    raise HTTPException(422, "unknown action")

                try:
                    await sess.page.wait_for_load_state(req.wait_until, timeout=req.timeout_ms)
                except Exception:
                    pass

                # detect popup/new tab
                await _maybe_switch_to_new_page(sess)

        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(500, f"act failed: {e}")

        new_out = await _distill(sess, DistillRequest(session_id=req.session_id))
        after_url = sess.page.url
        after_hash = new_out["content_hash"]
        after_sections = new_out["sections"]
        after_actions_n = len(new_out["actions"])

        sec_diff = _sections_diff(before_sections, after_sections)
        changed = {
            "url_changed": before_url != after_url,
            "hash_changed": before_hash != after_hash,
            "before_hash": before_hash,
            "after_hash": after_hash,
            "sections_before": len(before_sections),
            "sections_after": len(after_sections),
            "actions_before": before_actions_n,
            "actions_after": after_actions_n,
            **sec_diff,
        }

        top_sections = (
            [Section(**s) for s in after_sections[: req.top_k_sections]]
            if req.return_top_sections
            else []
        )
        actions_sample = [ActionDescriptor(**a) for a in new_out["actions"][:12]]

        title = None
        try:
            title = await sess.page.title()
        except Exception:
            title = new_out.get("title")

        return ActResponse(
            url_before=before_url,
            url_after=after_url,
            title=title,
            changed=changed,
            top_sections=top_sections,
            actions_sample=actions_sample,
        )


# /session/scroll endpoint
@app.post("/session/scroll", response_model=NavigateResponse)
async def scroll(req: ScrollRequest) -> NavigateResponse:
    sess = await _ensure_session(req.session_id)
    async with sess.lock:
        delta = req.pixels if req.direction == "down" else -req.pixels
        await sess.page.evaluate(f"window.scrollBy(0, {delta})")

        # wait for possible lazy-load
        await asyncio.sleep(0.3)

        title = None
        try:
            title = await sess.page.title()
        except Exception:
            pass
        return NavigateResponse(url=sess.page.url, title=title)


# /session/navigate endpoint (back / forward)
@app.post("/session/navigate", response_model=NavigateResponse)
async def navigate(req: NavigateRequest) -> NavigateResponse:
    sess = await _ensure_session(req.session_id)
    async with sess.lock:
        try:
            if req.direction == "back":
                await sess.page.go_back(wait_until=req.wait_until, timeout=req.timeout_ms)
            else:
                await sess.page.go_forward(wait_until=req.wait_until, timeout=req.timeout_ms)
        except Exception as e:
            raise HTTPException(502, f"navigate {req.direction} failed: {e}")

        title = None
        try:
            title = await sess.page.title()
        except Exception:
            pass
        return NavigateResponse(url=sess.page.url, title=title)


# WebMCP: discover page-exposed tools
@app.post("/session/webmcp_discover", response_model=WebMCPDiscoverResponse)
async def webmcp_discover(req: WebMCPDiscoverRequest) -> WebMCPDiscoverResponse:
    sess = await _ensure_session(req.session_id)
    async with sess.lock:
        result = await _discover_webmcp_tools(sess, force=req.force_refresh)
        return WebMCPDiscoverResponse(
            session_id=req.session_id,
            url=sess.page.url,
            webmcp_available=result.get("available", False),
            tools=[
                WebMCPToolDescriptor(
                    name=t["name"],
                    description=t.get("description", ""),
                    input_schema=t.get("input_schema"),
                    read_only=t.get("read_only", False),
                    auto_submit=t.get("auto_submit"),
                    source=t.get("source", "webmcp"),
                )
                for t in result.get("tools", [])
            ],
            errors=result.get("errors", []),
        )


# WebMCP: invoke tool directly (bypass DOM)
@app.post("/session/webmcp_invoke", response_model=WebMCPInvokeResponse)
async def webmcp_invoke(req: WebMCPInvokeRequest) -> WebMCPInvokeResponse:
    sess = await _ensure_session(req.session_id)
    async with sess.lock:
        url_before = sess.page.url
        result = await _invoke_webmcp_tool(
            sess, req.tool_name, req.params, timeout_ms=req.timeout_ms
        )
        return WebMCPInvokeResponse(
            session_id=req.session_id,
            tool_name=req.tool_name,
            success=result.get("success", False),
            result=result.get("result"),
            error=result.get("error"),
            url_before=url_before,
            url_after=sess.page.url,
        )


@app.post("/session/export_storage_state")
async def export_storage_state(req: ExportStorageRequest) -> dict:
    sess = await _ensure_session(req.session_id)
    async with sess.lock:
        state = await sess.context.storage_state()
        return {"storage_state": state}


# Pydantic model instead of Dict[str, Any]
@app.post("/session/close")
async def close_session(req: CloseSessionRequest) -> dict:
    await sessions.remove(req.session_id)
    return {"ok": True}


@app.post("/capture", response_model=CaptureResponse)
async def capture(req: CaptureRequest) -> CaptureResponse:
    """One-shot web capture: navigate to URL, distill, persist to document library, return doc_id.

    Internally runs: session/new → goto → distill → persist → session/close.
    The session is always closed, even on error.
    """
    _validate_url(req.url)
    if _capture_sem.locked():
        raise HTTPException(429, "too many concurrent captures")
    if not _browser:
        raise HTTPException(500, "browser not ready")

    async with _capture_sem:
        context = await _browser.new_context(
            user_agent=DEFAULT_UA,
            locale=req.lang,
            viewport={"width": 900, "height": 700},
        )
        await _setup_routing(context, block_resources=True)
        page = await context.new_page()
        sid = "s_" + secrets.token_hex(8)
        sess = Session(context=context, page=page, lang=req.lang)
        await sessions.put(sid, sess)

        try:
            try:
                await sess.page.goto(req.url, wait_until="domcontentloaded", timeout=req.timeout_ms)
            except Exception as e:
                raise HTTPException(502, f"capture goto failed: {e}")

            sess.webmcp_tools = None
            sess.webmcp_available = False

            distill_req = DistillRequest(
                session_id=sid,
                include_actions=False,
                include_diff=False,
                extract_tables=req.extract_tables,
            )
            try:
                out = await _distill(sess, distill_req)
            except Exception as e:
                raise HTTPException(500, f"capture distill failed: {e}")

            url = out["url"]
            title = out.get("title")
            sections: list[dict[str, Any]] = out["sections"]
            content_hash: str = out["content_hash"]
            table_sections = [s for s in sections if s.get("type") == "table"]

            digest = _build_web_digest(title, sections)
            docs_dir = _get_docs_dir()
            doc_id = _next_web_doc_id(docs_dir)
            content_type = _normalize_content_type(req.content_type)
            _persist_web_capture(
                doc_id=doc_id,
                url=url,
                title=title,
                sections=sections,
                digest=digest,
                tags=req.tags,
                content_hash=content_hash,
                docs_dir=docs_dir,
                content_type=content_type,
            )

            return CaptureResponse(
                doc_id=doc_id,
                content_type=content_type,
                storage_path=_doc_storage_rel_path(doc_id, content_type),
                digest=digest,
                section_count=len(sections),
                table_count=len(table_sections),
            )
        finally:
            await sessions.remove(sid)
