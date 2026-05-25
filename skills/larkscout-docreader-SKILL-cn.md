---
name: larkscout-docreader
description: 长文档解析与阅读 HTTP API。适用于需要读取、分析或总结 PDF、Office、HTML、CSV、文本/JSON/XML 等文件的场景。支持文件上传解析、三级摘要（digest/brief/full）、按需加载 section、表格提取、metadata 持久化、source 文件引用，以及通过 HTTP API 访问文档库搜索。输出 doc-index v2 格式，并与 larkscout-browser 的网页抓取结果共享统一索引。它是 LarkScout 开源数据采集平台中的文档解析引擎。
triggers:
  - "读取文档"
  - "解析文档"
  - "分析这个 PDF"
  - "这个 Word 文件"
  - "文档摘要"
  - "提取内容"
  - "跨文档"
  - "整合文档"
  - "上传文档"
  - "文档库搜索"
  - ".pdf"
  - ".doc"
  - ".docx"
  - ".ppt"
  - ".pptx"
  - ".xls"
  - ".xlsx"
  - ".csv"
  - ".html"
  - ".txt"
  - ".json"
  - ".jsonl"
  - ".xml"
---

# SKILL: LarkScout DocReader（文档解析 HTTP API）

## 1. 用途

适用于：文档分析、跨文档整合、研究报告提取、财务数据采集、文档审阅、会议纪要处理。

---

## 2. 服务依赖

- Base URL: `http://127.0.0.1:9898/doc/`

---

## 3. Agent 执行策略（低 Token 规则，必须遵守）

### 3.1 三级加载规则

| Tier | Endpoint                                     | Token Cost | 适用时机 |
| ---- | -------------------------------------------- | ---------- | -------- |
| L1   | `GET /doc/library/{doc_id}/digest`           | ~200       | 文档刚被提到时，快速了解主题 |
| L2   | `GET /doc/library/{doc_id}/brief`            | ~1500      | 需要理解各章节关键点时 |
| L3   | `GET /doc/library/{doc_id}/section/{sid}`    | On-demand  | 需要某个具体 section 的原文时 |
| L4   | `GET /doc/library/{doc_id}/full`             | Full       | **几乎不要用**，只有极端情况才用 |

**不要把全文直接注入上下文。应通过 section/{sid} 按需加载具体 section。**

### 3.2 黄金工作流

```
POST /doc/parse (upload file)
↓
返回 doc_id + digest（摘要已包含在响应中，无需额外请求）
↓
需要更多细节 → GET /doc/library/{doc_id}/brief
↓
需要某个 section 的原文 → GET /doc/library/{doc_id}/sections（获取 section 列表）
                               → GET /doc/library/{doc_id}/section/{sid}
↓
需要表格数据 → GET /doc/library/{doc_id}/table/{table_id}
```

### 3.3 跨文档整合

在整合多份文档时：

1. 先读取所有相关文档的 digest（每份约 200 tokens）
2. 找出需要横向比较的维度
3. 按需加载各文档的相关 section
4. 综合分析并产出整合报告

```
上下文成本：
  3 × digest              = ~600 tokens
  + 4 个按需 section      = ~4000 tokens
  ────────────────────────────────
  总计                     ≈ 4600 tokens

相比直接注入 3 份全文：     ≈ 180,000 tokens
节省：97%
```

### 3.4 文档库搜索

```
GET /doc/library/search?q=revenue&tags=financial&file_type=pdf&metadata.customer=ACME
↓
返回匹配的 doc_id 列表 + digest 预览
↓
再按需加载具体文档的 brief 或 section
```

**禁止行为：**

- 直接请求 full（浪费 tokens）
- 不看 digest 就先读 brief（先判断是否有必要）
- 不用 search 而遍历所有文档（应优先使用 search）

---

## 4. API 说明

> 所有请求使用 `Content-Type: application/json`（查询类接口）或 `multipart/form-data`（上传类接口）

### 4.1 健康检查

