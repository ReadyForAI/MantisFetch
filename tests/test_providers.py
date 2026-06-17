"""Tests for the Multi-LLM Provider Abstraction (TASK-006)."""

import os
from unittest.mock import MagicMock, patch

import pytest

import providers as providers_module
from providers import get_provider, reset_provider
from providers.base import LLMProvider
from providers.vendor_profiles import get_vendor_profile


@pytest.fixture(autouse=True)
def _reset():
    """Reset the cached provider before every test."""
    reset_provider()
    yield
    reset_provider()


# ── AC-1: abstract base class ──────────────────────────────────────────────────

class TestLLMProviderInterface:
    def test_is_abstract(self):
        """LLMProvider cannot be instantiated directly."""
        with pytest.raises(TypeError):
            LLMProvider()  # type: ignore[abstract]

    def test_concrete_must_implement_both_methods(self):
        """A subclass that skips summarize or ocr raises TypeError."""
        class BadProvider(LLMProvider):
            def summarize(self, text, prompt, max_retries=2):
                return ""
            # ocr not implemented

        with pytest.raises(TypeError):
            BadProvider()

    def test_concrete_provider_satisfies_interface(self):
        """A fully implemented subclass can be instantiated."""
        class GoodProvider(LLMProvider):
            def summarize(self, text, prompt, max_retries=2):
                return "ok"

            def ocr(self, image_bytes, page_num):
                return "text"

        p = GoodProvider()
        assert isinstance(p, LLMProvider)


# ── AC-2: Gemini provider ─────────────────────────────────────────────────────

class TestGeminiProvider:
    def test_get_provider_returns_gemini_by_default(self, monkeypatch):
        """With no env var, get_provider() returns a GeminiProvider."""
        monkeypatch.delenv("MANTISFETCH_LLM_PROVIDER", raising=False)
        p = get_provider()
        assert "gemini" in type(p).__name__.lower()

    def test_gemini_provider_is_llm_provider(self, monkeypatch):
        monkeypatch.delenv("MANTISFETCH_LLM_PROVIDER", raising=False)
        p = get_provider()
        assert isinstance(p, LLMProvider)

    def test_get_provider_is_thread_safe_singleton(self, monkeypatch):
        """C38: concurrent cold get_provider() calls must share one instance."""
        import threading

        monkeypatch.delenv("MANTISFETCH_LLM_PROVIDER", raising=False)
        reset_provider()
        results: list[LLMProvider] = []
        barrier = threading.Barrier(8)

        def grab():
            barrier.wait()  # release all threads onto the cold path at once
            results.append(get_provider())

        threads = [threading.Thread(target=grab) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results) == 8
        assert all(r is results[0] for r in results)

    def test_gemini_summarize_delegates_to_sdk(self, monkeypatch):
        """GeminiProvider.summarize() calls client.models.generate_content."""
        monkeypatch.delenv("MANTISFETCH_LLM_PROVIDER", raising=False)
        monkeypatch.setenv("GEMINI_API_KEY", "fake-key")

        mock_response = MagicMock()
        mock_response.text = "  summary result  "
        mock_client = MagicMock()
        mock_client.models.generate_content.return_value = mock_response

        mock_genai = MagicMock()
        mock_genai.Client.return_value = mock_client

        with patch.dict("sys.modules", {"google": MagicMock(genai=mock_genai), "google.genai": mock_genai}):
            p = get_provider()
            result = p.summarize("some text", "summarise this")

        assert result == "summary result"
        mock_client.models.generate_content.assert_called_once()

    def test_gemini_ocr_delegates_to_sdk(self, monkeypatch):
        """GeminiProvider.ocr() calls client.models.generate_content with an image."""
        monkeypatch.delenv("MANTISFETCH_LLM_PROVIDER", raising=False)
        monkeypatch.setenv("GEMINI_API_KEY", "fake-key")

        mock_response = MagicMock()
        mock_response.text = "extracted text"
        mock_client = MagicMock()
        mock_client.models.generate_content.return_value = mock_response

        mock_genai = MagicMock()
        mock_genai.Client.return_value = mock_client

        # Minimal 1×1 PNG bytes
        import base64
        png_b64 = (
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
            "YPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
        )
        image_bytes = base64.b64decode(png_b64)

        mock_pil_image = MagicMock()
        mock_pil_module = MagicMock()
        mock_pil_module.Image.open.return_value = mock_pil_image

        with patch.dict(
            "sys.modules",
            {
                "google": MagicMock(genai=mock_genai),
                "google.genai": mock_genai,
                "PIL": mock_pil_module,
                "PIL.Image": mock_pil_module.Image,
            },
        ):
            p = get_provider()
            result = p.ocr(image_bytes, page_num=1)

        assert result == "extracted text"
        assert mock_client.models.generate_content.call_count == 2


