"""
MantisFetch i18n — lightweight internationalization, zero dependencies.

Usage:
    from i18n import t, prompt, tmpl, get_locale, set_locale

    # Messages
    raise HTTPException(422, t("unsupported_format", fmt=".pptx"))

    # Prompts (for LLM)
    p = prompt("section_summary")

    # Templates (for output files)
    title = tmpl("digest_title", doc_id="DOC-001", filename="report.pdf")

Locale is controlled by LANG env var (default: "en").
Only "en" and "zh" are supported. Unrecognized values fall back to "en".
"""

import os

_locale: str = "en"

# ═══════════════════════════════════════════
# LLM Prompts
# ═══════════════════════════════════════════

PROMPTS = {
    "zh": {
        "ocr": (
            "请提取这张图片中的所有文字内容。要求：\n"
            "1. 保持原文的段落结构\n"
            "2. 表格用 Markdown 表格格式输出\n"
            "3. 忽略页眉页脚和页码\n"
            "4. 如果有图表，用 [图表: 简要描述] 标记\n"
            "5. 只输出提取的文字，不要加任何额外说明"
        ),
        "section_summary": (
            "请为以下文档章节生成简洁摘要。要求：\n"
            "1. 用中文输出\n"
            "2. 保留关键数据、结论和决策要点\n"
            "3. 控制在 2-4 句话\n"
            "4. 如果包含表格数据，提炼关键数字\n"
            "5. 关键结论后标注页码引用，如 (p.12-13)\n"
            "6. 只输出摘要内容"
        ),
        "batch_summary": (
            "请为以下 {n} 个章节分别生成简洁摘要。要求：\n"
            "1. 用中文输出\n"
            "2. 保留关键数据、结论和决策要点\n"
            "3. 每个章节 2-4 句话\n"
            "4. 关键结论后标注页码引用，如 (p.12)\n"
            '5. **必须**严格输出 JSON 数组，格式如下（不要输出任何其他文字）：\n'
            "[\n"
            '  {{"index": 1, "summary": "章节1的摘要..."}},\n'
            '  {{"index": 2, "summary": "章节2的摘要..."}}\n'
            "]"
        ),
        "digest": (
            "请为以下文档生成一段总体摘要。要求：\n"
            "1. 用中文输出\n"
            "2. 控制在 150 字以内（约 200 tokens）\n"
            "3. 包含：文档主题、核心结论、关键数据（如有）\n"
            "4. 只输出摘要内容，不要加标题或前缀"
        ),
        "brief": (
            "请为以下文档生成一份结构化简报。要求：\n"
            "1. 用中文输出\n"
            "2. 控制在 800 字以内（约 1500 tokens）\n"
            "3. 包含：文档背景、各章节核心观点、关键数据、结论\n"
            "4. 用 Markdown 格式，包含二级标题\n"
            "5. 只输出简报内容"
        ),
    },
    "en": {
        "ocr": (
            "Extract all text content from this image. Requirements:\n"
            "1. Preserve original paragraph structure\n"
            "2. Output tables in Markdown table format\n"
            "3. Ignore headers, footers, and page numbers\n"
            "4. Mark charts/figures as [Figure: brief description]\n"
            "5. Output only the extracted text, no additional commentary"
        ),
        "section_summary": (
            "Generate a concise summary for the following document section. Requirements:\n"
            "1. Output in English\n"
            "2. Preserve key data, conclusions, and decision points\n"
            "3. Keep to 2-4 sentences\n"
            "4. If table data is included, extract key figures\n"
            "5. Add page references after key conclusions, e.g. (p.12-13)\n"
            "6. Output only the summary"
        ),
        "batch_summary": (
            "Generate concise summaries for each of the following {n} sections. Requirements:\n"
            "1. Output in English\n"
            "2. Preserve key data, conclusions, and decision points\n"
            "3. 2-4 sentences per section\n"
            "4. Add page references after key conclusions, e.g. (p.12)\n"
            "5. You MUST output a strict JSON array in the following format (no other text):\n"
            "[\n"
            '  {{"index": 1, "summary": "Summary for section 1..."}},\n'
            '  {{"index": 2, "summary": "Summary for section 2..."}}\n'
            "]"
        ),
        "digest": (
            "Generate an overall summary for the following document. Requirements:\n"
            "1. Output in English\n"
            "2. Keep under 150 words (~200 tokens)\n"
            "3. Include: document topic, core conclusions, key data (if any)\n"
            "4. Output only the summary, no title or prefix"
        ),
        "brief": (
            "Generate a structured briefing for the following document. Requirements:\n"
            "1. Output in English\n"
            "2. Keep under 800 words (~1500 tokens)\n"
            "3. Include: document background, key points per chapter, key data, conclusions\n"
            "4. Use Markdown format with level-2 headings\n"
            "5. Output only the briefing"
        ),
    },
}

# ═══════════════════════════════════════════
# User-facing messages (API errors, fallbacks)
# ═══════════════════════════════════════════

