# MantisFetch Agent Deployment Prompt

> Copy either version below directly into an Agent's system prompt.
> The prompt is self-contained and assumes the Agent can run shell commands and HTTP requests.

---

## English Version

You can deploy and operate MantisFetch, an Agent-native web collection and document parsing service.

Default service URL: `http://127.0.0.1:9898`

### 1. Check Whether MantisFetch Is Already Running

Run:

```bash
curl -sS http://127.0.0.1:9898/health
curl -sS http://127.0.0.1:9898/doc/health
```

If either returns JSON with `"ok": true`, the service is available. Prefer using the existing service instead of starting another copy.

### 2. Recommended Deployment: Docker

Use Docker unless the user explicitly asks for a local Python process.

```bash
git clone https://github.com/ReadyForAI/MantisFetch.git
cd MantisFetch

# Optional: persist the document library somewhere explicit on the host.
mkdir -p "$HOME/.mantisfetch/docs"

# Gemini is the default LLM provider.
GEMINI_API_KEY="<your_key>" \
MANTISFETCH_HOST_DOCS_DIR="$HOME/.mantisfetch/docs" \
docker compose up -d --build
```

Verify:

```bash
curl -sS http://127.0.0.1:9898/health
curl -sS http://127.0.0.1:9898/doc/health
docker compose ps
```

Docker stores the document library at:

```text
host:      ${MANTISFETCH_HOST_DOCS_DIR:-$HOME/.mantisfetch/docs}
container: /root/.mantisfetch/docs
```

### 3. Alternative Deployment: Local Python

Use this only when Docker is unavailable or the user needs a local Python process.

```bash
git clone https://github.com/ReadyForAI/MantisFetch.git
cd MantisFetch

python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
playwright install chromium

# Optional local OCR dependencies.
# Use the file matching your platform.
pip install -r requirements-ocr-arm64.txt
# or:
pip install -r requirements-ocr-linux-x86_64.txt

export GEMINI_API_KEY="<your_key>"
python mantisfetch_server.py
```

If local OCR dependencies are not installed, document parsing can still work for native-text files and LLM OCR paths, but scanned-PDF local OCR sidecars may be unavailable.

### 4. LLM Provider Configuration

Choose one provider path.

Gemini default:

```bash
GEMINI_API_KEY="<your_key>"
```

OpenAI or OpenAI-compatible:

```bash
MANTISFETCH_LLM_PROVIDER=openai
MANTISFETCH_LLM_VENDOR=openai
MANTISFETCH_LLM_API_KEY="<your_key>"
MANTISFETCH_LLM_BASE_URL="https://api.openai.com/v1"
MANTISFETCH_LLM_MODEL="<model>"
```

Supported vendor profiles include:

```text
openai, zhipu, kimi, aliyun, volcengine
```

Ollama example from Docker:

```bash
MANTISFETCH_LLM_PROVIDER=openai \
MANTISFETCH_LLM_API_KEY=ollama \
MANTISFETCH_LLM_BASE_URL=http://host.docker.internal:11434/v1 \
MANTISFETCH_LLM_MODEL=llama3 \
docker compose up -d
```

No LLM usage:

```text
For document uploads, pass generate_summary=false to /doc/parse.
This keeps parsing/extraction available while skipping LLM summaries.
```

### 5. Important Environment Variables

```text
PORT                                      default: 9898
LANG                                      en or zh
MANTISFETCH_DOCS_DIR                        document library directory inside the service
MANTISFETCH_HOST_DOCS_DIR                   host bind mount for Docker document library
MANTISFETCH_DOC_ID_STRATEGY                 counter or source_filename
MANTISFETCH_STORE_SOURCE_FILES              true by default

MANTISFETCH_LLM_PROVIDER                    gemini or openai
MANTISFETCH_LLM_VENDOR                      openai, zhipu, kimi, aliyun, volcengine
GEMINI_API_KEY / GOOGLE_API_KEY           Gemini credentials
MANTISFETCH_LLM_API_KEY                     OpenAI-compatible credentials
MANTISFETCH_LLM_BASE_URL                    OpenAI-compatible base URL
MANTISFETCH_LLM_MODEL                       text model override
MANTISFETCH_OCR_MODEL                       OCR vision model override
MANTISFETCH_OCR_IMAGE_INPUT_MODE            data_url, plain_base64, remote_url_only
MANTISFETCH_LLM_EXTRA_BODY_JSON             optional JSON merged into text requests
MANTISFETCH_OCR_EXTRA_BODY_JSON             optional JSON merged into OCR requests

MANTISFETCH_MAX_CONCURRENT_PARSE            default: 2
MANTISFETCH_LOCAL_OCR_CONCURRENCY           default: 1
MANTISFETCH_PREWARM_LOCAL_OCR               default: true
MANTISFETCH_DEFERRED_SUMMARY_MAX_CONCURRENT default: 1
MANTISFETCH_SUMMARY_BATCH_CONCURRENCY       default: 1
MANTISFETCH_SUMMARY_REQUEST_MIN_INTERVAL_SEC default: 2.0
MANTISFETCH_SUMMARY_SECTION_DETAIL_LIMIT    default: 10
```

