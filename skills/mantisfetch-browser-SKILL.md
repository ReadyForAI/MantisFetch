---
name: mantisfetch-browser
description: Web browsing via the MantisFetch Browser service (Python Playwright). Supports page navigation, semantic distillation of body text and tables, incremental diff (changed_sids), executable actions (click/type/select/scroll/invoke), automatic HTML <table> extraction to Markdown with numeric column statistics, WebMCP structured tool discovery and invocation (Chrome 146+ navigator.modelContext), optional Readability.js reader mode, A11y auto-fallback, SPA-friendly (wait_for_selector + text density detection), and strict length limiting via "body budget + total output budget" to significantly reduce token consumption. Part of the MantisFetch open-source data collection platform.
triggers:
  - "browse web"
  - "open link"
  - "visit page"
  - "extract body"
  - "web distill"
  - "low-token scrape"
  - "click"
  - "type"
  - "select"
  - "scroll"
  - "page change"
  - "incremental read"
  - "go back"
  - "go forward"
  - "pagination"
  - "WebMCP"
  - "structured tool"
  - "web tool call"
  - "table extraction"
  - "web table"
  - "data collection"
  - "web capture"
  - "capture page"
---

# SKILL: MantisFetch Browser (Semantic Distillation Browser + WebMCP)

## 1. Purpose

Use for: information gathering, research, competitive analysis, news/blog extraction, **web table data collection**, form/search interaction, page content monitoring (incremental), **WebMCP structured tool invocation** (flight search, e-commerce orders, ticket submission, etc.).

---

## 2. Service Dependency

- External service: MantisFetch Browser Service (FastAPI + Playwright)
- Base URL: `http://127.0.0.1:9898/web/`
- If the server is started with TLS (`MANTISFETCH_TLS_CERTFILE` + `MANTISFETCH_TLS_KEYFILE`), use `https://` instead.
- Agents that connect over the Model Context Protocol can use the same capabilities as MCP tools (`web_capture`, `web_session_open`, `web_goto`, `web_distill`, `web_act`, …) — see the [mantisfetch-mcp](./mantisfetch-mcp-SKILL.md) skill.

---

## 3. WebMCP Overview

### 3.1 What Is WebMCP

WebMCP (Web Model Context Protocol) is a W3C standard proposal introduced in Chrome 146+. It allows websites to expose **structured callable tools** to in-browser AI Agents via the `navigator.modelContext` API.

Traditional Agents browse by "screenshot + guess DOM" to manipulate elements. WebMCP lets websites **proactively tell** the Agent:

- What this page can do (tool name + description)
- What parameters are needed (JSON Schema)
- How to invoke (execute callback / form auto-fill)

### 3.2 Two API Types

| Type                    | Mechanism                                                                     | Characteristics                                                                                |
| ----------------------- | ----------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------- |
| **Imperative**          | Website registers JS functions via `navigator.modelContext.registerTool()`     | Suited for complex interactions (search, order, multi-step flows); has `inputSchema` + `execute()` |
| **Declarative**         | HTML forms annotated with `toolname` / `tooldescription` attributes           | Suited for simple forms (contact, booking); browser auto-synthesizes schema; optional `toolautosubmit` |

### 3.3 Significance for Agents

- **Highest confidence (0.95)**: WebMCP tools appear first in distill's actions list, ahead of the DOM/A11y action pipeline (see §3.4) and Vision (0.60)
- **More reliable**: Directly invokes website-defined functions without relying on CSS selectors or element visibility
- **Faster**: Skips the DOM locate → wait visible → click/fill pipeline; a single `invoke` completes the entire operation
- **Backward compatible**: When a page doesn't support WebMCP, automatically falls back to the DOM/A11y/Vision pipeline — no extra Agent logic needed

### 3.4 Action Source Priority (non-WebMCP)

Below WebMCP, actions are extracted **accessibility-tree-first**:

| Priority | Source | Role in the action | Confidence |
| -------- | ------ | ------------------ | ---------- |
| 1        | **A11y tree** | **Primary**: always runs. Gives every action a stable `role + name + nth` identity that survives CSS/markup churn | 0.82–0.85 |
| 2        | **DOM**       | Always runs. Enriches matching actions with a **css fallback** and adds elements the tree didn't surface | 0.80 |
| 3        | **Vision (YOLO)** | Last resort, only when tree + DOM came up thin (`< min_actions_before_fallback`) and `enable_vision_fallback=true` | 0.60 |

- Each non-WebMCP action is **dual-strategy**: the `role+name+nth` identity is the primary locator, with a css selector as fallback. If the identity stops resolving (e.g. a renamed control) but the css still matches, `act` recovers via the fallback; otherwise it keeps the identity locator so Playwright's actionability wait still applies (no fast-fail on SPA transient re-renders).
- Duplicate `role+name` controls stay individually addressable via `nth` (DOM order) — `aid` is stable across distills regardless of the css fallback.

