from services.docreader.larkscout_docreader import (
    PageContent,
    ParsedDocument,
    Section,
    _load_document_profile,
    _parsed_document_locale,
    _split_sections,
    _summary_placeholder_text,
    generate_summaries,
)


def test_split_sections_keeps_preface_before_toc_body() -> None:
    text = """
封面
目录
1. 总则
1.1 项目概况
1.2 招标依据
1. 总则
1.1 项目概况
这里是项目概况正文。这里是项目概况正文。这里是项目概况正文。这里是项目概况正文。这里是项目概况正文。
1.2 招标依据
这里是招标依据正文。这里是招标依据正文。这里是招标依据正文。这里是招标依据正文。这里是招标依据正文。这里是招标依据正文。这里是招标依据正文。这里是招标依据正文。这里是招标依据正文。这里是招标依据正文。
""".strip()

    profile = _load_document_profile("bid_cn", None)
    assert profile is not None

    sections = _split_sections(
        [PageContent(page_num=1, text=text)],
        section_policy=profile.section_policy,
    )

    assert sections[0].title == "前言/目录"
    titles = [section.title for section in sections]
    assert "1.1 项目概况" in titles
    assert "1.2 招标依据" in titles
    assert "封面" in sections[0].text


def test_generate_summaries_uses_chinese_prompt_for_chinese_documents(monkeypatch) -> None:
    prompts: list[str] = []

    def fake_summarize(text: str, summarize_prompt: str, max_retries: int = 2) -> str:
        prompts.append(summarize_prompt)
        return "中文摘要"

    monkeypatch.setattr("services.docreader.larkscout_docreader.gemini_summarize", fake_summarize)
    parsed = ParsedDocument(
        filename="中文招标文件.pdf",
        file_type="pdf",
        total_pages=1,
        pages=[],
        sections=[
            Section(
                index=1,
                title="1.1 项目概况",
                level=2,
                text="这是中文招标文件正文，包含项目目标、招标依据和验收要求。",
                page_range="p.1-1",
            )
        ],
    )

    digest, brief, _sections = generate_summaries(parsed)

    assert digest == "中文摘要"
    assert brief == "中文摘要"
    assert _parsed_document_locale(parsed) == "zh"
    assert prompts
    assert all("中文输出" in prompt for prompt in prompts)


def test_summary_placeholder_uses_document_locale() -> None:
    assert _summary_placeholder_text("pending", locale="zh") == "(摘要待生成)"
    assert _summary_placeholder_text("running", locale="zh") == "(摘要生成中)"
    assert _summary_placeholder_text("failed", "rate limited", locale="zh").startswith("(摘要生成失败")


def test_split_sections_suppresses_numbered_clauses_in_formal_chinese_docs() -> None:
    text = """
第一章 投 标 须 知
这里是第一章说明。这里是第一章说明。这里是第一章说明。
一、总 则
1、项目名称
这里是项目名称正文。这里是项目名称正文。这里是项目名称正文。
2、招标范围
这里是招标范围正文。这里是招标范围正文。这里是招标范围正文。
3.1 招标人不组织踏勘现场，投标人需自行组织踏勘现场。
二、招标文件
4、招标文件组成
这里是招标文件正文。这里是招标文件正文。这里是招标文件正文。
三、投标文件的编制
9.1 投标文件由综合标部分、商务部分和技术部分组成。
这里是投标文件正文。这里是投标文件正文。这里是投标文件正文。
四、投标文件的提交
这里是提交要求正文。这里是提交要求正文。这里是提交要求正文。
""".strip()

    sections = _split_sections([PageContent(page_num=1, text=text)])
    titles = [section.title for section in sections]

    assert "一、总 则" in titles
    assert "二、招标文件" in titles
    assert "三、投标文件的编制" in titles
    assert "1、项目名称" not in titles
    assert not any(title.startswith("3.1 招标人不组织") for title in titles)
