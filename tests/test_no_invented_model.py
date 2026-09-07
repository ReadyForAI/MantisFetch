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
    resolution path from the one under test.
    """
    for key in (
        "MANTISFETCH_LLM_DEFAULT",
        "MANTISFETCH_LLM_EXTRA",
        "MANTISFETCH_SUM_MODEL_DEFAULT",
        "MANTISFETCH_SUM_MODEL_FALLBACK",
        "MANTISFETCH_OCR_MODEL_DEFAULT",
        "MANTISFETCH_OCR_MODEL_FALLBACK",
        "MANTISFETCH_OCR_MODEL",
        "MANTISFETCH_LLM_MODEL",
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