---

## 4. Agent Execution Strategy (Low-Token Rules — Must Follow)

### 4.1 Golden Workflow (Recommended Default)

1. `POST /web/session/new` — create session
2. `POST /web/session/goto` — open URL
3. `POST /web/session/distill` — get skeleton: sections + actions (incl. WebMCP tools) + meta.diff
4. If `meta.webmcp.available=true`, prefer WebMCP tools (see §4.6)
5. Need detailed reading: only call `POST /web/session/read_sections` for a few sids
6. Need interaction: use `POST /web/session/act` to execute actions, then preferentially read `changed_sids`
7. Need pagination / load more: use `POST /web/session/scroll` down, then `distill` for new content
8. Need to go back: use `POST /web/session/navigate` back/forward
9. When done: `POST /web/session/close`

**Prohibited behaviors:**

- Pulling the entire page HTML directly
- Re-reading the entire page without checking diff
- Reading a large number of sections at once (wastes tokens)
- Using DOM operations when the page has WebMCP tools (inefficient and unreliable)

### 4.2 Diff-First (Incremental Priority)

- If `distill.meta.diff.changed_sids` is non-empty: only read `changed_sids` (add `added_sids` if needed)
- If `hash_changed=false` and `changed_sids` is empty: skip detailed reading by default (unless the user requests it)

### 4.3 Action-First (Interaction Priority)

- When there's a clear intent (search/filter/login), find `actions` first:
  - `role=textbox` → `act(type)`
  - `role=button/link/checkbox/radio` → `act(click)`
  - `role=combobox` → `act(select)`
- After `act`, don't read the whole page: check returned `changed_sids` first, then `read_sections(changed_sids)`
- **Occlusion guard**: a `click` whose target is covered (cookie banner, modal, sticky header) returns **409** naming the covering element instead of timing out 25 s. Dismiss/scroll the blocker, then retry.

### 4.4 Scroll-Then-Distill (Scroll Loading)

- When page content is incomplete, has "load more", or you need to see further content:
  1. `POST /web/session/scroll` (direction=down)
  2. `POST /web/session/distill` (include_diff=true)
  3. Only read `added_sids` (newly appeared sections)
- Don't blindly scroll multiple times: after each scroll, distill and check diff — stop when no new content appears

### 4.5 SPA / Async Page Strategy

- If the page is a SPA (React/Vue, etc.), DOM may be empty after `goto`
- Pass `wait_for_selector` when calling `distill` to wait for key elements:

```json
{
  "session_id": "s_xxx",
  "wait_for_selector": "article, main, [role='main']",
  "wait_for_timeout_ms": 5000
}
```

- Simple mode automatically detects direct text within div/section (text density detection), extracting content even without standard p/li tags

### 4.6 WebMCP-First (Structured Tool Priority)

**This is the most important new strategy.** When a page supports WebMCP, always prefer structured tools over DOM operations.

### 4.7 Table-First (Table Data Priority)

**When the task goal is data collection / price comparison / metric extraction, prioritize table sections.**

- After `distill`, scan `sections` for entries with `type="table"`
- Check `table_meta.heading` / `table_meta.caption` to determine if it's the target table
- Check `table_meta.stats` for the numbers you need — **if stats has the answer, no need to read the full table**
- Only `read_sections([table sid])` when stats are insufficient
- When `truncated=true`, note that stats are computed on the full data (unaffected by truncation), but the `t` field only contains the first N rows

**Prohibited behaviors:**

- The table section's `t` is already complete Markdown — don't `read_sections` on it again (wastes tokens)
- Don't ignore `table_meta.stats` — it already provides min/max/avg; most data analysis questions can use it directly

**How to determine WebMCP support:**

- `distill` returns `meta.webmcp.available` as `true`
- Or `actions` list contains entries with `source` of `"webmcp_imperative"` / `"webmcp_declarative"`
- Or call `/web/session/webmcp_discover` for the full tool list

**Method 1: Via /web/session/act (recommended, unified interface)**

In distill's returned actions, WebMCP tools are identified by:

- `role` = `"webmcp_tool"`
- `name` prefixed with `"[WebMCP]"`
- `strategy.type` = `"webmcp"`
- `actions` = `["invoke"]`

Invoke with `action="invoke"`, passing parameters via the `text` field as JSON:

```json
{
  "session_id": "s_xxx",
  "aid": "a_webmcp_xxx",
  "action": "invoke",
  "text": "{\"query\": \"SFO to NRT\", \"date\": \"2026-04-01\"}"
}
```

**Method 2: Via dedicated endpoint (more flexible)**

