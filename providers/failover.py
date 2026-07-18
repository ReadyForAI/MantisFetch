"""Availability failover between two concrete providers.

Concrete providers raise typed ``ProviderError`` subclasses after retries, or
(for older/stub paths) still return the historical failure sentinel strings.
This wrapper:

* fails over on **retryable** errors (``ProviderRateLimited``,
  ``ProviderUnavailable``, unknown exceptions, and failure sentinels);
* does **not** fail over on ``ProviderRejected`` (4xx / content policy) — the
  same request is unlikely to succeed on another vendor and would waste quota;
* treats blank OCR text as success (not a failure), matching
  ``_is_ocr_failed_text``.

The outer ``SentinelBoundary`` (see ``get_provider``) folds any remaining
typed errors into sentinel strings for the rest of the pipeline.
"""

from __future__ import annotations

import logging

from providers.base import LLMProvider
from providers.errors import ProviderError, ProviderRejected

logger = logging.getLogger(__name__)


def _summary_failed(text: str | None) -> bool:
    if not text:
        return True
    return text.strip().lower() in {
        "[summary generation failed]",
        "summary generation failed",
    }


def _ocr_failed(text: str | None) -> bool:
    # Mirror ocr.engines._is_ocr_failed_text: an empty/blank result is a
    # genuinely blank page, NOT a failure. Only the explicit sentinel fails over
    # — otherwise a blank page would needlessly hit the fallback, which might
    # hallucinate text for it.
    if not text:
        return False
    return text.strip().startswith(("[OCR failed", "[OCR 失败"))


class FailoverProvider(LLMProvider):
    """Try ``primary``; on retryable failure, retry once on ``fallback``."""

    def __init__(self, primary: LLMProvider, fallback: LLMProvider, *, role: str) -> None:
        self._primary = primary
        self._fallback = fallback
        self._role = role

    def summarize(self, text: str, prompt: str, max_retries: int = 2) -> str:
        try:
            result = self._primary.summarize(text, prompt, max_retries=max_retries)
        except ProviderRejected as exc:
            logger.warning(
                "summary primary rejected request (not failing over): %s",
                exc,
            )
            raise
        except ProviderError as exc:
            logger.warning(
                "summary primary provider failed (%s); failing over to the fallback provider",
                type(exc).__name__,
            )
            return self._fallback.summarize(text, prompt, max_retries=max_retries)
        except Exception as exc:
            logger.warning(
                "summary primary provider raised (%s); failing over to the fallback provider",
                exc,
            )
            return self._fallback.summarize(text, prompt, max_retries=max_retries)

        if not _summary_failed(result):
            return result
        logger.warning(
            "summary primary provider failed (sentinel); failing over to the fallback provider"
        )
        return self._fallback.summarize(text, prompt, max_retries=max_retries)

    def ocr(self, image_bytes: bytes, page_num: int, proofread: bool | None = None) -> str:
        try:
            result = self._primary.ocr(image_bytes, page_num, proofread=proofread)
        except ProviderRejected as exc:
            logger.warning(
                "OCR primary rejected page %d (not failing over): %s",
                page_num,
                exc,
            )
            raise
        except ProviderError as exc:
            logger.warning(
                "OCR primary provider failed for page %d (%s); failing over to the fallback provider",
                page_num,
                type(exc).__name__,
            )
            return self._fallback.ocr(image_bytes, page_num, proofread=proofread)
        except Exception as exc:
            logger.warning(
                "OCR primary provider raised for page %d (%s); failing over to the fallback provider",
                page_num,
                exc,
            )
            return self._fallback.ocr(image_bytes, page_num, proofread=proofread)

        if not _ocr_failed(result):
            return result
        logger.warning(
            "OCR primary provider failed for page %d (sentinel); failing over to the fallback provider",
            page_num,
        )
        return self._fallback.ocr(image_bytes, page_num, proofread=proofread)
