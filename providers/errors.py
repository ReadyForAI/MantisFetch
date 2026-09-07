"""Typed LLM provider failures for precise failover.

Concrete providers raise these after exhausting their own retries (instead of
only collapsing every failure into a sentinel string). Two questions get asked
of every failure, and they are not the same question:

  retryable — will the *same call to the same provider* behave differently?
  failover  — might *another provider* succeed where this one did not?

A 429 is both. A malformed request is neither. And a retired model or an
expired key is the pair that used to be missing: asking this vendor again is
pointless, asking the other one is exactly what the second slot is for.

Callers that still expect the historical sentinel strings use
``SentinelBoundary`` (wired by ``get_provider``) which folds remaining
``ProviderError`` values back into ``[summary generation failed]`` /
``[OCR failed for page N]``.
"""

from __future__ import annotations


class ProviderError(Exception):
    """Base class for classified provider failures."""

    retryable: bool = True
    #: Whether the *other* provider is worth trying. Defaults to ``retryable``
    #: because that was the only distinction for a long time; a subclass says
    #: otherwise when the failure is about this vendor rather than the request.
    _failover: bool | None = None

    def __init__(self, message: str = "", *, retryable: bool | None = None) -> None:
        super().__init__(message)
        if retryable is not None:
            self.retryable = retryable

    @property
    def failover(self) -> bool:
        return self.retryable if self._failover is None else self._failover


class ProviderRateLimited(ProviderError):
    """HTTP 429 / quota exhaustion — safe to try another vendor."""

    retryable = True


class ProviderUnavailable(ProviderError):
    """Timeouts, connection errors, 5xx — safe to try another vendor."""

    retryable = True


class ProviderRejected(ProviderError):
    """A bad request — 400/422, content policy. The peer would reject it too."""

    retryable = False
    _failover = False


class ProviderUnusable(ProviderError):
    """This vendor cannot serve the call at all — 401, 403, 404.

    Retrying is pointless: an expired key, a revoked permission and a retired
    model all fail identically on the next attempt. Failing over is not
    pointless, and this is the case the second slot exists for — the peer has
    its own credentials and its own models. Reading "4xx" as "the request is
    bad, so nobody can serve it" folded these in with the malformed ones and
    left a configured fallback unused exactly when it was needed.
    """

    retryable = False
    _failover = True


#: 4xx codes that describe the vendor rather than the request. 401 and 403 are
#: this deployment's credentials for *this* provider; 404 is a model or endpoint
#: this provider no longer serves. None of them says anything about whether the
#: peer can do it.
_VENDOR_SCOPED_STATUS = frozenset({401, 403, 404})


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
    if status in _VENDOR_SCOPED_STATUS:
        return ProviderUnusable(msg)
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


def _genai_status_code(exc: BaseException) -> int | None:
    """The HTTP status of a Google GenAI SDK error, or None if it is not one.

    That SDK does not use the attribute names the others do: the HTTP status is
    ``code``, and ``status`` holds a string like ``INVALID_ARGUMENT``. Without
    this, every Gemini 4xx fell through to the default and was treated as a
    retryable outage — so a bad argument or a content refusal failed over to the
    peer provider, which is exactly what "4xx does not fail over" forbids.

    Matched by type rather than by reading ``code`` off anything that has one:
    an errno, a subprocess return code and a JSON error code are all integers in
    the same range and none of them is an HTTP status.
    """
    try:
        from google.genai.errors import APIError  # noqa: PLC0415 - optional dep
    except Exception:  # pragma: no cover - SDK not installed
        return None
    if not isinstance(exc, APIError):
        return None
    code = getattr(exc, "code", None)
    return code if isinstance(code, int) else None


def _status_code(exc: BaseException) -> int | None:
    sdk_status = _genai_status_code(exc)
    if sdk_status is not None:
        return sdk_status
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
