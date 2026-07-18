"""Typed LLM provider failures for precise failover.

Concrete providers raise these after exhausting their own retries (instead of
only collapsing every failure into a sentinel string). FailoverProvider then
retries the fallback only for *retryable* errors (rate limit / unavailable),
and does not spend a second paid call on permanent rejections (4xx / policy).

Callers that still expect the historical sentinel strings use
``SentinelBoundary`` (wired by ``get_provider``) which folds remaining
``ProviderError`` values back into ``[summary generation failed]`` /
``[OCR failed for page N]``.
"""

from __future__ import annotations


class ProviderError(Exception):
    """Base class for classified provider failures."""

    retryable: bool = True

    def __init__(self, message: str = "", *, retryable: bool | None = None) -> None:
        super().__init__(message)
        if retryable is not None:
            self.retryable = retryable


class ProviderRateLimited(ProviderError):
    """HTTP 429 / quota exhaustion — safe to try another vendor."""

    retryable = True


class ProviderUnavailable(ProviderError):
    """Timeouts, connection errors, 5xx — safe to try another vendor."""

    retryable = True


class ProviderRejected(ProviderError):
    """HTTP 4xx (except 429) / content policy — same input will fail on peers too."""

    retryable = False


def classify_provider_error(exc: BaseException) -> ProviderError:
    """Map an SDK/transport exception to a typed provider error.

    Status codes are read from common OpenAI-SDK / httpx attributes when present.
    Unknown failures default to *retryable* (``ProviderUnavailable``) so a
    flaky primary still fails over.
    """
    if isinstance(exc, ProviderError):
        return exc

    status = _status_code(exc)
    msg = str(exc) or type(exc).__name__

    if status == 429:
        return ProviderRateLimited(msg)
    if status is not None and 400 <= status < 500:
        return ProviderRejected(msg)
    if status is not None and status >= 500:
        return ProviderUnavailable(msg)

    name = type(exc).__name__.lower()
    text = msg.lower()
    if any(
        token in name or token in text
        for token in (
            "timeout",
            "timed out",
            "connection",
            "connecterror",
            "apiconnection",
            "unavailable",
            "temporarily",
        )
    ):
        return ProviderUnavailable(msg)

    return ProviderUnavailable(msg)


def _status_code(exc: BaseException) -> int | None:
    for attr in ("status_code", "status"):
        val = getattr(exc, attr, None)
        if isinstance(val, int):
            return val
    response = getattr(exc, "response", None)
    if response is not None:
        val = getattr(response, "status_code", None)
        if isinstance(val, int):
            return val
    return None
