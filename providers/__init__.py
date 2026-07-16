"""LLM provider factory.

Two configuration schemes are supported:

**Legacy (single provider)** — ``MANTISFETCH_LLM_PROVIDER`` selects one backend
("gemini" or "openai") shared by every role. This is the original behaviour and
stays active whenever the dual-slot scheme is not configured.

**Dual-slot (per-role + failover)** — activated when ``MANTISFETCH_LLM_DEFAULT``
is set. Two credential slots are configured:

    MANTISFETCH_LLM_DEFAULT       = <vendor tag, e.g. zhipu>
    MANTISFETCH_LLM_DEFAULT_BASE_URL / _API_KEY
    MANTISFETCH_LLM_EXTRA         = <vendor tag, e.g. deepseek>
    MANTISFETCH_LLM_EXTRA_BASE_URL / _API_KEY

Each role picks a primary and an optional fallback model, written two-segment as
``<vendor>/<model>``; the vendor prefix routes the call to the slot whose tag
matches, using that slot's base_url + api_key:

    MANTISFETCH_SUM_MODEL_DEFAULT  = deepseek/deepseek-chat
    MANTISFETCH_SUM_MODEL_FALLBACK = zhipu/glm-4.6
    MANTISFETCH_OCR_MODEL_DEFAULT  = zhipu/glm-4.6v
    MANTISFETCH_OCR_MODEL_FALLBACK = deepseek/deepseek-chat

This lets OCR and summarisation run on different vendors (cost/capability) and
fail over to a backup on failure. Resolved providers are cached per role.
"""

import logging
import os
import threading

from providers.base import LLMProvider

logger = logging.getLogger(__name__)

_ROLES = ("summary", "ocr")
# Legacy mode shares one instance across roles, keyed by this sentinel so its
# behaviour (a single process-wide singleton) is byte-identical to before.
_LEGACY_KEY = "_legacy"

# role (or _LEGACY_KEY) -> provider. Guarded by _providers_lock: parse/summary
# work runs in a ThreadPoolExecutor, so two threads can hit a cold slot at once.
_providers: dict[str, LLMProvider] = {}
_providers_lock = threading.Lock()


def _dual_mode() -> bool:
    return bool(os.environ.get("MANTISFETCH_LLM_DEFAULT", "").strip())


def get_provider(role: str = "summary") -> LLMProvider:
    """Return the cached LLM provider for ``role`` ("summary" or "ocr").

    In legacy mode the same instance is returned for every role.
    """
    if role not in _ROLES:
        raise ValueError(f"role must be one of {_ROLES}, got {role!r}")

    key = role if _dual_mode() else _LEGACY_KEY
    existing = _providers.get(key)
    if existing is not None:
        return existing

    with _providers_lock:
        # Double-checked: another thread may have created it while we waited.
        existing = _providers.get(key)
        if existing is not None:
            return existing
        provider = _build_dual(role) if _dual_mode() else _build_legacy()
        _providers[key] = provider
        return provider


def reset_provider() -> None:
    """Clear the cached providers (used in tests)."""
    with _providers_lock:
        _providers.clear()


# ── Legacy single-provider path ───────────────────────────────────────────────

def _build_legacy() -> LLMProvider:
    name = os.environ.get("MANTISFETCH_LLM_PROVIDER", "gemini").lower().strip()
    if name == "gemini":
        from providers.gemini import GeminiProvider

        return GeminiProvider()
    if name == "openai":
        from providers.openai_compat import OpenAICompatProvider

        return OpenAICompatProvider()
    raise ValueError(
        f"Unknown MANTISFETCH_LLM_PROVIDER={name!r}. "
        "Supported values: 'gemini', 'openai'."
    )


# ── Dual-slot per-role path ───────────────────────────────────────────────────

class _Slot:
    __slots__ = ("label", "base_url", "api_key")

    def __init__(self, label: str, base_url: str, api_key: str) -> None:
        self.label = label  # DEFAULT | EXTRA — used only for error messages
        self.base_url = base_url
        self.api_key = api_key


