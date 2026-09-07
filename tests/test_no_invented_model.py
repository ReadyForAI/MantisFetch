"""No model name is written into the code, and an unset one says so at boot.

`gemini-2.5-flash` shipped as the built-in default. Google later stopped
serving it to new keys, so a fresh deployment that set an API key and nothing
else got a 404 on its first document — several layers from the cause, and with
nothing in the code that could know it had gone stale. Six more model names sat
in the vendor profiles with the same shelf life.

The rest of this package already refused to guess: the openai-compatible path
raises when no model resolves, the dual-slot path requires `<vendor>/<model>`,
and an unknown vendor raises rather than defaulting to OpenAI. This makes the
last two exceptions consistent with that, and surfaces the gap at startup
instead of on the first document.
"""

import pytest


def test_no_model_name_is_written_into_the_code() -> None:
    """The property, not an example of it: a name here is only correct on the
    day it is written."""
    from providers.vendor_profiles import _VENDOR_PROFILES

    named = {
        vendor: (p.default_text_model, p.default_ocr_model)
        for vendor, p in _VENDOR_PROFILES.items()
        if p.default_text_model or p.default_ocr_model
    }
    assert not named, f"vendor profiles are naming models again: {named}"


def test_a_profile_still_knows_how_to_reach_its_vendor() -> None:
    """What a profile is for survives: the endpoint and the protocol quirks."""
    from providers.vendor_profiles import _VENDOR_PROFILES

    for vendor, profile in _VENDOR_PROFILES.items():
        assert profile.base_url.startswith("https://"), vendor
        assert profile.image_input_mode


