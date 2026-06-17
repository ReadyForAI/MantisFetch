# MantisFetch 模块化重构设计

> 状态：草案 / 待 review · 作者：重构规划 · 日期：2026-06-11
> 目标读者：维护者。本文只定架构与计划，不含实现 diff。

## 1. 背景与问题

当前仓库已经是「两个 FastAPI 子应用挂在单进程」的干净形态，但**文件内部**已严重失衡：

| 单元 | 行数 | 函数/类数 | 端点数 |
|---|---:|---:|---:|
| `services/docreader/mantisfetch_docreader.py` | **7894** | ~250 | 22 |
| `services/browser/mantisfetch_browser.py` | 2660 | ~70 | 13 |
| `providers/`（已分层，正面样板） | 533（4 文件） | — | — |
| `mantisfetch_server.py`（统一入口） | 75 | — | 1 |

三个具体问题：

1. **docreader 是上帝文件**：7894 行、~250 个函数挤在一个模块。可读性、合并冲突、测试隔离、上手成本全部恶化。（注：`CLAUDE.md` 仍写「docreader ~1300 行」，已严重过期。）
2. **browser 偏大**：2660 行里含一个 ~590 行的 `_setup_routing` 巨函数、自包含的 YOLO 视觉栈、24 个 Pydantic 模型混在逻辑里。
3. **跨服务重复代码**：以下函数/常量在 browser 与 docreader 中**字节级重复**——这是 doc-index v2「/web 与 /doc 共享统一索引」设计的直接副产物：存储**格式**共享，但存储**代码**被复制了两份。
   - `_get_docs_dir` · `_normalize_content_type` · `_doc_storage_rel_path` · `_doc_storage_dir`
   - 常量 `CONTENT_TYPE_DIRS` · `_CONTENT_TYPE_ALIASES`
   - 各写一份的：`_mask_path` · 原子写（`_write_text_atomic` / `_write_text`）

## 2. 目标与非目标

### 目标
- 把 docreader 从 7894 行单文件拆成高内聚子模块；入口模块降到可读规模。
- 把 browser 的视觉栈、模型、巨函数抽离。
- 抽出 `services/common` 共享存储层，**消除两服务间的重复代码**。
- **零行为变更**：纯结构重排，靠现有 26 个测试文件守护。

### 非目标（已与维护者确认）
- **不拆成多进程/多服务**。继续 `mantisfetch_server.py` 单进程、`/web` + `/doc`、单端口 9898。模块边界按「未来可独立部署」来切，但当前不引入网络跳转、不引入独立部署编排。
- 不做性能优化、不改 API 契约、不动 OCR/解析算法逻辑。
- 不清理预先存在的死代码（除非是本次重构产生的孤儿 import）。

## 3. 核心约束：测试命名空间兼容（重构成败关键）

现有测试与扁平模块名**深度耦合**，这是本次重构最大的技术约束，必须当成一等公民设计，否则会演变成几百行测试改动 + 行为漂移：

| 耦合形式 | 量级 | 例子 |
|---|---:|---|
| `patch("mantisfetch_docreader.X")` / `monkeypatch.setattr(mantisfetch_docreader, ...)` | **~87 处** | `patch("mantisfetch_docreader._get_docs_dir")` ×50+ |
| `from mantisfetch_docreader import <内部符号>` | 数十处 | `Section`, `_split_sections`, `export_pdf_region_crop`, `_next_doc_id`, `_update_doc_index` … |
| `from mantisfetch_browser import <内部符号>` | 多处 | `_validate_url`, `Session`, `SessionManager`, `_next_web_doc_id` |
| 异类路径 | 1 处 | `services.docreader.mantisfetch_docreader.gemini_summarize`（test_sectioning_and_locale.py:51） |

### 3.1 `mock.patch` 的语义底线

`patch("mantisfetch_docreader._get_docs_dir")` 替换的是 **`mantisfetch_docreader` 模块对象上的属性**。它只能影响：
- 外部 `mantisfetch_docreader._get_docs_dir()` 形式的调用，**或**
- **`mantisfetch_docreader` 模块内部**以裸名 `_get_docs_dir()` 发起的调用（裸名在本模块全局命名空间查找）。

它**无法**影响一个子模块里以裸名调用自己 import 进来的同名函数（经典的 "patch where it's used, not where it's defined"）。

### 3.2 由此推导出的硬性设计规则

> **R1（入口即命名空间）**：`mantisfetch_docreader` / `mantisfetch_browser` 这两个**对外可被 import 与 patch 的顶层模块名必须保留**。拆分把它们从「单文件」变成「包」，但 `from mantisfetch_docreader import app/Section/...` 与 `patch("mantisfetch_docreader.X")` 必须照旧解析。