```json
POST /web/session/webmcp_invoke
{
  "session_id": "s_xxx",
  "tool_name": "searchFlights",
  "params": {
    "from": "SFO",
    "to": "NRT",
    "date": "2026-04-01"
  }
}
```

**WebMCP Decision Tree:**

```
distill → check meta.webmcp.available
↓
If true:
  ├─ Matching WebMCP tool for intent → webmcp_invoke / act(invoke)
  ├─ No matching tool → fall back to DOM act(click/type)
  └─ invoke fails → fall back to DOM act(click/type)
↓
If false:
  └─ Use the A11y-first DOM/Vision pipeline (§3.4; fully compatible)
```

---

## 5. API Reference

> All requests use `Content-Type: application/json`

### 5.1 Health Check

- `GET /web/health`

Verifies:

- Service is online
- Readability availability
- YOLO enabled status
- `webmcp_support` field (always `true`, indicating server-side WebMCP support)

Response example:

```json
{
  "ok": true,
  "sessions": 2,
  "readability_available": true,
  "readability_js_path": "~/.mantisfetch/Readability.js",
  "yolo_enabled": false,
  "yolo_onnx_path": null,
  "yolo_input_size": 640,
  "webmcp_support": true
}
```

Notes:
- Filesystem paths are masked (`~` replaces the home directory) for security
- `sessions` shows the current number of active browser sessions

### 5.2 Create Session

- `POST /web/session/new`

Request body (example):

```json
{
  "lang": "en-US",
  "block_resources": true,
  "viewport": { "width": 900, "height": 700 },
  "storage_state": null
}
```

Response (example):

```json
{ "session_id": "s_a1b2c3d4e5f6" }
```

Notes:

- `block_resources=true` blocks images/fonts/media, reducing resource usage and cost
- `storage_state` can import login state (cookies/localStorage)
- session_id uses `secrets.token_hex`, no collisions under high concurrency

### 5.3 Open Web Page

- `POST /web/session/goto`

Request body (example):

```json
{
  "session_id": "s_xxx",
  "url": "https://en.wikipedia.org/wiki/Main_Page",
  "wait_until": "domcontentloaded",
  "timeout_ms": 25000
}
```

Recommendations:

- Default `wait_until=domcontentloaded` (more stable and faster)
- Use `load` for complex sites; avoid `networkidle` (tends to time out)
- After `goto`, WebMCP cache is automatically cleared — new pages require tool re-discovery (triggered automatically during distill or webmcp_discover)

### 5.4 Semantic Distillation (Core)

- `POST /web/session/distill`

Recommended default parameters (balancing low token usage and usability):

```json
{
  "session_id": "s_xxx",
  "distill_mode": "auto",
  "max_sections": 30,
  "max_section_chars": 800,
  "total_text_budget_chars": 6000,

  "total_output_budget_chars": 9000,
  "min_actions_to_keep": 8,
  "max_action_name_chars": 80,
  "max_selector_chars": 120,

  "include_actions": true,
  "max_actions": 60,
  "include_diff": true,

  "min_actions_before_fallback": 8,
  "enable_a11y_fallback": true,

  "enable_vision_fallback": false,
  "vision_max_boxes": 12,
  "vision_conf_thresh": 0.35,
  "vision_iou_thresh": 0.45,

  "extract_tables": true,
  "max_table_rows": 80,
  "max_tables": 20,

  "wait_for_selector": null,
  "wait_for_timeout_ms": 5000
}
```

Key response fields:

- `sections[]`: Semantic paragraphs (stable sids for incremental use)
  - `h`: Heading or auto-generated first-sentence summary; **table sections are prefixed with `[Table]`**
  - `t`: Paragraph body text; **table sections contain Markdown table text**
  - `sid`: Stable ID (hash based on heading + first 400 chars of text)
  - `type`: `"text"` or `"table"` — Agent uses this to distinguish text paragraphs from tables
  - `table_meta`: Only present when `type="table"` — contains row/col counts, header detection, truncation status, numeric statistics
- `actions[]`: Executable actions (aid), **now includes WebMCP tools**
- `meta.diff`: Changes relative to the previous distill (changed_sids, etc.)
- `meta.a11y`: A11y fallback status
- `meta.webmcp`: WebMCP tool discovery status
- `meta.tables_extracted`: Total `<table>` elements found on the page
- `meta.table_sections_count`: Number of tables included in sections

**Caller notes:**

- The body text field is `t` (not `text`), the heading field is `h` (not `heading`)
- The section `sid` is a stable hash of heading + first 400 chars of body — always use the actual `sid` returned by distill
- Table sections have `type` = `"table"` and `h` prefixed with `[Table]`. Agent can quickly filter all tables via `type == "table"`
- Table section `table_meta.stats` provides numeric column statistical summaries — Agent can get key numbers without reading the full table

