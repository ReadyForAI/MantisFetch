"""Availability failover between two concrete providers.

Both concrete providers (Gemini, OpenAI-compat) already retry transient errors
internally and collapse *any* failure to a sentinel string rather than raising
(``[summary generation failed]`` / ``[OCR failed for page N]``). This wrapper
reacts to that sentinel: when the primary returns it, the same call is retried
once against the fallback provider.

Detection is by sentinel because the concrete providers do not surface HTTP
status codes — they are swallowed into the sentinel. Precise "only fail over on
429/5xx" classification would require both providers to re-raise, which they do
not; the sentinel is the single failure signal they expose.
"""

import logging

from providers.base import LLMProvider

logger = logging.getLogger(__name__)


def _summary_failed(text: str | None) -> bool:
    if not text:
        return True
    return text.strip().lower() in {
        "[summary generation failed]",
        "summary generation failed",
    }


def _ocr_failed(text: str | None) -> bool:
    if not text:
        return True
    return text.strip().startswith(("[OCR failed", "[OCR 失败"))


class FailoverProvider(LLMProvider):
    """Try ``primary``; on its failure sentinel, retry once on ``fallback``."""

    def __init__(self, primary: LLMProvider, fallback: LLMProvider, *, role: str) -> None:
        self._primary = primary
        self._fallback = fallback
        self._role = role

    def summarize(self, text: str, prompt: str, max_retries: int = 2) -> str:
        result = self._primary.summarize(text, prompt, max_retries=max_retries)
        if not _summary_failed(result):
            return result
        logger.warning(
            "summary primary provider failed; failing over to the fallback provider"
        )
        return self._fallback.summarize(text, prompt, max_retries=max_retries)

    def ocr(self, image_bytes: bytes, page_num: int, proofread: bool | None = None) -> str:
        result = self._primary.ocr(image_bytes, page_num, proofread=proofread)
        if not _ocr_failed(result):
            return result
        logger.warning(
            "OCR primary provider failed for page %d; failing over to the fallback provider",
            page_num,
        )
        return self._fallback.ocr(image_bytes, page_num, proofread=proofread)