# ── AC-3: OpenAI-compat provider ──────────────────────────────────────────────

class TestOpenAICompatProvider:
    def test_get_provider_returns_openai_compat(self, monkeypatch):
        monkeypatch.setenv("MANTISFETCH_LLM_PROVIDER", "openai")
        monkeypatch.setenv("MANTISFETCH_LLM_API_KEY", "sk-test")
        p = get_provider()
        assert "openai" in type(p).__name__.lower()

    def test_openai_compat_is_llm_provider(self, monkeypatch):
        monkeypatch.setenv("MANTISFETCH_LLM_PROVIDER", "openai")
        monkeypatch.setenv("MANTISFETCH_LLM_API_KEY", "sk-test")
        p = get_provider()
        assert isinstance(p, LLMProvider)

    def test_openai_compat_missing_api_key_raises(self, monkeypatch):
        monkeypatch.setenv("MANTISFETCH_LLM_PROVIDER", "openai")
        monkeypatch.delenv("MANTISFETCH_LLM_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="MANTISFETCH_LLM_API_KEY"):
            get_provider()

    def test_openai_compat_summarize_calls_openai_sdk(self, monkeypatch):
        """OpenAICompatProvider.summarize() uses the official OpenAI SDK."""
        monkeypatch.setenv("MANTISFETCH_LLM_PROVIDER", "openai")
        monkeypatch.setenv("MANTISFETCH_LLM_API_KEY", "sk-test")
        monkeypatch.setenv("MANTISFETCH_LLM_BASE_URL", "https://api.example.com/v1")
        monkeypatch.setenv("MANTISFETCH_LLM_MODEL", "gpt-test")

        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content="  great summary  "))]
        )
        mock_openai = MagicMock()
        mock_openai.OpenAI.return_value = mock_client

        with patch.dict("sys.modules", {"openai": mock_openai}):
            p = get_provider()
            result = p.summarize("body text", "system prompt")

        assert result == "great summary"
        mock_openai.OpenAI.assert_called_once_with(
            api_key="sk-test",
            base_url="https://api.example.com/v1",
            max_retries=0,
            timeout=120,
        )
        mock_client.chat.completions.create.assert_called_once()
        call_kwargs = mock_client.chat.completions.create.call_args.kwargs
        assert call_kwargs["model"] == "gpt-test"

    def test_openai_compat_uses_vendor_defaults_when_overrides_absent(self, monkeypatch):
        monkeypatch.setenv("MANTISFETCH_LLM_PROVIDER", "openai")
        monkeypatch.setenv("MANTISFETCH_LLM_VENDOR", "zhipu")
        monkeypatch.setenv("MANTISFETCH_LLM_API_KEY", "sk-test")
        monkeypatch.delenv("MANTISFETCH_LLM_BASE_URL", raising=False)
        monkeypatch.delenv("MANTISFETCH_LLM_MODEL", raising=False)
        monkeypatch.delenv("MANTISFETCH_OCR_MODEL", raising=False)

        mock_client = MagicMock()
        mock_openai = MagicMock()
        mock_openai.OpenAI.return_value = mock_client

        with patch.dict("sys.modules", {"openai": mock_openai}):
            p = get_provider()

        assert p._base_url == "https://open.bigmodel.cn/api/paas/v4"
        assert p._model == "glm-5.1"
        assert p._ocr_model == "glm-4.6v"
        mock_openai.OpenAI.assert_called_once_with(
            api_key="sk-test",
            base_url="https://open.bigmodel.cn/api/paas/v4",
            max_retries=0,
            timeout=120,
        )

    def test_openai_compat_ocr_sends_base64_image(self, monkeypatch):
        """OpenAICompatProvider.ocr() encodes image as base64 and sends multipart content."""
        monkeypatch.setenv("MANTISFETCH_LLM_PROVIDER", "openai")
        monkeypatch.setenv("MANTISFETCH_LLM_API_KEY", "sk-test")

        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content="page text"))]
        )
        mock_openai = MagicMock()
        mock_openai.OpenAI.return_value = mock_client

        with patch.dict("sys.modules", {"openai": mock_openai}):
            p = get_provider()
            result = p.ocr(b"\x89PNG", page_num=2)

        assert result == "page text"
        messages = mock_client.chat.completions.create.call_args.kwargs["messages"]
        assert any(
            isinstance(m.get("content"), list) for m in messages
        ), "Expected a multipart (list) content for vision"
        image_part = messages[0]["content"][1]
        assert image_part["image_url"]["url"].startswith("data:image/png;base64,")
        assert mock_client.chat.completions.create.call_count == 2

    def test_openai_compat_ocr_uses_dedicated_ocr_model_when_set(self, monkeypatch):
        monkeypatch.setenv("MANTISFETCH_LLM_PROVIDER", "openai")
        monkeypatch.setenv("MANTISFETCH_LLM_API_KEY", "sk-test")
        monkeypatch.setenv("MANTISFETCH_LLM_MODEL", "glm-5.1")
        monkeypatch.setenv("MANTISFETCH_OCR_MODEL", "glm-4.6v")

        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content="page text"))]
        )
        mock_openai = MagicMock()
        mock_openai.OpenAI.return_value = mock_client

        with patch.dict("sys.modules", {"openai": mock_openai}):
            p = get_provider()
            p.ocr(b"\x89PNG", page_num=2)

        call_kwargs = mock_client.chat.completions.create.call_args.kwargs
        assert call_kwargs["model"] == "glm-4.6v"

    def test_openai_compat_ocr_proofread_can_be_disabled(self, monkeypatch):
        monkeypatch.setenv("MANTISFETCH_LLM_PROVIDER", "openai")
        monkeypatch.setenv("MANTISFETCH_LLM_API_KEY", "sk-test")
        monkeypatch.setenv("MANTISFETCH_OCR_PROOFREAD", "false")

        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content="page text"))]
        )
        mock_openai = MagicMock()
        mock_openai.OpenAI.return_value = mock_client

        with patch.dict("sys.modules", {"openai": mock_openai}):
            p = get_provider()
            p.ocr(b"\x89PNG", page_num=2)

        assert mock_client.chat.completions.create.call_count == 1

    def test_openai_compat_ocr_supports_plain_base64_mode(self, monkeypatch):
        monkeypatch.setenv("MANTISFETCH_LLM_PROVIDER", "openai")
        monkeypatch.setenv("MANTISFETCH_LLM_API_KEY", "sk-test")
        monkeypatch.setenv("MANTISFETCH_OCR_IMAGE_INPUT_MODE", "plain_base64")

        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content="page text"))]
        )
        mock_openai = MagicMock()
        mock_openai.OpenAI.return_value = mock_client

        with patch.dict("sys.modules", {"openai": mock_openai}):
            p = get_provider()
            p.ocr(b"\x89PNG", page_num=2)

        image_part = mock_client.chat.completions.create.call_args.kwargs["messages"][0]["content"][1]
        assert image_part["image_url"]["url"] == "iVBORw=="

    def test_openai_compat_ocr_rejects_remote_url_only_mode(self, monkeypatch):
        monkeypatch.setenv("MANTISFETCH_LLM_PROVIDER", "openai")
        monkeypatch.setenv("MANTISFETCH_LLM_API_KEY", "sk-test")
        monkeypatch.setenv("MANTISFETCH_OCR_IMAGE_INPUT_MODE", "remote_url_only")

        mock_client = MagicMock()
        mock_openai = MagicMock()
        mock_openai.OpenAI.return_value = mock_client

        with patch.dict("sys.modules", {"openai": mock_openai}):
            p = get_provider()
            with pytest.raises(RuntimeError, match="remote_url_only"):
                p.ocr(b"\x89PNG", page_num=2)

    def test_openai_compat_rejects_invalid_image_input_mode(self, monkeypatch):
        monkeypatch.setenv("MANTISFETCH_LLM_PROVIDER", "openai")
        monkeypatch.setenv("MANTISFETCH_LLM_API_KEY", "sk-test")
        monkeypatch.setenv("MANTISFETCH_OCR_IMAGE_INPUT_MODE", "bad_mode")

        mock_openai = MagicMock()

        with patch.dict("sys.modules", {"openai": mock_openai}):
            with pytest.raises(RuntimeError, match="MANTISFETCH_OCR_IMAGE_INPUT_MODE"):
                get_provider()

    def test_openai_compat_merges_ocr_extra_body_json(self, monkeypatch):
        monkeypatch.setenv("MANTISFETCH_LLM_PROVIDER", "openai")
        monkeypatch.setenv("MANTISFETCH_LLM_API_KEY", "sk-test")
        monkeypatch.setenv("MANTISFETCH_OCR_EXTRA_BODY_JSON", '{"image_url_detail":"high"}')

        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content="page text"))]
        )
        mock_openai = MagicMock()
        mock_openai.OpenAI.return_value = mock_client

        with patch.dict("sys.modules", {"openai": mock_openai}):
            p = get_provider()
            p.ocr(b"\x89PNG", page_num=2)

        call_kwargs = mock_client.chat.completions.create.call_args.kwargs
        assert call_kwargs["extra_body"]["image_url_detail"] == "high"

    def test_openai_compat_merges_text_extra_body_json(self, monkeypatch):
        monkeypatch.setenv("MANTISFETCH_LLM_PROVIDER", "openai")
        monkeypatch.setenv("MANTISFETCH_LLM_API_KEY", "sk-test")
        monkeypatch.setenv("MANTISFETCH_LLM_EXTRA_BODY_JSON", '{"reasoning_effort":"low"}')

        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content="summary"))]
        )
        mock_openai = MagicMock()
        mock_openai.OpenAI.return_value = mock_client

        with patch.dict("sys.modules", {"openai": mock_openai}):
            p = get_provider()
            p.summarize("body text", "system prompt")

        call_kwargs = mock_client.chat.completions.create.call_args.kwargs
        assert call_kwargs["extra_body"]["reasoning_effort"] == "low"

    def test_openai_compat_rejects_invalid_extra_body_json(self, monkeypatch):
        monkeypatch.setenv("MANTISFETCH_LLM_PROVIDER", "openai")
        monkeypatch.setenv("MANTISFETCH_LLM_API_KEY", "sk-test")
        monkeypatch.setenv("MANTISFETCH_OCR_EXTRA_BODY_JSON", "[1,2,3]")

        mock_openai = MagicMock()

        with patch.dict("sys.modules", {"openai": mock_openai}):
            with pytest.raises(RuntimeError, match="MANTISFETCH_OCR_EXTRA_BODY_JSON"):
                get_provider()