**Table Section Format Example:**

```json
{
  "sid": "s_8f3a2b1c05",
  "h": "[Table] Q3 Revenue by Region",
  "t": "| Region | Revenue | Growth |\n| --- | --- | --- |\n| North America | $45M | 12% |\n| APAC | $28M | 23% |\n| Europe | $18M | 8% |",
  "type": "table",
  "table_meta": {
    "rows": 4,
    "cols": 3,
    "has_header": true,
    "truncated": false,
    "caption": null,
    "heading": "Q3 Revenue by Region",
    "stats": {
      "Growth": { "min": 8, "max": 23, "avg": 14.33, "count": 3 }
    }
  }
}
```

**table_meta Field Reference:**

| Field        | Description                                                                                                       |
| ------------ | ----------------------------------------------------------------------------------------------------------------- |
| `rows`       | Total rows in the original table (including header)                                                               |
| `cols`       | Number of columns                                                                                                 |
| `has_header` | Whether a header row was detected (`<th>` preferred; heuristic fallback: first row is all short text)             |
| `truncated`  | Whether the table was truncated beyond `max_table_rows`                                                           |
| `caption`    | `<caption>` tag content (may be null)                                                                             |
| `heading`    | Nearest preceding `<h1-h6>` heading before the table (may be null)                                                |
| `stats`      | Numeric column statistics: `{column_name: {min, max, avg, count}}`. Only generated for columns with >50% numeric rows. Null means no numeric columns |

**Two Table Extraction Modes:**

- **Simple mode**: `DISTILL_SIMPLE_JS` extracts `<table>` alongside text blocks in a single JS call
- **Readability mode**: Readability.js drops `<table>` tags, so the service runs a separate `EXTRACT_TABLES_JS` on the raw DOM to supplement. **Both modes are transparent to the Agent — no differentiation needed**

**Actions List — WebMCP Entries:**

When a page supports WebMCP, the actions list includes WebMCP tools at the top:

```json
{
  "aid": "a_webmcp_xxx",
  "role": "webmcp_tool",
  "name": "[WebMCP] searchFlights: Search for available flights",
  "strategy": {
    "type": "webmcp",
    "tool_name": "searchFlights",
    "source": "webmcp_imperative",
    "input_schema": {
      "type": "object",
      "properties": {
        "from": { "type": "string", "description": "Departure airport code" },
        "to": { "type": "string", "description": "Arrival airport code" }
      },
      "required": ["from", "to"]
    }
  },
  "actions": ["invoke"],
  "confidence": 0.95,
  "source": "webmcp_imperative"
}
```

**Identifying WebMCP actions:** `strategy.type == "webmcp"` or `role == "webmcp_tool"`

**meta.webmcp Fields:**

```json
{
  "webmcp": {
    "available": true,
    "tools_count": 3,
    "errors": []
  }
}
```

| Field         | Description                                                                      |
| ------------- | -------------------------------------------------------------------------------- |
| `available`   | Whether the page has WebMCP tools (imperative or declarative)                    |
| `tools_count` | Total number of discovered tools                                                 |
| `errors`      | Errors during discovery (usually empty)                                          |

**Parameter Reference**

| Parameter                   | Description                                                                                                                                                 |
| --------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `total_text_budget_chars`   | Limits only the total length of **body sections**                                                                                                           |
| `total_output_budget_chars` | Limits the **total output** (sections + actions + meta), preventing actions/selectors from bloating token usage                                              |
| `min_actions_to_keep`       | Minimum number of actions to retain when total output budget is tight                                                                                       |
| `max_action_name_chars`     | Maximum characters for action names (smart truncation at word boundaries)                                                                                   |
| `max_selector_chars`        | Maximum characters for CSS selectors                                                                                                                        |
| `include_diff`              | First call: `meta.diff.note = "no_previous_snapshot"`; subsequent: includes `added_sids/removed_sids/changed_sids`                                          |
| `extract_tables`            |  Whether to extract `<table>` elements as Markdown table sections; default `true`                                                                     |
| `max_table_rows`            |  Maximum rows to keep per table; default 80. Excess rows are truncated with `table_meta.truncated=true`, but `stats` are computed on the full data    |
| `max_tables`                |  Maximum tables to extract per page; default 20                                                                                                       |
| `wait_for_selector`         | Wait for a CSS selector to appear before distilling; useful for SPA pages                                                                                   |
| `wait_for_timeout_ms`       | Timeout for `wait_for_selector`; default 5000ms; times out silently and proceeds with distill                                                               |

**A11y Compatibility Notes**

When `enable_a11y_fallback=true`, the service will:

1. First try `page.accessibility.snapshot()`
2. If the current Playwright version/bindings don't support `accessibility`, auto-degrade to `locator("body").aria_snapshot()` parsing role/name
3. Neither failure causes distill to fail (no 500 errors)

