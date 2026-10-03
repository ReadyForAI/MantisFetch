"""A page cut off at the output limit is not a transcribed page (F08).

OpenAI-compatible OCR checked the finish reason only when the content was
null; Gemini only when there was no text. Non-empty text with `length` /
MAX_TOKENS came back as a success — and the PDF layer cached it, so every
later parse kept serving half a page as if it were the whole of it.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from providers.errors import ProviderTruncated


def _openai(monkeypatch, replies: list[tuple[str | None, str]], *, proofread: bool):
    import providers.openai_compat as oc

    provider = oc.OpenAICompatProvider.__new__(oc.OpenAICompatProvider)
    provider._ocr_model = "vision-test"
    provider._ocr_extra_body = None
    provider._ocr_proofread = proofread
    queue = list(replies)
    monkeypatch.setattr(provider, "_build_ocr_image_part", lambda b: {"type": "image"})
    monkeypatch.setattr(provider, "_chat", lambda *a, **k: queue.pop(0))
    return provider


def test_a_transcription_cut_off_is_raised_not_returned(monkeypatch) -> None:
    """The report's probe: ("Only the first half", "length"), proofread off."""
    provider = _openai(monkeypatch, [("Only the first half", "length")], proofread=False)
    with pytest.raises(ProviderTruncated, match="output limit"):
        provider.ocr(b"png", 3)


def test_a_complete_transcription_still_returns(monkeypatch) -> None:
    provider = _openai(monkeypatch, [("The whole page.", "stop")], proofread=False)
    assert provider.ocr(b"png", 3) == "The whole page."


def test_a_proofread_cut_off_keeps_the_complete_draft(monkeypatch) -> None:
    provider = _openai(
        monkeypatch,
        [("The whole page, as transcribed.", "stop"), ("The whole pa", "length")],
        proofread=True,
    )
    assert provider.ocr(b"png", 3) == "The whole page, as transcribed."


def test_truncation_fails_over_but_is_not_retried() -> None:
    exc = ProviderTruncated("cut off")
    assert exc.failover and not exc.retryable


def _gemini(responses: list[object]):
    import providers.gemini as gem

    provider = gem.GeminiProvider.__new__(gem.GeminiProvider)
    provider._init = lambda: None
    provider._ocr_model = "gemini-test"
    provider._ocr_proofread = len(responses) > 1
    queue = list(responses)
    calls = {"n": 0}

    def generate_content(**kwargs):
        calls["n"] += 1
        return queue.pop(0)

    provider._client = SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))
    return provider, calls


def _png() -> bytes:
    import io

    import PIL.Image

    buf = io.BytesIO()
    PIL.Image.new("RGB", (4, 4)).save(buf, format="PNG")
    return buf.getvalue()


def _response(text: str, finish: str) -> object:
    return SimpleNamespace(text=text, candidates=[SimpleNamespace(finish_reason=finish)])


def test_gemini_text_cut_off_at_max_tokens_is_raised_once() -> None:
    provider, calls = _gemini([_response("Only the first half", "FinishReason.MAX_TOKENS")])
    with pytest.raises(ProviderTruncated):
        provider.ocr(_png(), 2, proofread=False)
    assert calls["n"] == 1, "a truncation was retried at the same budget"


def test_gemini_proofread_cut_off_keeps_the_draft() -> None:
    provider, _ = _gemini(
        [_response("The whole page.", "STOP"), _response("The who", "MAX_TOKENS")]
    )
    assert provider.ocr(_png(), 2, proofread=True) == "The whole page."


def test_the_pipeline_sees_a_failed_page_not_a_short_one(monkeypatch) -> None:
    """Through the sentinel boundary: the page becomes the failure sentinel,
    which the PDF layer neither caches nor counts as transcribed."""
    from mantisfetch_docreader.ocr.engines import _is_ocr_failed_text

    from providers.sentinel import SentinelBoundary

    provider = _openai(monkeypatch, [("Only the first half", "length")], proofread=False)
    text = SentinelBoundary(provider).ocr(b"png", 7)
    assert _is_ocr_failed_text(text), text
