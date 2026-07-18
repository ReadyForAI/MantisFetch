"""OCR/summary failure-sentinel contract: failures degrade, never crash or persist raw."""

import base64
import sys
from unittest.mock import MagicMock, patch

import pytest


def _png() -> bytes:
    return base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
        "YPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
    )


def _section(idx: int, summary):
    from mantisfetch_docreader.models import Section

    return Section(
        index=idx, title=f"S{idx}", level=1, text="body text",
        page_range=f"p.{idx}", summary=summary, sid=f"s{idx}", image_refs=[],
    )


def test_compress_brief_excludes_failed_summaries():  # #43
    from mantisfetch_docreader.summaries import _compress_sections_for_brief

    out = _compress_sections_for_brief(
        [_section(1, "good one"), _section(2, "[summary generation failed]")]
    )
    assert "good one" in out
    assert "failed" not in out


def test_single_section_summary_raises_on_sentinel(monkeypatch):  # #20
    import mantisfetch_docreader as dr
    from mantisfetch_docreader.summaries import _summarize_batch

    monkeypatch.setattr(dr, "gemini_summarize", lambda *a, **k: "[summary generation failed]")
    with pytest.raises(RuntimeError):
        _summarize_batch([_section(1, None)])


def test_openai_summarize_and_ocr_return_sentinels(monkeypatch):  # #21
    monkeypatch.setitem(sys.modules, "openai", MagicMock())
    monkeypatch.setenv("MANTISFETCH_LLM_API_KEY", "sk-test")
    from providers.openai_compat import OpenAICompatProvider
    from providers.sentinel import SentinelBoundary

    # Concrete providers raise typed errors; the public boundary folds them
    # back into the historical failure-sentinel strings.
    p = SentinelBoundary(OpenAICompatProvider())

    def boom(*a, **k):
        raise RuntimeError("upstream down")

    monkeypatch.setattr(p._inner, "_chat", boom)
    assert p.summarize("text", "prompt") == "[summary generation failed]"
    assert p.ocr(_png(), 7) == "[OCR failed for page 7]"


def test_gemini_ocr_proofreads_after_transcribe_retry(monkeypatch):  # #22
    import providers.gemini as gem
    from providers import get_provider, reset_provider

    monkeypatch.delenv("MANTISFETCH_LLM_PROVIDER", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    monkeypatch.setattr(gem.time, "sleep", lambda *_: None)
    reset_provider()

    transcribe = MagicMock()
    transcribe.text = "draft"
    proofread = MagicMock()
    proofread.text = "corrected"
    client = MagicMock()
    client.models.generate_content.side_effect = [RuntimeError("transient"), transcribe, proofread]
    genai = MagicMock()
    genai.Client.return_value = client
    pil = MagicMock()
    pil.Image.open.return_value = MagicMock()

    with patch.dict(
        "sys.modules",
        {
            "google": MagicMock(genai=genai),
            "google.genai": genai,
            "PIL": pil,
            "PIL.Image": pil.Image,
        },
    ):
        p = get_provider()
        result = p.ocr(_png(), page_num=1)
    reset_provider()

    # Proofread ran after the transcribe retry (old code gated on attempt == 0):
    assert result == "corrected"
    assert client.models.generate_content.call_count == 3


def test_ocr_prompts_are_single_source_of_truth():  # #31
    """Both providers must reference the shared base prompt constants — no
    byte-for-byte duplication that can drift."""
    from providers import gemini, openai_compat
    from providers.base import OCR_PROOFREAD_PROMPT, OCR_TRANSCRIBE_PROMPT

    assert gemini._OCR_TRANSCRIBE_PROMPT is OCR_TRANSCRIBE_PROMPT
    assert gemini._OCR_PROOFREAD_PROMPT is OCR_PROOFREAD_PROMPT
    assert openai_compat._OCR_TRANSCRIBE_PROMPT is OCR_TRANSCRIBE_PROMPT
    assert openai_compat._OCR_PROOFREAD_PROMPT is OCR_PROOFREAD_PROMPT
