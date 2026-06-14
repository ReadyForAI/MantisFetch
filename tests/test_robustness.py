"""Robustness tests for TASK-018: row limits, rate limiting, health masking, OCR retry, atomic writes."""

import csv
import importlib.util
import os
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from starlette.testclient import TestClient


class TestCSVParse:
    """CSV parsing via MarkItDown produces valid results."""

    def test_csv_small_file(self):
        import larkscout_docreader

        with tempfile.NamedTemporaryFile(suffix=".csv", mode="w", delete=False, newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["a", "b"])
            for i in range(5):
                writer.writerow([str(i), str(i)])
            path = Path(f.name)

        try:
            result = larkscout_docreader.parse_csv(path)
            assert result.file_type == "csv"
            assert result.total_pages == 1
        finally:
            path.unlink(missing_ok=True)


class TestXLSXParse:
    """XLSX parsing via MarkItDown produces valid results."""

    def test_xlsx_basic_parse(self):
        openpyxl = pytest.importorskip("openpyxl")
        import larkscout_docreader

        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as f:
            path = Path(f.name)

        try:
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.append(["col_a", "col_b"])
            for i in range(10):
                ws.append([f"val_{i}", i])
            wb.save(path)
            wb.close()

            result = larkscout_docreader.parse_xlsx(path)
            assert result.file_type == "xlsx"
            assert result.total_pages >= 1
        finally:
            path.unlink(missing_ok=True)


