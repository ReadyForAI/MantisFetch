"""4xx does not fail over, and does not get sent three times either.

Two rules the project already states, neither of which the code kept:

  - "a 4xx is the caller's problem, so do not fail over to a peer" — but the
    classifier read `status_code` / `status`, and the installed Google SDK puts
    the HTTP code in `.code` with a *string* in `.status`. A Gemini 400 was
    therefore unclassified, defaulted to retryable, and failed over.
  - "non-retryable means non-retryable" — but the retry loops caught every
    exception alike, so a content-policy refusal or a bad argument was re-sent
    on the same client until the attempts ran out.

The tests use the real SDK exception objects rather than a stub with a
`status_code` attribute: a stub is what hid the first bug (the existing
classification test passes against both the broken and the fixed classifier).
"""

import pytest

from providers.errors import (
    ProviderRateLimited,
    ProviderRejected,
    ProviderUnavailable,
    classify_provider_error,
)


def _genai_error(cls_name: str, code: int, status: str):
    errors = pytest.importorskip("google.genai.errors")
    cls = getattr(errors, cls_name)
    return cls(code, {"error": {"message": "boom", "status": status}})


# ── the classifier, against what the SDK actually raises ─────────────────────────
def test_a_real_gemini_4xx_is_a_rejection(monkeypatch) -> None:
    """`.code` holds the HTTP status; `.status` is a string like
    'INVALID_ARGUMENT' and `.status_code` does not exist."""
    exc = _genai_error("ClientError", 400, "INVALID_ARGUMENT")
    assert getattr(exc, "status_code", None) is None
    assert isinstance(exc.status, str)

    result = classify_provider_error(exc)
    assert isinstance(result, ProviderRejected)
    assert result.retryable is False


def test_a_real_gemini_429_is_rate_limiting() -> None:
    result = classify_provider_error(_genai_error("ClientError", 429, "RESOURCE_EXHAUSTED"))
    assert isinstance(result, ProviderRateLimited)


def test_a_real_gemini_5xx_still_fails_over() -> None:
    result = classify_provider_error(_genai_error("ServerError", 503, "UNAVAILABLE"))
    assert isinstance(result, ProviderUnavailable)
    assert result.retryable is True


def test_an_integer_code_on_an_unrelated_exception_is_not_an_http_status() -> None:
    """Reading `.code` off anything that has one would classify an OS error, a
    subprocess exit or a JSON error as an HTTP status."""

    class _NotAnSdkError(Exception):
        code = 404  # e.g. an errno, a return code, anything

    assert isinstance(classify_provider_error(_NotAnSdkError("unrelated")), ProviderUnavailable)


# ── the retry loops ──────────────────────────────────────────────────────────────
class _Boom:
    """Minimal stand-in for the OpenAI client, counting calls."""

    def __init__(self, exc):
        self.calls = 0
        self._exc = exc
        self.chat = self

    @property
    def completions(self):
        return self

    def create(self, **kwargs):
        self.calls += 1
        raise self._exc


def _openai_provider(monkeypatch, exc):
    import providers.openai_compat as oc

    monkeypatch.setattr(oc.time, "sleep", lambda s: None)
    provider = oc.OpenAICompatProvider.__new__(oc.OpenAICompatProvider)
    provider._client = _Boom(exc)
    provider._model = "test-model"
    provider._chat_extra_body = None
    return provider


def test_a_rejection_is_sent_once(monkeypatch) -> None:
    """A refusal or a bad argument fails the same way on the retry, so retrying
    only spends the caller's time and the provider's quota."""
    provider = _openai_provider(monkeypatch, ProviderRejected("model refusal"))

    with pytest.raises(ProviderRejected):
        provider._chat([{"role": "user", "content": "x"}], max_retries=2)

    assert provider._client.calls == 1


def test_a_real_4xx_from_the_sdk_is_also_sent_once(monkeypatch) -> None:
    import httpx2 as httpx

    request = httpx.Request("POST", "https://api.example.com/v1/chat/completions")
    response = httpx.Response(400, request=request, json={"error": {"message": "bad"}})
    provider = _openai_provider(
        monkeypatch, httpx.HTTPStatusError("400", request=request, response=response)
    )

    with pytest.raises(Exception):
        provider._chat([{"role": "user", "content": "x"}], max_retries=2)

    assert provider._client.calls == 1


def test_a_retryable_failure_still_retries(monkeypatch) -> None:
    """The other half: this is a retry loop, and it must stay one."""
    provider = _openai_provider(monkeypatch, TimeoutError("timed out"))

    with pytest.raises(Exception):
        provider._chat([{"role": "user", "content": "x"}], max_retries=2)

    assert provider._client.calls == 3