Observable in `meta.a11y`:

- `mode` = `"accessibility.snapshot"` | `"aria_snapshot"` | `"unavailable"`
- `error`: If fallback fails, truncated error text for debugging

### 5.5 Read Specific Sections

- `POST /web/session/read_sections`

Request body (example):

```json
{
  "session_id": "s_xxx",
  "section_ids": ["s_abc123", "s_def456"],
  "max_section_chars": 1200
}
```

Purpose:

- Pull back only the sections you need (typically `meta.diff.changed_sids` / `added_sids`)

### 5.6 Execute Action (Click / Type / Select / Scroll-to-Element / WebMCP Invoke)

- `POST /web/session/act`

**Traditional DOM operations (click/type/select):**

```json
{
  "session_id": "s_xxx",
  "aid": "a1234567890",
  "action": "type",
  "text": "OpenAI",
  "wait_until": "domcontentloaded",
  "timeout_ms": 25000,
  "return_top_sections": true,
  "top_k_sections": 3
}
```

**WebMCP invoke operation:**

```json
{
  "session_id": "s_xxx",
  "aid": "a_webmcp_xxx",
  "action": "invoke",
  "text": "{\"query\": \"laptop\", \"category\": \"electronics\"}",
  "timeout_ms": 30000
}
```

| Action Type        | Purpose                    | `text` Field Meaning          |
| ------------------ | -------------------------- | ----------------------------- |
| `click`            | Click button/link          | Not needed                    |
| `type`             | Enter text                 | Text to input                 |
| `select`           | Select dropdown option     | Passed via `value` field      |
| `scroll_into_view` | Scroll to element position | Not needed                    |
| **`invoke`**       | **Invoke WebMCP tool**     | **JSON-formatted parameters** |

Key response fields:

- `changed.added_sids/removed_sids/changed_sids`
- `top_sections`: A few sections (for quick next-step decisions)
- `url_before` / `url_after`: Detect whether navigation occurred

Execution strategy:

- After `act`, don't read the whole page — prioritize `read_sections(changed_sids)`
- `act` depends on `aid`, which comes from the latest `distill(include_actions=true)`
- If `aid not found`, re-run `distill` to get the latest actions
- If WebMCP `invoke` fails, fall back to DOM operations (find the corresponding click/type action)

### 5.7 WebMCP Tool Discovery

- `POST /web/session/webmcp_discover`

Request body:

```json
{
  "session_id": "s_xxx",
  "force_refresh": false
}
```

| Field           | Description                                           |
| --------------- | ----------------------------------------------------- |
| `force_refresh` | `false` uses cache; `true` forces page re-scan        |

Response example:

```json
{
  "session_id": "s_xxx",
  "url": "https://travel.example.com/flights",
  "webmcp_available": true,
  "tools": [
    {
      "name": "searchFlights",
      "description": "Search for available flights between two airports",
      "input_schema": {
        "type": "object",
        "properties": {
          "from": { "type": "string", "description": "Departure airport code" },
          "to": { "type": "string", "description": "Arrival airport code" },
          "date": { "type": "string", "description": "Travel date (YYYY-MM-DD)" }
        },
        "required": ["from", "to", "date"]
      },
      "read_only": true,
      "source": "webmcp_imperative"
    },
    {
      "name": "bookFlight",
      "description": "Book a selected flight",
      "input_schema": {
        "type": "object",
        "properties": {
          "flightId": { "type": "string" },
          "passengers": { "type": "integer" }
        },
        "required": ["flightId"]
      },
      "read_only": false,
      "auto_submit": null,
      "source": "webmcp_imperative"
    },
    {
      "name": "contact-form",
      "description": "Submit a contact inquiry",
      "input_schema": {
        "type": "object",
        "properties": {
          "name": { "type": "string" },
          "email": { "type": "string" },
          "message": { "type": "string" }
        },
        "required": ["name", "email"]
      },
      "read_only": false,
      "auto_submit": true,
      "source": "webmcp_declarative"
    }
  ],
  "errors": []
}
```

**Key field reference:**

| Field          | Description                                                                             |
| -------------- | --------------------------------------------------------------------------------------- |
| `source`       | `"webmcp_imperative"` = JS registerTool; `"webmcp_declarative"` = HTML form[toolname]   |
| `input_schema` | JSON Schema parameter definition — Agent uses this to construct invocation parameters    |
| `read_only`    | `true` means the tool only reads data without modifying state (e.g., search); safe to call repeatedly |
| `auto_submit`  | Declarative tools only: `true` auto-submits the form; `false/null` fills without submitting |

**Use cases:**