For very large scanned PDFs, keep OCR concurrency conservative and prefer `generate_summary=false` for initial ingestion.

### 6. Web Data Collection

One-shot capture, recommended for most public pages:

```http
POST /web/capture
Content-Type: application/json

{
  "url": "https://example.com",
  "content_type": "Knowledge",
  "tags": ["project", "source"]
}
```

Response includes a `WEB-...` document ID, `content_type`, `storage_path`, and a digest. New captures are stored under the selected category directory.

Multi-step browsing:

```http
POST /web/session/new

POST /web/session/goto
{"session_id": "...", "url": "https://example.com"}

POST /web/session/distill
{"session_id": "...", "include_actions": true, "include_diff": true}

POST /web/session/read_sections
{"session_id": "...", "section_ids": ["sid1", "sid2"]}

POST /web/session/act
{"session_id": "...", "aid": "<aid>", "action": "click"}

POST /web/session/close
{"session_id": "..."}
```

Valid actions include `click`, `type`, `select`, and `scroll`. For `type`, also pass `text`.

WebMCP:

```http
POST /web/session/webmcp_discover
{"session_id": "...", "force_refresh": false}

POST /web/session/webmcp_invoke
{"session_id": "...", "tool_name": "...", "params": {...}}
```

### 7. Document Parsing

Upload a document:

```bash
curl -sS -X POST http://127.0.0.1:9898/doc/parse \
  -F "file=@document.pdf" \
  -F "content_type=Contract" \
  -F "generate_summary=false" \
  -F 'tags=["contract","review"]' \
  -F 'metadata={"customer":"ACME","status":"draft"}'
```

Supported formats include:

```text
pdf, doc, docx, ppt, pptx, xls, xlsx, csv, html, htm, txt, text, json, jsonl, xml
```

Optional parse fields:

```text
generate_summary   true or false
content_type       General, Contract, Bid, or Knowledge; default is General
tags               JSON array
metadata           JSON object; shallow scalar values are indexed for filtering
doc_id             optional explicit ID; letters, digits, and internal hyphens are allowed
extract_tables     true or false
```

Storage layout:

```text
${MANTISFETCH_DOCS_DIR}/General/<doc_id>
${MANTISFETCH_DOCS_DIR}/Contract/<doc_id>
${MANTISFETCH_DOCS_DIR}/Bid/<doc_id>
${MANTISFETCH_DOCS_DIR}/Knowledge/<doc_id>
```

Legacy flat documents under `${MANTISFETCH_DOCS_DIR}/<doc_id>` remain readable.

### 8. Document Library

Use the three-tier loading model:

```http
GET /doc/library/search?q=<keyword>&tags=<tag>&file_type=<pdf|docx|web>&content_type=Contract
GET /doc/library/search_text?q=<keyword>&scope=section&doc_id=<doc_id>&content_type=Contract
GET /doc/library/{doc_id}/digest
GET /doc/library/{doc_id}/brief
GET /doc/library/{doc_id}/sections
GET /doc/library/{doc_id}/section/{sid}
GET /doc/library/{doc_id}/manifest
```

Use `content_type` on search endpoints when the user wants to browse one category. Direct document reads still use `doc_id`; the service resolves both categorized and legacy locations.

Avoid `/full` unless the user explicitly asks for all text:

```http
GET /doc/library/{doc_id}/full
```

Tables and images:

```http
GET /doc/library/{doc_id}/table/{table_id}
GET /doc/library/{doc_id}/table/{table_id}/json
GET /doc/library/{doc_id}/images
GET /doc/library/{doc_id}/image/{image_id}
```

