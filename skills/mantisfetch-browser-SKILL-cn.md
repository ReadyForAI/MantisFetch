---
name: mantisfetch-browser
description: 通过 MantisFetch Browser 服务（Python Playwright）进行网页浏览。支持页面导航、正文与表格的语义蒸馏、增量 diff（changed_sids）、可执行动作（click/type/select/scroll/invoke）、自动将 HTML <table> 提取为 Markdown 并附带数值列统计、WebMCP 结构化工具发现与调用（Chrome 146+ navigator.modelContext）、可选 Readability.js 阅读模式、A11y 自动回退、对 SPA 友好（wait_for_selector + 文本密度检测），以及通过“body budget + total output budget”严格限制输出长度，从而显著降低 token 消耗。它是 MantisFetch 开源数据采集平台的一部分。
triggers:
  - "浏览网页"
  - "打开链接"
  - "访问页面"
  - "提取正文"
  - "网页蒸馏"
  - "低 token 抓取"
  - "点击"
  - "输入"
  - "选择"
  - "滚动"
  - "页面变化"
  - "增量读取"
  - "返回"
  - "前进"
  - "分页"
  - "WebMCP"
  - "结构化工具"
  - "网页工具调用"
  - "表格提取"
  - "网页表格"
  - "数据采集"
  - "网页抓取入库"
  - "保存页面"
---

# SKILL: MantisFetch Browser（语义蒸馏浏览器 + WebMCP）

## 1. 用途

适用于：信息收集、研究、竞品分析、新闻/博客提取、**网页表格数据采集**、表单/搜索交互、页面内容监控（增量模式）、**WebMCP 结构化工具调用**（航班搜索、电商订单、工单提交等）。

---

## 2. 服务依赖

- 外部服务：MantisFetch Browser Service（FastAPI + Playwright）
- Base URL: `http://127.0.0.1:9898/web/`

---

## 3. WebMCP 概览

### 3.1 什么是 WebMCP

WebMCP（Web Model Context Protocol）是 Chrome 146+ 中提出的一个 W3C 标准提案。它允许网站通过 `navigator.modelContext` API，向浏览器内的 AI Agent 暴露**可结构化调用的工具**。

传统 Agent 通过“截图 + 猜 DOM”的方式操作页面。WebMCP 则允许网站**主动告诉** Agent：

- 这个页面能做什么（工具名 + 描述）
- 需要哪些参数（JSON Schema）
- 如何调用（执行回调 / 自动填表）

### 3.2 两类 API 形式

| Type                    | Mechanism                                                                     | Characteristics |
| ----------------------- | ----------------------------------------------------------------------------- | --------------- |
| **Imperative**          | Website registers JS functions via `navigator.modelContext.registerTool()`     | 适合复杂交互（搜索、下单、多步骤流程）；带 `inputSchema` + `execute()` |
| **Declarative**         | HTML forms annotated with `toolname` / `tooldescription` attributes           | 适合简单表单（联系、预订）；浏览器自动生成 schema；可选 `toolautosubmit` |

### 3.3 对 Agent 的意义

- **最高置信度（0.95）**：WebMCP 工具会优先出现在 distill 的 actions 列表中，优先于 DOM/A11y 动作流程（见 §3.4）和 Vision（0.60）
- **更可靠**：直接调用网站定义的函数，不依赖 CSS selector 或元素可见性
- **更快**：跳过 DOM 定位 → 等待可见 → 点击/填表 这条链路，一次 `invoke` 即可完成整个操作
- **向后兼容**：如果页面不支持 WebMCP，会自动回退到 DOM/A11y/Vision 流程，不需要 Agent 额外处理

### 3.4 动作来源优先级（非 WebMCP）

在 WebMCP 之下，动作按**无障碍树优先（accessibility-tree-first）**抽取：

| 优先级 | 来源 | 作用 | 置信度 |
| ------ | ---- | ---- | ------ |
| 1      | **A11y 树** | **主来源**：始终运行。为每个动作提供稳定的 `role + name + nth` 身份，能抵抗 CSS/标记变动 | 0.82–0.85 |
| 2      | **DOM**     | 始终运行。为匹配到的动作补充 **css 回退选择器**，并补全 A11y 树未覆盖的元素 | 0.80 |
| 3      | **Vision（YOLO）** | 最后兜底，仅当树 + DOM 结果过少（`< min_actions_before_fallback`）且 `enable_vision_fallback=true` 时 | 0.60 |