- Explore page capabilities ahead of time to decide whether DOM operations are needed
- Get the full `input_schema` to construct correct invocation parameters
- `distill` already triggers WebMCP discovery automatically — this endpoint usually doesn't need to be called separately

### 5.8 WebMCP Tool Invocation

- `POST /web/session/webmcp_invoke`

Request body:

```json
{
  "session_id": "s_xxx",
  "tool_name": "searchFlights",
  "params": {
    "from": "SFO",
    "to": "NRT",
    "date": "2026-04-01"
  },
  "timeout_ms": 30000
}
```

Response example:

```json
{
  "session_id": "s_xxx",
  "tool_name": "searchFlights",
  "success": true,
  "result": {
    "success": true,
    "result": {
      "flights": [
        {
          "id": "FL123",
          "price": 850,
          "departure": "10:30",
          "arrival": "14:30+1"
        }
      ]
    }
  },
  "error": null,
  "url_before": "https://travel.example.com/flights",
  "url_after": "https://travel.example.com/flights?results=1"
}
```

**Comparison with /web/session/act invoke:**

| Dimension    | `/web/session/act` (invoke)            | `/web/session/webmcp_invoke`            |
| ------------ | -------------------------------------- | --------------------------------------- |
| Parameters   | JSON string via `text` field           | Object directly via `params` field      |
| Prerequisite | Requires distill first to get aid      | Only needs tool_name                    |
| Response     | ActResponse (with diff)                | WebMCPInvokeResponse (with result)      |
| Best for     | Unified flow, shared interface with DOM ops | When you know exactly which WebMCP tool to call |

**Recommendation:** Use `/web/session/act` (invoke) in unified flows; use `/web/session/webmcp_invoke` when you know the exact tool name.

### 5.9 Page Scroll

- `POST /web/session/scroll`

Request body (example):

```json
{
  "session_id": "s_xxx",
  "direction": "down",
  "pixels": 600
}
```

| Field       | Description                                  |
| ----------- | -------------------------------------------- |
| `direction` | `"down"` or `"up"`                           |
| `pixels`    | Scroll pixels; default 600; range 50–5000    |

### 5.10 Forward / Back Navigation

- `POST /web/session/navigate`

Request body (example):

```json
{
  "session_id": "s_xxx",
  "direction": "back",
  "wait_until": "domcontentloaded",
  "timeout_ms": 15000
}
```

### 5.11 Export Login State (Optional)

- `POST /web/session/export_storage_state`

Request body: `{ "session_id": "s_xxx" }`

Purpose: Export after a single login, then import on subsequent `session/new` to avoid repeated logins.

Compliance note: When encountering CAPTCHAs or consent pages, prompt the user for manual handling or switch sources; do not attempt to bypass CAPTCHAs.

### 5.12 Close Session

- `POST /web/session/close`

Request body: `{ "session_id": "s_xxx" }`

Notes: Always `close` after each task to release resources. Even if forgotten, the service auto-cleans sessions after 30 minutes of idle time.

### 5.13 One-Shot Web Capture (Persist to Document Library)

- `POST /web/capture`

Request body:

```json
{
  "url": "https://example.com/article",
  "content_type": "Knowledge",
  "tags": ["research", "Q3"],
  "extract_tables": true,
  "lang": "en-US",
  "timeout_ms": 25000
}
```

| Parameter        | Type     | Default        | Description                                      |
| ---------------- | -------- | -------------- | ------------------------------------------------ |
| `url`            | string   | (required)     | URL to capture                                   |
| `content_type`   | string   | `"General"`    | Library category: `General`, `Contract`, `Bid`, or `Knowledge` |
| `tags`           | string[] | `[]`           | Tags for the captured document                   |
| `extract_tables` | bool     | `true`         | Whether to extract HTML tables                   |
| `lang`           | string   | `"en-US"`      | Browser locale                                   |
| `timeout_ms`     | int      | `25000`        | Page load timeout in milliseconds                |
| `force_refresh`  | bool     | `false`        | Bypass the URL dedup cache and always re-fetch (see notes) |

Response example:

```json
{
  "doc_id": "WEB-005",
  "content_type": "Knowledge",
  "storage_path": "Knowledge/WEB-005",
  "digest": "Article covers Q3 revenue trends across regions...",
  "section_count": 8,
  "table_count": 2,
  "reused": false,
  "cache_age_hours": null
}
```

**Key notes:**

- This is a convenience endpoint that internally runs: `session/new → goto → distill → persist → session/close`
- The captured page is persisted to the document library (same `doc-index.json` shared with DocReader)
- Omitted `content_type` defaults to `General`; new captures are stored under `docs/<content_type>/<doc_id>`
- The session is always closed after capture, even on error
- Rate-limited: returns `429` when too many concurrent captures are in progress
- URL validation: private IPs, localhost, and non-HTTP(S) schemes are blocked
- **URL dedup (opt-in):** when `MANTISFETCH_CAPTURE_TTL_HOURS > 0`, a capture of the same `url` + `content_type` made within that window is reused — the response has `reused: true` and `cache_age_hours`, and no re-fetch happens. Default (`0`) always captures. Pass `force_refresh: true` to bypass the cache for a single call.