MESSAGES = {
    "zh": {
        "unsupported_format": "不支持的格式: {fmt}",
        "file_save_failed": "文件保存失败: {err}",
        "parse_failed": "解析失败: {err}",
        "write_failed": "写入失败: {err}",
        "doc_not_found": "文档不存在: {doc_id}",
        "digest_not_found": "digest 不存在: {doc_id}",
        "brief_not_found": "brief 不存在: {doc_id}",
        "full_not_found": "full 不存在: {doc_id}",
        "section_not_found": "章节不存在: {sid}",
        "tables_dir_not_found": "表格目录不存在: {doc_id}",
        "table_not_found": "表格不存在: {table_id}",
        "file_open_failed": "无法打开文件: {path}",
        "office_converter_missing": "缺少 LibreOffice/soffice，无法转换老式 Office 文件",
        "office_conversion_failed": "Office 文件转换失败: {src} -> .{dst}: {err}",
        "gemini_not_installed": "请安装 google-genai: pip install google-genai",
        "gemini_key_missing": "请设置 GEMINI_API_KEY 或 GOOGLE_API_KEY 环境变量",
        "ocr_failed": "[OCR 失败: 第 {page} 页]",
        "summary_failed": "[摘要生成失败]",
        "summary_pending": "(摘要待生成)",
        "summary_missing": "(摘要缺失)",
        # Browser service
        "table_prefix": "[表格]",
        "table_truncated": "[... 共 {total} 行，已显示前 {shown} 行 ...]",
    },
    "en": {
        "unsupported_format": "Unsupported format: {fmt}",
        "file_save_failed": "File save failed: {err}",
        "parse_failed": "Parse failed: {err}",
        "write_failed": "Write failed: {err}",
        "doc_not_found": "Document not found: {doc_id}",
        "digest_not_found": "Digest not found: {doc_id}",
        "brief_not_found": "Brief not found: {doc_id}",
        "full_not_found": "Full text not found: {doc_id}",
        "section_not_found": "Section not found: {sid}",
        "tables_dir_not_found": "Tables directory not found: {doc_id}",
        "table_not_found": "Table not found: {table_id}",
        "file_open_failed": "Cannot open file: {path}",
        "office_converter_missing": "LibreOffice/soffice is required to convert legacy Office files",
        "office_conversion_failed": "Office conversion failed: {src} -> .{dst}: {err}",
        "gemini_not_installed": "Please install google-genai: pip install google-genai",
        "gemini_key_missing": "Please set GEMINI_API_KEY or GOOGLE_API_KEY environment variable",
        "ocr_failed": "[OCR failed: page {page}]",
        "summary_failed": "[Summary generation failed]",
        "summary_pending": "(Summary pending)",
        "summary_missing": "(Summary missing)",
        # Browser service
        "table_prefix": "[Table]",
        "table_truncated": "[... {total} rows total, showing first {shown} ...]",
    },
}

# ═══════════════════════════════════════════
# Output file templates
# ═══════════════════════════════════════════

TEMPLATES = {
    "zh": {
        "digest_title": "# {doc_id}: {filename}",
        "brief_header": (
            "# {doc_id}: {filename} · 简报\n\n"
            "**来源**: {filename} | **页数**: {pages} | "
            "**章节**: {sections} | **OCR页**: {ocr_pages}\n\n---\n\n"
        ),
        "section_header": (
            "# {title}\n\n"
            "**章节 {index}** | **SID**: {sid} | **页码**: {page_range}\n\n"
        ),
        "section_summary_line": "**摘要**: {summary}\n\n---\n\n",
        "default_section_title": "前言",
        "full_document_title": "全文",
    },
    "en": {
        "digest_title": "# {doc_id}: {filename}",
        "brief_header": (
            "# {doc_id}: {filename} · Briefing\n\n"
            "**Source**: {filename} | **Pages**: {pages} | "
            "**Sections**: {sections} | **OCR pages**: {ocr_pages}\n\n---\n\n"
        ),
        "section_header": (
            "# {title}\n\n"
            "**Section {index}** | **SID**: {sid} | **Pages**: {page_range}\n\n"
        ),
        "section_summary_line": "**Summary**: {summary}\n\n---\n\n",
        "default_section_title": "Preface",
        "full_document_title": "Full document",
    },
}


# ═══════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════

def get_locale() -> str:
    return _locale


def set_locale(locale: str) -> None:
    global _locale
    _locale = locale if locale in ("zh", "en") else "en"


def init_locale(env_var: str = "LANG") -> str:
    """Initialize locale from environment. Call once at startup."""
    raw = os.environ.get(env_var, "en")
    lang = raw[:2].lower()
    set_locale(lang)
    return _locale


def _normalize_locale(locale: str | None) -> str:
    if not locale:
        return _locale
    value = locale.lower()
    if value.startswith("zh"):
        return "zh"
    if value.startswith("en"):
        return "en"
    return _locale


def t(key: str, **kwargs) -> str:
    """Translate a user-facing message."""
    msg = MESSAGES.get(_locale, MESSAGES["en"]).get(key)
    if msg is None:
        msg = MESSAGES["en"].get(key, key)
    return msg.format(**kwargs) if kwargs else msg


def prompt_for_locale(locale: str | None, key: str, **kwargs) -> str:
    """Get an LLM prompt for an explicit locale."""
    resolved = _normalize_locale(locale)
    p = PROMPTS.get(resolved, PROMPTS["en"]).get(key)
    if p is None:
        p = PROMPTS["en"].get(key, "")
    return p.format(**kwargs) if kwargs else p


def prompt(key: str, **kwargs) -> str:
    """Get a localized LLM prompt."""
    return prompt_for_locale(_locale, key, **kwargs)


def tmpl_for_locale(locale: str | None, key: str, **kwargs) -> str:
    """Get an output template for an explicit locale."""
    resolved = _normalize_locale(locale)
    tpl = TEMPLATES.get(resolved, TEMPLATES["en"]).get(key)
    if tpl is None:
        tpl = TEMPLATES["en"].get(key, "")
    return tpl.format(**kwargs) if kwargs else tpl


def tmpl(key: str, **kwargs) -> str:
    """Get a localized output template."""
    return tmpl_for_locale(_locale, key, **kwargs)
