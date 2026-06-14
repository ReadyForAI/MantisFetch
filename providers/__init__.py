"""LLM provider factory.

Selects the active provider via LARKSCOUT_LLM_PROVIDER env var (default: gemini).
Supported values: "gemini", "openai" (any OpenAI-compatible REST API).

The resolved provider is cached as a module-level singleton so callers share one
instance per process.
"""

import os
import threading

from providers.base import LLMProvider

_provider: LLMProvider | None = None
# Guards lazy creation of the singleton: parse/summary work runs in a
# ThreadPoolExecutor, so two threads can hit a cold get_provider() at once.
_provider_lock = threading.Lock()


def get_provider() -> LLMProvider:
    """Return the cached LLM provider, creating it on first call.

    The provider is selected by the LARKSCOUT_LLM_PROVIDER environment variable:
      - "gemini"  (default) → GeminiProvider
      - "openai"            → OpenAICompatProvider
    """
    global _provider
    if _provider is not None:
        return _provider

    with _provider_lock:
        # Double-checked: another thread may have created it while we waited.
        if _provider is not None:
            return _provider

        name = os.environ.get("LARKSCOUT_LLM_PROVIDER", "gemini").lower().strip()

        if name == "gemini":
            from providers.gemini import GeminiProvider

            provider: LLMProvider = GeminiProvider()
        elif name == "openai":
            from providers.openai_compat import OpenAICompatProvider

            provider = OpenAICompatProvider()
        else:
            raise ValueError(
                f"Unknown LARKSCOUT_LLM_PROVIDER={name!r}. "
                "Supported values: 'gemini', 'openai'."
            )

        _provider = provider
        return _provider


def reset_provider() -> None:
    """Clear the cached provider (used in tests)."""
    global _provider
    with _provider_lock:
        _provider = None