class TestPDFParse:
    """PDF parsing should preserve page-level location hints."""

    def test_paddle_worker_uses_v2_api_for_paddleocr_2x(self, monkeypatch):
        worker_path = Path(__file__).parents[1] / "services" / "docreader" / "paddle_ocr_worker.py"
        spec = importlib.util.spec_from_file_location("paddle_ocr_worker_test_v2", worker_path)
        assert spec is not None and spec.loader is not None
        worker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(worker)

        class LegacyPaddleOCR:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

            def ocr(self, image_array, cls=False):
                return [[[None, ("甲方：测试公司", 0.99)]]]

        paddleocr_module = types.SimpleNamespace(PaddleOCR=LegacyPaddleOCR)
        monkeypatch.setitem(sys.modules, "paddleocr", paddleocr_module)
        monkeypatch.setattr(worker.importlib.metadata, "version", lambda name: "2.10.0")

        engine, api_version = worker._build_engine()
        text = worker._flatten_paddle_ocr_result(worker._predict(engine, api_version, object()))

        assert api_version == "v2"
        assert engine.kwargs["lang"] == "ch"
        assert "text_detection_model_name" not in engine.kwargs
        assert text == "甲方：测试公司"

    def test_local_ocr_uses_isolated_worker(self, tmp_path, monkeypatch):
        import larkscout_docreader

        worker = tmp_path / "worker.py"
        worker.write_text(
            "\n".join(
                [
                    "import json, sys",
                    "print(json.dumps({'type': 'ready'}), flush=True)",
                    "for line in sys.stdin:",
                    "    req = json.loads(line)",
                    "    print(json.dumps({'ok': True, 'page_num': req['page_num'], 'text': '甲方：测试公司'}, ensure_ascii=False), flush=True)",
                ]
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("LARKSCOUT_LOCAL_OCR_WORKER_CMD", f"{sys.executable} {worker}")
        monkeypatch.setattr(larkscout_docreader.ocr.engines, "_local_ocr_disabled_until", 0.0)
        monkeypatch.setattr(larkscout_docreader.ocr.engines, "LOCAL_OCR_WORKER_STARTUP_TIMEOUT_SEC", 3.0)
        monkeypatch.setattr(larkscout_docreader.ocr.engines, "LOCAL_OCR_WORKER_REQUEST_TIMEOUT_SEC", 3.0)

        try:
            text = larkscout_docreader.local_ocr(b"not-an-image", 1, "paddleocr")
        finally:
            larkscout_docreader._stop_local_ocr_worker()

        assert text == "甲方：测试公司"

    def test_local_ocr_worker_crash_does_not_crash_parent(self, tmp_path, monkeypatch):
        import larkscout_docreader

        worker = tmp_path / "worker_crash.py"
        worker.write_text(
            "\n".join(
                [
                    "import json, sys",
                    "print(json.dumps({'type': 'ready'}), flush=True)",
                    "sys.stdin.readline()",
                    "sys.exit(139)",
                ]
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("LARKSCOUT_LOCAL_OCR_WORKER_CMD", f"{sys.executable} {worker}")
        monkeypatch.setattr(larkscout_docreader.ocr.engines, "_local_ocr_disabled_until", 0.0)
        monkeypatch.setattr(larkscout_docreader.ocr.engines, "LOCAL_OCR_WORKER_STARTUP_TIMEOUT_SEC", 3.0)
        monkeypatch.setattr(larkscout_docreader.ocr.engines, "LOCAL_OCR_WORKER_REQUEST_TIMEOUT_SEC", 3.0)
        monkeypatch.setattr(larkscout_docreader.ocr.engines, "LOCAL_OCR_CIRCUIT_BREAKER_SEC", 0.0)

        try:
            text = larkscout_docreader.local_ocr(b"not-an-image", 1, "paddleocr")
        finally:
            larkscout_docreader._stop_local_ocr_worker()

        assert text.startswith("[OCR failed")

    def test_local_ocr_worker_default_command_points_at_real_worker(self, monkeypatch):
        # No env override: the default must resolve to the on-disk worker script.
        # Regression guard for the __file__-relative path after the ocr/ package move.
        import larkscout_docreader

        monkeypatch.delenv("LARKSCOUT_LOCAL_OCR_WORKER_CMD", raising=False)
        cmd = larkscout_docreader.ocr.engines._local_ocr_worker_command()
        worker_path = Path(cmd[-1])
        assert worker_path.name == "paddle_ocr_worker.py"
        assert worker_path.exists(), f"default worker path does not exist: {worker_path}"

    def test_load_document_profile_contract_cn(self):
        from larkscout_docreader import _load_document_profile

        profile = _load_document_profile("contract_cn", None)

        assert profile is not None
        assert profile.name == "contract_cn"
        assert profile.upgrade_policy.local_ocr_backend == "paddleocr"
        assert profile.processing_policy.large_file_threshold_mb == 50
        assert profile.processing_policy.max_local_ocr_pixels == 4_000_000
        assert profile.summary_policy.async_modes == ("fast", "accurate")
        assert profile.classification.required_terms

    def test_resolve_ocr_render_scale_caps_large_pages(self):
        from larkscout_docreader import _resolve_ocr_render_scale

        class Rect:
            width = 1500
            height = 1500

        class Page:
            rect = Rect()

        scale, pixels, capped = _resolve_ocr_render_scale(
            Page(),
            requested_scale=2.0,
            max_pixels=4_000_000,
            min_scale=1.25,
        )

        assert capped is True
        assert scale < 2.0
        assert pixels <= 4_000_000

    def test_assess_contract_quality_detects_scan_only_pdf(self):
        from larkscout_docreader import _assess_contract_quality, _load_document_profile

        profile = _load_document_profile("contract_cn", None)
        assessment = _assess_contract_quality(
            "合同\n甲方\n乙方",
            [
                {"page_num": 1, "text_len": 0, "image_count": 1, "scan_like": True},
                {"page_num": 2, "text_len": 12, "image_count": 1, "scan_like": True},
                {"page_num": 3, "text_len": 0, "image_count": 1, "scan_like": True},
            ],
            profile,
        )

        assert assessment["document_quality"] == "scan_only"
        assert assessment["is_contract"] is True

    def test_classify_contract_text_matches_required_terms(self):
        from larkscout_docreader import _classify_contract_text, _load_document_profile

        profile = _load_document_profile("contract_cn", None)
        is_contract, matched_terms = _classify_contract_text(
            "采购合同\n甲方：测试公司\n乙方：示例公司",
            profile,
        )

        assert is_contract is True
        assert matched_terms == ["合同", "甲方", "乙方"]

    def test_plan_pdf_ocr_uses_local_backend_for_scan_only_accurate_mode(self):
        from larkscout_docreader import _load_document_profile, _plan_pdf_ocr

        profile = _load_document_profile("contract_cn", None)
        plan = _plan_pdf_ocr(
            profile=profile,
            parse_mode="accurate",
            force_ocr=False,
            explicit_ocr_pages=None,
            assessment={
                "document_quality": "scan_only",
                "scan_like_pages": [1, 2, 3],
                "sparse_pages": [1, 2, 3],
                "image_pages": [1, 2, 3],
                "page_signals": [
                    {"page_num": 1},
                    {"page_num": 2},
                    {"page_num": 3},
                ],
            },
        )

        assert plan["local_backend"] == "paddleocr"
        assert plan["local_ocr_pages"] == [1, 2, 3]
        assert plan["llm_ocr_pages"] == []
        assert plan["region_llm"] is True

    def test_plan_pdf_ocr_force_ocr_uses_llm_full_path(self):
        from larkscout_docreader import _load_document_profile, _plan_pdf_ocr

        profile = _load_document_profile("contract_cn", None)
        plan = _plan_pdf_ocr(
            profile=profile,
            parse_mode="accurate",
            force_ocr=True,
            explicit_ocr_pages=None,
            assessment={
                "document_quality": "scan_only",
                "scan_like_pages": [1, 2],
                "sparse_pages": [1, 2],
                "image_pages": [1, 2],
                "page_signals": [{"page_num": 1}, {"page_num": 2}],
            },
        )

        assert plan["llm_ocr_pages"] == [1, 2]
        assert plan["proofread"] is True

    def test_plan_pdf_ocr_explicit_pages_upgrade_only_selected_pages(self):
        from larkscout_docreader import _load_document_profile, _plan_pdf_ocr

        profile = _load_document_profile("contract_cn", None)
        plan = _plan_pdf_ocr(
            profile=profile,
            parse_mode="accurate",
            force_ocr=False,
            explicit_ocr_pages={2},
            assessment={
                "document_quality": "scan_only",
                "scan_like_pages": [1, 2, 3],
                "sparse_pages": [1, 2, 3],
                "image_pages": [1, 2, 3],
                "page_signals": [{"page_num": 1}, {"page_num": 2}, {"page_num": 3}],
            },
        )

        assert plan["llm_ocr_pages"] == [2]
        assert plan["local_ocr_pages"] == [1, 3]
        assert plan["region_llm"] is True

    def test_markdown_headings_use_min_level_as_section_boundary(self):
        """Regression for #72: when MarkItDown output has explicit ## markers, every
        ## becomes a section boundary (even if its stripped text fails the legacy
        heuristics or would be suppressed as an arabic-clause heading), while deeper
        ### markers stay inside the parent section.
        """
        from larkscout_docreader import PageContent, _split_sections

        body = "段落正文" * 30  # 120 chars, defeats _merge_short_sections
        text = "\n".join(
            [
                "## 一、项目名称",
                body,
                "## 二、服务要求",
                body,
                "## 相关附件",
                body,
                "---",
                "### 概述",
                "技术规范书卷首段落,概述应吸收到上一节。" + body,
                "## 4.1前端网站使用需求",
                body,
                "## 4.2后端应用性能监控需求",
                body,
            ]
        )
        sections = _split_sections([PageContent(page_num=1, text=text)])

        titles = [s.title for s in sections]
        assert titles == [
            "一、项目名称",
            "二、服务要求",
            "相关附件",
            "4.1前端网站使用需求",
            "4.2后端应用性能监控需求",
        ]
        # `### 概述` must not become its own section; its body lives inside the prior ##.
        attachments = next(s for s in sections if s.title == "相关附件")
        assert "概述" in attachments.text
        assert "技术规范书卷首段落" in attachments.text

    def test_polluted_heading_with_sentence_period_is_demoted(self):
        """Regression for #74: ``## `` lines whose text ends in 句号 (or are long
        and end in 冒号/分号) are paragraph body misstyled as a heading in the
        source docx. They must not become section boundaries; their content
        stays in the parent section body.
        """
        from larkscout_docreader import PageContent, _split_sections

        body = "段落正文" * 30
        text = "\n".join(
            [
                "## 第四部分 合同主要条款",
                body,
                # Heading 2 misapplied: 97-char clause ending in 句号
                "## 一、乙方应遵守《商业银行应用程序接口安全管理规范》《个人金融信息保护技术规范》和其他相关监管要求，不对甲方信息安全环境和其他系统造成负面影响。",
                body,
                # Heading 2 misapplied: 44-char clause ending in 冒号
                "## 十二、乙方应对在甲方现场和非现场的乙方人员每半年至少进行1次网络安全教育，包括但不限于：",
                body,
                # Legitimate heading: short, no terminator
                "## 二、保密义务",
                body,
            ]
        )
        sections = _split_sections([PageContent(page_num=1, text=text)])

        titles = [s.title for s in sections]
        assert titles == ["第四部分 合同主要条款", "二、保密义务"]
        contracts = sections[0]
        assert "乙方应遵守《商业银行应用程序接口安全管理规范》" in contracts.text
        assert "乙方应对在甲方现场和非现场的乙方人员" in contracts.text

    def test_polluted_heading_filter_spares_short_enumeration_headings(self):
        """A short ``## 6.2 ... 内容:`` style heading (<=30 chars) is legitimate
        even though it ends in 冒号, and must not be demoted.
        """
        from larkscout_docreader import PageContent, _split_sections

        body = "段落正文" * 30
        text = "\n".join(
            [
                "## 6.1 项目概况",
                body,
                "## 6.2乙方向甲方初步交付的研发成果包括但不限于以下内容：",
                body,
                "## 6.3 验收",
                body,
            ]
        )
        sections = _split_sections([PageContent(page_num=1, text=text)])

        assert [s.title for s in sections] == [
            "6.1 项目概况",
            "6.2乙方向甲方初步交付的研发成果包括但不限于以下内容：",
            "6.3 验收",
        ]

    def test_markdown_heading_h3_only_doc_still_cuts_sections(self):
        """When a doc uses only H3 markers (no H1/H2), H3 becomes the section level."""
        from larkscout_docreader import PageContent, _split_sections

        body = "段落正文" * 30
        text = "\n".join(
            [
                "### 第一章 引言",
                body,
                "### 第二章 方法",
                body,
                "### 第三章 结论",
                body,
            ]
        )
        sections = _split_sections([PageContent(page_num=1, text=text)])

        assert [s.title for s in sections] == ["第一章 引言", "第二章 方法", "第三章 结论"]

    def test_tender_section_split_keeps_third_level_under_second_level(self):
        from larkscout_docreader import PageContent, _split_sections

        pages = [
            PageContent(
                page_num=1,
                text="\n".join(
                    [
                        "3. 项目目标与范围",
                        "3.1 项目目标",
                        "3.1.1 全域系统监控覆盖",
                        "覆盖 MES、BIP、CRM。",
                        "3.1.2 智能故障发现与定位",
                        "支持链路分析。",
                        "3.2 项目范围",
                        "系统管理员和运维人员。",
                        "覆盖招标人指定的业务系统、基础资源、应用组件、数据库和前端体验监测范围。",
                        "投标人需结合现场情况完成部署、联调、测试、培训和验收支持工作。",
                    ]
                ),
            )
        ]

        sections = _split_sections(pages)

        assert [section.title for section in sections] == ["3.1 项目目标", "3.2 项目范围"]
        assert "3. 项目目标与范围" in sections[0].text
        assert "3.1.1 全域系统监控覆盖" in sections[0].text
        assert "3.1.2 智能故障发现与定位" in sections[0].text

    def test_dense_pdf_toc_compacts_same_page_entries_without_duplicates(self):
        from larkscout_docreader import PageContent, _split_sections_from_toc

        pages = [
            PageContent(
                page_num=5,
                text="\n".join(
                    [
                        "1. 总则",
                        "1.1 项目概况",
                        "项目背景说明。",
                        "1.2 招标依据",
                        "法规依据说明。",
                    ]
                ),
            ),
            PageContent(page_num=6, text="2. 投标人资格要求\n2.1 基本资质要求\n资质说明。"),
        ]
        toc = [
            [1, "1.总则", 5],
            [2, "1.1项目概况", 5],
            [2, "1.2招标依据", 5],
            [1, "2.投标人资格要求", 6],
            [2, "2.1基本资质要求", 6],
        ]

        sections = _split_sections_from_toc(pages, toc)

        assert [section.title for section in sections] == [
            "1.1项目概况",
            "1.2招标依据",
            "2.1基本资质要求",
        ]
        assert len({section.text for section in sections}) == len(sections)

    def test_long_document_summary_skips_per_section_llm_calls(self, monkeypatch):
        import larkscout_docreader
        from larkscout_docreader import ParsedDocument, Section

        calls = []

        def fake_summarize(text, summarize_prompt, max_retries=2):
            calls.append(text)
            if "Briefing:" in text:
                return "digest"
            return "brief"

        monkeypatch.setattr(larkscout_docreader, "SUMMARY_SECTION_DETAIL_LIMIT", 2)
        monkeypatch.setattr(larkscout_docreader, "gemini_summarize", fake_summarize)
        parsed = ParsedDocument(
            filename="tender.pdf",
            file_type="pdf",
            total_pages=3,
            pages=[],
            sections=[
                Section(index=i, title=f"{i}.1 标题", level=2, text="正文" * 300, page_range=f"p.{i}-{i}")
                for i in range(1, 5)
            ],
        )

        digest, brief, sections = larkscout_docreader.generate_summaries(parsed, concurrency=3)

        assert digest == "digest"
        assert brief == "brief"
        assert sections == parsed.sections
        assert len(calls) == 2
        assert all(not section.summary for section in parsed.sections)

    def test_plan_pdf_ocr_skips_detected_blank_scan_pages(self):
        from larkscout_docreader import _load_document_profile, _plan_pdf_ocr

        profile = _load_document_profile("contract_cn", None)
        plan = _plan_pdf_ocr(
            profile=profile,
            parse_mode="accurate",
            force_ocr=False,
            explicit_ocr_pages=None,
            assessment={
                "document_quality": "scan_only",
                "scan_like_pages": [1, 2, 3],
                "sparse_pages": [1, 2, 3],
                "image_pages": [1, 2, 3],
                "blank_pages": [2],
                "page_signals": [{"page_num": 1}, {"page_num": 2}, {"page_num": 3}],
            },
        )

        assert plan["local_ocr_pages"] == [1, 3]
        assert plan["llm_ocr_pages"] == []

    def test_metadata_page_range_spec_accepts_list_values(self):
        from larkscout_docreader import _metadata_page_range_spec

        assert _metadata_page_range_spec([20, 28, "32-34"]) == "20,28,32-34"

    def test_resolve_summary_mode_uses_contract_profile_async_for_accurate(self):
        from larkscout_docreader import _load_document_profile, _resolve_summary_mode

        profile = _load_document_profile("contract_cn", None)
        mode = _resolve_summary_mode(
            profile=profile,
            parse_mode="accurate",
            generate_summary=True,
            requested_mode=None,
        )

        assert mode == "defer"

    def test_classify_summary_error_maps_rate_limit(self):
        from larkscout_docreader import _classify_summary_error

        code, message = _classify_summary_error(RuntimeError("Error code: 429 - 速率限制"))

        assert code == "rate_limit"
        assert message == "upstream rate limit"

    def test_strip_section_storage_wrapper_removes_summary_prefix(self):
        from larkscout_docreader import _strip_section_storage_wrapper

        raw = (
            "# 合同条款\n\n"
            "**章节 1** | **SID**: abc | **页码**: p.1-1\n\n"
            "**摘要**: 示例摘要\n\n---\n\n"
            "正文内容"
        )

        assert _strip_section_storage_wrapper(raw) == "正文内容"

    def test_pdf_page_ranges_are_not_collapsed_to_page_one(self):
        from larkscout_docreader import _page_bounds, parse_pdf

        from tests.e2e.fixtures.generate_fixtures import generate_pdf

        with tempfile.TemporaryDirectory() as tmp:
            path = generate_pdf(Path(tmp) / "sample.pdf")
            result = parse_pdf(path, extract_tables=False)

        assert result.total_pages == 2
        assert result.sections
        page_ranges = [_page_bounds(sec.page_range) for sec in result.sections]
        assert any((start == 2 or end == 2) for start, end in page_ranges), page_ranges

    def test_extract_tables_from_ocr_text_strips_footer_page_number(self):
        from larkscout_docreader import _extract_tables_from_ocr_text

        text, tables = _extract_tables_from_ocr_text(
            "合同正文\n甲方：测试公司\n2",
            page_num=3,
            total_pages=15,
        )

        assert text == "合同正文\n甲方：测试公司"
        assert tables == []

    def test_cleanup_ocr_text_removes_watermark_noise_and_footer(self):
        from larkscout_docreader import _cleanup_ocr_text

        cleaned = _cleanup_ocr_text(
            "\n".join(
                [
                    "[Tp]",
                    "次",
                    "[24Yeeeeai_a_a入的场所处tblaeta告i可ztg",
                    "括其雇员、工作员或代理，不得进入甲方的任何场所。",
                    "4.2乙方应在本合同附件《APM应用性能监测软件采贝合同补充条款》",
                    "eeaeee]",
                    "5.1.1许可软件安装元成后应符合软件说明书的标准。",
                    "[T1tb_e可e_i_eieteeobleset]",
                    "第 4 页 / 共 25 页",
                ]
            )
        )

        assert "[Tp]" not in cleaned
        assert "eeaeee" not in cleaned
        assert "tblaeta" not in cleaned
        assert "第 4 页" not in cleaned
        assert "软件采购合同补充条款" in cleaned
        assert "许可软件安装完成" in cleaned
        assert "括其雇员、工作员或代理" in cleaned

    def test_cleanup_ocr_text_removes_stray_dingzuo_after_sign_place(self):
        from larkscout_docreader import _cleanup_ocr_text

        cleaned = _cleanup_ocr_text(
            "\n".join(
                [
                    "[Tbabla_e_ea_l_e_e_e_a_T_e_eantrp]",
                    "合同签订地点：",
                    "上海市浦东新区",
                    "定作",
                    "-第1页共19页-",
                ]
            )
        )

        assert "Tbabla" not in cleaned
        assert "定作" not in cleaned
        assert "第1页" not in cleaned
        assert "上海市浦东新区" in cleaned

    def test_cleanup_ocr_text_uses_source_filename_for_leading_doc_id(self):
        from larkscout_docreader import _cleanup_ocr_text

        cleaned = _cleanup_ocr_text(
            "NBS220752\n甲方（委托方）：华夏基金管理有限公司\n第1页 / 共25页",
            source_filename="NBS220952.pdf",
        )

        assert cleaned.splitlines()[0] == "NBS220952"
        assert "第1页" not in cleaned

    def test_cleanup_ocr_text_removes_generic_llm_preface(self):
        from larkscout_docreader import _cleanup_ocr_text

        cleaned = _cleanup_ocr_text(
            "Preface\n兴业数字金融服务（上海）股份有限公司\n合同编号： CFT-JT-FZ-202205-0018"
        )

        assert cleaned.splitlines()[0] == "兴业数字金融服务（上海）股份有限公司"

    def test_cleanup_ocr_text_normalizes_product_and_numeric_noise(self):
        from larkscout_docreader import _cleanup_ocr_text

        cleaned = _cleanup_ocr_text(
            "基调研云APM监测268个探针\n"
            "基调所元Network监测\n"
            "小计￥711，016.00\n"
            "微服务只支持lava AgentV3.00+，不支持其他语吉探针"
        )

        assert "基调听云APM" in cleaned
        assert "基调听云Network" in cleaned
        assert "￥711，016.00" in cleaned
        assert "Java AgentV3.00+" in cleaned
        assert "其他语言探针" in cleaned

    def test_extract_profile_fields_rejects_bad_cover_values_and_uses_filename(self):
        from larkscout_docreader import PageContent, _extract_profile_fields, _load_document_profile

        profile = _load_document_profile("contract_cn", None)
        fields = _extract_profile_fields(
            [
                PageContent(
                    page_num=1,
                    text="\n".join(
                        [
                            "合同编号：",
                            "甲",
                            "乙方：",
                            "てさ",
                            "合同签订地点：",
                            "上海市浦东新区",
                        ]
                    ),
                )
            ],
            profile,
            source_filename="NBS220952.pdf",
        )

        assert fields["contract_no"]["value"] == "NBS220952"
        assert fields["contract_no"]["source"] == "source_filename"
        assert "party_b_name" not in fields
        assert fields["sign_place"]["value"] == "上海市浦东新区"

    def test_extract_profile_fields_supports_cover_party_labels(self):
        from larkscout_docreader import PageContent, _extract_profile_fields, _load_document_profile

        profile = _load_document_profile("contract_cn", None)
        fields = _extract_profile_fields(
            [
                PageContent(
                    page_num=1,
                    text="\n".join(
                        [
                            "甲方（委托方）：华夏基金管理有限公司",
                            "乙方（受托方）：北京基调网络股份有限公司",
                            "签订地点：北京市顺义区后沙峪镇空港B区安庆大街甲3号",
                        ]
                    ),
                )
            ],
            profile,
            source_filename="NBS220952.pdf",
        )

        assert fields["party_a_name"]["value"] == "华夏基金管理有限公司"
        assert fields["party_b_name"]["value"] == "北京基调网络股份有限公司"
        assert fields["sign_place"]["value"] == "北京市顺义区后沙峪镇空港B区安庆大街甲3号"

    def test_extract_tables_from_ocr_text_keeps_table_complete(self):
        from larkscout_docreader import _extract_tables_from_ocr_text

        text, tables = _extract_tables_from_ocr_text(
            "\n".join(
                [
                    "1. 软件产品",
                    "序号 名称 数量 税率 含税金额",
                    "1 平台A 1 13% ¥29,800.00",
                    "2 平台B 1 13% ¥562,520.00",
                    "服务小计 ¥592,320.00",
                    "2. 合同价款的支付方式",
                ]
            ),
            page_num=3,
            total_pages=15,
        )

        assert text == "1. 软件产品\n2. 合同价款的支付方式"
        assert tables == [
            "\n".join(
                [
                    "序号 名称 数量 税率 含税金额",
                    "1 平台A 1 13% ¥29,800.00",
                    "2 平台B 1 13% ¥562,520.00",
                    "服务小计 ¥592,320.00",
                ]
            )
        ]

    def test_extract_tables_from_ocr_text_ignores_header_only_markdown_table(self):
        from larkscout_docreader import _extract_tables_from_ocr_text

        text, tables = _extract_tables_from_ocr_text(
            "\n".join(
                [
                    "| VO.® |  | 委外服务协议 工作说明 |",
                    "|------|---|----------------------|",
                    "3-0 工作描述",
                ]
            ),
            page_num=2,
            total_pages=5,
        )

        assert "VO.®" in text
        assert "3-0 工作描述" in text
        assert tables == []

    def test_split_sections_does_not_treat_table_rows_as_headings(self):
        from larkscout_docreader import PageContent, _split_sections

        pages = [
            PageContent(
                page_num=1,
                text="\n".join(
                    [
                        "1. 软件产品",
                        "产品说明",
                        "2. 合同价款的支付方式",
                        "付款安排",
                    ]
                ),
                tables=[
                    "\n".join(
                        [
                            "序号 名称 数量 税率 含税金额",
                            "1 平台A 1 13% ¥29,800.00",
                            "2 平台B 1 13% ¥562,520.00",
                        ]
                    )
                ],
            )
        ]

        sections = _split_sections(pages)

        assert [sec.title for sec in sections] == ["1. 软件产品", "2. 合同价款的支付方式"]
        assert "1 平台A 1 13% ¥29,800.00" in sections[0].text

    def test_split_sections_does_not_treat_price_table_values_as_headings(self):
        from larkscout_docreader import PageContent, _split_sections

        pages = [
            PageContent(
                page_num=1,
                text="\n".join(
                    [
                        "附件一：服务产品及价格确认书",
                        "基调听云报价清单",
                        "产品名称",
                        "单价（元）服务时间",
                        "95 元/个",
                        "探针/月",
                        "2024 年8 月",
                        "5 日至2026 年8 月4 日",
                        "每个月统计使用数量，探针每月起订数量为100 个且总数量少于",
                        "200 个（不包含",
                        "200 个）",
                        "75 月/个",
                        "每个月统计使用数量，按季度6%增值税专用发票结算付款",
                    ]
                ),
            )
        ]

        sections = _split_sections(pages)

        assert len(sections) == 1
        assert "95 元/个" in sections[0].text
        assert "2024 年8 月" in sections[0].text
        assert "200 个（不包含" in sections[0].text
        assert "75 月/个" in sections[0].text

    def test_split_sections_ocr_mode_ignores_logo_and_nested_clauses(self):
        from larkscout_docreader import PageContent, _split_sections

        pages = [
            PageContent(
                page_num=1,
                text="\n".join(
                    [
                        "GF FUTURES",
                        "广发期货",
                        "1.服务内容",
                        "1.1乙方向甲方提供智能业务运维技术服务。",
                        "2.服务费用及支付方式",
                        "2.1固定服务费用为【￥：60,000】。",
                    ]
                ),
                is_ocr=True,
            )
        ]

        sections = _split_sections(pages)

        titles = [sec.title for sec in sections]
        assert "GF FUTURES" not in titles
        assert "1.1乙方向甲方提供智能业务运维技术服务。" not in titles
        assert "2.1固定服务费用为【￥：60,000】。" not in titles
        assert "1.服务内容" in titles
        assert "2.服务费用及支付方式" in titles
        assert "2.1固定服务费用" in next(
            sec.text for sec in sections if sec.title == "2.服务费用及支付方式"
        )

    def test_source_contract_no_is_prepended_to_region_ocr(self):
        from larkscout_docreader import _prepend_source_contract_no_if_missing

        text = _prepend_source_contract_no_if_missing(
            "北京基调网络股份有限公司\n技术服务合同",
            "NBS250961.pdf",
        )

        assert text.splitlines()[0] == "NBS250961"

    def test_split_sections_ocr_mode_merges_tiny_sections(self):
        from larkscout_docreader import PageContent, _split_sections

        pages = [
            PageContent(
                page_num=1,
                text="\n".join(
                    [
                        "1.主要条款",
                        "这是较长的主要条款正文，包含足够信息用于形成一个 section。",
                        "2.碎片标题",
                        "短",
                    ]
                ),
                is_ocr=True,
            )
        ]

        sections = _split_sections(pages)

        assert len(sections) == 1
        assert "2.碎片标题" in sections[0].text

    def test_is_ocr_failed_text_accepts_chinese_placeholder(self):
        from larkscout_docreader import _is_ocr_failed_text

        assert _is_ocr_failed_text("[OCR failed: page 1]")
        assert _is_ocr_failed_text("[OCR 失败: 第 1 页]")

    def test_split_sections_does_not_treat_account_number_as_heading(self):
        from larkscout_docreader import PageContent, _split_sections

        pages = [
            PageContent(
                page_num=1,
                text="\n".join(
                    [
                        "2.合同价款的支付方式",
                        "开户行及帐号：",
                        "626 526 390",
                        "2.4甲方的付款方式为：银行转账。",
                        "3.软件产品交付、质量与验收",
                        "合同签订后90天完成交付，并按照双方确认的验收标准完成上线和最终验收。",
                    ]
                ),
                is_ocr=True,
            )
        ]

        sections = _split_sections(pages)

        assert [sec.title for sec in sections] == [
            "2.合同价款的支付方式",
            "3.软件产品交付、质量与验收",
        ]
        assert "626 526 390" in sections[0].text

    def test_split_sections_supports_dash_numbered_contract_headings(self):
        from larkscout_docreader import PageContent, _split_sections

        pages = [
            PageContent(
                page_num=1,
                text="\n".join(
                    [
                        "1-0总则",
                        "本工作说明通过援引基础协议签署。",
                        "2-0 术语解释和说明",
                        "工作授权书是指联想对供应商执行特定交易的确认文件。",
                        "400-898-9580",
                        "3-0 工作描述",
                        "联想委托供应商提供性能监测服务，服务内容包括网络监测、真实用户体验监测和应用性能监测。",
                    ]
                ),
                is_ocr=True,
            )
        ]

        sections = _split_sections(pages)

        assert [sec.title for sec in sections] == [
            "1-0总则",
            "2-0 术语解释和说明",
            "3-0 工作描述",
        ]
        assert "400-898-9580" in sections[1].text

    def test_replace_blob_segment_falls_back_to_matching_alias(self):
        from larkscout_docreader import FieldGroup, _replace_blob_segment

        group = FieldGroup(
            id="quotation_block",
            aliases=("客户名称", "合同总价款"),
            start_alias="1. 软件产品",
            end_alias="2. 合同价款的支付方式",
            replace_mode="block_between_aliases",
        )

        text = "页眉\n客户名称联想（北京）有限公司\n小计￥711，016.00元"
        replaced = _replace_blob_segment(text, group, "客户名称：联想（北京）有限公司\n小计 ¥711,016.00")

        assert replaced == "页眉\n\n客户名称：联想（北京）有限公司\n小计 ¥711,016.00"

    def test_normalize_document_text_removes_signature_watermark_lines(self):
        from larkscout_docreader import PageContent, _normalize_document_text

        pages = [
            PageContent(
                page_num=1,
                text="\n".join(
                    [
                        "万翼签         万翼签         万翼",
                        "TINGYUN.COM",
                        "甲方：深圳市万物云科技有限公司",
                        "75 月/个",
                        "乙方：北京基调网络股份有限公司",
                    ]
                ),
            )
        ]

        _normalize_document_text(pages)

        assert "万翼签" not in pages[0].text
        assert "TINGYUN.COM" not in pages[0].text
        assert "甲方：深圳市万物云科技有限公司" in pages[0].text
        assert "75 元/个" in pages[0].text
        assert "乙方：北京基调网络股份有限公司" in pages[0].text


class TestDocIdStrategy:
    """doc_id generation can derive a safe directory name from the source filename."""

    def test_source_filename_strategy_uses_stem(self, monkeypatch):
        import larkscout_docreader

        monkeypatch.setenv("LARKSCOUT_DOC_ID_STRATEGY", "source_filename")

        with tempfile.TemporaryDirectory() as tmp:
            docs_dir = Path(tmp)
            assert larkscout_docreader._resolve_doc_id(docs_dir, "NBS250321.pdf", None) == "NBS250321"

    def test_source_filename_strategy_filters_unsupported_chars(self, monkeypatch):
        import larkscout_docreader

        monkeypatch.setenv("LARKSCOUT_DOC_ID_STRATEGY", "source_filename")

        with tempfile.TemporaryDirectory() as tmp:
            docs_dir = Path(tmp)
            result = larkscout_docreader._resolve_doc_id(
                docs_dir,
                "合同/NBS_250321（终版）.pdf",
                None,
            )
            assert result == "NBS-250321"

    def test_source_filename_strategy_falls_back_when_nothing_usable_remains(self, monkeypatch):
        import larkscout_docreader

        monkeypatch.setenv("LARKSCOUT_DOC_ID_STRATEGY", "source_filename")

        with tempfile.TemporaryDirectory() as tmp:
            docs_dir = Path(tmp)
            result = larkscout_docreader._resolve_doc_id(docs_dir, "合同终版.pdf", None)
            assert result == "DOC-001"


class TestHealthPathMasking:
    """M10: Health endpoints must not expose absolute filesystem paths."""

    def test_doc_health_masks_path(self, client: TestClient) -> None:
        resp = client.get("/doc/health")
        assert resp.status_code == 200
        data = resp.json()
        docs_dir = data.get("docs_dir", "")
        home = os.path.expanduser("~")
        assert not docs_dir.startswith(home), f"Absolute path exposed: {docs_dir}"

    def test_web_health_masks_paths(self, client: TestClient) -> None:
        resp = client.get("/web/health")
        assert resp.status_code == 200
        data = resp.json()
        home = os.path.expanduser("~")
        for key in ("readability_js_path", "yolo_onnx_path"):
            val = data.get(key)
            if val:
                assert not val.startswith(home), f"{key} exposes absolute path: {val}"


class TestAtomicWriteText:
    """M9: _write_text must use atomic write pattern."""

    def test_write_text_is_atomic(self):
        from larkscout_docreader import _write_text

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.md"
            _write_text(path, "hello world")
            assert path.read_text() == "hello world"
            # No leftover .tmp file
            assert not path.with_suffix(".tmp").exists()


class TestGeminiOCRRetry:
    """M6: OCR must retry on failure like summarize()."""

    def test_docreader_ocr_wrapper_handles_provider_init_failure(self, monkeypatch):
        import larkscout_docreader

        import providers

        monkeypatch.setattr(providers, "get_provider", lambda: (_ for _ in ()).throw(RuntimeError("missing key")))

        result = larkscout_docreader.gemini_ocr(b"png-bytes", page_num=2)

        assert result == "[OCR failed: page 2]"

    def test_ocr_retries_on_failure(self):
        from providers.gemini import GeminiProvider

        provider = GeminiProvider()
        mock_client = MagicMock()
        provider._client = mock_client

        # First call raises, second succeeds
        mock_response = MagicMock()
        mock_response.text = "extracted text"
        mock_client.models.generate_content.side_effect = [
            RuntimeError("transient"),
            mock_response,
        ]

        # Minimal 1x1 white PNG
        import io

        from PIL import Image as PILImage

        buf = io.BytesIO()
        PILImage.new("RGB", (1, 1)).save(buf, format="PNG")
        img_bytes = buf.getvalue()

        # proofread=False isolates retry behavior; proofread now correctly runs
        # after a transcribe retry (covered in test_failure_sentinel).
        with patch("time.sleep"):
            result = provider.ocr(img_bytes, page_num=1, max_retries=2, proofread=False)

        assert result == "extracted text"
        assert mock_client.models.generate_content.call_count == 2

    def test_ocr_exhausts_retries(self):
        from providers.gemini import GeminiProvider

        provider = GeminiProvider()
        mock_client = MagicMock()
        provider._client = mock_client
        mock_client.models.generate_content.side_effect = RuntimeError("persistent")

        import io

        from PIL import Image as PILImage

        buf = io.BytesIO()
        PILImage.new("RGB", (1, 1)).save(buf, format="PNG")
        img_bytes = buf.getvalue()

        with patch("time.sleep"):
            result = provider.ocr(img_bytes, page_num=3, max_retries=2)

        assert "[OCR failed for page 3]" in result
        assert mock_client.models.generate_content.call_count == 3


class TestGeminiTimeout:
    """M7: Gemini API calls must include timeout config."""

    def test_summarize_passes_timeout(self):
        from providers.gemini import GeminiProvider

        provider = GeminiProvider()
        mock_client = MagicMock()
        provider._client = mock_client

        mock_response = MagicMock()
        mock_response.text = "summary"
        mock_client.models.generate_content.return_value = mock_response

        provider.summarize("text", "prompt")

        call_kwargs = mock_client.models.generate_content.call_args
        config = call_kwargs.kwargs.get("config") or call_kwargs[1].get("config")
        assert config is not None
        assert config["http_options"]["timeout"] == 60_000

    def test_ocr_passes_timeout(self):
        from providers.gemini import GeminiProvider

        provider = GeminiProvider()
        mock_client = MagicMock()
        provider._client = mock_client

        mock_response = MagicMock()
        mock_response.text = "ocr text"
        mock_client.models.generate_content.return_value = mock_response

        import io

        from PIL import Image as PILImage

        buf = io.BytesIO()
        PILImage.new("RGB", (1, 1)).save(buf, format="PNG")
        img_bytes = buf.getvalue()

        provider.ocr(img_bytes, page_num=1)

        call_kwargs = mock_client.models.generate_content.call_args
        config = call_kwargs.kwargs.get("config") or call_kwargs[1].get("config")
        assert config is not None
        assert config["http_options"]["timeout"] == 60_000
