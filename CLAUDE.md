# MantisFetch

Open-source data collection and document parsing platform by ReadyForAI.
MIT License. GitHub: ReadyForAI/MantisFetch. Default port: 9898.

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:

- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:

- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:

- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:

- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:

```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

---

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.

## Architecture

Single-process FastAPI application with two mounted sub-apps:

- `/web/*` — Browser service (Playwright-based web scraping, semantic distillation, WebMCP)
- `/doc/*` — Document reader (PDF/DOCX parsing, OCR, three-tier summaries)

Unified entry point: `mantisfetch_server.py` (mounts `/web`, `/doc`, `/mcp`)

Key source files:

- `services/browser/mantisfetch_browser/` — Web browser service package
  (`__init__.py`, `session.py`, `models.py`, `ranking.py`, `vision.py`)
- `services/docreader/mantisfetch_docreader/` — Document reader package
  (`__init__.py`, `storage.py`, `summaries.py`, `pdf.py`, `word.py`, `ocr/`)
- `services/mcp/mantisfetch_mcp/` — MCP server (streamable-HTTP at `/mcp`)
- `mantisfetch_common/` — shared storage/atomic-write/path helpers
- `providers/` — LLM (Gemini / OpenAI-compatible) and search providers
- `i18n.py` — Internationalization (zh/en, default en, LANG env switch)

## Tech Stack

- Python 3.11+
- FastAPI + uvicorn
- Playwright (browser automation)
- PyMuPDF / python-docx (document parsing)
- Gemini API (OCR + summarization)

## Code Conventions

- All code, comments, logs, error messages in English (or i18n calls)
- Type hints required on all public functions
- Docstrings in English for all public APIs
- Use `ruff` for linting and formatting
- Commit messages: `{type}({scope}): {description}` in English
  - Types: feat, fix, refactor, docs, ci, test, chore

## Project Structure

```
mantisfetch/
├── mantisfetch_server.py                       # Unified entry (mounts /web /doc /mcp)
├── services/
│   ├── browser/mantisfetch_browser/            # Browser service package
│   ├── docreader/mantisfetch_docreader/        # Document reader package (incl. ocr/)
│   └── mcp/mantisfetch_mcp/                     # MCP server
├── mantisfetch_common/                         # Shared storage/atomic/path helpers
├── providers/                                  # LLM + search providers
├── i18n.py                                     # Internationalization
├── requirements.txt
├── tests/                                      # ~60 test modules
├── sdk/python/                                 # Python client SDK
└── skills/                                     # Agent SKILL files
    ├── mantisfetch-browser-SKILL.md
    └── mantisfetch-docreader-SKILL.md
```

## Commands

```bash
# Install (exact versions, as CI installs them; the image skips requirements-dev.txt)
pip install -r requirements.txt -r requirements-dev.txt -c constraints.txt

# Run the service
python mantisfetch_server.py                    # default port 9898

# Lint
ruff check .

# Format
ruff format .

# Test
pytest tests/ -v

# Type check (optional)
pyright mantisfetch_*.py
```

## Key Design Decisions

- doc-index v2 format: shared unified index between /web (web captures) and /doc (uploaded documents)
- Three-tier loading: digest (~200 tokens) → brief (~1500 tokens) → section (on-demand) → full (almost never)
- Table extraction: automatic HTML <table> → Markdown with numeric column statistics
- WebMCP support: Chrome 146+ structured tool discovery and invocation
- Provenance tracking: every document/capture has manifest.json with source, timestamp, content_hash

## Before Making Changes

1. Read relevant SKILL files in `skills/` to understand API contracts
2. Check `docs/mantisfetch_opensource_design.md` for feature boundaries (open-source vs commercial)
3. Run `ruff check .` and `pytest tests/ -v` before committing

## 依赖版本锁定（constraints.txt）

镜像用 `pip install -r requirements.txt -c constraints.txt` 安装依赖，CI 另加 `-r requirements-dev.txt`（ruff、pytest，也锁在同一份文件里）。`constraints.txt` 锁定了镜像里每个包的确切版本，覆盖 CPython 3.11、Linux x86_64，以及 `requirements.txt` 加 x86_64 OCR 依赖的完整闭包。同一个 tag 无论在哪台机器上构建、有没有层缓存，装出来的依赖都一样。`pyproject.toml` 保持宽松，给 PyPI 用户留兼容空间。

- **改依赖必须同 PR 重新生成锁文件**：增删或修改 requirements 文件里的任何一行，都要在同一个 PR 里跑 `scripts/refresh_constraints.sh`。不加参数时保留已有版本，只调整新要求逼出来的变化；需要的 uv 版本写在脚本里。`tests/test_constraints_in_sync.py` 会把漏跑抓成红灯。
- **底座镜像锁 digest**：Dockerfile 的 `FROM` 锁定 `python:3.11-slim-trixie` 的多架构 index digest，用 `scripts/refresh_base_image.sh` 刷新（需要 docker buildx）。apt 装的系统包（LibreOffice、字体、Chromium 依赖）仍按构建当天的镜像源解析，**没有锁**。
- **升级走每周 PR**：`.github/workflows/deps-refresh.yml` 每周一跑 `--upgrade`，并刷新底座 digest，开或更新 `deps/refresh` 分支的 PR。这个 PR 是用 `GITHUB_TOKEN` 开的，不会触发 CI；dispatch 跑出来的检查也不算进必需的 `ci` 检查（#338 实测过）。所以要**关闭再重开**它来跑 CI。合并前要看大版本、小版本变动的 Release Notes。
- arm64 的 OCR 依赖（`requirements-ocr-arm64.txt`）只供开发用，不受这份锁文件约束：它要 numpy<2 和 paddle 2.6.2，和 x86_64 的锁定版本冲突。

## 对外特性登记（public-features.yaml）

官网（ReadyForAI website）通过本仓根目录 `public-features.yaml` 发现新能力、撤回已下线能力。规则全文见 SharedSpecs `notice/public-features-ledger-20261011/notice.md`，格式见 `schemas/public_features_v1.json`。

- **同 PR 登记**：合入后外部用户/运维可感知的变化，在**同一个 PR** 里新增或更新一条。包括：HTTP API / MCP tool 的新增、变更、移除；新的集成、搜索或 LLM 服务商支持；默认行为或开关变化；对外口径里的数字变化（MCP 工具数、支持格式等）；部署形态变化（镜像变体、PyPI 包）；下线或改名。纯重构、内部存储结构、测试/CI、依赖升级、普通 bugfix 不登记。
- **只写事实**：`fact` 写做什么、边界、默认值、依赖，不写营销话术。本仓开源，本文件随仓公开，只写已公开的事实。
- **可见度由本仓提议**：`public` 可上官网 / `ask` 需 Seer 拍板 / `internal` 存在但不对外。拿不准填 `ask`。
- **不删条目**：下线改 `status: removed`；改名新增一条并用 `supersedes` 指向旧 id。
- **日期加引号**（`since: "2026-10-11"`），`evidence` 只写版本库里存在的路径或 PR 链接。
- **提交前校验**（在仓根目录执行；SharedSpecs #922 合入前 SS main 上还没有这个 schema，从 PR 分支 `notice/public-features-ledger-20261011` 取）：
  `.venv/bin/python -c 'import json,sys,yaml,jsonschema; jsonschema.validate(yaml.safe_load(open("public-features.yaml")), json.load(open(sys.argv[1])), format_checker=jsonschema.FormatChecker()); print("public-features.yaml OK")' /Users/seer/Products/SharedSpecs/schemas/public_features_v1.json`
