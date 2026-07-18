"""Fold typed provider errors into the historical failure-sentinel strings.

Keeps the rest of the pipeline (summaries, OCR, tests) on the sentinel contract
while concrete providers and FailoverProvider speak in typed exceptions.
"""

from __future__ import annotations

import logging

from providers.base import LLMProvider
from providers.errors import ProviderError

logger = logging.getLogger(__name__)


class SentinelBoundary(LLMProvider):
    """Adapter: ``ProviderError`` → ``[summary generation failed]`` / OCR sentinel."""

    def __init__(self, inner: LLMProvider) -> None:
        self._inner = inner

    def __getattr__(self, name: str):
        # Proxy provider-specific attrs (``_base_url``, ``_model``, …) for tests
        # and diagnostics without re-declaring every field.
        return getattr(self._inner, name)

    def summarize(self, text: str, prompt: str, max_retries: int = 2) -> str:
        try:
            return self._inner.summarize(text, prompt, max_retries=max_retries)
        except ProviderError as exc:
            logger.error("summary provider failed (%s): %s", type(exc).__name__, exc)
            return "[summary generation failed]"

    def ocr(self, image_bytes: bytes, page_num: int, proofread: bool | None = None) -> str:
        try:
            return self._inner.ocr(image_bytes, page_num, proofread=proofread)
        except ProviderError as exc:
            logger.warning(
                "OCR provider failed for page %d (%s): %s",
                page_num,
                type(exc).__name__,
                exc,
            )
            return f"[OCR failed for page {page_num}]"