class TestVendorProfiles:
    def test_openai_vendor_profile_defaults(self):
        profile = get_vendor_profile("openai")
        assert profile.base_url == "https://api.openai.com/v1"
        assert profile.default_text_model == "gpt-4o-mini"

    def test_zhipu_vendor_profile_defaults(self):
        profile = get_vendor_profile("zhipu")
        assert profile.base_url == "https://open.bigmodel.cn/api/paas/v4"
        assert profile.default_text_model == "glm-5.1"
        assert profile.default_ocr_model == "glm-4.6v"

    def test_kimi_vendor_profile_defaults(self):
        profile = get_vendor_profile("kimi")
        assert profile.base_url == "https://api.moonshot.cn/v1"
        assert profile.default_text_model == "kimi-k2.6"
        assert profile.default_ocr_model == "kimi-k2.6"

    def test_aliyun_vendor_profile_defaults(self):
        profile = get_vendor_profile("aliyun")
        assert profile.base_url == "https://dashscope.aliyuncs.com/compatible-mode/v1"
        assert profile.default_text_model == "qwen-plus"
        assert profile.default_ocr_model == "qwen-vl-ocr"

    def test_volcengine_vendor_profile_defaults(self):
        profile = get_vendor_profile("volcengine")
        assert profile.base_url == "https://ark.cn-beijing.volces.com/api/v3"
        assert profile.default_text_model is None
        assert profile.default_ocr_model is None

    def test_unknown_vendor_falls_back_to_openai(self):
        profile = get_vendor_profile("unknown")
        assert profile.name == "openai"


# ── AC-4: provider caching ────────────────────────────────────────────────────

class TestProviderCaching:
    def test_same_instance_returned_on_repeated_calls(self, monkeypatch):
        monkeypatch.delenv("MANTISFETCH_LLM_PROVIDER", raising=False)
        p1 = get_provider()
        p2 = get_provider()
        assert p1 is p2

    def test_reset_clears_cache(self, monkeypatch):
        monkeypatch.delenv("MANTISFETCH_LLM_PROVIDER", raising=False)
        p1 = get_provider()
        reset_provider()
        p2 = get_provider()
        assert p1 is not p2


# ── AC-5: unknown provider raises ─────────────────────────────────────────────

class TestUnknownProvider:
    def test_unknown_provider_raises_value_error(self, monkeypatch):
        monkeypatch.setenv("MANTISFETCH_LLM_PROVIDER", "anthropic")
        with pytest.raises(ValueError, match="anthropic"):
            get_provider()