**When to use `/capture` vs manual session flow:**

| Scenario                           | Use                          |
| ---------------------------------- | ---------------------------- |
| Save a page for later reference    | `/capture` (one-shot)        |
| Interactive browsing + persistence | Manual session flow + custom persist |
| Batch URL collection               | Multiple `/capture` calls    |

---

## 6. Agent Call Templates (Recommended)

### 6.1 Retrieve Web Page Info (Lowest Token Cost)

```
new → goto → distill(include_diff=true)
↓
If meta.diff.changed_sids is non-empty: read_sections(changed_sids)
↓
Summarize based on read sections (don't repeat the entire page)
```

### 6.2 Search / Form Interaction (Traditional DOM)

```
distill(include_actions=true)
↓
Find role=textbox → act(type)
↓
Find role=button/link (name contains Search/Go/Submit) → act(click)
↓
distill → read_sections(changed_sids/added_sids)
```

### 6.3 Search / Form Interaction (WebMCP-First)

```
distill → check meta.webmcp.available
↓
If true:
  Find role=webmcp_tool in actions matching intent
  ↓
  Check strategy.input_schema for parameter requirements
  ↓
  act(invoke, text=JSON_params) or webmcp_invoke(tool_name, params)
  ↓
  distill → read_sections(changed_sids)
↓
If false:
  Use §6.2 traditional flow
```

### 6.4 Scroll Loading

```
distill → content insufficient / need more
↓
scroll(down) → distill(include_diff=true)
↓
If added_sids is non-empty: read_sections(added_sids)
↓
Repeat until added_sids is empty (reached bottom)
```

### 6.5 Multi-Page Browsing

```
goto(list page) → distill → find target link → act(click)
↓
distill(detail page) → read_sections → get needed info
↓
navigate(back) → return to list page
↓
distill(include_diff=true) → find next target → act(click) ...
```

### 6.6 SPA Pages

```
goto(SPA URL) → distill(wait_for_selector="main, article, [role='main']")
↓
If sections are empty or very few: scroll(down) → distill
↓
Continue with normal flow
```

### 6.7 WebMCP Full Interaction Flow

Flight search example:

```
goto("https://travel.example.com") → distill
↓
meta.webmcp.available = true
actions contain: [WebMCP] searchFlights, [WebMCP] bookFlight
↓
webmcp_invoke("searchFlights", {"from":"SFO","to":"NRT","date":"2026-04-01"})
↓
Returns structured result: {flights: [{id:"FL123", price:850, ...}]}
↓
distill → read_sections(changed_sids) to get updated page results
↓
webmcp_invoke("bookFlight", {"flightId":"FL123","passengers":1})
↓
Booking complete → close
```

### 6.8 Web Table Data Collection

For competitive price monitoring, financial report collection, data comparison, etc.:

```
goto(target URL) → distill(extract_tables=true)
↓
Filter sections for type="table" entries
↓
Quick check: inspect table_meta.heading/caption to confirm it's the target table
↓
If table_meta.stats already has needed numbers (e.g., avg/max) → use directly, skip detailed read
↓
If full data needed → read_sections([table sid])
↓
If table truncated=true and full data required → consider page interaction (export/pagination)
```

**Example — getting key numbers without detailed reading:**

```
distill returns a table section:
  h = "[Table] Q3 Revenue by Region"
  table_meta.stats.Revenue = {min: 8M, max: 45M, avg: 22M, count: 4}
  table_meta.rows = 5, truncated = false

→ Agent can directly answer "Q3 average revenue by region is $22M" without reading the full Markdown table, saving tokens
```

**Large table handling strategy:**

```
distill(max_table_rows=80) → table truncated=true, rows=500
↓
Option A: stats already contain needed values → use directly
Option B: need specific rows → scroll to target position → re-distill
Option C: need full data → prompt user to export CSV/XLSX, hand off to MantisFetch DocReader for processing
```

### 6.9 MantisFetch Document Library Persistence Flow

Persist collection results (including tables) to the MantisFetch document library:

```
distill(extract_tables=true, include_actions=false)
↓
Separate text sections and table sections:
  text sections → docs/<content_type>/WEB-xxx/sections/
  table sections → docs/<content_type>/WEB-xxx/tables/
↓
Table section Markdown content and table_meta written together
↓
Shared doc-index.json index with XLSX / PDF parsed results
↓
When Agent later searches "Q3 revenue", web tables and Excel tables are discovered uniformly
```