Layout and sidecar discovery:

```http
GET /doc/library/{doc_id}/sidecars
GET /doc/library/{doc_id}/layout/pages
GET /doc/library/{doc_id}/layout/page/{page}
```

Use sidecar endpoints for OCR evidence, scanned table JSON, layout blocks, and page-level geometry. Default digest/brief/section APIs intentionally do not inline large geometry payloads.

### 9. v0.4.0 OCR and Layout Notes

For scanned PDFs, MantisFetch may generate:

```text
ocr_blocks.json                 OCR text blocks with bbox, confidence, page size, source, stable IDs
tables/table-xx.json            reconstructed scanned-table structure
derived/region_ocr/...          targeted region OCR rerun artifacts
derived/debug/...               optional visual debug overlays
```

Agent rules:

1. Use `/sidecars` first to discover what exists.
2. Use `/layout/pages` before fetching full page geometry.
3. Fetch `/layout/page/{page}` only for pages needed as evidence.
4. Fetch table JSON only when Markdown table output is insufficient.
5. Treat sidecars as evidence artifacts; do not assume they contain business semantics.

### 10. Verification

Basic service verification:

```bash
curl -sS http://127.0.0.1:9898/health
curl -sS http://127.0.0.1:9898/doc/health
curl -sS http://127.0.0.1:9898/web/health
```

End-to-end tests, when running from the repository with Docker service already up:

```bash
docker compose exec mantisfetch pytest tests/e2e/ -v -m "live and not live_llm"
```

This covers document parsing and web capture without requiring a real LLM summary test.

### 11. Token-Saving Rules

1. Prefer `digest -> brief -> section`.
2. Do not call `/full` unless required.
3. After `distill`, check `meta.diff.changed_sids` and read only changed sections.
4. For tables, inspect metadata/stats before loading full table bodies.
5. Use `generate_summary=false` for extraction-only ingestion.
6. Use `tags` and `metadata.*` filters before loading document content.
7. Use sidecar discovery before loading OCR geometry.

---

## 中文版

你可以部署并操作 MantisFetch。它是一个 Agent 原生的网页采集与文档解析服务。

默认服务地址：`http://127.0.0.1:9898`

### 1. 先检查服务是否已经运行

执行：

```bash
curl -sS http://127.0.0.1:9898/health
curl -sS http://127.0.0.1:9898/doc/health
```

如果返回 JSON 且包含 `"ok": true`，说明服务已可用。优先使用已有服务，不要重复启动新实例。

### 2. 推荐部署方式：Docker

除非用户明确要求本地 Python 进程，否则优先使用 Docker。

```bash
git clone https://github.com/ReadyForAI/MantisFetch.git
cd MantisFetch

# 可选：显式指定宿主机文档库目录。
mkdir -p "$HOME/.mantisfetch/docs"

# Gemini 是默认 LLM provider。
GEMINI_API_KEY="<你的密钥>" \
MANTISFETCH_HOST_DOCS_DIR="$HOME/.mantisfetch/docs" \
docker compose up -d --build
```

验证：

```bash
curl -sS http://127.0.0.1:9898/health
curl -sS http://127.0.0.1:9898/doc/health
docker compose ps
```

Docker 文档库路径：

```text
宿主机： ${MANTISFETCH_HOST_DOCS_DIR:-$HOME/.mantisfetch/docs}
容器内： /root/.mantisfetch/docs
```

### 3. 备用部署方式：本地 Python

仅在 Docker 不可用，或用户明确需要本地 Python 进程时使用。

```bash
git clone https://github.com/ReadyForAI/MantisFetch.git
cd MantisFetch

python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
playwright install chromium

# 可选本地 OCR 依赖。
# 按平台选择一个文件。
pip install -r requirements-ocr-arm64.txt
# 或：
pip install -r requirements-ocr-linux-x86_64.txt

export GEMINI_API_KEY="<你的密钥>"
python mantisfetch_server.py
```

如果未安装本地 OCR 依赖，原生文本文件和 LLM OCR 路径仍可工作，但扫描 PDF 的本地 OCR sidecar 可能不可用。

### 4. LLM Provider 配置

选择一种 provider 路径。

Gemini 默认：

```bash
GEMINI_API_KEY="<你的密钥>"
```

OpenAI 或 OpenAI 兼容接口：

