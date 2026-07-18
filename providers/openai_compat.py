"""OpenAI-compatible LLM provider.

Works with OpenAI and OpenAI-compatible REST APIs such as local Ollama,
Together AI, Groq, and similar providers via the official OpenAI SDK.

Environment variables:
  MANTISFETCH_LLM_VENDOR    — Vendor profile for compatible APIs (openai, zhipu, kimi)
  MANTISFETCH_LLM_API_KEY   — API key (required; use "ollama" for local Ollama)
  MANTISFETCH_LLM_BASE_URL  — Base URL override (defaults to vendor profile URL)
  MANTISFETCH_LLM_MODEL     — Model name override (defaults to vendor profile model)
  MANTISFETCH_OCR_MODEL     — OCR vision model override (defaults to vendor OCR model or text model)
  MANTISFETCH_OCR_IMAGE_INPUT_MODE — OCR image serialization mode: data_url, plain_base64, remote_url_only
"""

import base64
import json
import logging
import os
import time

from providers.base import OCR_PROOFREAD_PROMPT, OCR_TRANSCRIBE_PROMPT, LLMProvider
from providers.errors import ProviderUnavailable, classify_provider_error
from providers.vendor_profiles import get_vendor_profile

logger = logging.getLogger(__name__)

_OCR_TRANSCRIBE_PROMPT = OCR_TRANSCRIBE_PROMPT
_OCR_PROOFREAD_PROMPT = OCR_PROOFREAD_PROMPT

# Distinguishes "argument not passed → read the legacy env var" from an explicit
# value (including None) supplied by the dual-slot factory. A plain None default
# could not tell those apart.
_UNSET = object()