- 每个非 WebMCP 动作都是**双策略**：`role+name+nth` 身份为主定位器，css selector 为回退。`act` 按 身份 → css → 明确报错 的顺序解析（不再静默等待 25 秒超时）。
- 同名同 role 的多个控件通过 `nth`（DOM 顺序）保持各自可定位；`aid` 在多次 distill 间保持稳定，与 css 回退无关。

---

## 4. Agent 执行策略（低 Token 规则，必须遵守）

### 4.1 黄金工作流（推荐默认）

1. `POST /web/session/new` — 创建 session
2. `POST /web/session/goto` — 打开 URL
3. `POST /web/session/distill` — 获取页面骨架：sections + actions（含 WebMCP 工具）+ meta.diff
4. 如果 `meta.webmcp.available=true`，优先用 WebMCP 工具（见 §4.6）
5. 需要细读时，只调用 `POST /web/session/read_sections` 读取少量 sids
6. 需要交互时，使用 `POST /web/session/act` 执行动作，然后优先读取 `changed_sids`
7. 需要分页 / 加载更多时，调用 `POST /web/session/scroll` 向下滚动，再 `distill` 读取新内容
8. 需要返回上一页时，调用 `POST /web/session/navigate` back/forward
9. 使用完成后：`POST /web/session/close`

**禁止行为：**

- 直接拉取整页 HTML
- 不检查 diff 就重复读取整页
- 一次读取大量 sections（浪费 tokens）
- 页面已经有 WebMCP 工具时还坚持用 DOM 操作（低效且不稳定）

### 4.2 Diff-First（增量优先）

- 如果 `distill.meta.diff.changed_sids` 非空：默认只读取 `changed_sids`（必要时加上 `added_sids`）
- 如果 `hash_changed=false` 且 `changed_sids` 为空：默认跳过详细读取（除非用户明确要求）

### 4.3 Action-First（交互优先）

- 当意图明确（搜索 / 过滤 / 登录）时，先找 `actions`：
  - `role=textbox` → `act(type)`
  - `role=button/link/checkbox/radio` → `act(click)`
  - `role=combobox` → `act(select)`
- `act` 之后不要读整页，先看返回的 `changed_sids`，再 `read_sections(changed_sids)`
- **遮挡保护**：当 `click` 目标被遮挡（cookie 横幅、弹窗、吸顶栏）时，会返回 **409** 并指出遮挡元素，而不是干等 25 秒超时。先关闭/滚开遮挡物再重试。

### 4.4 Scroll-Then-Distill（滚动加载）

- 当页面内容不完整、有“加载更多”，或需要继续往下看时：
  1. `POST /web/session/scroll`（direction=down）
  2. `POST /web/session/distill`（include_diff=true）
  3. 只读取 `added_sids`（新出现的内容）
- 不要盲目连续滚动很多次：每次滚动后都先 distill 检查 diff，没有新内容就停止

### 4.5 SPA / 异步页面策略

- 如果页面是 SPA（React/Vue 等），`goto` 之后 DOM 可能暂时为空
- 调用 `distill` 时可传 `wait_for_selector` 等待关键元素：

```json
{
  "session_id": "s_xxx",
  "wait_for_selector": "article, main, [role='main']",
  "wait_for_timeout_ms": 5000
}
```

- simple mode 会自动检测 div/section 内的直接文本（文本密度检测），即使没有标准的 p/li 标签也能提取内容

### 4.6 WebMCP-First（结构化工具优先）

**这是最重要的新策略。** 当页面支持 WebMCP 时，始终优先使用结构化工具，而不是 DOM 操作。

### 4.7 Table-First（表格数据优先）

**当目标是数据采集 / 价格比较 / 指标提取时，优先关注 table sections。**

- `distill` 之后，先扫描 `sections` 中 `type="table"` 的条目
- 检查 `table_meta.heading` / `table_meta.caption`，判断是否为目标表格
- 检查 `table_meta.stats` 是否已包含所需数字，**如果 stats 已能回答问题，就不需要读完整表格**
- 只有在 stats 不够用时，才对表格 section 执行 `read_sections([table sid])`
- 当 `truncated=true` 时要注意：stats 仍然基于完整数据计算，不受截断影响；但 `t` 字段只包含前 N 行

**禁止行为：**

