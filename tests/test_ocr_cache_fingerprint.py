"""An OCR cache entry is only valid for the configuration that produced it.

The key was the page's rendered image hash and nothing else. Point the service
at a better vision model, re-parse with `force_ocr=true`, and the old text came
straight back out of `.cache/` — the new model was never called. Same for
turning proofreading on, and same for a failover to a different vendor.

`force_ocr` keeps its meaning (which pages get routed to the LLM, not "ignore
the cache"). What changes is that a cache entry now knows which engine, model,
prompt and proofread setting produced it, so a different configuration is a
miss rather than a silent hit.
"""

import pytest


def test_the_fingerprint_changes_with_the_model(monkeypatch) -> None:
    from mantisfetch_docreader.ocr import engines

    class _P:
        def __init__(self, model):
            self._model = model

        def ocr_fingerprint(self):
            return f"openai-compat/{self._model}/proofread=True"

    monkeypatch.setattr(
        engines, "_ocr_provider_fingerprint", lambda: _P("model-a").ocr_fingerprint()
    )
    a = engines.llm_ocr_cache_key(proofread=None)
    monkeypatch.setattr(
        engines, "_ocr_provider_fingerprint", lambda: _P("model-b").ocr_fingerprint()
    )
    b = engines.llm_ocr_cache_key(proofread=None)

    assert a != b


def test_the_fingerprint_changes_with_proofreading(monkeypatch) -> None:
    from mantisfetch_docreader.ocr import engines

    monkeypatch.setattr(engines, "_ocr_provider_fingerprint", lambda: "vendor/model")
    assert engines.llm_ocr_cache_key(proofread=True) != engines.llm_ocr_cache_key(proofread=False)


def test_the_fingerprint_changes_with_the_prompt(monkeypatch) -> None:
    """A prompt edit changes what the same model returns for the same image."""
    from mantisfetch_docreader.ocr import engines

    import providers.base as base

    monkeypatch.setattr(engines, "_ocr_provider_fingerprint", lambda: "vendor/model")
    before = engines.llm_ocr_cache_key(proofread=None)
    monkeypatch.setattr(base, "OCR_TRANSCRIBE_PROMPT", "Transcribe differently.")
    after = engines.llm_ocr_cache_key(proofread=None)

    assert before != after


def test_the_same_configuration_still_hits(monkeypatch) -> None:
    """The cache has to stay a cache: identical configuration, identical key."""
    from mantisfetch_docreader.ocr import engines

    monkeypatch.setattr(engines, "_ocr_provider_fingerprint", lambda: "vendor/model")
    assert engines.llm_ocr_cache_key(proofread=None) == engines.llm_ocr_cache_key(proofread=None)


def test_the_key_is_a_safe_filename_component(monkeypatch) -> None:
    """It goes into a path. A model name with a slash must not become a
    directory."""
    from mantisfetch_docreader.ocr import engines

    monkeypatch.setattr(engines, "_ocr_provider_fingerprint", lambda: "zhipu/glm-4.6v:free")
    key = engines.llm_ocr_cache_key(proofread=None)

    assert "/" not in key and ":" not in key


def test_a_provider_reports_its_ocr_identity() -> None:
    """The fingerprint has to come from the provider — the model is resolved
    there, from env, vendor profile and per-role overrides."""
    import providers.openai_compat as oc

    provider = oc.OpenAICompatProvider.__new__(oc.OpenAICompatProvider)
    provider._ocr_model = "glm-4.6v"
    provider._ocr_proofread = True
    provider._vendor = type("V", (), {"name": "zhipu"})()

    fingerprint = provider.ocr_fingerprint()
    assert "glm-4.6v" in fingerprint
    assert "zhipu" in fingerprint


def test_a_failover_pair_is_not_the_same_as_either_half() -> None:
    """A result may have come from either provider, so a cache entry written
    under a pair is only valid for that pair."""
    from providers.failover import FailoverProvider

    class _P:
        def __init__(self, name):
            self.name = name

        def ocr_fingerprint(self):
            return self.name

        def summarize(self, *a, **kw):
            return ""

        def ocr(self, *a, **kw):
            return ""

    pair = FailoverProvider(_P("primary"), _P("fallback"), role="ocr")
    assert pair.ocr_fingerprint() not in ("primary", "fallback")
    assert "primary" in pair.ocr_fingerprint() and "fallback" in pair.ocr_fingerprint()


# ── and it has to hold through a real parse ──────────────────────────────────────
@pytest.fixture()
def scanned_pdf(tmp_path):
    """A page with no text layer, i.e. what a scan looks like."""
    fitz = pytest.importorskip("fitz")

    path = tmp_path / "scan.pdf"
    doc = fitz.open()
    doc.new_page()
    doc.save(str(path))
    doc.close()
    return path


def test_a_new_model_is_actually_called_on_a_cached_page(scanned_pdf, tmp_path, monkeypatch):
    """The report's repro. Two parses of the same page with the same cache
    directory: the second one is configured with a different model and used to
    get the first model's text back without ever calling it."""
    import mantisfetch_docreader as dr
    from mantisfetch_docreader.ocr import engines

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    calls = {"a": 0, "b": 0}

    def _model_a(img_bytes, page_num, proofread=True):
        calls["a"] += 1
        return "TEXT FROM MODEL A"

    def _model_b(img_bytes, page_num, proofread=True):
        calls["b"] += 1
        return "TEXT FROM MODEL B"

    monkeypatch.setattr(engines, "_ocr_provider_fingerprint", lambda: "vendor/model-a")
    monkeypatch.setattr(dr, "gemini_ocr", _model_a)
    first = dr.parse_pdf(scanned_pdf, force_ocr=True, concurrency=1, cache_dir=cache_dir)
    assert "MODEL A" in first.pages[0].text

    monkeypatch.setattr(engines, "_ocr_provider_fingerprint", lambda: "vendor/model-b")
    monkeypatch.setattr(dr, "gemini_ocr", _model_b)
    second = dr.parse_pdf(scanned_pdf, force_ocr=True, concurrency=1, cache_dir=cache_dir)

    assert calls["b"] == 1, "the new model was never called"
    assert "MODEL B" in second.pages[0].text


def test_the_same_model_still_reads_its_own_cache(scanned_pdf, tmp_path, monkeypatch):
    """The cache has to stay a cache — this is the expensive call it exists to
    avoid."""
    import mantisfetch_docreader as dr
    from mantisfetch_docreader.ocr import engines

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    calls = {"n": 0}

    def _model(img_bytes, page_num, proofread=True):
        calls["n"] += 1
        return "TEXT FROM THE ONE MODEL"

    monkeypatch.setattr(engines, "_ocr_provider_fingerprint", lambda: "vendor/model-a")
    monkeypatch.setattr(dr, "gemini_ocr", _model)

    dr.parse_pdf(scanned_pdf, force_ocr=True, concurrency=1, cache_dir=cache_dir)
    second = dr.parse_pdf(scanned_pdf, force_ocr=True, concurrency=1, cache_dir=cache_dir)

    assert calls["n"] == 1, "the second parse re-ran OCR it had already paid for"
    assert "THE ONE MODEL" in second.pages[0].text