> **R2（被 patch 的符号留在调用现场）**：任何被测试 `patch`/`setattr` 的符号，其**调用方代码必须仍然执行在顶层模块（包 `__init__`）的命名空间内**，且该符号被 import 进顶层命名空间、以裸名调用。典型：`_get_docs_dir` 会迁到 `services/common`，但顶层模块 `from mantisfetch_common.storage import _get_docs_dir`，端点继续裸名调用 → `patch("mantisfetch_docreader._get_docs_dir")` 重绑顶层名后，端点看到的就是 mock。✅

> **R3（被直接 import 的符号一律 re-export）**：所有被 `from mantisfetch_docreader import X` 的纯函数/数据类（`Section`、`_split_sections`、`export_pdf_region_crop` …），在包 `__init__` 里 re-export，import 风格的测试零改动。

### 3.3 诚实的测试改动评估

不能承诺「零测试改动」。按 R1–R3，改动分三档：
- **安全（零改动）**：~50 处 `_get_docs_dir` patch（用于端点，符合 R2）+ 全部直接 import（符合 R3）。
- **需改 patch 目标路径（~15–20 处）**：被 patch 的符号若**只在子模块深处被调用**（如 `parse_pdf` 内部调用的 `gemini_ocr`、test_robustness/test_security/test_layout_sidecar_contract 里那批），patch 目标要从 `mantisfetch_docreader.X` 改成 `mantisfetch_docreader.<submodule>.X`。**与对应抽离 PR 同批修改**。
- **1 处异类路径**：`services.docreader.mantisfetch_docreader.gemini_summarize` 需统一到新结构路径。

> 每个抽离 PR 的 DoD 必须包含：「列出本 PR 移动的符号 → 检查其全部 patch/import 引用 → 同 PR 内更新失配的目标路径」。

## 4. 目标目录结构

保持现有 `sys.path` 挂载约定（`mantisfetch_server.py` 把两个 service 目录各自插入 `sys.path`；repo 根在 path 上，故 `i18n`、`providers`、新增 `mantisfetch_common` 均可裸名 import）。每个 monolith 文件 → **同名包**（满足 R1）。

```
mantisfetch/
├── mantisfetch_server.py                 # 不变：统一入口，mount /web /doc
├── i18n.py                             # 不变
├── providers/                         # 不变（已分层，样板）
│
├── mantisfetch_common/                  # ★ 新增：跨服务共享层（repo 根包，与 providers 平级）
│   ├── __init__.py
│   ├── storage.py                     # docs_dir 解析 / content_type / 存储路径 / doc-index 读写
│   ├── atomic.py                      # 原子写 _write_text_atomic / _write_bytes / _write_json
│   └── paths.py                       # _mask_path / 安全文件名 / id 校验正则（共享部分）
│
└── services/
    ├── browser/
    │   ├── mantisfetch_browser/         # ★ 单文件 → 包（保留可 import/patch 名 mantisfetch_browser）
    │   │   ├── __init__.py            # = FastAPI app + 13 端点 + re-export（命名空间门面）
    │   │   ├── models.py              # 24 个 Pydantic Request/Response（L201–411）
    │   │   ├── session.py             # Session / SessionManager（L109–200）
    │   │   ├── routing.py             # _setup_routing 巨函数 + 资源拦截（L635–1239）
    │   │   ├── vision.py              # YOLO + readability：_init_yolo/_letterbox/_nms/_decode/detect（L1239–1373）
    │   │   ├── actions.py             # dom/a11y/vision action 抽取 + 排序/预算（L411–648, 1373–1653）
    │   │   ├── webmcp.py              # _discover/_invoke webmcp（L1653–1748）
    │   │   ├── distill.py             # _distill 编排（L1748–1925）
    │   │   ├── capture.py             # web capture 持久化（_persist_web_capture 等，L2014–2186）
    │   │   └── security.py            # _validate_url + SSRF 常量（L65–108）
    │   ├── readability.js
    │   └── (yolo onnx 等资源)
    │
    └── docreader/
        ├── mantisfetch_docreader/       # ★ 单文件 → 包（保留可 import/patch 名 mantisfetch_docreader）
        │   ├── __init__.py            # = FastAPI app + 22 端点 + 全量 re-export（命名空间门面，约 1200–1600 行）
        │   ├── models.py             # 数据类 + 策略 + Pydantic（PageContent/Section/ParsedDocument/*Policy/DocumentProfile/响应模型）
        │   ├── ocr/
        │   │   ├── engines.py        # gemini_ocr / local_ocr / worker 管道 / OCR 缓存 / 熔断
        │   │   └── tables.py         # OCR 表格检测·重建·续表链接 + markdown 表格工具
        │   ├── parsers/
        │   │   ├── pdf.py            # parse_pdf / _plan_pdf_ocr / _should_prewarm
        │   │   ├── word.py           # parse_word + 内嵌图抽取/锚定
        │   │   ├── tabular.py        # parse_xlsx / parse_csv
        │   │   └── generic.py        # parse_generic + markitdown 转换
        │   ├── sectioning.py         # TOC/标题/split/merge/demote/renumber（L3503–4210）
        │   ├── summaries.py          # gemini_summarize / generate_summaries / batch / 限流
        │   ├── profiles.py           # DocumentProfile 加载 + 字段抽取 + 质量评估（L774–2120 的字段部分）
        │   ├── images.py            # 内嵌图渲染/哈希/库存/OCR（L2857–3187, 5641–5712）
        │   ├── regions.py           # region crop / rerun_region_ocr / 可视化 debug（L5754–6240）
        │   ├── storage.py           # docreader 侧存储：doc_id 解析、doc_dir 解析、index、section/table 读写
        │   └── text_utils.py        # 文本清洗/金额大写/公司名归一（L1332–1724）
        ├── paddle_ocr_worker.py      # 不变（已是独立子进程脚本）
        └── (configs 引用不变，仍指向 repo/configs/)
```