- 表格 section 的 `t` 已经是完整 Markdown，不要再对它重复 `read_sections`（浪费 tokens）
- 不要忽略 `table_meta.stats`，它已经给出了 min/max/avg，很多数据分析问题可以直接用

**如何判断页面是否支持 WebMCP：**

- `distill` 返回 `meta.webmcp.available = true`
- 或 `actions` 列表中出现 `source` 为 `"webmcp_imperative"` / `"webmcp_declarative"` 的条目
- 或调用 `/web/session/webmcp_discover` 获取完整工具列表

**方式一：通过 /web/session/act（推荐，统一接口）**

在 distill 返回的 actions 中，WebMCP 工具具备以下特征：

- `role` = `"webmcp_tool"`
- `name` 以 `"[WebMCP]"` 开头
- `strategy.type` = `"webmcp"`
- `actions` = `["invoke"]`

调用方式是 `action="invoke"`，并通过 `text` 字段传 JSON 参数：

```json
{
  "session_id": "s_xxx",
  "aid": "a_webmcp_xxx",
  "action": "invoke",
  "text": "{\"query\": \"SFO to NRT\", \"date\": \"2026-04-01\"}"
}
```

**方式二：通过专用接口（更灵活）**

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

**WebMCP 决策树：**

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

## 5. API 说明

> 所有请求使用 `Content-Type: application/json`

### 5.1 健康检查

- `GET /web/health`

用于验证：

- 服务在线
- Readability 是否可用
- YOLO 是否启用
- `webmcp_support` 字段（恒为 `true`，表示服务端支持 WebMCP）

响应示例：

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

说明：
- 出于安全原因，文件系统路径会被掩码（家目录用 `~` 表示）
- `sessions` 表示当前活跃浏览器会话数

### 5.2 创建 Session

- `POST /web/session/new`

请求体示例：

```json
{
  "lang": "en-US",
  "block_resources": true,
  "viewport": { "width": 900, "height": 700 },
  "storage_state": null
}
```

响应示例：

```json
{ "session_id": "s_a1b2c3d4e5f6" }
```

说明：

- `block_resources=true` 会屏蔽图片/字体/媒体资源，降低资源使用与成本
- `storage_state` 可导入登录状态（cookies/localStorage）
- `session_id` 使用 `secrets.token_hex` 生成，在高并发下也不会碰撞

### 5.3 打开网页

- `POST /web/session/goto`

请求体示例：

```json
{
  "session_id": "s_xxx",
  "url": "https://en.wikipedia.org/wiki/Main_Page",
  "wait_until": "domcontentloaded",
  "timeout_ms": 25000
}
```

建议：

- 默认使用 `wait_until=domcontentloaded`（更稳定也更快）
- 复杂站点可用 `load`；尽量避免 `networkidle`（容易超时）
- `goto` 后 WebMCP 缓存会自动清空；新页面需要重新发现工具（在 distill 或 webmcp_discover 时自动触发）

### 5.4 语义蒸馏（核心）

- `POST /web/session/distill`

推荐默认参数（在低 token 与可用性之间取得平衡）：

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

响应中的关键字段：

- `sections[]`：语义段落（带稳定 sid，可做增量读取）
  - `h`：标题或自动生成的首句摘要；**table section 会以 `[Table]` 为前缀**
  - `t`：段落正文；**table section 中这里是 Markdown 表格文本**
  - `sid`：稳定 ID（基于标题 + 前 400 字正文的哈希）
  - `type`：`"text"` 或 `"table"`，Agent 可据此区分普通文本和表格
  - `table_meta`：仅当 `type="table"` 时出现，包含行列数、表头识别、截断状态和数值统计
- `actions[]`：可执行动作（aid），**现在也包含 WebMCP 工具**
- `meta.diff`：相对上一次 distill 的变化（changed_sids 等）
- `meta.a11y`：A11y 回退状态
- `meta.webmcp`：WebMCP 工具发现状态
- `meta.tables_extracted`：页面中识别出的 `<table>` 总数
- `meta.table_sections_count`：最终进入 sections 的表格数量

**调用方注意：**

- 正文字段是 `t`（不是 `text`），标题字段是 `h`（不是 `heading`）
- section 的 `sid` 是基于标题 + 前 400 字正文的稳定哈希，必须使用 distill 返回的真实 `sid`
- table section 的 `type` = `"table"`，且 `h` 会加 `[Table]` 前缀。Agent 可以直接用 `type == "table"` 过滤出所有表格
- `table_meta.stats` 提供数值列统计，很多问题无需读取完整表格即可得到答案

