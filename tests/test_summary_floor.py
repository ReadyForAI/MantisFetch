"""Summary floors: near-empty input must never reach the LLM.

Observed: a scan whose OCR silently failed left 48 chars of real text (four
year headings), and the generated digest/brief invented an entire audit report
— "总资产50亿元, 同比增长8%", "连续四年无保留意见" — none of it in the source.
"""

import pytest
from mantisfetch_docreader import PageContent, ParsedDocument, Section, generate_summaries


def _doc(sections: list[Section]) -> ParsedDocument:
    return ParsedDocument(
        filename="scan.pdf",
        file_type="pdf",
        total_pages=len(sections),
        pages=[
            PageContent(page_num=i + 1, text=s.text, is_ocr=True) for i, s in enumerate(sections)
        ],
        sections=sections,
        ocr_page_count=len(sections),
    )


def _section(index: int, title: str, text: str) -> Section:
    return Section(index=index, title=title, level=1, text=text, page_range=f"p.{index}-{index}")


def test_below_document_floor_never_calls_llm(monkeypatch):
    import mantisfetch_docreader as dr

    def _forbidden(*args, **kwargs):
        raise AssertionError("LLM must not be called below the summary floor")

    monkeypatch.setattr(dr, "gemini_summarize", _forbidden)

    # The observed poisoned ingest: four year headings, 48 chars total.
    sections = [
        _section(1, "Page 1", "2025年财务审计报告"),
        _section(2, "Page 54", "2024年财务审计报告"),
        _section(3, "Page 108", "2023年财务审计报告"),
        _section(4, "Page 161", "2022年财务审计报告"),
    ]
    digest, brief, out_sections = generate_summaries(_doc(sections))

    # The "summary" is the actual content, not an invention.
    assert "2025年财务审计报告" in digest
    assert "2025年财务审计报告" in brief
    assert "2022年财务审计报告" in brief
    # No section acquired an LLM summary (Section.summary defaults to "").
    assert all(not s.summary for s in out_sections)


def test_empty_document_returns_note_digest(monkeypatch):
    import mantisfetch_docreader as dr

    monkeypatch.setattr(
        dr,
        "gemini_summarize",
        lambda *a, **k: pytest.fail("LLM must not be called for an empty document"),
    )

    digest, brief, _ = generate_summaries(_doc([_section(1, "Page 1", "")]))

    assert digest.strip()  # the note, not an empty string
    assert brief.strip()


def test_tiny_section_echoes_text_instead_of_llm_summary(monkeypatch):
    import mantisfetch_docreader as dr

    calls: list[str] = []

    def fake_llm(text, prompt, max_retries=2):
        calls.append(text)
        return "LLM-OUT"

    monkeypatch.setattr(dr, "gemini_summarize", fake_llm)

    tiny = _section(1, "封面", "2025年财务审计报告")  # 10 chars — below section floor
    big = _section(2, "正文", "审计发现：" + "营业收入一二三四五六七八九十。" * 30)
    digest, brief, out_sections = generate_summaries(_doc([tiny, big]))

    # The tiny section echoes its own text; only the big one got an LLM summary.
    assert out_sections[0].summary == "2025年财务审计报告"
    assert out_sections[1].summary == "LLM-OUT"
    # No section-summary request ever carried ONLY the tiny fragment.
    assert digest == "LLM-OUT"
    assert calls, "LLM should have been used for the big section, brief, digest"