class OpenAICompatProvider(LLMProvider):
    """LLM provider backed by the official OpenAI SDK.

    Called with no arguments it reads the legacy ``MANTISFETCH_LLM_*`` env vars.
    The dual-slot factory instead passes ``vendor``/``api_key``/``base_url``/
    ``model``/``ocr_model`` explicitly so one process can hold several providers
    pointed at different vendors.
    """

    def __init__(
        self,
        *,
        vendor=_UNSET,
        api_key=_UNSET,
        base_url=_UNSET,
        model=_UNSET,
        ocr_model=_UNSET,
    ) -> None:
        from openai import OpenAI

        base_url_in = (
            os.environ.get("MANTISFETCH_LLM_BASE_URL") if base_url is _UNSET else base_url
        )
        vendor_in = (
            os.environ.get("MANTISFETCH_LLM_VENDOR") if vendor is _UNSET else vendor
        )
        self._vendor = get_vendor_profile(vendor_in, fallback_base_url=base_url_in)
        self._api_key = (
            os.environ.get("MANTISFETCH_LLM_API_KEY", "") if api_key is _UNSET else api_key
        ) or ""
        self._base_url = (base_url_in or self._vendor.base_url).rstrip("/")
        model_in = os.environ.get("MANTISFETCH_LLM_MODEL") if model is _UNSET else model
        self._model = model_in or self._vendor.default_text_model
        if not self._model:
            raise RuntimeError(
                f"vendor {self._vendor.name!r} has no default model; "
                "set MANTISFETCH_LLM_MODEL to the model name to use."
            )
        ocr_model_in = (
            os.environ.get("MANTISFETCH_OCR_MODEL") if ocr_model is _UNSET else ocr_model
        )
        self._ocr_model = (
            ocr_model_in
            or self._vendor.default_ocr_model
            or self._model
        )
        self._ocr_image_input_mode = self._resolve_image_input_mode(
            os.environ.get("MANTISFETCH_OCR_IMAGE_INPUT_MODE") or self._vendor.image_input_mode
        )
        self._chat_extra_body = self._merge_extra_body(
            self._vendor.extra_chat_body,
            os.environ.get("MANTISFETCH_LLM_EXTRA_BODY_JSON"),
            env_name="MANTISFETCH_LLM_EXTRA_BODY_JSON",
        )
        self._ocr_extra_body = self._merge_extra_body(
            self._vendor.extra_ocr_body,
            os.environ.get("MANTISFETCH_OCR_EXTRA_BODY_JSON"),
            env_name="MANTISFETCH_OCR_EXTRA_BODY_JSON",
        )
        self._ocr_proofread = os.environ.get("MANTISFETCH_OCR_PROOFREAD", "true").strip().lower() not in {
            "0",
            "false",
            "no",
            "off",
        }

        if not self._api_key:
            raise RuntimeError(
                "MANTISFETCH_LLM_API_KEY is not set. "
                "Export it before starting the service."
            )

        self._client = OpenAI(
            api_key=self._api_key,
            base_url=self._base_url,
            max_retries=0,
            timeout=120,
        )

    @staticmethod
    def _resolve_image_input_mode(raw_mode: str | None) -> str:
        mode = (raw_mode or "data_url").strip().lower()
        allowed = {"data_url", "plain_base64", "remote_url_only"}
        if mode not in allowed:
            raise RuntimeError(
                "MANTISFETCH_OCR_IMAGE_INPUT_MODE must be one of: "
                "data_url, plain_base64, remote_url_only."
            )
        return mode

    @staticmethod
    def _merge_extra_body(
        base: dict,
        raw_json: str | None,
        *,
        env_name: str,
    ) -> dict:
        merged = dict(base)
        if not raw_json:
            return merged
        try:
            parsed = json.loads(raw_json)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"{env_name} must be valid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise RuntimeError(f"{env_name} must decode to a JSON object.")
        merged.update(parsed)
        return merged

    @staticmethod
    def _message_text(message_content) -> str | None:
        # Some OpenAI-compatible backends (reasoning models, tool-only replies,
        # content-filter drops) return content: null. str(None) would yield the
        # literal "None", which is neither empty nor a failure sentinel and gets
        # written into digests/OCR as real text.
        #
        # Return None for null so callers can distinguish it from a genuine empty
        # string: summarize treats both as failure, but OCR must treat only null as
        # failure (empty string = blank page success — see FailoverProvider).
        if message_content is None:
            return None
        if isinstance(message_content, str):
            return message_content.strip()
        if isinstance(message_content, list):
            parts: list[str] = []
            for item in message_content:
                if isinstance(item, dict) and item.get("type") == "text":
                    text = item.get("text")
                    if text:
                        parts.append(str(text).strip())
            return "\n".join(part for part in parts if part).strip()
        return str(message_content).strip()

    @staticmethod
    def _is_ocr_failure_sentinel(text: str | None) -> bool:
        """True only for the explicit OCR failure sentinel (not any '[' prefix).

        Gating proofread / failure-log on ``startswith("[")`` misclassifies real
        page text that opens with ``[1]``, footnotes, or similar.
        """
        if not text:
            return False
        value = text.strip()
        return value.startswith(("[OCR failed", "[OCR 失败"))

    def _build_ocr_image_part(self, image_bytes: bytes) -> dict:
        b64 = base64.b64encode(image_bytes).decode("ascii")
        if self._ocr_image_input_mode == "data_url":
            url = f"data:image/png;base64,{b64}"
        elif self._ocr_image_input_mode == "plain_base64":
            url = b64
        else:
            raise RuntimeError(
                "MANTISFETCH_OCR_IMAGE_INPUT_MODE=remote_url_only is not supported by the "
                "current OCR pipeline because it renders pages in-memory and does not have "
                "a hosted image URL to send upstream."
            )
        return {
            "type": "image_url",
            "image_url": {"url": url},
        }

    def _chat(
        self,
        messages: list,
        max_retries: int = 2,
        model: str | None = None,
        extra_body: dict | None = None,
    ) -> str | None:
        """Call chat.completions.create and return the assistant text.

        Returns ``None`` when the upstream message content is null (distinct from
        an empty string). Callers decide role-specific handling.
        """

        last_exc: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                kwargs = {
                    "model": model or self._model,
                    "messages": messages,
                }
                if extra_body:
                    kwargs["extra_body"] = extra_body
                resp = self._client.chat.completions.create(
                    **kwargs,
                )
                return self._message_text(resp.choices[0].message.content)
            except Exception as exc:
                last_exc = exc
                if attempt < max_retries:
                    logger.warning(
                        "OpenAI-compat chat retry (%d/%d): %s", attempt + 1, max_retries, exc
                    )
                    time.sleep(2**attempt)
                else:
                    logger.error("OpenAI-compat chat failed after %d retries: %s", max_retries, exc)
                    raise
        raise RuntimeError(f"OpenAI-compat chat failed: {last_exc}")

    def summarize(self, text: str, prompt: str, max_retries: int = 2) -> str:
        """Generate a summary via the OpenAI chat completions endpoint."""
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": text},
        ]
        try:
            result = self._chat(
                messages,
                max_retries=max_retries,
                extra_body=self._chat_extra_body,
            )
            # Null content → empty string: summary pipeline treats empty as failed
            # (_summary_failed_text / FailoverProvider._summary_failed).
            return result if result is not None else ""
        except Exception as exc:
            # Raise typed errors so FailoverProvider can skip non-retryable 4xx.
            # get_provider wraps with SentinelBoundary for callers that still
            # expect "[summary generation failed]".
            logger.error("OpenAI-compat summarize failed: %s", exc)
            raise classify_provider_error(exc) from exc

    def ocr(self, image_bytes: bytes, page_num: int, proofread: bool | None = None) -> str:
        """OCR a page image via the OpenAI vision endpoint (base64-encoded)."""
        image_part = self._build_ocr_image_part(image_bytes)
        do_proofread = self._ocr_proofread if proofread is None else proofread
        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": _OCR_TRANSCRIBE_PROMPT,
                    },
                    image_part,
                ],
            }
        ]
        try:
            result = self._chat(
                messages,
                max_retries=2,
                model=self._ocr_model,
                extra_body=self._ocr_extra_body,
            )
        except Exception as exc:
            # Typed raise → FailoverProvider (retryable only) + SentinelBoundary.
            logger.warning("OpenAI-compat OCR failed for page %d: %s", page_num, exc)
            raise classify_provider_error(exc) from exc
        # content:null is a failed OCR call, not a blank page. Empty string is
        # reserved for genuinely blank pages (must not trigger failover).
        if result is None:
            logger.warning(
                "OpenAI-compat OCR returned null content for page %d", page_num
            )
            raise ProviderUnavailable(
                f"OpenAI-compat OCR returned null content for page {page_num}"
            )
        if do_proofread and result and not self._is_ocr_failure_sentinel(result):
            review_messages = [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": _OCR_PROOFREAD_PROMPT.format(draft=result),
                        },
                        image_part,
                    ],
                }
            ]
            try:
                reviewed = self._chat(
                    review_messages,
                    max_retries=1,
                    model=self._ocr_model,
                    extra_body=self._ocr_extra_body,
                )
            except Exception as exc:
                logger.warning("OpenAI-compat OCR proofread skipped for page %d: %s", page_num, exc)
                reviewed = ""
            # Ignore null/empty/sentinel proofread; keep the transcription draft.
            if reviewed and not self._is_ocr_failure_sentinel(reviewed):
                result = reviewed
        if self._is_ocr_failure_sentinel(result):
            logger.warning("OpenAI-compat OCR may have failed for page %d", page_num)
        return result