- `GET /doc/health`

响应示例：

```json
{
  "ok": true,
  "version": "3.0.0",
  "docs_dir": "~/.larkscout/docs",
  "supported_formats": ["pdf", "doc", "docx", "ppt", "pptx", "xls", "xlsx", "csv", "html", "htm", "txt", "text", "json", "jsonl", "xml"]
}
```

说明：
- `docs_dir` 会显示脱敏后的路径（家目录以 `~` 表示），这是有意为之的安全设计
- `supported_formats` 包括 PDF、Office、CSV、HTML、文本、JSON、JSONL、XML；`.doc` 和 `.ppt` 会先由服务端转换为 `.docx` / `.pptx`
- `.doc` / `.ppt` 支持依赖服务端已安装 LibreOffice/soffice；Docker 镜像默认包含转换组件
- 文档解析由 [MarkItDown](https://github.com/microsoft/markitdown)（Microsoft）驱动

### 4.2 上传并解析文档（核心）

- `POST /doc/parse`
- Content-Type: `multipart/form-data`

请求参数：

| Parameter             | Type   | Default    | 说明 |
| --------------------- | ------ | ---------- | ---- |
| `file`                | File   | (required) | 上传文件（.pdf, .doc/.docx, .ppt/.pptx, .xls/.xlsx, .csv, .html/.htm, .txt/.text, .json/.jsonl/.xml） |
| `doc_id`              | string | Auto-increment | 手动指定 DOC-ID |
| `generate_summary`    | bool   | `true`     | 是否生成摘要（false = 仅提取文本） |
| `summary_mode`        | string | null       | 摘要模式：`sync` / `defer` / `off`。长文档和业务 Skill 推荐 `defer` |
| `document_profile`    | string | null       | 可选文档 profile 名称；仅在调用方明确知道可用 profile 时传入 |
| `id_strategy`         | string | null       | DOC-ID 策略：`counter` / `source_filename` |
| `skip_ocr_pages`      | string | null       | 已确认空白或无需 OCR 的页码，例如 `"30,104,106-108"` |
| `force_ocr`           | bool   | `false`    | 强制使用 LLM OCR 处理全部页面；成本较高，只在明确需要视觉模型重识别整份文档时使用 |
| `ocr_pages`           | string | null       | 指定页范围升级为 LLM OCR，例如 `"10-30"`；未指定页仍按服务端自动策略处理 |
| `extract_tables`      | bool   | `true`     | 是否提取表格 |
| `extract_images`      | bool   | `false`    | 是否抽取 Word 内嵌图片并输出 `images.json` / `images/`；可先只做轻量图片清单 |
| `ocr_images`          | bool   | `false`    | 是否对已抽取的 Word 内嵌图片做 OCR |
| `image_ocr_backend`   | string | `auto`     | 图片 OCR 后端：`auto` / `local` / `llm`；大标书推荐显式使用 `local`，避免默认触发 LLM fallback |
| `max_images`          | int    | `200`      | 单文档最多处理的内嵌图片数量 |
| `max_ocr_images`      | int    | `80`       | 开启 `ocr_images=true` 时允许 OCR 的最大 Word 内嵌图片数量；实际 OCR 数量超过该值会返回 422 |
| `max_tables_per_page` | int    | `3`        | 每页最多提取的表格数量 |
| `concurrency`         | int    | `3`        | OCR/摘要并发度 |
| `tags`                | string | null       | 标签，支持 JSON 数组（`'["Q3","financial"]'`）或逗号分隔（`"Q3,financial"`） |
| `metadata`            | string | null       | 自定义 metadata（JSON object）。会写入 manifest；浅层标量字段会进入索引。 |

调用示例：

```bash
curl -X POST http://localhost:9898/doc/parse \
  -F "file=@report.pdf" \
  -F "generate_summary=true" \
  -F "extract_tables=true" \
  -F 'tags=["Q3","financial"]'
```

调用方不需要依赖 Python SDK；可以直接用 `curl` 调 LarkScout 入库。LarkScout 只负责底层解析、索引和来源保留；具体业务场景、业务字段、命名规则和后续操作由上层调用方自行定义。

Word 内嵌图片的推荐入库方式是先抽轻量图片清单，不默认 OCR 全部图片。`images.json` 会包含图片文件、锚点、尺寸、hash、上下文关键词和候选 hints，供下游工具按业务要求筛选候选图片：

```bash
curl -X POST http://localhost:9898/doc/parse \
  -F "file=@/path/to/document.docx" \
  -F "summary_mode=defer" \
  -F "extract_tables=true" \
  -F "extract_images=true" \
  -F "ocr_images=false" \
  -F "max_images=1000" \
  -F 'metadata={"display_name":"document.docx","source_system":"agent_upload"}'
```

如确实需要在入库时 OCR 少量图片，应显式限制数量并优先用本地 OCR：

```bash
curl -X POST http://localhost:9898/doc/parse \
  -F "file=@/path/to/document.docx" \
  -F "summary_mode=defer" \
  -F "extract_images=true" \
  -F "ocr_images=true" \
  -F "image_ocr_backend=local" \
  -F "max_images=50" \
  -F "max_ocr_images=50"
```

如果本次请求实际会 OCR 的图片数量超过 `max_ocr_images`，服务会拒绝执行图片 OCR。大标书调用方应使用 `ocr_images=false` 先入库正文和图片清单，再由下游工具按招标要求筛选候选图片后做定向 OCR/视觉审查。

该能力只输出图片来源、附近标题、section 锚点、图片文件、inventory 元数据和可选 OCR 文本；图片代表什么材料、是否满足业务要求，由上层工具自行判断。

带 metadata 的通用入库示例：

```bash
curl -X POST http://localhost:9898/doc/parse \
  -F "file=@/path/to/document.pdf" \
  -F "summary_mode=defer" \
  -F "extract_tables=true" \
  -F 'metadata={"display_name":"document.pdf","source_system":"manual_upload"}'
```

如果已确认部分页面为空白或不需要 OCR，可追加：

```bash
-F "skip_ocr_pages=30,104,106,108,110,112"
```

响应示例：

```json
{
  "doc_id": "DOC-010",
  "filename": "report.pdf",
  "file_type": "pdf",
  "total_pages": 45,
  "section_count": 12,
  "table_count": 8,
  "ocr_page_count": 3,
  "digest": "Q3 revenue grew 15%, net profit up 23% YoY...",
  "manifest_path": "docs/DOC-010/manifest.json",
  "processing_time_sec": 23.5,
  "source_ref": "source/report.pdf"
}
```

**关键说明：**

- 返回里的 `digest` 已经包含摘要前 300 个字符，通常无需再额外请求 `/doc/library/{doc_id}/digest`
- `generate_summary=false` 只提取文本和表格，不调用 LLM，速度更快但没有摘要
- `metadata` 必须是 JSON object；嵌套对象会保留在 manifest 中，而浅层标量字段可用于 `/doc/library/search` 过滤
- `source_ref` 指向文档目录内保存的上传原件，前提是 `LARKSCOUT_STORE_SOURCE_FILES=true`
- 大文件（100+ 页 PDF）解析可能需要 30–60 秒，Agent 应设置更长的超时

### 4.3 搜索文档库

- `GET /doc/library/search`

| Parameter   | 说明 |
| ----------- | ---- |
| `q`         | 关键词（搜索 filename、digest、tags、metadata 摘要） |
| `tags`      | 标签过滤，逗号分隔 |
| `file_type` | 文件类型过滤（`pdf` / `docx` / `web`） |
| `metadata.*`| 等值 metadata 过滤，例如 `metadata.customer=ACME` |
| `limit`     | 返回结果上限（默认 20） |

响应示例：

```json
{
  "results": [
    {
      "doc_id": "DOC-010",
      "filename": "Q3-report.pdf",
      "file_type": "pdf",
      "digest": "Q3 revenue grew 15%...",
      "tags": ["Q3", "financial"],
      "source": "upload",
      "metadata": {"customer": "ACME", "category": "report"},
      "source_ref": "source/Q3-report.pdf",
      "source_filename": "Q3-report.pdf",
      "source_available": true,
      "score": 3.5
    }
  ],
  "total": 1
}
```

**该搜索同时覆盖 DocReader 上传的文档和 LarkScout Browser 抓取的网页。** `source` 字段用于区分来源：`"upload"` 表示文件上传，`"web_capture"` 表示网页抓取。

### 4.4 全文 / Section 搜索

- `GET /doc/library/search_text`

| Parameter   | 说明 |
| ----------- | ---- |
| `q`         | 必填查询字符串 |
| `tags`      | 标签过滤，逗号分隔 |
| `file_type` | 文件类型过滤 |
| `doc_id`    | 限制到单个文档 |
| `scope`     | `all` / `full` / `section`（默认 `all`） |
| `limit`     | 返回结果上限（默认 20） |
| `metadata.*`| 等值 metadata 过滤 |

响应示例：

```json
{
  "results": [
    {
      "doc_id": "DOC-010",
      "filename": "Q3-report.pdf",
      "file_type": "pdf",
      "digest": "Q3 revenue grew 15%...",
      "tags": ["Q3", "financial"],
      "source": "upload",
      "metadata": {"customer": "ACME"},
      "sid": "a3f8e1b902cd",
      "section_title": "Payment Terms",
      "page_range": "p.12-13",
      "page_start": 12,
      "page_end": 13,
      "snippet": "...payment terms require invoice submission within 30 days...",
      "score": 1.5
    }
  ],
  "total": 1
}
```

当你需要在读取 section 全文之前先获得命中片段和页码提示时，应使用这个接口。

### 4.5 获取文档 Digest（最低 Token 成本）

- `GET /doc/library/{doc_id}/digest`

响应：`{"doc_id": "DOC-010", "content": "# DOC-010: report.pdf\n\nQ3 revenue grew 15%..."}`

### 4.6 获取文档 Brief（中等 Token 成本）

- `GET /doc/library/{doc_id}/brief`

响应：`{"doc_id": "DOC-010", "content": "# DOC-010: report.pdf · Brief\n\n..."}`

### 4.7 获取全文（高 Token 成本，谨慎使用）

- `GET /doc/library/{doc_id}/full`

响应：`{"doc_id": "DOC-010", "content": "# report.pdf\n\n..."}`

### 4.8 列出文档所有 Sections

- `GET /doc/library/{doc_id}/sections`

响应示例：

```json
{
  "doc_id": "DOC-010",
  "sections": [
    {
      "sid": "a3f8e1b902cd",
      "index": 1,
      "title": "Executive Summary",
      "page_range": "p.1-3",
      "page_start": 1,
      "page_end": 3,
      "char_count": 2500,
      "summary_preview": "Q3 revenue grew 15%, net profit up 23% YoY..."
    },
    {
      "sid": "b7c2d4e5f612",
      "index": 2,
      "title": "Financial Analysis",
      "page_range": "p.4-15",
      "page_start": 4,
      "page_end": 15,
      "char_count": 12000,
      "summary_preview": "Revenue mix shifted, service revenue share rose to 42%..."
    }
  ]
}
```

**Agent 应先调用这个接口拿到 section 列表，再按 sid 读取具体内容。**

### 4.9 读取单个 Section

- `GET /doc/library/{doc_id}/section/{sid}`

响应：`{"doc_id": "DOC-010", "sid": "a3f8e1b902cd", "content": "# Executive Summary\n\n..."}`

### 4.10 读取单个表格

- `GET /doc/library/{doc_id}/table/{table_id}`

table_id 格式：`"01"` 或 `"table-01"`。

响应：`{"doc_id": "DOC-010", "table_id": "01", "content": "# Table 1 (Page 5)\n\n| ... |"}`

### 4.11 读取 Word 内嵌图片结果

- `GET /doc/library/{doc_id}/images`
- `GET /doc/library/{doc_id}/image/{image_id}`

只有调用 `/doc/parse` 时设置 `extract_images=true` 才会产生结果。`image_id` 格式为 `"001"` 或 `"IMG-001"`。

### 4.12 获取 Manifest

- `GET /doc/library/{doc_id}/manifest`

返回完整的 `manifest.json` 内容，包括文档结构、section 列表、图片/表格路径信息、metadata、source 文件引用和 provenance。

---

## 5. 文档库目录结构

所有解析结果都保存在 `DOCS_DIR` 下：

```text
docs/
  ├─ doc-index.json              ← 全局索引（v2 格式，与 LarkScout Browser 共享）
  │
  ├─ DOC-001/                    ← PDF 解析结果
  │   ├─ .meta.json
  │   ├─ manifest.json           ← 包含 provenance 跟踪信息
  │   ├─ source/                 ← 原始上传文件（启用时保存）
  │   │   └─ original.pdf
  │   ├─ digest.md               ← ~200 tokens
  │   ├─ brief.md                ← ~1500 tokens
  │   ├─ full.md                 ← 全文
  │   ├─ sections/               ← 按章节切分的文件
  │   │   ├─ 01-{sid}-{title}.md
  │   │   └─ 02-{sid}-{title}.md
  │   ├─ tables/                 ← 提取出的表格
  │   │   ├─ table-01.md
  │   │   └─ table-02.md
  │   ├─ images.json             ← Word 内嵌图片的锚点、文件和 OCR 元数据
  │   └─ images/                 ← Word 内嵌图片原图、渲染图和 OCR 文本
  │       ├─ IMG-001.original.png
  │       ├─ IMG-001.png
  │       └─ IMG-001.ocr.txt
  │
  └─ WEB-001/                    ← 网页抓取结果（由 LarkScout Browser 写入，共享索引）
      ├─ manifest.json
      ├─ digest.md
      ├─ sections/
      └─ tables/
```

**doc-index.json v2 关键字段：**

| Field          | 说明 |
| -------------- | ---- |
| `id`           | DOC-001 / WEB-001 |
| `source`       | `"upload"` 或 `"web_capture"` |
| `tags`         | 标签数组 |
| `metadata`     | 从上传 metadata 中提取的可索引标量字段 |
| `source_ref`   | 指向 `source/` 下保存上传原件的相对路径 |
| `content_hash` | 内容 SHA256，用于去重和变更检测 |
| `digest`       | 摘要前 200 个字符 |

---

## 6. Agent 调用模板

### 6.1 单文档分析

```
POST /doc/parse (upload file)
↓
返回 doc_id + digest → 判断文档是否相关
↓
GET /doc/library/{doc_id}/brief → 理解各 section 的关键点
↓
GET /doc/library/{doc_id}/section/{target_sid} → 深读关键 section
```

### 6.2 跨文档对比

```
POST /doc/parse (Document A) → doc_id_a
POST /doc/parse (Document B) → doc_id_b
↓
GET /doc/library/{doc_id_a}/digest + GET /doc/library/{doc_id_b}/digest
↓
比较 digest，找出需要横向对比的维度
↓
GET /doc/library/{doc_id_a}/section/{relevant_sid}
GET /doc/library/{doc_id_b}/section/{relevant_sid}
↓
综合分析并输出对比报告
```

### 6.3 文档库搜索

```
GET /doc/library/search?q=Q3+revenue&tags=financial&metadata.customer=ACME
↓
返回匹配文档列表 + digest 预览
↓
选择目标文档 → GET /doc/library/{doc_id}/brief
↓
按需深入 → GET /doc/library/{doc_id}/section/{sid}
```

在读取 section 之前，如果需要页级提示：

```
GET /doc/library/search_text?q=payment+terms&doc_id=DOC-010&scope=section
↓
返回 snippet + sid + page_start/page_end
↓
GET /doc/library/{doc_id}/section/{sid}
```

### 6.4 仅提取文本（不生成摘要）

```
POST /doc/parse (generate_summary=false)
↓
返回 doc_id → 文本已提取，可直接读取 sections
↓
GET /doc/library/{doc_id}/sections → section 列表
GET /doc/library/{doc_id}/section/{sid} → 读取内容
```

适用于：Agent 自己做分析、不需要 LLM 摘要，或者需要节省 LLM API 调用的场景。

---

## 7. 常见错误与处理方式

| Error                                              | Cause                          | Solution |
| -------------------------------------------------- | ------------------------------ | -------- |
| `422 unsupported format`                           | 上传了不支持的文件格式         | 通过 `/doc/health` 的 `supported_formats` 检查当前支持格式 |
| `429 too many concurrent requests`                 | 触发限流                       | 等待后重试，服务端限制了并发解析数 |
| `404 document not found`                           | doc_id 无效或文档尚未入库      | 先用 search 确认 doc_id |
| `404 section not found`                            | sid 无效                       | 先调用 `/doc/library/{doc_id}/sections` 获取有效 sid 列表 |
| `500 parse failed`                                 | PDF 损坏或加密                 | 提示用户检查文件 |
| 与缺少 LLM 凭证相关的 `500 RuntimeError`           | LLM provider 凭证未配置        | 检查当前启用的 LLM provider 配置并重启服务 |
| Parsing takes too long                             | 文件较大且包含 OCR             | 先用 `generate_summary=false` 做快速提取，再单独生成摘要 |
| Table is empty                                     | PDF 中的表格是图片或版式复杂   | 先确认正文 OCR 是否已入库；如关键表格缺失，再只对相关页使用 `ocr_pages` 或在明确接受成本时使用 `force_ocr=true` |
| OCR 结果出现 `No image provided` 一类内容          | 视觉模型或图片输入模式不匹配   | 先检查当前 OCR 模型、vendor profile 和 OCR 图片输入模式，再决定是否重试 |
| XLSX/CSV truncated warning in metadata             | 文件超过 `MAX_PARSE_ROWS`      | 正常现象，为安全起见大表会被截断；可检查 `metadata.truncated` |

---

## 8. 推荐默认参数

**Parsing：**

- `generate_summary=true`（需要摘要时）
- `extract_tables=true`
- `max_tables_per_page=3`
- `concurrency=3`（可根据上游 LLM/OCR 配额调整）

**OCR：**

- 普通文档和扫描文档：默认不要传 `force_ocr`；服务会自动检测扫描页，并优先使用本地 PaddleOCR
- 已确认空白页或无需 OCR 页：传 `skip_ocr_pages`，避免浪费解析时间
- 只有在少数页面需要更高质量视觉识别时，传 `ocr_pages="10-30"`，将指定页升级为 LLM OCR
- 只有在明确接受成本和耗时、且整份文档都需要视觉模型重识别时，才传 `force_ocr=true`
- 本地 PaddleOCR 在服务端隔离 worker 进程中运行；worker 崩溃不会拖垮主服务，也不会默认自动切换到 LLM OCR
- 如果 OCR 以某个 provider 特有的方式失败，先检查服务当前使用的 OCR 模型 / vendor 配置，不要先归因到文档本身

---

## 9. 安全与合规

- 上传文件的临时副本在解析后会自动清理
- 文档库通过 `DOCS_DIR` 目录物理隔离
- provenance 跟踪：每份文档的 manifest 都会包含 provenance 信息（`created_at`、`content_hash`，以及可用时的 `source_ref`）
- 如果 `LARKSCOUT_STORE_SOURCE_FILES=true`（默认），原始上传文件会保存在各文档目录下的 `source/` 子目录中，供后续引用