def test_gemini_without_a_model_names_the_key(monkeypatch) -> None:
    from providers.gemini import GeminiProvider

    monkeypatch.delenv("MANTISFETCH_LLM_MODEL", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    with pytest.raises(RuntimeError, match="MANTISFETCH_LLM_MODEL"):
        GeminiProvider()


def test_the_dual_slot_path_already_required_one(monkeypatch) -> None:
    """Unchanged, and worth pinning: a slot spec without a model was already an
    error, which is why this change is only about the legacy path."""
    from providers import _build_slot_provider

    with pytest.raises(RuntimeError, match="must be '<vendor>/<model>'"):
        _build_slot_provider("zhipu", {})


# ── said at boot, not on the first document ──────────────────────────────────────
def test_startup_reports_a_role_that_cannot_run(monkeypatch, caplog) -> None:
    import mantisfetch_server as server
    import providers

    _legacy_single_provider(monkeypatch)
    providers.reset_provider()

    with caplog.at_level("WARNING"):
        server._warn_unconfigured_llm()

    assert "MANTISFETCH_LLM_MODEL" in caplog.text
    assert "not usable" in caplog.text


def test_startup_says_nothing_when_the_roles_resolve(monkeypatch, caplog) -> None:
    import providers

    _legacy_single_provider(monkeypatch)
    monkeypatch.setenv("MANTISFETCH_LLM_MODEL", "test-model")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    providers.reset_provider()

    import mantisfetch_server as server

    with caplog.at_level("WARNING"):
        server._warn_unconfigured_llm()

    assert "not usable" not in caplog.text
    providers.reset_provider()


def test_an_unconfigured_role_does_not_stop_the_service(client) -> None:
    """Parsing, capture and local OCR need no LLM at all, and a deployment using
    only those has to keep working — so this is a warning, not a refusal to
    start. The 404-on-first-document failure it replaces was worse only because
    it was silent until then."""
    assert client.get("/health").status_code == 200
    resp = client.post(
        "/doc/parse",
        files={"file": ("p.html", b"<h1>T</h1><p>Body worth keeping.</p>", "text/html")},
        data={"summary_mode": "off", "generate_summary": "false"},
    )
    assert resp.status_code == 200


def _legacy_single_provider(monkeypatch) -> None:
    """Pin the legacy single-provider path.

    magika loads the developer's .env at import time, so the dual-slot keys can
    be present here and would put the process in per-role mode — a different
    resolution path from the one under test. The same .env can also select the
    openai-compatible backend, which validates its key in ``__init__``; these
    tests are about the gemini path, so pin it.
    """
    monkeypatch.setenv("MANTISFETCH_LLM_PROVIDER", "gemini")
    for key in (
        "MANTISFETCH_LLM_DEFAULT",
        "MANTISFETCH_LLM_EXTRA",
        "MANTISFETCH_SUM_MODEL_DEFAULT",
        "MANTISFETCH_SUM_MODEL_FALLBACK",
        "MANTISFETCH_OCR_MODEL_DEFAULT",
        "MANTISFETCH_OCR_MODEL_FALLBACK",
        "MANTISFETCH_OCR_MODEL",
        "MANTISFETCH_LLM_MODEL",
        "MANTISFETCH_LLM_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)


def test_health_says_which_model_each_role_resolved_to(client, monkeypatch) -> None:
    """ "Summaries are off" and "summaries are broken" look identical from the
    outside otherwise."""
    import providers

    _legacy_single_provider(monkeypatch)
    monkeypatch.setenv("MANTISFETCH_LLM_MODEL", "some-model")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    providers.reset_provider()

    llm = client.get("/health").json()["llm"]
    assert llm["summary"] == "some-model"
    assert llm["ocr"] == "some-model"
    providers.reset_provider()


def test_health_says_why_a_role_cannot_run(client, monkeypatch) -> None:
    import providers

    _legacy_single_provider(monkeypatch)
    providers.reset_provider()

    llm = client.get("/health").json()["llm"]
    assert llm["summary"].startswith("unconfigured:")
    assert "MANTISFETCH_LLM_MODEL" in llm["summary"]
    providers.reset_provider()


# ── the diagnostic has to be right about the configurations we support ──────────


def _dual_slot(monkeypatch, **extra: str) -> None:
    """Two gemini slots with per-role models, i.e. credential scheme C."""
    _legacy_single_provider(monkeypatch)
    monkeypatch.setenv("MANTISFETCH_LLM_DEFAULT", "gemini")
    monkeypatch.setenv("MANTISFETCH_LLM_DEFAULT_API_KEY", "test-key")
    monkeypatch.setenv("MANTISFETCH_SUM_MODEL_DEFAULT", "gemini/primary-model")
    monkeypatch.setenv("MANTISFETCH_OCR_MODEL_DEFAULT", "gemini/vision-model")
    for key, value in extra.items():
        monkeypatch.setenv(key, value)


def test_health_names_both_models_when_a_role_can_fail_over(client, monkeypatch) -> None:
    """A configured fallback wraps the role in FailoverProvider, which holds no
    ``_model`` of its own — so reading the attribute off the provider answered
    with the wrapper's class name and told the operator nothing."""
    import providers

    _dual_slot(
        monkeypatch,
        MANTISFETCH_SUM_MODEL_FALLBACK="gemini/backup-model",
        MANTISFETCH_OCR_MODEL_FALLBACK="gemini/vision-backup",
    )
    providers.reset_provider()

    llm = client.get("/health").json()["llm"]
    assert llm["summary"] == "primary-model -> backup-model"
    assert llm["ocr"] == "vision-model -> vision-backup"
    providers.reset_provider()


def test_a_failover_pair_survives_one_broken_half(monkeypatch) -> None:
    """Degraded is not unconfigured: the call still lands. It is not silent
    either — the half that cannot run is named."""
    from providers.failover import FailoverProvider
    from providers.gemini import GeminiProvider

    # Keys pinned per provider, not via the environment: a provider with no
    # explicit key reads the environment at check time, and this test is about
    # one half being broken while the other is not.
    healthy = GeminiProvider(api_key="test-key", model="primary-model")
    broken = GeminiProvider(api_key="", model="backup-model")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    pair = FailoverProvider(healthy, broken, role="summary")
    pair.check_configuration()  # does not raise
    assert "primary-model" in pair.describe_model("summary")
    assert "GEMINI_API_KEY" in pair.describe_model("summary")

    both_broken = FailoverProvider(broken, broken, role="summary")
    with pytest.raises(RuntimeError, match="primary:.*fallback:"):
        both_broken.check_configuration()


def test_a_missing_key_is_not_a_configured_role(client, monkeypatch) -> None:
    """A model resolves without proving a key exists — every backend opens its
    client lazily, so construction alone said "healthy" for a role that would
    401 on the first document."""
    import providers

    _legacy_single_provider(monkeypatch)
    monkeypatch.setenv("MANTISFETCH_LLM_MODEL", "some-model")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    providers.reset_provider()

    llm = client.get("/health").json()["llm"]
    assert llm["summary"].startswith("unconfigured:")
    assert "GEMINI_API_KEY" in llm["summary"]
    providers.reset_provider()


def test_startup_reports_a_missing_key_too(monkeypatch, caplog) -> None:
    import logging

    import providers

    _legacy_single_provider(monkeypatch)
    monkeypatch.setenv("MANTISFETCH_LLM_MODEL", "some-model")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    providers.reset_provider()

    import mantisfetch_server

    with caplog.at_level(logging.WARNING):
        mantisfetch_server._warn_unconfigured_llm()
    assert "GEMINI_API_KEY" in caplog.text
    providers.reset_provider()


def test_the_sentinel_wrapper_delegates_both_hooks(monkeypatch) -> None:
    """``LLMProvider`` gives every subclass a default for these, and
    ``__getattr__`` only fires for attributes the wrapper does *not* have — so a
    wrapper that forgets to delegate answers with its own class name and looks
    fine. That is exactly how ``ocr_fingerprint`` was quietly neutered once."""
    from providers.gemini import GeminiProvider
    from providers.sentinel import SentinelBoundary

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    wrapped = SentinelBoundary(GeminiProvider(model="text-model", ocr_model="vision-model"))

    assert wrapped.describe_model("summary") == "text-model"
    assert wrapped.describe_model("ocr") == "vision-model"

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        wrapped.check_configuration()