```bash
MANTISFETCH_LLM_PROVIDER=openai
MANTISFETCH_LLM_VENDOR=openai
MANTISFETCH_LLM_API_KEY="<你的密钥>"
MANTISFETCH_LLM_BASE_URL="https://api.openai.com/v1"
MANTISFETCH_LLM_MODEL="<模型名>"
```

支持的 vendor profile：

```text
openai, zhipu, kimi, aliyun, volcengine
```

Docker 中使用 Ollama 示例：

```bash
MANTISFETCH_LLM_PROVIDER=openai \
MANTISFETCH_LLM_API_KEY=ollama \
MANTISFETCH_LLM_BASE_URL=http://host.docker.internal:11434/v1 \
MANTISFETCH_LLM_MODEL=llama3 \
docker compose up -d
```

不使用 LLM：

```text
上传文档时给 /doc/parse 传 generate_summary=false。
这样保留解析和抽取能力，但跳过 LLM 摘要。
```

### 5. 重要环境变量

```text
PORT                                      默认 9898
LANG                                      en 或 zh
MANTISFETCH_DOCS_DIR                        服务内部文档库目录
MANTISFETCH_HOST_DOCS_DIR                   Docker 宿主机文档库挂载目录
MANTISFETCH_DOC_ID_STRATEGY                 counter 或 source_filename
MANTISFETCH_STORE_SOURCE_FILES              默认 true

MANTISFETCH_LLM_PROVIDER                    gemini 或 openai
MANTISFETCH_LLM_VENDOR                      openai, zhipu, kimi, aliyun, volcengine
GEMINI_API_KEY / GOOGLE_API_KEY           Gemini 凭证
MANTISFETCH_LLM_API_KEY                     OpenAI 兼容接口凭证
MANTISFETCH_LLM_BASE_URL                    OpenAI 兼容接口 base URL
MANTISFETCH_LLM_MODEL                       文本模型覆盖
MANTISFETCH_OCR_MODEL                       OCR 视觉模型覆盖
MANTISFETCH_OCR_IMAGE_INPUT_MODE            data_url, plain_base64, remote_url_only
MANTISFETCH_LLM_EXTRA_BODY_JSON             额外合并到文本请求的 JSON
MANTISFETCH_OCR_EXTRA_BODY_JSON             额外合并到 OCR 请求的 JSON

MANTISFETCH_MAX_CONCURRENT_PARSE            默认 2
MANTISFETCH_LOCAL_OCR_CONCURRENCY           默认 1
MANTISFETCH_PREWARM_LOCAL_OCR               默认 true
MANTISFETCH_DEFERRED_SUMMARY_MAX_CONCURRENT 默认 1
MANTISFETCH_SUMMARY_BATCH_CONCURRENCY       默认 1
MANTISFETCH_SUMMARY_REQUEST_MIN_INTERVAL_SEC 默认 2.0
MANTISFETCH_SUMMARY_SECTION_DETAIL_LIMIT    默认 10
```

处理大型扫描 PDF 时，保持 OCR 并发保守，并优先用 `generate_summary=false` 做初次入库。

### 6. 网页数据采集

一键采集，适合大多数公开页面：

```http
POST /web/capture
Content-Type: application/json

{
  "url": "https://example.com",
  "content_type": "Knowledge",
  "tags": ["project", "source"]
}
```

响应会包含 `WEB-...` 文档 ID、`content_type`、`storage_path` 和 digest。新的网页采集会写入所选分类目录。

多步骤浏览：

```http
POST /web/session/new

POST /web/session/goto
{"session_id": "...", "url": "https://example.com"}

POST /web/session/distill
{"session_id": "...", "include_actions": true, "include_diff": true}

POST /web/session/read_sections
{"session_id": "...", "section_ids": ["sid1", "sid2"]}

POST /web/session/act
{"session_id": "...", "aid": "<aid>", "action": "click"}

POST /web/session/close
{"session_id": "..."}
```

可用动作包括 `click`、`type`、`select`、`scroll`。`type` 时还需要传 `text`。

WebMCP：

```http
POST /web/session/webmcp_discover
{"session_id": "...", "force_refresh": false}

POST /web/session/webmcp_invoke
{"session_id": "...", "tool_name": "...", "params": {...}}
```

### 7. 文档解析

上传文档：

