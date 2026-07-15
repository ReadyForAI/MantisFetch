# MantisFetch

<p align="center">
  <img src="assets/banner.png" alt="MantisFetch Banner" width="100%">
</p>

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)
[![CI](https://github.com/ReadyForAI/MantisFetch/actions/workflows/ci.yml/badge.svg)](https://github.com/ReadyForAI/MantisFetch/actions)

[English](#english) | [中文](#中文)

---

## English

**MantisFetch** is an Agent-native data collection and document parsing toolkit by [ReadyForAI](https://github.com/ReadyForAI).

### Why MantisFetch?

Most scrapers hand your Agent a wall of raw HTML — expensive to process and mostly noise. MantisFetch semantically distills web pages into structured sections and actions, cutting token usage by 90%+ compared to raw HTML. A three-tier loading model (digest → brief → section) lets Agents pull only what they need instead of loading entire documents into context. Diff-first incremental reads mean revisiting a page costs near-zero tokens, and table-aware extraction precomputes statistics so Agents can answer data questions without reading a single row.

### Features

- **Semantic distillation** — web pages → structured sections + interactive actions; 4,000–8,000 chars per distill vs. full HTML
- **Three-tier loading** — digest (~200 tokens) → brief (~1,500 tokens) → section (on-demand); Agents load only what they need
- **Diff-first** — `distill` returns only changed sections (`changed_sids`); repeat visits cost near-zero tokens
- **Table-aware** — auto-extracts tables with precomputed stats (min/max/avg); answer data questions without reading rows
- **Document parsing** — PDF, DOCX, PPTX, XLSX, CSV, HTML via [MarkItDown](https://github.com/microsoft/markitdown), with Gemini OCR fallback
- **Multi-LLM support** — Gemini (default), OpenAI, Ollama, Groq, or any OpenAI-compatible API
- **MCP server** — exposes `/web` and `/doc` as native MCP tools (streamable-HTTP at `/mcp`) for agent runtimes; loopback-only by default, with bearer-token + optional TLS for remote clients
- **WebMCP client** — discovers and invokes structured tools that Chrome 146+ pages expose via `navigator.modelContext`
- **i18n** — English and Chinese (set `LANG=zh`)

### Quick Start

#### Docker (recommended)

```bash
git clone https://github.com/ReadyForAI/MantisFetch.git
cd MantisFetch
GEMINI_API_KEY=your_key_here docker compose up -d

# Check health
curl http://localhost:9898/health
```

> `/web` and `/doc` are **loopback-only** without a token. Host requests reach the
> container across the Docker bridge (a non-loopback peer), so set
> `MANTISFETCH_MCP_TOKEN=…` and send `Authorization: Bearer …` to use them; only
> `/health` is exempt.

#### Python (local)

```bash
git clone https://github.com/ReadyForAI/MantisFetch.git
cd MantisFetch
pip install -r requirements.txt
playwright install chromium

export GEMINI_API_KEY=your_key_here
python mantisfetch_server.py     # listens on port 9898
```

### Docker

The `docker-compose.yml` provides a single-service setup. By default, the document library is bind-mounted to the current user's `~/.mantisfetch/docs` directory on the host. See [`DEPLOYMENT.md`](DEPLOYMENT.md) for container hardening, shared-volume ownership (SMB/NFS), and the single-process boundary.

**Image variants (`WITH_LOCAL_OCR` build arg):** the offline PaddleOCR stack is ~1 GB of the image. Build with or without it:

```bash
# Full — bundled offline OCR (default)
docker build -t readyforai/mantisfetch:latest .

# Slim — ~1 GB smaller, no local OCR; OCR runs via the configured LLM provider
docker build --build-arg WITH_LOCAL_OCR=false -t readyforai/mantisfetch:slim .
```

With `WITH_LOCAL_OCR=false`, `image_ocr_backend=auto` (the default) falls back to LLM/vision OCR, so **an LLM provider must be configured** for the slim image to OCR at all; an explicit `image_ocr_backend=local` request returns a failed status (no local worker). Startup prewarm is auto-disabled to match. Inspect a running image with `docker inspect --format '{{index .Config.Labels "com.readyforai.mantisfetch.local-ocr"}}' <image>`.

```yaml
# docker-compose.yml (excerpt)
services:
  mantisfetch:
    build: .
    ports:
      - "9898:9898"
    volumes:
      - ${MANTISFETCH_HOST_DOCS_DIR:-${HOME}/.mantisfetch/docs}:/root/.mantisfetch/docs
```

**Environment variables (pass via `.env` or `docker compose` `environment` block):**

| Variable | Default | Description |
|---|---|---|
| `MANTISFETCH_LLM_PROVIDER` | `gemini` | LLM backend: `gemini` or `openai` |
| `MANTISFETCH_LLM_VENDOR` | `openai` | Vendor profile for OpenAI-compatible APIs: `openai`, `zhipu`, `kimi`, `aliyun`, `volcengine` |
| `GEMINI_API_KEY` | — | Google Gemini API key |
| `MANTISFETCH_LLM_API_KEY` | — | API key for OpenAI-compatible provider |
| `MANTISFETCH_LLM_BASE_URL` | vendor default | Base URL override for OpenAI-compat provider |
| `MANTISFETCH_LLM_MODEL` | vendor default | Model name override |
| `MANTISFETCH_OCR_MODEL` | `MANTISFETCH_LLM_MODEL` | Optional OCR vision model override |
| `MANTISFETCH_OCR_IMAGE_INPUT_MODE` | `data_url` | OCR image serialization mode: `data_url`, `plain_base64`, `remote_url_only` |
| `MANTISFETCH_LLM_EXTRA_BODY_JSON` | — | Optional JSON object merged into text chat request body |
| `MANTISFETCH_OCR_EXTRA_BODY_JSON` | — | Optional JSON object merged into OCR request body |
| `MANTISFETCH_DOCS_DIR` | `~/.mantisfetch/docs` | Document library directory |
| `MANTISFETCH_STORE_SOURCE_FILES` | `true` | Persist uploaded source files under each document's `source/` directory |

#### Using a local Ollama model

```bash
MANTISFETCH_LLM_PROVIDER=openai \
MANTISFETCH_LLM_API_KEY=ollama \
MANTISFETCH_LLM_BASE_URL=http://host.docker.internal:11434/v1 \
MANTISFETCH_LLM_MODEL=llama3 \
docker compose up
```

#### OpenAI-compatible vendor profiles

OpenAI-compatible integrations can use a vendor profile to supply a default `base_url`,
text model, and OCR model. You can still override any of them explicitly.
The global default OCR image input mode stays `data_url` for maximum compatibility.

```bash
# Zhipu: text + OCR models split by default
MANTISFETCH_LLM_PROVIDER=openai \
MANTISFETCH_LLM_VENDOR=zhipu \
MANTISFETCH_LLM_API_KEY=your_key_here \
docker compose up

# Kimi: one multimodal model for text and OCR by default
MANTISFETCH_LLM_PROVIDER=openai \
MANTISFETCH_LLM_VENDOR=kimi \
MANTISFETCH_LLM_API_KEY=your_key_here \
docker compose up

# Aliyun Bailian: vendor defaults can be overridden
MANTISFETCH_LLM_PROVIDER=openai \
MANTISFETCH_LLM_VENDOR=aliyun \
MANTISFETCH_LLM_API_KEY=your_key_here \
docker compose up

# Volcengine Ark: set your deployed endpoint/model explicitly
MANTISFETCH_LLM_PROVIDER=openai \
MANTISFETCH_LLM_VENDOR=volcengine \
MANTISFETCH_LLM_API_KEY=your_key_here \
MANTISFETCH_LLM_MODEL=your_endpoint_or_model \
MANTISFETCH_OCR_MODEL=your_vision_endpoint_or_model \
docker compose up
```

Vendor-specific request fields can be injected without code changes:

```bash
MANTISFETCH_OCR_EXTRA_BODY_JSON='{"image_url_detail":"high"}'
```

OCR image serialization can also be switched explicitly:

```bash
MANTISFETCH_OCR_IMAGE_INPUT_MODE=plain_base64
```

`remote_url_only` is recognized but currently fails fast in the built-in OCR pipeline,
because MantisFetch renders pages in-memory and does not yet upload them to a hosted URL.

### API

All endpoints are served on port **9898**.

#### Core

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Aggregated health check for all services |
| `GET` | `/web/health` | Browser sub-app health |
| `GET` | `/doc/health` | DocReader sub-app health (includes `docs_dir`) |
| `POST` | `/web/capture` | Capture a URL and persist it to the document library |
| `POST` | `/doc/parse` | Upload and parse a document (PDF, DOCX, PPTX, XLSX, CSV, HTML), optionally storing metadata, source file references, and embedded Word image OCR results |

#### Browser Session API

Stateful browser sessions for multi-step web automation.

| Method | Path | Description |
|---|---|---|
| `POST` | `/web/session/new` | Open a new Playwright browser session |
| `POST` | `/web/session/goto` | Navigate the session to a URL |
| `POST` | `/web/session/distill` | Extract structured content from the current page |
| `POST` | `/web/session/read_sections` | Retrieve specific sections by ID from the last distill |
| `POST` | `/web/session/act` | Click, type, select, or scroll an interactive element |
| `POST` | `/web/session/scroll` | Scroll the page up or down by a pixel amount |
| `POST` | `/web/session/navigate` | Go back or forward in the browser history |
| `POST` | `/web/session/webmcp_discover` | Discover WebMCP tools exposed by the current page |
| `POST` | `/web/session/webmcp_invoke` | Invoke a WebMCP tool by name |
| `POST` | `/web/session/export_storage_state` | Export cookies and local storage for session reuse |
| `POST` | `/web/session/close` | Close the session and release browser resources |

#### Document Library API

Access documents stored by `/web/capture` and `/doc/parse`.

| Method | Path | Description |
|---|---|---|
| `GET` | `/doc/library/search` | Search by keyword, tag, and/or file type |
| `GET` | `/doc/library/search_text` | Search full text / sections with snippets and page hints |
| `POST` | `/doc/library/{doc_id}/search_sections` | Search within one document's sections (sid + page provenance) |
| `GET` | `/doc/library/{doc_id}/digest` | Short summary (~200 tokens) |
| `GET` | `/doc/library/{doc_id}/brief` | Extended summary (~1500 tokens) |
| `GET` | `/doc/library/{doc_id}/full` | Full document text |
| `GET` | `/doc/library/{doc_id}/sections` | List all sections with metadata |
| `GET` | `/doc/library/{doc_id}/section/{sid}` | Full text of a single section |
| `POST` | `/doc/library/{doc_id}/sections/batch` | Read multiple sections by sid in one request |
| `GET` | `/doc/library/{doc_id}/table/{table_id}` | Markdown table with column statistics |
| `GET` | `/doc/library/{doc_id}/table/{table_id}/json` | Structured table JSON (cells with row/column spans) |
| `POST` | `/doc/library/{doc_id}/chunks` | Section-boundary chunks for downstream retrieval/RAG |
| `GET` | `/doc/library/{doc_id}/images` | List embedded Word images with anchors and OCR results |
| `GET` | `/doc/library/{doc_id}/image/{image_id}` | Metadata and OCR text for one embedded Word image |
| `GET` | `/doc/library/{doc_id}/image/{image_id}/raw` | Raw image bytes (`variant=rendered\|original`) for visual reads |
| `GET` | `/doc/library/{doc_id}/summary` | Summary status (poll after a deferred parse) |
| `POST` | `/doc/library/{doc_id}/summary` | (Re)schedule or retry summary generation |
| `GET` | `/doc/library/{doc_id}/manifest` | Provenance metadata (source, timestamps, content hash) |

#### MCP

MantisFetch also exposes `/web` and `/doc` as **Model Context Protocol** tools over streamable-HTTP at `/mcp`, for agent runtimes (e.g. NodalOS) that speak MCP. It is **loopback-only by default**; set `MANTISFETCH_MCP_TOKEN` (and optionally TLS) to allow remote clients.

Full API reference: see [`skills/mantisfetch-browser-SKILL.md`](skills/mantisfetch-browser-SKILL.md), [`skills/mantisfetch-docreader-SKILL.md`](skills/mantisfetch-docreader-SKILL.md), and [`skills/mantisfetch-mcp-SKILL.md`](skills/mantisfetch-mcp-SKILL.md).

DocReader notes:

- `POST /doc/parse` accepts an optional `metadata` form field containing a JSON object; shallow scalar values are indexed for later filtering.
- `POST /doc/parse` also accepts an optional `doc_id` form field. Valid values may contain letters, digits, and internal hyphens, for example `DOC-001`, `NBS250321`, or `doc001`. Reusing an existing `doc_id` returns `409` unless `replace=true`.
- `POST /doc/parse` accepts `summary_mode` (`sync` / `defer` / `off`); with `defer`, the summary is generated in the background — poll `GET /doc/library/{doc_id}/summary` for status, and `POST` the same path to retry a failed one.
- `POST /doc/parse` accepts `parse_mode` (`fast` / `accurate` / `full`, default `accurate`) to tune PDF parsing intensity vs. cost.
- `GET /doc/library/{doc_id}/table/{table_id}/json` returns structured cells; for tables reconstructed from scanned pages, merged cells now carry a recovered `colspan` (the Markdown form is unchanged).
- `POST /doc/parse` and `POST /web/capture` accept `content_type`, one of `General`, `Contract`, `Bid`, or `Knowledge` (case-insensitive on input; persisted in title case); the default is `General`.
- New ingested documents are stored under `${MANTISFETCH_DOCS_DIR}/<content_type>/<doc_id>`, while legacy flat `${MANTISFETCH_DOCS_DIR}/<doc_id>` documents remain readable.
- `GET /doc/library/search` also accepts query params prefixed with `metadata.` for equality-style filtering, for example `metadata.customer=ACME`.
- `GET /doc/library/search` and `GET /doc/library/search_text` accept `content_type` for category-filtered browsing.
- `GET /doc/library/search_text` returns `snippet`, `sid`, `page_range`, `page_start`, and `page_end` to support page-level follow-up actions.
- Parsed document manifests now include `metadata`, `source_file`, and enriched section page bounds.
- If `MANTISFETCH_DOC_ID_STRATEGY=source_filename`, uploaded files may use a sanitized source filename as the document directory name. Unsupported characters are stripped, separators such as spaces / `_` / `.` are normalized to `-`, and the service falls back to `DOC-xxx` when no usable characters remain.

### Web search (optional)

Off by default. Set `MANTISFETCH_SEARCH_PROVIDER` to enable two endpoints:

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/web/search` | Web search — returns ranked `{url, title, snippet, ...}`; captures nothing |
| `POST` | `/web/search_and_capture` | Search, then capture the top N hits (≤3, serial) into the library with search provenance — returns `doc_id`s ready for the three-tier read |

Providers: `searxng` (self-hosted, zero API cost — the recommended default), `tavily`, `bocha` (博查, China), `brave`. A comma-separated `MANTISFETCH_SEARCH_FALLBACK` switches provider only on connection error / 5xx / timeout (a 4xx config error or an empty result does **not** fall back). When unset, `/web/search*` return `404` and the MCP `web_search` / `web_search_capture` tools are not registered.

Zero-cost SearXNG in one command:

```bash
MANTISFETCH_SEARCH_PROVIDER=searxng docker compose --profile search up
```

For **China deployments**, SearXNG's default upstreams (Google, DuckDuckGo) are usually blocked — activate the China preset first (a Baidu / 360 / Sogou / Quark / Bing starting set; **validate it from your own network**): `cp configs/searxng/settings.cn.yml configs/searxng/settings.yml`.

`search_and_capture` reuses the `/web/capture` path, so its results are deduplicated by the same `MANTISFETCH_CAPTURE_TTL_HOURS` cache — **enable it (`> 0`) to share captures (and paid-API savings) across agents/hosts** (it is `0`/off by default). A cache hit keeps the document's original provenance (first-touch): a URL first captured directly stays `source=web_capture`. Filter search-sourced documents with `metadata.source=web_search`, **not** the top-level `source`.

| Variable | Default | Description |
|---|---|---|
| `MANTISFETCH_SEARCH_PROVIDER` | — | `searxng` / `tavily` / `bocha` / `brave`; unset disables search |
| `MANTISFETCH_SEARCH_FALLBACK` | — | Comma-separated fallback chain, e.g. `tavily,searxng` |
| `MANTISFETCH_SEARXNG_URL` | — | SearXNG instance URL (the `search` compose profile defaults it to `http://searxng:8080`) |
| `MANTISFETCH_SEARCH_API_KEY` | — | API key for the active provider (Tavily / Bocha / Brave) |
| `MANTISFETCH_SEARCH_MAX_RESULTS` | `10` | Default result cap (hard max 20) |
| `MANTISFETCH_SEARCH_MIN_INTERVAL_SEC` | `2` | Minimum seconds between searches (`429` when exceeded) |

### Configuration

MantisFetch is configured entirely through environment variables. See the table in the **Docker** section above for LLM settings. Additional variables:

| Variable | Default | Description |
|---|---|---|
| `HOST` | `0.0.0.0` | Bind address |
| `PORT` | `9898` | HTTP listening port |
| `LANG` | `en` | UI language (`en` or `zh`) |
| `MANTISFETCH_TLS_CERTFILE` | — | TLS certificate path; set **with** `MANTISFETCH_TLS_KEYFILE` to serve HTTPS (setting only one is ignored) |
| `MANTISFETCH_TLS_KEYFILE` | — | TLS private key path (paired with `MANTISFETCH_TLS_CERTFILE`) |
| `MANTISFETCH_MCP_TOKEN` | — | Bearer token for the `/mcp`, `/web`, `/doc` and `/deliverables` surfaces; without it they are **loopback-only** (non-loopback callers get 403) |
| `MANTISFETCH_MCP_ALLOWED_HOSTS` | — | Extra hosts/origins (comma-separated) for the MCP DNS-rebinding guard |
| `MANTISFETCH_ALLOWED_DOC_ROOTS` | — | Allowlist roots for the MCP `doc_parse` `rel_path` source; unset disables local-path parsing over MCP |
| `MANTISFETCH_DELIVERABLES_ROOT` | — | Fence root for the read-only `GET /deliverables/{rel_path}` byte face; unset disables it (every request 404s). Must not overlap `MANTISFETCH_DOCS_DIR` or `MANTISFETCH_ALLOWED_DOC_ROOTS` |
| `MANTISFETCH_DELIVERABLES_MAX_MB` | `200` | Size cap for a single deliverable download; larger files get 413 |
| `MANTISFETCH_DOC_ID_STRATEGY` | `counter` | Document directory naming strategy: `counter` keeps `DOC-xxx`; `source_filename` derives a safe directory name from the uploaded filename stem |
| `MANTISFETCH_CAPTURE_TTL_HOURS` | `0` | Reuse a prior `/web/capture` of the same URL + content_type made within this many hours instead of re-fetching; `0` disables (default). `force_refresh=true` always bypasses |
| `MANTISFETCH_SUMMARY_BATCH_CONCURRENCY` | `1` | Maximum concurrent section-summary LLM batches per document |
| `MANTISFETCH_SUMMARY_REQUEST_MIN_INTERVAL_SEC` | `2.0` | Minimum spacing between summary LLM requests across the service |
| `MANTISFETCH_SUMMARY_SECTION_DETAIL_LIMIT` | `10` | Documents above this section count skip per-section LLM summaries and generate document-level summaries from section excerpts |

### For AI Agents

MantisFetch is designed to be deployed and operated autonomously by AI Agents. A self-contained deployment prompt (English + Chinese) is available at [`docs/agent-deployment-prompt.md`](docs/agent-deployment-prompt.md) — copy it directly into your Agent's system prompt, no modification needed.

### Contributing

Contributions are welcome! Please read [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, code conventions, and the PR process.

### License

MIT — see [LICENSE](LICENSE).

---

## 中文

**MantisFetch** 是由 [ReadyForAI](https://github.com/ReadyForAI) 开源的 Agent 原生数据采集与文档解析工具包。

### 为什么选择 MantisFetch？

大多数爬虫工具返回原始 HTML——对 LLM 而言噪音极多、token 消耗过高。MantisFetch 专为 AI Agent 设计：网页经过语义蒸馏，转化为结构化段落（sections）和可交互元素（actions），相比原始 HTML 节省 90%+ 的 token。三层加载模型（digest → brief → section）让 Agent 按需取用，无需把整篇文档塞进上下文。增量读取（diff-first）机制确保重复访问同一页面时的额外开销接近于零；表格自动预计算统计值，Agent 无需逐行读取即可回答数据问题。

### 功能特性

- **语义蒸馏** — 网页 → 结构化段落 + 可交互元素；每次 distill 仅 4,000–8,000 字符，对比原始 HTML 节省 90%+ token
- **三层加载** — digest（约 200 token）→ brief（约 1,500 token）→ section（按需）；Agent 只取所需
- **增量读取** — `distill` 仅返回变化段落（`changed_sids`），重复访问近零 token 开销
- **表格感知** — 自动提取并预计算统计值（min/max/avg），无需读取原始行即可回答数据问题
- **文档解析** — 支持 PDF、DOCX、PPTX、XLSX、CSV、HTML，基于 [MarkItDown](https://github.com/microsoft/markitdown)（微软），可自动 Gemini OCR 兜底
- **多 LLM 支持** — Gemini（默认）、OpenAI、Ollama、Groq，以及任意 OpenAI 兼容接口
- **MCP 服务** — 把 `/web` 和 `/doc` 暴露为原生 MCP 工具（streamable-HTTP，挂载在 `/mcp`），供 Agent 运行时使用；默认仅 loopback，远程客户端需 bearer token + 可选 TLS
- **WebMCP 客户端** — 发现并调用 Chrome 146+ 页面通过 `navigator.modelContext` 暴露的结构化工具
- **多语言** — 支持中英文（设置 `LANG=zh` 切换为中文）

### 快速上手

#### Docker（推荐）

```bash
git clone https://github.com/ReadyForAI/MantisFetch.git
cd MantisFetch
GEMINI_API_KEY=your_key_here docker compose up -d

# 检查服务状态
curl http://localhost:9898/health
```

#### Python（本地运行）

```bash
git clone https://github.com/ReadyForAI/MantisFetch.git
cd MantisFetch
pip install -r requirements.txt
playwright install chromium

export GEMINI_API_KEY=your_key_here
python mantisfetch_server.py     # 监听 9898 端口
```

### Docker 配置

`docker-compose.yml` 提供单服务部署方案。默认会把文档库 bind mount 到宿主机当前用户的 `~/.mantisfetch/docs` 目录。

**镜像变体（`WITH_LOCAL_OCR` 构建参数）：** 离线 PaddleOCR 栈约占镜像 1 GB。可选择带或不带：

```bash
# 完整版 —— 内置离线 OCR（默认）
docker build -t readyforai/mantisfetch:latest .

# 精简版 —— 体积小约 1 GB，不含本地 OCR；OCR 走配置的 LLM provider
docker build --build-arg WITH_LOCAL_OCR=false -t readyforai/mantisfetch:slim .
```

设 `WITH_LOCAL_OCR=false` 时，`image_ocr_backend=auto`（默认值）会 fallback 到 LLM/vision OCR，因此精简版**必须配置 LLM provider** 才能做 OCR；若显式请求 `image_ocr_backend=local` 则返回失败状态（无本地 worker）。启动期 prewarm 会随之自动关闭。用 `docker inspect --format '{{index .Config.Labels "com.readyforai.mantisfetch.local-ocr"}}' <image>` 查看某镜像是哪种变体。

```yaml
# docker-compose.yml（节选）
services:
  mantisfetch:
    build: .
    ports:
      - "9898:9898"
    volumes:
      - ${MANTISFETCH_HOST_DOCS_DIR:-${HOME}/.mantisfetch/docs}:/root/.mantisfetch/docs
```

**环境变量（通过 `.env` 文件或 `docker compose` 的 `environment` 块传入）：**

| 变量 | 默认值 | 说明 |
|---|---|---|
| `MANTISFETCH_LLM_PROVIDER` | `gemini` | LLM 后端：`gemini` 或 `openai` |
| `MANTISFETCH_LLM_VENDOR` | `openai` | OpenAI 兼容厂商配置：`openai`、`zhipu`、`kimi`、`aliyun`、`volcengine` |
| `GEMINI_API_KEY` | — | Google Gemini API Key |
| `MANTISFETCH_LLM_API_KEY` | — | OpenAI 兼容接口的 API Key |
| `MANTISFETCH_LLM_BASE_URL` | 厂商默认值 | 覆盖 OpenAI 兼容接口的 Base URL |
| `MANTISFETCH_LLM_MODEL` | 厂商默认值 | 指定模型名称 |
| `MANTISFETCH_OCR_MODEL` | `MANTISFETCH_LLM_MODEL` | 可选：单独指定 OCR 视觉模型 |
| `MANTISFETCH_OCR_IMAGE_INPUT_MODE` | `data_url` | OCR 图片序列化模式：`data_url`、`plain_base64`、`remote_url_only` |
| `MANTISFETCH_LLM_EXTRA_BODY_JSON` | — | 可选：合并到文本请求体中的 JSON 对象 |
| `MANTISFETCH_OCR_EXTRA_BODY_JSON` | — | 可选：合并到 OCR 请求体中的 JSON 对象 |
| `MANTISFETCH_DOCS_DIR` | `~/.mantisfetch/docs` | 文档库存储目录 |
| `MANTISFETCH_STORE_SOURCE_FILES` | `true` | 是否将上传原件保存在每个文档目录下的 `source/` 子目录 |

#### 使用本地 Ollama 模型

```bash
MANTISFETCH_LLM_PROVIDER=openai \
MANTISFETCH_LLM_API_KEY=ollama \
MANTISFETCH_LLM_BASE_URL=http://host.docker.internal:11434/v1 \
MANTISFETCH_LLM_MODEL=llama3 \
docker compose up
```

#### OpenAI 兼容厂商配置

OpenAI 兼容接口支持按厂商 profile 自动补全默认 `base_url`、文本模型和 OCR 模型；如果你已有固定配置，也可以继续显式覆盖。
OCR 图片输入模式的全局默认值仍固定为 `data_url`，优先保证兼容面。

```bash
# 智谱：默认文本模型与 OCR 模型分离
MANTISFETCH_LLM_PROVIDER=openai \
MANTISFETCH_LLM_VENDOR=zhipu \
MANTISFETCH_LLM_API_KEY=your_key_here \
docker compose up

# Kimi：默认使用同一个多模态模型处理文本与 OCR
MANTISFETCH_LLM_PROVIDER=openai \
MANTISFETCH_LLM_VENDOR=kimi \
MANTISFETCH_LLM_API_KEY=your_key_here \
docker compose up

# 阿里百炼：可使用 profile 默认模型，也可自行覆盖
MANTISFETCH_LLM_PROVIDER=openai \
MANTISFETCH_LLM_VENDOR=aliyun \
MANTISFETCH_LLM_API_KEY=your_key_here \
docker compose up

# 火山方舟：通常需要显式填写你的推理接入点 / 模型名
MANTISFETCH_LLM_PROVIDER=openai \
MANTISFETCH_LLM_VENDOR=volcengine \
MANTISFETCH_LLM_API_KEY=your_key_here \
MANTISFETCH_LLM_MODEL=your_endpoint_or_model \
MANTISFETCH_OCR_MODEL=your_vision_endpoint_or_model \
docker compose up
```

如果厂商需要额外请求参数，也可以直接透传 JSON：

```bash
MANTISFETCH_OCR_EXTRA_BODY_JSON='{"image_url_detail":"high"}'
```

也可以显式切换 OCR 图片输入模式：

```bash
MANTISFETCH_OCR_IMAGE_INPUT_MODE=plain_base64
```

其中 `remote_url_only` 目前只做了显式报错路径：MantisFetch 现在是把 PDF 页渲染到内存里直接上送，还没有内建“先上传图片再传远程 URL”的流程。

### API 接口

所有接口均运行在 **9898** 端口。

#### 核心接口

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/health` | 全服务聚合健康检查 |
| `GET` | `/web/health` | Browser 子服务健康检查 |
| `GET` | `/doc/health` | DocReader 子服务健康检查（含 `docs_dir`） |
| `POST` | `/web/capture` | 抓取 URL 并保存到文档库 |
| `POST` | `/doc/parse` | 上传并解析文档（PDF、DOCX、PPTX、XLSX、CSV、HTML），可附带 metadata、保留原始文件引用，并可抽取 Word 内嵌图片 OCR 结果 |

#### Browser Session API

有状态浏览器会话，支持多步骤网页自动化。

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/web/session/new` | 打开新的 Playwright 浏览器会话 |
| `POST` | `/web/session/goto` | 在当前会话中导航到指定 URL |
| `POST` | `/web/session/distill` | 从当前页面提取结构化内容 |
| `POST` | `/web/session/read_sections` | 按 ID 获取上次 distill 的指定章节 |
| `POST` | `/web/session/act` | 对交互元素执行点击、输入、选择或滚动 |
| `POST` | `/web/session/scroll` | 按像素上下滚动页面 |
| `POST` | `/web/session/navigate` | 浏览器前进或后退 |
| `POST` | `/web/session/webmcp_discover` | 发现当前页面暴露的 WebMCP 工具 |
| `POST` | `/web/session/webmcp_invoke` | 按名称调用 WebMCP 工具 |
| `POST` | `/web/session/export_storage_state` | 导出 Cookie 和 LocalStorage 以复用会话 |
| `POST` | `/web/session/close` | 关闭会话并释放浏览器资源 |

#### Document Library API

访问由 `/web/capture` 和 `/doc/parse` 存入文档库的文档。

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/doc/library/search` | 按关键词、标签和/或文件类型搜索 |
| `GET` | `/doc/library/search_text` | 按全文 / section 搜索，并返回片段与页码提示 |
| `POST` | `/doc/library/{doc_id}/search_sections` | 在单个文档的 section 内搜索（返回 sid + 页码 provenance） |
| `GET` | `/doc/library/{doc_id}/digest` | 简短摘要（约 200 token） |
| `GET` | `/doc/library/{doc_id}/brief` | 详细摘要（约 1500 token） |
| `GET` | `/doc/library/{doc_id}/full` | 完整文档正文 |
| `GET` | `/doc/library/{doc_id}/sections` | 列出所有章节及元数据 |
| `GET` | `/doc/library/{doc_id}/section/{sid}` | 单个章节的完整文本 |
| `POST` | `/doc/library/{doc_id}/sections/batch` | 一次请求按 sid 读取多个章节 |
| `GET` | `/doc/library/{doc_id}/table/{table_id}` | 带列统计的 Markdown 表格 |
| `GET` | `/doc/library/{doc_id}/table/{table_id}/json` | 结构化表格 JSON（单元格含行/列跨度） |
| `POST` | `/doc/library/{doc_id}/chunks` | 面向下游检索/RAG 的 section 边界分块 |
| `GET` | `/doc/library/{doc_id}/images` | 列出 Word 内嵌图片、锚点和 OCR 结果 |
| `GET` | `/doc/library/{doc_id}/image/{image_id}` | 单个 Word 内嵌图片的元数据和 OCR 文本 |
| `GET` | `/doc/library/{doc_id}/image/{image_id}/raw` | 图片原始字节（`variant=rendered\|original`），用于视觉读图 |
| `GET` | `/doc/library/{doc_id}/summary` | 摘要状态（延迟解析后轮询） |
| `POST` | `/doc/library/{doc_id}/summary` | （重新）调度或重试摘要生成 |
| `GET` | `/doc/library/{doc_id}/manifest` | 来源元数据（来源地址、时间戳、内容哈希） |

#### MCP

MantisFetch 还把 `/web` 和 `/doc` 暴露为 **Model Context Protocol** 工具（streamable-HTTP，挂载在 `/mcp`），供使用 MCP 的 Agent 运行时（如 NodalOS）使用。**默认仅 loopback**；设置 `MANTISFETCH_MCP_TOKEN`（并可选启用 TLS）以允许远程客户端。

完整 API 说明见 [`skills/mantisfetch-browser-SKILL.md`](skills/mantisfetch-browser-SKILL.md)、[`skills/mantisfetch-docreader-SKILL.md`](skills/mantisfetch-docreader-SKILL.md) 和 [`skills/mantisfetch-mcp-SKILL.md`](skills/mantisfetch-mcp-SKILL.md)。

DocReader 补充说明：

- `POST /doc/parse` 支持可选 `metadata` 表单字段，值为 JSON object；其中浅层标量字段会进入索引，供后续过滤。
- `POST /doc/parse` 也支持可选 `doc_id` 表单字段；合法值可包含字母、数字和中间连字符，例如 `DOC-001`、`NBS250321`、`doc001`。复用已有 `doc_id` 会返回 `409`，除非 `replace=true`。
- `POST /doc/parse` 支持 `summary_mode`（`sync` / `defer` / `off`）；用 `defer` 时摘要在后台生成 —— 轮询 `GET /doc/library/{doc_id}/summary` 查看状态，`POST` 同一路径可重试失败的摘要。
- `POST /doc/parse` 支持 `parse_mode`（`fast` / `accurate` / `full`，默认 `accurate`），用于在 PDF 解析强度与成本间权衡。
- `GET /doc/library/{doc_id}/table/{table_id}/json` 返回结构化单元格；对从扫描页重建的表格，合并单元格现在带恢复的 `colspan`（Markdown 形式不变）。
- `POST /doc/parse` 和 `POST /web/capture` 支持 `content_type`，可选值为 `General`、`Contract`、`Bid`、`Knowledge`（输入大小写不敏感，存储统一为首字母大写）；默认 `General`。
- 新入库文档会保存到 `${MANTISFETCH_DOCS_DIR}/<content_type>/<doc_id>`，旧版平铺的 `${MANTISFETCH_DOCS_DIR}/<doc_id>` 文档仍可读取。
- `GET /doc/library/search` 支持 `metadata.*` 形式的查询参数做等值过滤，例如 `metadata.customer=ACME`。
- `GET /doc/library/search` 和 `GET /doc/library/search_text` 支持 `content_type`，用于按分类浏览。
- `GET /doc/library/search_text` 会返回 `snippet`、`sid`、`page_range`、`page_start`、`page_end`，便于后续定位页面。
- `manifest.json` 现在会包含 `metadata`、`source_file`，以及补强后的 section 页码边界。
- 如果设置 `MANTISFETCH_DOC_ID_STRATEGY=source_filename`，上传文件会优先使用“过滤后的源文件名”作为文档目录名；空格、下划线、点会归一成 `-`，不支持的字符会被剔除；若过滤后没有剩余可用字符，则自动回退为 `DOC-xxx`。

### 网络搜索（可选）

默认关闭。设置 `MANTISFETCH_SEARCH_PROVIDER` 后启用两个端点：

| 方法 | 端点 | 说明 |
|---|---|---|
| `POST` | `/web/search` | 纯搜索——返回排序后的 `{url, title, snippet, ...}`，不采集 |
| `POST` | `/web/search_and_capture` | 搜索后串行采集前 N 条（≤3）入库并写入搜索来源标记——返回可直接三级加载的 `doc_id` |

Provider：`searxng`（自托管、零 API 成本，推荐默认）、`tavily`、`bocha`（博查，国内）、`brave`。逗号分隔的 `MANTISFETCH_SEARCH_FALLBACK` **仅**在连接错误 / 5xx / 超时时切换 provider（4xx 配置错误或空结果**不**切换）。未设置时 `/web/search*` 返回 `404`，MCP 的 `web_search` / `web_search_capture` 工具不注册。

一条命令启动零成本 SearXNG：

```bash
MANTISFETCH_SEARCH_PROVIDER=searxng docker compose --profile search up
```

**国内部署**:SearXNG 默认上游(Google、DuckDuckGo)通常被墙——先启用国内预设(Baidu / 360 / Sogou / Quark / Bing 起步组合,**请在目标网络实测校准**):`cp configs/searxng/settings.cn.yml configs/searxng/settings.yml`。

`search_and_capture` 复用 `/web/capture` 路径，因此结果由同一个 `MANTISFETCH_CAPTURE_TTL_HOURS` 缓存去重——**将其设为 `> 0` 可在多 Agent / 多主机间共享采集结果（及付费 API 配额节省）**（默认 `0` 关闭）。缓存命中保留文档原有来源（first-touch）：先被直接采集的 URL 仍为 `source=web_capture`。过滤搜索来源文档请用 `metadata.source=web_search`，**不要**用顶层 `source`。

| 变量 | 默认 | 说明 |
|---|---|---|
| `MANTISFETCH_SEARCH_PROVIDER` | — | `searxng` / `tavily` / `bocha` / `brave`；不设则禁用搜索 |
| `MANTISFETCH_SEARCH_FALLBACK` | — | 逗号分隔的降级链，如 `tavily,searxng` |
| `MANTISFETCH_SEARXNG_URL` | — | SearXNG 实例地址（`search` compose profile 默认为 `http://searxng:8080`） |
| `MANTISFETCH_SEARCH_API_KEY` | — | 当前 provider 的 API key（Tavily / 博查 / Brave） |
| `MANTISFETCH_SEARCH_MAX_RESULTS` | `10` | 单次结果上限（硬顶 20） |
| `MANTISFETCH_SEARCH_MIN_INTERVAL_SEC` | `2` | 两次搜索的最小间隔秒数（超出返回 `429`） |

### 配置项

MantisFetch 所有配置均通过环境变量管理。LLM 相关配置见上方 **Docker 配置** 一节，其他变量如下：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `HOST` | `0.0.0.0` | 绑定地址 |
| `PORT` | `9898` | HTTP 监听端口 |
| `LANG` | `en` | 界面语言（`en` 英文 / `zh` 中文） |
| `MANTISFETCH_TLS_CERTFILE` | — | TLS 证书路径；与 `MANTISFETCH_TLS_KEYFILE` **同时**设置才会启用 HTTPS（只设其一会被忽略） |
| `MANTISFETCH_TLS_KEYFILE` | — | TLS 私钥路径（与 `MANTISFETCH_TLS_CERTFILE` 配对） |
| `MANTISFETCH_MCP_TOKEN` | — | `/mcp` 接口的 bearer token；不设置则 `/mcp` 仅 loopback 可达 |
| `MANTISFETCH_MCP_ALLOWED_HOSTS` | — | MCP DNS-rebinding 防护的额外 host/origin（逗号分隔） |
| `MANTISFETCH_ALLOWED_DOC_ROOTS` | — | MCP `doc_parse` `rel_path` source 的 allowlist 根目录；不设置则禁用 MCP 上的本地路径解析 |
| `MANTISFETCH_DELIVERABLES_ROOT` | — | 只读 `GET /deliverables/{rel_path}` 字节接口的围栏根目录；不设置则禁用（所有请求返回 404）。不得与 `MANTISFETCH_DOCS_DIR` 或 `MANTISFETCH_ALLOWED_DOC_ROOTS` 重叠 |
| `MANTISFETCH_DELIVERABLES_MAX_MB` | `200` | 单个交付物下载的大小上限；超出返回 413 |
| `MANTISFETCH_DOC_ID_STRATEGY` | `counter` | 文档目录命名策略：`counter` 保持 `DOC-xxx`；`source_filename` 基于上传文件名生成安全目录名 |
| `MANTISFETCH_CAPTURE_TTL_HOURS` | `0` | 在这么多小时内对同一 URL + content_type 的 `/web/capture` 直接复用已有结果而不重抓；`0` 关闭（默认）。`force_refresh=true` 始终绕过 |
| `MANTISFETCH_SUMMARY_BATCH_CONCURRENCY` | `1` | 单文档 section 摘要的最大 LLM batch 并发数 |
| `MANTISFETCH_SUMMARY_REQUEST_MIN_INTERVAL_SEC` | `2.0` | 全服务摘要 LLM 请求之间的最小间隔秒数 |
| `MANTISFETCH_SUMMARY_SECTION_DETAIL_LIMIT` | `10` | 超过该 section 数量后跳过逐 section LLM 摘要，改用 section 摘录生成文档级摘要 |

### 接入 AI Agent

MantisFetch 支持由 AI Agent 自主部署和操作。[`docs/agent-deployment-prompt.md`](docs/agent-deployment-prompt.md) 提供开箱即用的中英双语部署提示词，直接复制到 Agent 的系统提示词中即可使用，无需任何修改。

### 参与贡献

欢迎提交 PR！开发环境搭建、代码规范和 PR 流程请参阅 [CONTRIBUTING.md](CONTRIBUTING.md)。

### 许可证

MIT 协议，详见 [LICENSE](LICENSE)。