**表格 Section 格式示例：**

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

**table_meta 字段说明：**

| Field        | 说明 |
| ------------ | ---- |
| `rows`       | 原始表格总行数（含表头） |
| `cols`       | 列数 |
| `has_header` | 是否识别到表头（优先看 `<th>`；否则启用启发式判断：首行均为短文本） |
| `truncated`  | 是否因超过 `max_table_rows` 而被截断 |
| `caption`    | `<caption>` 内容（可能为 null） |
| `heading`    | 表格前最近的 `<h1-h6>` 标题（可能为 null） |
| `stats`      | 数值列统计：`{column_name: {min, max, avg, count}}`。只有当某列超过 50% 行为数值时才生成。null 表示没有数值列。 |

**两种表格提取模式：**

- **Simple mode**：`DISTILL_SIMPLE_JS` 会在一次 JS 调用里同时提取正文和 `<table>`
- **Readability mode**：Readability.js 会丢弃 `<table>`，因此服务会对原始 DOM 再执行一次 `EXTRACT_TABLES_JS` 补全表格。**这对 Agent 完全透明，无需区分**

**actions 列表中的 WebMCP 条目：**

当页面支持 WebMCP 时，actions 列表顶部会出现 WebMCP 工具：

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

**识别 WebMCP action 的方式：** `strategy.type == "webmcp"` 或 `role == "webmcp_tool"`

**meta.webmcp 字段：**

```json
{
  "webmcp": {
    "available": true,
    "tools_count": 3,
    "errors": []
  }
}
```

| Field         | 说明 |
| ------------- | ---- |
| `available`   | 页面是否存在 WebMCP 工具（imperative 或 declarative） |
| `tools_count` | 发现到的工具总数 |
| `errors`      | 发现过程中出现的错误（通常为空） |

**参数说明**

| Parameter                   | 说明 |
| --------------------------- | ---- |
| `total_text_budget_chars`   | 仅限制 **正文 sections** 的总长度 |
| `total_output_budget_chars` | 限制 **整体输出**（sections + actions + meta），防止 actions/selectors 挤占过多 token |
| `min_actions_to_keep`       | 在整体输出预算紧张时，至少保留多少个 actions |
| `max_action_name_chars`     | action 名称的最大字符数（按单词边界智能截断） |
| `max_selector_chars`        | CSS selector 最大字符数 |
| `include_diff`              | 首次调用时：`meta.diff.note = "no_previous_snapshot"`；之后会包含 `added_sids/removed_sids/changed_sids` |
| `extract_tables`            | 是否将 `<table>` 提取为 Markdown table section；默认 `true` |
| `max_table_rows`            | 每个表格最多保留多少行；默认 80。超出部分会截断，并设置 `table_meta.truncated=true`，但 `stats` 仍基于完整数据计算 |
| `max_tables`                | 每页最多提取多少张表；默认 20 |
| `wait_for_selector`         | distill 前等待某个 CSS selector 出现；适合 SPA 页面 |
| `wait_for_timeout_ms`       | `wait_for_selector` 的超时；默认 5000ms；超时后会静默继续 distill |

**A11y 兼容性说明**

当 `enable_a11y_fallback=true` 时，服务会：

1. 先尝试 `page.accessibility.snapshot()`
2. 如果当前 Playwright 版本 / 绑定不支持 `accessibility`，则自动降级为 `locator("body").aria_snapshot()`，解析 role/name
3. 无论哪种失败，都不会导致 distill 失败（不会出现 500）

可以在 `meta.a11y` 中观察到：

- `mode` = `"accessibility.snapshot"` | `"aria_snapshot"` | `"unavailable"`
- `error`：如果回退仍失败，会返回截断后的错误文本用于调试

### 5.5 读取指定 Sections

- `POST /web/session/read_sections`

请求体示例：

```json
{
  "session_id": "s_xxx",
  "section_ids": ["s_abc123", "s_def456"],
  "max_section_chars": 1200
}
```

用途：

- 只拉回你真正需要的 sections（通常是 `meta.diff.changed_sids` / `added_sids`）

### 5.6 执行动作（Click / Type / Select / Scroll-to-Element / WebMCP Invoke）