def _load_slots() -> dict[str, _Slot]:
    """Read the DEFAULT and EXTRA credential slots, keyed by their vendor tag."""
    slots: dict[str, _Slot] = {}
    for label in ("DEFAULT", "EXTRA"):
        vendor = os.environ.get(f"MANTISFETCH_LLM_{label}", "").strip().lower()
        if not vendor:
            continue
        slots[vendor] = _Slot(
            label=label,
            base_url=os.environ.get(f"MANTISFETCH_LLM_{label}_BASE_URL", "").strip(),
            api_key=os.environ.get(f"MANTISFETCH_LLM_{label}_API_KEY", "").strip(),
        )
    if not slots:
        raise RuntimeError(
            "MANTISFETCH_LLM_DEFAULT is set but resolves to no provider slot."
        )
    return slots


def _build_slot_provider(spec: str, slots: dict[str, _Slot]) -> LLMProvider:
    """Build a concrete provider from a ``<vendor>/<model>`` spec + slot creds."""
    spec = spec.strip()
    if "/" not in spec:
        raise RuntimeError(
            f"model spec {spec!r} must be '<vendor>/<model>' (e.g. zhipu/glm-4.6v)."
        )
    vendor, _, model = spec.partition("/")
    vendor = vendor.strip().lower()
    model = model.strip()
    if not model:
        raise RuntimeError(f"model spec {spec!r} has an empty model name.")
    slot = slots.get(vendor)
    if slot is None:
        raise RuntimeError(
            f"model spec {spec!r} references vendor {vendor!r}, which is not a "
            f"configured slot; set MANTISFETCH_LLM_DEFAULT/_EXTRA to it. "
            f"Configured slots: {sorted(slots)}."
        )
    if not slot.api_key:
        # Require the slot's own key up front for both backends. For gemini this
        # also stops GeminiProvider from silently falling back to the legacy
        # GEMINI_API_KEY/GOOGLE_API_KEY (cross-slot credential leak), and makes a
        # keyless gemini fallback fail to build → discarded like any other bad
        # fallback, instead of raising only at first use.
        raise RuntimeError(
            f"slot for vendor {vendor!r} has no API key; "
            f"set MANTISFETCH_LLM_{slot.label}_API_KEY."
        )

    if vendor == "gemini":
        from providers.gemini import GeminiProvider

        return GeminiProvider(api_key=slot.api_key, model=model)

    from providers.openai_compat import OpenAICompatProvider

    return OpenAICompatProvider(
        vendor=vendor,
        api_key=slot.api_key,
        base_url=slot.base_url or None,
        model=model,
        ocr_model=model,
    )


def _build_dual(role: str) -> LLMProvider:
    slots = _load_slots()
    prefix = "SUM" if role == "summary" else "OCR"

    primary_spec = os.environ.get(f"MANTISFETCH_{prefix}_MODEL_DEFAULT", "").strip()
    if not primary_spec:
        raise RuntimeError(
            f"MANTISFETCH_{prefix}_MODEL_DEFAULT is required when "
            "MANTISFETCH_LLM_DEFAULT is set."
        )
    primary = _build_slot_provider(primary_spec, slots)

    fallback_spec = os.environ.get(f"MANTISFETCH_{prefix}_MODEL_FALLBACK", "").strip()
    if not fallback_spec:
        return primary

    try:
        fallback = _build_slot_provider(fallback_spec, slots)
    except Exception as exc:
        # A misconfigured *backup* must not take down the working primary; a
        # broken fallback degrades to no-failover with a loud log, not a crash.
        logger.error(
            "%s fallback model %r could not be built (%s); "
            "continuing without failover.",
            role,
            fallback_spec,
            exc,
        )
        return primary

    from providers.failover import FailoverProvider

    return FailoverProvider(primary, fallback, role=role)