> 行号为现状定位锚点，实现时以函数名为准。

## 5. 共享层 `mantisfetch_common` 设计

最高收益/成本比，且是后续两包拆分的公共地基。**先做**。

- `storage.py`：迁入两边重复的 `_get_docs_dir`、`_normalize_content_type`、`_doc_storage_rel_path`、`_doc_storage_dir`，及常量 `CONTENT_TYPE_DIRS` / `_CONTENT_TYPE_ALIASES`。`DEFAULT_DOCS_DIR` 统一为单一定义（读 `MANTISFETCH_DOCS_DIR`，默认 `~/.mantisfetch/docs`），两边现有的 `DEFAULT_DOCS_DIR` / `_DEFAULT_DOCS_DIR` 改为引用它。
- `atomic.py`：统一 `_write_text_atomic`（browser 版）与 `_write_text`/`_write_bytes`/`_write_json`（docreader 版）为一套原子写工具。
- `paths.py`：`_mask_path`（两边各一份）+ 共享的安全文件名/路径越界校验。

**命名空间兼容**：browser/docreader 顶层 `__init__` 通过 `from mantisfetch_common.storage import _get_docs_dir, _normalize_content_type, ...` 把这些名 import 进各自命名空间（满足 R2，`patch("mantisfetch_docreader._get_docs_dir")` 与 `patch("mantisfetch_browser._get_docs_dir")` 继续生效）。

**风险**：两边的 doc-index 写入存在并发锁（docreader `_doc_index_lock` / browser `_web_index_lock`，各自 `threading.Lock`）。共享层**只迁移纯路径/格式函数，不迁移锁与计数器**——锁是各服务的进程内状态，强行共享会改变并发语义。doc_id 计数器（`_next_doc_id` 的 `DOC-xxx` vs `_next_web_doc_id` 的 `WEB-xxx`）也保留在各服务，二者前缀不同、互不冲突。

## 6. 包门面 `__init__.py` 的职责（R1/R2/R3 落地）

每个服务包的 `__init__.py` 充当**命名空间门面 + HTTP 层**：

1. 定义 `app = FastAPI(...)` 与全部端点处理函数（端点必须留在此处，满足 R2——它们调用的被 patch 符号在此命名空间内裸名解析）。
2. `from .<submodule> import <helpers>` 把端点要用的、以及测试要 patch/import 的符号全部拉进本命名空间。
3. 保留模块级共享状态（信号量、`app`、startup 钩子）在此或在专门的 `state.py`，由 `__init__` re-export。

> 结果：docreader `__init__` 从 7894 行降到约 1200–1600 行（端点 + 门面 + 模块级状态），其余 ~6000 行逻辑进子模块。browser `__init__` 降到约 600–800 行。

## 7. 落地顺序（leaf-first，分多 PR）

每个 PR 独立可合、独立绿测，符合「surgical changes」。顺序按依赖叶子优先：

