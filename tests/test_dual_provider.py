"""Dual-slot per-role provider selection + failover.

Covers the MANTISFETCH_LLM_DEFAULT/_EXTRA slot scheme: two-segment
``<vendor>/<model>`` routing, per-role primary/fallback, and sentinel-triggered
failover. Legacy single-provider behaviour is exercised in test_providers.py.
"""

import sys
from unittest.mock import MagicMock, patch

import pytest

from providers import get_provider, reset_provider

_PROVIDER_ENV = (
    "MANTISFETCH_LLM_PROVIDER",
    "MANTISFETCH_LLM_VENDOR",
    "MANTISFETCH_LLM_API_KEY",
    "MANTISFETCH_LLM_BASE_URL",
    "MANTISFETCH_LLM_MODEL",
    "MANTISFETCH_OCR_MODEL",
    "MANTISFETCH_OCR_IMAGE_INPUT_MODE",
    "MANTISFETCH_OCR_EXTRA_BODY_JSON",
    "MANTISFETCH_LLM_EXTRA_BODY_JSON",
    "MANTISFETCH_OCR_PROOFREAD",
    "MANTISFETCH_LLM_DEFAULT",
    "MANTISFETCH_LLM_DEFAULT_BASE_URL",
    "MANTISFETCH_LLM_DEFAULT_API_KEY",
    "MANTISFETCH_LLM_EXTRA",
    "MANTISFETCH_LLM_EXTRA_BASE_URL",
    "MANTISFETCH_LLM_EXTRA_API_KEY",
    "MANTISFETCH_SUM_MODEL_DEFAULT",
    "MANTISFETCH_SUM_MODEL_FALLBACK",
    "MANTISFETCH_OCR_MODEL_DEFAULT",
    "MANTISFETCH_OCR_MODEL_FALLBACK",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Start each test from a blank provider env (a loaded .env must not leak in)."""
    for name in _PROVIDER_ENV:
        monkeypatch.delenv(name, raising=False)
    reset_provider()
    yield
    reset_provider()


def _dual(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)


def _mock_openai():
    mock_openai = MagicMock()
    mock_openai.OpenAI.return_value = MagicMock()
    return patch.dict(sys.modules, {"openai": mock_openai})


# ── Routing: role → slot ──────────────────────────────────────────────────────

def test_roles_route_to_different_slots(monkeypatch):
    """Summary and OCR resolve to different vendors/credentials by model prefix."""
    _dual(
        monkeypatch,
        MANTISFETCH_LLM_DEFAULT="zhipu",
        MANTISFETCH_LLM_DEFAULT_BASE_URL="https://zhipu.example/v4",
        MANTISFETCH_LLM_DEFAULT_API_KEY="zk",
        MANTISFETCH_LLM_EXTRA="deepseek",
        MANTISFETCH_LLM_EXTRA_BASE_URL="https://ds.example/v1",
        MANTISFETCH_LLM_EXTRA_API_KEY="dk",
        MANTISFETCH_SUM_MODEL_DEFAULT="deepseek/deepseek-chat",
        MANTISFETCH_OCR_MODEL_DEFAULT="zhipu/glm-4.6v",
    )
    with _mock_openai():
        sum_p = get_provider("summary")
        ocr_p = get_provider("ocr")

    assert sum_p._base_url == "https://ds.example/v1"
    assert sum_p._model == "deepseek-chat"
    assert sum_p._api_key == "dk"

    assert ocr_p._base_url == "https://zhipu.example/v4"
    assert ocr_p._ocr_model == "glm-4.6v"
    assert ocr_p._api_key == "zk"


def test_slot_base_url_falls_back_to_vendor_profile(monkeypatch):
    """An empty slot base_url uses the registered vendor profile default."""
    _dual(
        monkeypatch,
        MANTISFETCH_LLM_DEFAULT="zhipu",
        MANTISFETCH_LLM_DEFAULT_API_KEY="zk",  # no base_url → profile default
        MANTISFETCH_SUM_MODEL_DEFAULT="zhipu/glm-5.1",
    )
    with _mock_openai():
        p = get_provider("summary")
    assert p._base_url == "https://open.bigmodel.cn/api/paas/v4"


def test_gemini_prefix_routes_to_gemini_provider(monkeypatch):
    _dual(
        monkeypatch,
        MANTISFETCH_LLM_DEFAULT="gemini",
        MANTISFETCH_LLM_DEFAULT_API_KEY="gk",
        MANTISFETCH_SUM_MODEL_DEFAULT="gemini/gemini-2.5-pro",
    )
    from providers.gemini import GeminiProvider

    p = get_provider("summary")
    assert isinstance(p, GeminiProvider)
    assert p._model == "gemini-2.5-pro"
    assert p._api_key_override == "gk"


def test_unregistered_vendor_works_with_slot_base_url(monkeypatch):
    """Any OpenAI-compatible endpoint is usable config-only via its slot base_url."""
    _dual(
        monkeypatch,
        MANTISFETCH_LLM_DEFAULT="siliconflow",
        MANTISFETCH_LLM_DEFAULT_BASE_URL="https://api.siliconflow.cn/v1",
        MANTISFETCH_LLM_DEFAULT_API_KEY="sk",
        MANTISFETCH_SUM_MODEL_DEFAULT="siliconflow/some-model",
    )
    with _mock_openai():
        p = get_provider("summary")
    assert p._base_url == "https://api.siliconflow.cn/v1"
    assert p._model == "some-model"


# ── Failover logic (FailoverProvider in isolation) ────────────────────────────

class _StubProvider:
    def __init__(self, out):
        self.out = out
        self.calls = 0

    def summarize(self, text, prompt, max_retries=2):
        self.calls += 1
        return self.out

    def ocr(self, image_bytes, page_num, proofread=None):
        self.calls += 1
        return self.out


def test_failover_summarize_uses_fallback_on_sentinel():
    from providers.failover import FailoverProvider

    primary = _StubProvider("[summary generation failed]")
    fallback = _StubProvider("good summary")
    fp = FailoverProvider(primary, fallback, role="summary")
    assert fp.summarize("t", "p") == "good summary"
    assert primary.calls == 1
    assert fallback.calls == 1


def test_failover_summarize_skips_fallback_on_success():
    from providers.failover import FailoverProvider

    primary = _StubProvider("ok summary")
    fallback = _StubProvider("unused")
    fp = FailoverProvider(primary, fallback, role="summary")
    assert fp.summarize("t", "p") == "ok summary"
    assert fallback.calls == 0


def test_failover_ocr_uses_fallback_on_sentinel():
    from providers.failover import FailoverProvider

    primary = _StubProvider("[OCR failed for page 3]")
    fallback = _StubProvider("real page text")
    fp = FailoverProvider(primary, fallback, role="ocr")
    assert fp.ocr(b"\x89PNG", 3) == "real page text"
    assert fallback.calls == 1


def test_failover_ocr_skips_fallback_on_success():
    from providers.failover import FailoverProvider

    primary = _StubProvider("page text")
    fallback = _StubProvider("unused")
    fp = FailoverProvider(primary, fallback, role="ocr")
    assert fp.ocr(b"\x89PNG", 3) == "page text"
    assert fallback.calls == 0


def test_get_provider_wires_failover_when_fallback_set(monkeypatch):
    _dual(
        monkeypatch,
        MANTISFETCH_LLM_DEFAULT="zhipu",
        MANTISFETCH_LLM_DEFAULT_API_KEY="zk",
        MANTISFETCH_LLM_EXTRA="deepseek",
        MANTISFETCH_LLM_EXTRA_BASE_URL="https://ds.example/v1",
        MANTISFETCH_LLM_EXTRA_API_KEY="dk",
        MANTISFETCH_SUM_MODEL_DEFAULT="deepseek/deepseek-chat",
        MANTISFETCH_SUM_MODEL_FALLBACK="zhipu/glm-4.6",
    )
    from providers.failover import FailoverProvider

    with _mock_openai():
        p = get_provider("summary")
    assert isinstance(p, FailoverProvider)


# ── Validation / degradation ──────────────────────────────────────────────────

def test_broken_fallback_degrades_to_primary(monkeypatch):
    """A fallback referencing an unconfigured slot must not break the primary."""
    _dual(
        monkeypatch,
        MANTISFETCH_LLM_DEFAULT="zhipu",
        MANTISFETCH_LLM_DEFAULT_API_KEY="zk",
        MANTISFETCH_SUM_MODEL_DEFAULT="zhipu/glm-5.1",
        MANTISFETCH_SUM_MODEL_FALLBACK="bogus/x",  # no such slot
    )
    from providers.openai_compat import OpenAICompatProvider

    with _mock_openai():
        p = get_provider("summary")
    assert isinstance(p, OpenAICompatProvider)  # bare primary, no failover wrapper


def test_unknown_vendor_prefix_raises(monkeypatch):
    _dual(
        monkeypatch,
        MANTISFETCH_LLM_DEFAULT="zhipu",
        MANTISFETCH_LLM_DEFAULT_API_KEY="zk",
        MANTISFETCH_SUM_MODEL_DEFAULT="mystery/x",
    )
    with pytest.raises(RuntimeError, match="not a configured slot"):
        get_provider("summary")


def test_model_spec_without_slash_raises(monkeypatch):
    _dual(
        monkeypatch,
        MANTISFETCH_LLM_DEFAULT="zhipu",
        MANTISFETCH_LLM_DEFAULT_API_KEY="zk",
        MANTISFETCH_SUM_MODEL_DEFAULT="glm-4.6",  # missing vendor/
    )
    with pytest.raises(RuntimeError, match="<vendor>/<model>"):
        get_provider("summary")


def test_missing_primary_model_raises(monkeypatch):
    _dual(
        monkeypatch,
        MANTISFETCH_LLM_DEFAULT="zhipu",
        MANTISFETCH_LLM_DEFAULT_API_KEY="zk",
        # no MANTISFETCH_SUM_MODEL_DEFAULT
    )
    with pytest.raises(RuntimeError, match="MANTISFETCH_SUM_MODEL_DEFAULT is required"):
        get_provider("summary")


def test_openai_slot_without_api_key_raises(monkeypatch):
    _dual(
        monkeypatch,
        MANTISFETCH_LLM_DEFAULT="zhipu",  # no API key
        MANTISFETCH_SUM_MODEL_DEFAULT="zhipu/glm-5.1",
    )
    with pytest.raises(RuntimeError, match="MANTISFETCH_LLM_DEFAULT_API_KEY"):
        get_provider("summary")


def test_invalid_role_raises():
    with pytest.raises(ValueError, match="role must be one of"):
        get_provider("translate")


# ── Legacy fallthrough + caching ──────────────────────────────────────────────

def test_legacy_mode_when_default_slot_absent(monkeypatch):
    """No MANTISFETCH_LLM_DEFAULT → legacy single-provider path (gemini default)."""
    from providers.gemini import GeminiProvider

    p = get_provider("ocr")  # role ignored in legacy mode
    assert isinstance(p, GeminiProvider)


def test_reset_clears_per_role_cache(monkeypatch):
    _dual(
        monkeypatch,
        MANTISFETCH_LLM_DEFAULT="zhipu",
        MANTISFETCH_LLM_DEFAULT_API_KEY="zk",
        MANTISFETCH_SUM_MODEL_DEFAULT="zhipu/glm-5.1",
    )
    with _mock_openai():
        p1 = get_provider("summary")
        p2 = get_provider("summary")
        assert p1 is p2
        reset_provider()
        p3 = get_provider("summary")
    assert p1 is not p3