- `POST /web/session/act`

**传统 DOM 操作（click/type/select）：**

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

**WebMCP invoke 操作：**

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

关键响应字段：

- `changed.added_sids/removed_sids/changed_sids`
- `top_sections`：少量 sections，便于快速决定下一步
- `url_before` / `url_after`：用于判断是否发生导航

执行策略：

- `act` 之后不要读取整页，优先 `read_sections(changed_sids)`
- `act` 依赖 `aid`，而 `aid` 来自最近一次 `distill(include_actions=true)`
- 如果 `aid not found`，先重新执行 `distill` 获取最新 actions
- 如果 WebMCP `invoke` 失败，回退到 DOM 操作（找到对应 click/type action）

### 5.7 WebMCP 工具发现

- `POST /web/session/webmcp_discover`

请求体：

```json
{
  "session_id": "s_xxx",
  "force_refresh": false
}
```

| Field           | 说明 |
| --------------- | ---- |
| `force_refresh` | `false` 使用缓存；`true` 强制重新扫描页面 |

响应示例：

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

**关键字段说明：**

| Field          | 说明 |
| -------------- | ---- |
| `source`       | `"webmcp_imperative"` = JS registerTool；`"webmcp_declarative"` = HTML form[toolname] |
| `input_schema` | JSON Schema 参数定义，Agent 用它来构造调用参数 |
| `read_only`    | `true` 表示只读，不修改状态（如搜索）；可以安全重复调用 |
| `auto_submit`  | 仅 declarative 工具有意义：`true` 会自动提交表单；`false/null` 只填充不提交 |

**适用场景：**

- 提前探索页面能力，判断是否需要 DOM 操作
- 获取完整的 `input_schema`，构造正确的调用参数
- `distill` 本身已经会自动触发 WebMCP discovery，一般无需单独调用这个接口

### 5.8 调用 WebMCP 工具

- `POST /web/session/webmcp_invoke`

请求体：

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

响应示例：

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

**与 `/web/session/act` invoke 的对比：**

| Dimension    | `/web/session/act` (invoke)            | `/web/session/webmcp_invoke`            |
| ------------ | -------------------------------------- | --------------------------------------- |
| Parameters   | JSON string via `text` field           | Object directly via `params` field      |
| Prerequisite | Requires distill first to get aid      | Only needs tool_name                    |
| Response     | ActResponse (with diff)                | WebMCPInvokeResponse (with result)      |
| Best for     | Unified flow, shared interface with DOM ops | When you know exactly which WebMCP tool to call |

**建议：** 在统一流程里优先用 `/web/session/act`（invoke）；如果你已经明确知道要调用哪个 WebMCP 工具，再用 `/web/session/webmcp_invoke`。

### 5.9 页面滚动

- `POST /web/session/scroll`

请求体示例：

```json
{
  "session_id": "s_xxx",
  "direction": "down",
  "pixels": 600
}
```

| Field       | 说明 |
| ----------- | ---- |
| `direction` | `"down"` 或 `"up"` |
| `pixels`    | 滚动像素数；默认 600；范围 50–5000 |

### 5.10 前进 / 后退导航

- `POST /web/session/navigate`

请求体示例：

```json
{
  "session_id": "s_xxx",
  "direction": "back",
  "wait_until": "domcontentloaded",
  "timeout_ms": 15000
}
```

### 5.11 导出登录状态（可选）

- `POST /web/session/export_storage_state`

请求体：`{ "session_id": "s_xxx" }`

用途：登录一次后导出状态，后续在 `session/new` 中导入，避免重复登录。

合规说明：遇到 CAPTCHA 或同意页面时，应提示用户手动处理或切换数据源；不要尝试绕过 CAPTCHA。

### 5.12 关闭 Session

- `POST /web/session/close`

请求体：`{ "session_id": "s_xxx" }`

说明：每次任务结束后都应执行 `close` 释放资源。即便忘记调用，服务也会在 30 分钟空闲后自动清理 session。

### 5.13 一次性网页抓取并入库

- `POST /web/capture`

请求体：

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