| PR | 范围 | 验证 | 风险 |
|---|---|---|---|
| **0** | 更新 `CLAUDE.md` 过期行数描述；本设计文档合入 | review | 无 |
| **1** | 抽 `mantisfetch_common`（storage/atomic/paths）；browser+docreader 顶层改为 import 共享层，删除两边重复定义 | 全测试绿；`_get_docs_dir` patch 仍命中 | 中：DEFAULT_DOCS_DIR 统一、锁不迁移 |
| **2** | docreader：`mantisfetch_docreader.py` → 包；先迁**叶子** `models.py` + `text_utils.py`（零跨模块回调） | `from mantisfetch_docreader import Section/PageContent/...` 绿 | 低 |
| **3** | docreader：抽 `ocr/`（engines+tables）。审计并更新指向 `gemini_ocr`/OCR 的 patch 目标路径 | test_robustness / test_layout_sidecar 绿（patch 路径已更新） | **高**：模块级 OCR worker 单例、锁、熔断状态迁移 |
| **4** | docreader：抽 `parsers/`（pdf/word/tabular/generic）+ `sectioning.py` | test_robustness `_split_sections*` / test_csv/xlsx 绿 | 中 |
| **5** | docreader：抽 `summaries.py` + `profiles.py` + `images.py` + `regions.py` + `storage.py`；`__init__` 收敛为门面 | 全量绿；test_region_crop / test_sectioning 异类路径修正 | 中 |
| **6** | browser：`mantisfetch_browser.py` → 包；迁 `models.py`+`security.py`+`session.py`（叶子） | test_security `_validate_url` / test_concurrency `Session` 绿 | 低 |
| **7** | browser：抽 `vision.py`+`routing.py`+`actions.py`+`webmcp.py`+`distill.py`+`capture.py`；conftest `patch("mantisfetch_browser.async_playwright")` 目标核对 | test_web_capture（`_distill`/`_browser` patch）绿 | 中：`_browser` 全局、playwright 生命周期 |
| **8** | 收尾：更新 `pyproject.toml` packages.find glob（确保新包被打包）、SKILL 文档若引用文件路径则同步 | `pip install -e .` + 全测试 | 低 |

> PR 1–5（docreader 链路）是主战场；PR 6–7（browser）可与之并行或顺延。每个 PR 控制在「一个模块簇 + 其测试目标修正」的粒度。

## 8. 模块级共享状态迁移清单（最易出错处）

拆包时**最危险的不是函数，是模块级单例/锁/状态**——它们必须迁到唯一拥有者模块，且被 `__init__` re-export 以兼容现有引用：

**docreader：**
- `_parse_sem` / `_upload_sem` / `_doc_id_parse_locks` / `_doc_id_parse_locks_guard`（并发闸）→ 留 `__init__`（端点直接用）
- `_md_converter` / `_md_converter_lock`（markitdown 单例）→ `parsers/generic.py`
- `_local_ocr_worker` 及一族 `_local_ocr_worker_lock/_ready/_initializing/_disabled_until`、`_summary_llm_lock/_next_allowed_at`、`_deferred_summary_sem` → `ocr/engines.py` 与 `summaries.py`
- `_doc_counter_lock` / `_doc_index_lock` → `storage.py`
- 各 `re.compile` 正则常量、`HEADING_PATTERNS`、`SUPPORTED_FORMATS`、`OCR_*` 配置 → 随其使用簇就近迁移

**browser：**
- `_pw` / `_browser`（playwright 全局）→ 留 `__init__`（生命周期 + `_distill` 用；test_web_capture patch `_browser`）
- `sessions = SessionManager()` 单例 → `__init__`（端点用）
- `_capture_sem` / `_session_sem` → `__init__`
- `YOLO_SESSION/_INPUT_NAME/_OUTPUT_NAMES/_ENABLED`、`READABILITY_JS/_AVAILABLE` → `vision.py`
- `_web_counter_lock` / `_web_index_lock` → `capture.py` 或共享层（见 §5 风险，倾向留 browser）
- 大段 JS 常量（`DISTILL_SIMPLE_JS`/`ACTIONS_DOM_JS`/`WEBMCP_*_JS` 等）→ 随其使用簇（distill/actions/webmcp）

## 9. 验证策略

- **每个 PR**：`ruff check .` + `pytest tests/ -v` 全绿（排除 `live`/`live_llm` 标记的需真实服务用例）。
- **命名空间回归**：保留一个轻量 smoke 测试断言 `from mantisfetch_docreader import app, Section, _split_sections, _get_docs_dir` 与 `from mantisfetch_browser import app, _validate_url` 全部可解析——防止门面 re-export 漏项。
- **行为零变更证据**：PR 不新增/修改断言逻辑，仅在「被 patch 符号迁出顶层模块」时更新 patch 目标路径，且在 PR 描述列明每一处路径变更。
- **打包**：PR 8 后 `pip install -e . && python -c "import mantisfetch_server"` 通过。

## 10. 待 review 的决策点

1. 共享层命名：`mantisfetch_common`（与 `providers` 平级的 repo 根包）vs 放 `services/common`（需把 `services/` 加入 `sys.path`）。本文选前者，因与现有 `i18n.py`/`providers/` 的 repo 根约定一致，**无需改 sys.path**。
2. 是否本轮就拆 `_setup_routing`（590 行）内部逻辑，还是仅整体搬到 `routing.py`、内部拆解留后续。本文倾向**仅搬移**（零行为变更优先）。
3. browser 的 `_web_index_lock` 是否进共享层。本文倾向**留 browser**（并发语义按服务隔离）。