Use `content_type` (`General`, `Contract`, `Bid`, `Knowledge`) to put captured pages into the same categorized library layout as uploaded documents. Legacy flat `docs/WEB-xxx` captures remain readable.

---

## 7. Common Errors and Solutions

| Error                                       | Cause                                          | Solution                                                                                           |
| ------------------------------------------- | ---------------------------------------------- | -------------------------------------------------------------------------------------------------- |
| `429 too many concurrent requests`          | Rate limit exceeded                            | Wait and retry — server limits concurrent captures/sessions                                        |
| `404 session not found`                     | Session expired or closed                      | Create a new session with `new`                                                                    |
| `502 goto failed`                           | Page load timeout or network issue             | Switch to `wait_until=domcontentloaded` or increase `timeout_ms`                                   |
| `404 aid not found`                         | Actions expired (page changed)                 | Re-run `distill` to get latest actions                                                             |
| `502 navigate back failed`                  | No browser history to go back                  | Use `goto` to navigate directly instead                                                            |
| Consent page / CAPTCHA                      | Site blocking                                  | Inform user "site blocked"; suggest importing `storage_state` or manual intervention; **never provide bypass methods** |
| distill returns empty content               | SPA not fully loaded                           | Use `wait_for_selector` parameter, or `scroll` first to trigger loading                            |
| `read_sections` returns empty               | Passed hard-coded stale sids                   | Always use `sid` values from the latest `distill` response                                         |
| **webmcp_invoke fails**                     | **Page doesn't support WebMCP / wrong tool name** | **Fall back to DOM act(click/type); verify tool_name spelling**                                |
| **meta.webmcp.available=false**             | **Page hasn't registered WebMCP tools**        | **Normal — use traditional DOM flow**                                                              |
| **invoke result has no result**             | **Tool execute returned undefined**            | **Check if params match input_schema; declarative tools may need auto_submit=true**                |
| **Table section t is empty**                | **`<table>` exists but has no visible rows**   | **Normal — may be a decorative or CSS-hidden layout table; ignore it**                             |
| **tables_extracted=0 but page has tables**  | **Tables inside `<iframe>` or Shadow DOM**     | **distill only scans main document `<table>` — nested content not yet supported**                  |
| **Table stats is null**                     | **No numeric columns (all text)**              | **Normal — stats only generated for columns with >50% numeric rows**                               |

---

## 8. Recommended Default Parameters (Use as Agent Constants)

**distill:**

- `distill_mode=auto`
- `max_section_chars=800~1200`
- `total_text_budget_chars=4000~8000`
- `total_output_budget_chars=7000~12000`
- `include_actions=true`
- `include_diff=true`
- `enable_a11y_fallback=true`
- `enable_vision_fallback=false` (unless you've deployed YOLO ONNX)
- `extract_tables=true` (default on; must be on for data collection scenarios)
- `max_table_rows=80` (sufficient for small tables like competitive pricing; use 30–50 for large datasets to save tokens)
- `max_tables=20`
- `wait_for_selector=null` (set as needed for SPA pages)

**act:**

- `wait_until=domcontentloaded`
- `timeout_ms=25000`
- `return_top_sections=true`, `top_k_sections=3`

**scroll:**

- `direction=down`
- `pixels=600`

**navigate:**

- `direction=back`
- `wait_until=domcontentloaded`
- `timeout_ms=15000`

**webmcp_invoke:**

- `timeout_ms=30000`

---

## 9. Action Priority and Confidence ( Full Chain)

distill's action collection follows this priority order. Agents should prefer actions with higher confidence:

| Priority | Source                 | Confidence | Description                                                         |
| -------- | ---------------------- | ---------- | ------------------------------------------------------------------- |
| 1        | **WebMCP imperative**  | 0.95       | Structured tools registered via JS by the website; most reliable    |
| 2        | **WebMCP declarative** | 0.95       | HTML forms with toolname attribute; browser auto-synthesizes schema |
| 3        | DOM extraction         | 0.80       | Traditional CSS selector / role attribute targeting                 |
| 4        | A11y fallback          | 0.82       | accessibility.snapshot / aria_snapshot parsing                      |
| 5        | Vision fallback        | 0.60       | YOLO ONNX screenshot detection + elementFromPoint                  |

---

## 10. Security and Compliance

- Do not proactively scrape user privacy, paywalled content, or restricted content
- When encountering login/CAPTCHA/consent pages: prioritize informing the user and suggest compliant handling (`storage_state` import or manual intervention)
- Do not provide automated CAPTCHA bypass strategies
- WebMCP tools with `read_only=false` modify state (e.g., placing orders, submitting forms) — Agent should confirm user intent before invoking
- Declarative tools with `auto_submit` automatically submit forms — use cautiously for non-read-only operations