| Parameter        | Type     | Default        | 说明 |
| ---------------- | -------- | -------------- | ---- |
| `url`            | string   | (required)     | 要抓取的 URL |
| `content_type`   | string   | `"General"`    | 文档库分类：`General`、`Contract`、`Bid`、`Knowledge` |
| `tags`           | string[] | `[]`           | 写入文档库的标签 |
| `extract_tables` | bool     | `true`         | 是否提取 HTML 表格 |
| `lang`           | string   | `"en-US"`      | 浏览器语言环境 |
| `timeout_ms`     | int      | `25000`        | 页面加载超时（毫秒） |

响应示例：

```json
{
  "doc_id": "WEB-005",
  "content_type": "Knowledge",
  "storage_path": "Knowledge/WEB-005",
  "digest": "Article covers Q3 revenue trends across regions...",
  "section_count": 8,
  "table_count": 2
}
```

**关键说明：**

- 这是一个便捷接口，内部会执行：`session/new → goto → distill → persist → session/close`
- 抓取结果会持久化到文档库（与 DocReader 共用同一个 `doc-index.json`）
- 未传 `content_type` 时默认入库到 `General`；新抓取结果会保存到 `docs/<content_type>/<doc_id>`
- 即使发生错误，session 也一定会被关闭
- 有并发限流：并发抓取过多时会返回 `429`
- URL 校验：私有 IP、localhost、非 HTTP(S) 协议都会被拦截

**什么时候用 `/capture`，什么时候用手动 session 流程：**

| Scenario                           | Use                          |
| ---------------------------------- | ---------------------------- |
| Save a page for later reference    | `/capture` (one-shot)        |
| Interactive browsing + persistence | Manual session flow + custom persist |
| Batch URL collection               | Multiple `/capture` calls    |

---

## 6. Agent 调用模板（推荐）

### 6.1 获取网页信息（最低 Token 成本）

```
new → goto → distill(include_diff=true)
↓
If meta.diff.changed_sids is non-empty: read_sections(changed_sids)
↓
基于读取到的 sections 做总结（不要重复整页内容）
```

### 6.2 搜索 / 表单交互（传统 DOM）

```
distill(include_actions=true)
↓
Find role=textbox → act(type)
↓
Find role=button/link (name contains Search/Go/Submit) → act(click)
↓
distill → read_sections(changed_sids/added_sids)
```

### 6.3 搜索 / 表单交互（WebMCP 优先）

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

### 6.4 滚动加载

```
distill → content insufficient / need more
↓
scroll(down) → distill(include_diff=true)
↓
If added_sids is non-empty: read_sections(added_sids)
↓
Repeat until added_sids is empty (reached bottom)
```

### 6.5 多页面浏览

```
goto(list page) → distill → find target link → act(click)
↓
distill(detail page) → read_sections → get needed info
↓
navigate(back) → return to list page
↓
distill(include_diff=true) → find next target → act(click) ...
```

### 6.6 SPA 页面

```
goto(SPA URL) → distill(wait_for_selector="main, article, [role='main']")
↓
If sections are empty or very few: scroll(down) → distill
↓
Continue with normal flow
```

### 6.7 WebMCP 完整交互流程

航班搜索示例：

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

### 6.8 网页表格数据采集

适用于竞品价格监控、财报数据采集、指标对比等场景：

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

**示例：无需细读即可取关键数字**

```
distill returns a table section:
  h = "[Table] Q3 Revenue by Region"
  table_meta.stats.Revenue = {min: 8M, max: 45M, avg: 22M, count: 4}
  table_meta.rows = 5, truncated = false

→ Agent can directly answer "Q3 average revenue by region is $22M" without reading the full Markdown table, saving tokens
```

**大表处理策略：**

```
distill(max_table_rows=80) → table truncated=true, rows=500
↓
Option A: stats already contain needed values → use directly
Option B: need specific rows → scroll to target position → re-distill
Option C: need full data → prompt user to export CSV/XLSX, hand off to MantisFetch DocReader for processing
```

### 6.9 MantisFetch 文档库持久化流程

将采集结果（包括表格）写入 MantisFetch 文档库：

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

使用 `content_type`（`General`、`Contract`、`Bid`、`Knowledge`）可以把网页抓取结果放入和上传文档一致的分类文档库结构。旧版平铺的 `docs/WEB-xxx` 抓取结果仍可读取。

---

## 7. 常见错误与处理方式