def test_gemini_does_not_re_send_a_rejected_request(monkeypatch) -> None:
    """Gemini classified only on the last attempt, so a 400 was sent three
    times before anyone looked at it."""
    import providers.gemini as gem

    monkeypatch.setattr(gem.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def _boom(*a, **kw):
        calls["n"] += 1
        raise _genai_error("ClientError", 400, "INVALID_ARGUMENT")

    provider = gem.GeminiProvider.__new__(gem.GeminiProvider)
    provider._init = lambda: None
    provider._model = "gemini-test"
    provider._client = type(
        "C", (), {"models": type("M", (), {"generate_content": staticmethod(_boom)})()}
    )()

    with pytest.raises(ProviderRejected):
        provider.summarize("text", "prompt", max_retries=2)

    assert calls["n"] == 1


# ── "will a retry work" and "will the other vendor work" are two questions ───────
def test_a_retired_model_is_not_the_requests_fault() -> None:
    """404 is this vendor's model list, not the caller's input. Folding it in
    with a malformed request left a configured fallback unused exactly when it
    was needed — the case the second slot exists for."""
    from providers.errors import ProviderUnusable

    result = classify_provider_error(_genai_error("ClientError", 404, "NOT_FOUND"))

    assert isinstance(result, ProviderUnusable)
    assert result.retryable is False, "asking the same vendor again is pointless"
    assert result.failover is True, "asking the other vendor is not"


@pytest.mark.parametrize(("code", "status"), [(401, "UNAUTHENTICATED"), (403, "PERMISSION_DENIED")])
def test_this_deployments_credentials_are_not_the_requests_fault(code, status) -> None:
    from providers.errors import ProviderUnusable

    result = classify_provider_error(_genai_error("ClientError", code, status))
    assert isinstance(result, ProviderUnusable)
    assert (result.retryable, result.failover) == (False, True)


def test_a_malformed_request_still_goes_nowhere() -> None:
    """The other half: a bad request is bad everywhere, and failing it over
    only spends a second vendor's quota to be told the same thing."""
    result = classify_provider_error(_genai_error("ClientError", 400, "INVALID_ARGUMENT"))

    assert isinstance(result, ProviderRejected)
    assert (result.retryable, result.failover) == (False, False)


def test_the_retryable_ones_still_fail_over() -> None:
    for exc, expected in (
        (_genai_error("ClientError", 429, "RESOURCE_EXHAUSTED"), ProviderRateLimited),
        (_genai_error("ServerError", 503, "UNAVAILABLE"), ProviderUnavailable),
    ):
        result = classify_provider_error(exc)
        assert isinstance(result, expected)
        assert result.failover is True


class _Fixed:
    def __init__(self, exc=None, text="ok"):
        self.exc, self.text, self.calls = exc, text, 0

    def summarize(self, text, prompt, max_retries=2):
        self.calls += 1
        if self.exc:
            raise self.exc
        return self.text

    def ocr(self, image_bytes, page_num, proofread=None):
        self.calls += 1
        if self.exc:
            raise self.exc
        return self.text


def test_the_second_slot_is_used_when_the_first_vendor_cannot_serve() -> None:
    from providers.errors import ProviderUnusable
    from providers.failover import FailoverProvider

    primary = _Fixed(ProviderUnusable("404 model retired"))
    fallback = _Fixed(text="summary from the peer")
    pair = FailoverProvider(primary, fallback, role="summary")

    assert pair.summarize("text", "prompt") == "summary from the peer"
    assert (primary.calls, fallback.calls) == (1, 1)


def test_the_second_slot_is_used_for_ocr_too() -> None:
    from providers.errors import ProviderUnusable
    from providers.failover import FailoverProvider

    primary = _Fixed(ProviderUnusable("401 expired key"))
    fallback = _Fixed(text="page text from the peer")
    pair = FailoverProvider(primary, fallback, role="ocr")

    assert pair.ocr(b"png", 1) == "page text from the peer"
    assert (primary.calls, fallback.calls) == (1, 1)


def test_the_second_slot_is_not_spent_on_a_bad_request() -> None:
    from providers.failover import FailoverProvider

    primary = _Fixed(ProviderRejected("400 malformed"))
    fallback = _Fixed(text="never reached")
    pair = FailoverProvider(primary, fallback, role="summary")

    with pytest.raises(ProviderRejected):
        pair.summarize("text", "prompt")
    assert fallback.calls == 0


def test_an_unusable_vendor_is_still_only_called_once(monkeypatch) -> None:
    """Not retryable means not retried, even though it does fail over."""
    provider = _openai_provider(monkeypatch, _genai_error("ClientError", 404, "NOT_FOUND"))

    with pytest.raises(Exception):
        provider._chat([{"role": "user", "content": "x"}], max_retries=2)

    assert provider._client.calls == 1