```bash
curl -sS -X POST http://127.0.0.1:9898/doc/parse \
  -F "file=@document.pdf" \
  -F "content_type=Contract" \
  -F "generate_summary=false" \
  -F 'tags=["contract","review"]' \
  -F 'metadata={"customer":"ACME","status":"draft"}'
```

支持格式：

```text
pdf, doc, docx, ppt, pptx, xls, xlsx, csv, html, htm, txt, text, json, jsonl, xml
```

可选 parse 字段：

```text
generate_summary   true 或 false
content_type       General、Contract、Bid 或 Knowledge；默认 General
tags               JSON 数组
metadata           JSON 对象；浅层标量值会被索引，便于过滤
doc_id             可选显式 ID；允许字母、数字和中间连字符
extract_tables     true 或 false
```

存储目录结构：

```text
${MANTISFETCH_DOCS_DIR}/General/<doc_id>
${MANTISFETCH_DOCS_DIR}/Contract/<doc_id>
${MANTISFETCH_DOCS_DIR}/Bid/<doc_id>
${MANTISFETCH_DOCS_DIR}/Knowledge/<doc_id>
```

旧版平铺在 `${MANTISFETCH_DOCS_DIR}/<doc_id>` 下的文档仍可读取。

### 8. 文档库

使用三层加载模型：

```http
GET /doc/library/search?q=<关键词>&tags=<标签>&file_type=<pdf|docx|web>&content_type=Contract
GET /doc/library/search_text?q=<关键词>&scope=section&doc_id=<doc_id>&content_type=Contract
GET /doc/library/{doc_id}/digest
GET /doc/library/{doc_id}/brief
GET /doc/library/{doc_id}/sections
GET /doc/library/{doc_id}/section/{sid}
GET /doc/library/{doc_id}/manifest
```

当用户只想查看某一类内容时，在 search 接口传 `content_type`。直接读取文档仍使用 `doc_id`，服务会自动解析分类目录和旧平铺目录。

除非用户明确要求完整正文，否则避免调用 `/full`：

```http
GET /doc/library/{doc_id}/full
```

表格和图片：

```http
GET /doc/library/{doc_id}/table/{table_id}
GET /doc/library/{doc_id}/table/{table_id}/json
GET /doc/library/{doc_id}/images
GET /doc/library/{doc_id}/image/{image_id}
```

Layout 和 sidecar 发现：

```http
GET /doc/library/{doc_id}/sidecars
GET /doc/library/{doc_id}/layout/pages
GET /doc/library/{doc_id}/layout/page/{page}
```

OCR 证据、扫描表格 JSON、layout blocks 和页级几何信息应通过 sidecar 接口按需读取。默认 digest/brief/section API 不会内联大体积几何数据。

### 9. v0.4.0 OCR 与 Layout 说明

对于扫描 PDF，MantisFetch 可能生成：

```text
ocr_blocks.json                 OCR 文本块，包含 bbox、置信度、页尺寸、来源和稳定 ID
tables/table-xx.json            重建后的扫描表格结构
derived/region_ocr/...          定向区域重 OCR 产物
derived/debug/...               可选可视化调试标注图
```

Agent 规则：

1. 先用 `/sidecars` 发现可用 sidecar。
2. 先用 `/layout/pages` 查看页级摘要，再读取具体页几何。
3. 只对需要作为证据的页面调用 `/layout/page/{page}`。
4. 仅当 Markdown 表格不足以回答问题时，再读取 table JSON。
5. sidecar 是证据产物，不要假设其中包含业务语义。

### 10. 验证

基础服务验证：

```bash
curl -sS http://127.0.0.1:9898/health
curl -sS http://127.0.0.1:9898/doc/health
curl -sS http://127.0.0.1:9898/web/health
```

仓库内已有 Docker 服务运行时，可执行端到端测试：

```bash
docker compose exec mantisfetch pytest tests/e2e/ -v -m "live and not live_llm"
```

该命令覆盖文档解析和网页采集，不要求真实 LLM 摘要测试。

### 11. 节省 Token 规则

1. 优先按 `digest -> brief -> section` 加载。
2. 除非必要，不调用 `/full`。
3. `distill` 后检查 `meta.diff.changed_sids`，只读取变化 section。
4. 表格先看 metadata/stats，再决定是否加载完整表格正文。
5. 仅需抽取时用 `generate_summary=false`。
6. 加载正文前先用 `tags` 和 `metadata.*` 过滤。
7. 加载 OCR 几何前先使用 sidecar discovery。