| Error                                       | Cause                                          | Solution |
| ------------------------------------------- | ---------------------------------------------- | -------- |
| `429 too many concurrent requests`          | 触发并发限流                                   | 等待后重试，服务限制了并发 capture/session 数量 |
| `404 session not found`                     | session 已过期或已关闭                         | 重新调用 `new` 创建 session |
| `502 goto failed`                           | 页面加载超时或网络异常                         | 改用 `wait_until=domcontentloaded` 或增大 `timeout_ms` |
| `404 aid not found`                         | 页面已变化，actions 失效                       | 重新执行 `distill` 获取最新 actions |
| `502 navigate back failed`                  | 浏览器历史中没有可返回页面                     | 改用 `goto` 直接导航 |
| Consent page / CAPTCHA                      | 网站阻拦                                       | 告知用户“站点阻拦”，建议导入 `storage_state` 或手动介入；**不要提供绕过方法** |
| distill returns empty content               | SPA 尚未完全加载                               | 使用 `wait_for_selector`，或先 `scroll` 触发加载 |
| `read_sections` returns empty               | 传入了过期硬编码 sid                           | 必须始终使用最近一次 `distill` 返回的 `sid` |
| **webmcp_invoke fails**                     | **页面不支持 WebMCP / tool_name 错误**         | **回退到 DOM act(click/type)；确认 tool_name 拼写** |
| **meta.webmcp.available=false**             | **页面尚未注册 WebMCP 工具**                   | **正常现象，改用传统 DOM 流程** |
| **invoke result has no result**             | **工具 execute 返回了 undefined**              | **检查 params 是否符合 input_schema；declarative 工具可能需要 auto_submit=true** |
| **Table section t is empty**                | **页面存在 `<table>`，但没有可见行**           | **正常，可能是装饰性或 CSS 隐藏的布局表，可忽略** |
| **tables_extracted=0 but page has tables**  | **表格在 `<iframe>` 或 Shadow DOM 内**         | **distill 当前只扫描主文档中的 `<table>`，暂不支持嵌套内容** |
| **Table stats is null**                     | **没有数值列（全是文本）**                     | **正常，stats 只会为超过 50% 行为数值的列生成** |

---

## 8. 推荐默认参数（可作为 Agent 常量）

**distill：**

- `distill_mode=auto`
- `max_section_chars=800~1200`
- `total_text_budget_chars=4000~8000`
- `total_output_budget_chars=7000~12000`
- `include_actions=true`
- `include_diff=true`
- `enable_a11y_fallback=true`
- `enable_vision_fallback=false`（除非你已经部署了 YOLO ONNX）
- `extract_tables=true`（默认开启；做数据采集时必须开启）
- `max_table_rows=80`（适合竞品定价等小表；大数据集可调到 30–50 节省 token）
- `max_tables=20`
- `wait_for_selector=null`（SPA 页面按需设置）

**act：**

- `wait_until=domcontentloaded`
- `timeout_ms=25000`
- `return_top_sections=true`, `top_k_sections=3`

**scroll：**

- `direction=down`
- `pixels=600`

**navigate：**

- `direction=back`
- `wait_until=domcontentloaded`
- `timeout_ms=15000`

**webmcp_invoke：**

- `timeout_ms=30000`

---

## 9. 动作优先级与置信度（全链路）

distill 的 action 收集遵循以下优先顺序。Agent 应优先使用更高置信度的 action：

| Priority | Source                 | Confidence | Description |
| -------- | ---------------------- | ---------- | ----------- |
| 1        | **WebMCP imperative**  | 0.95       | 网站通过 JS 注册的结构化工具，最可靠 |
| 2        | **WebMCP declarative** | 0.95       | 带 toolname 属性的 HTML 表单；浏览器自动生成 schema |
| 3        | DOM extraction         | 0.80       | 传统 CSS selector / role 属性定位 |
| 4        | A11y fallback          | 0.82       | accessibility.snapshot / aria_snapshot 解析 |
| 5        | Vision fallback        | 0.60       | YOLO ONNX 截图检测 + elementFromPoint |

---

## 10. 安全与合规

- 不要主动抓取用户隐私内容、付费墙内容或受限内容
- 遇到登录 / CAPTCHA / 同意页面时：优先告知用户，并建议合规处理方式（导入 `storage_state` 或人工介入）
- 不要提供 CAPTCHA 自动绕过策略
- `read_only=false` 的 WebMCP 工具会修改状态（如提交表单、下单）—— 调用前应确认用户意图
- 带 `auto_submit` 的 declarative 工具会自动提交表单 —— 对非只读操作应谨慎使用
