"""Gemini LLM provider (default).

Reads credentials from the environment:
  GEMINI_API_KEY  or  GOOGLE_API_KEY  — required
  MANTISFETCH_LLM_MODEL                 — optional; defaults to gemini-2.5-flash
"""

import io
import logging
import os
import time

from providers.base import OCR_PROOFREAD_PROMPT, OCR_TRANSCRIBE_PROMPT, LLMProvider

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "gemini-2.5-flash"
_OCR_TRANSCRIBE_PROMPT = OCR_TRANSCRIBE_PROMPT
_OCR_PROOFREAD_PROMPT = OCR_PROOFREAD_PROMPT

# See providers.openai_compat._UNSET — tells "not passed" from an explicit value.
_UNSET = object()


class GeminiProvider(LLMProvider):
    """LLM provider backed by the Google Gemini API (google-genai SDK).

    With no arguments it reads the legacy env vars (``MANTISFETCH_LLM_MODEL`` +
    ``GEMINI_API_KEY``/``GOOGLE_API_KEY``). The dual-slot factory passes
    ``model``/``api_key`` explicitly for a ``gemini/<model>`` slot.
    """

    def __init__(self, *, api_key=_UNSET, model=_UNSET) -> None:
        self._client = None
        model_in = os.environ.get("MANTISFETCH_LLM_MODEL") if model is _UNSET else model
        self._model = model_in or _DEFAULT_MODEL
        self._api_key_override = None if api_key is _UNSET else (api_key or None)
        self._ocr_proofread = os.environ.get("MANTISFETCH_OCR_PROOFREAD", "true").strip().lower() not in {
            "0",
            "false",
            "no",
            "off",
        }

    def _init(self) -> None:
        """Lazy-initialise the Gemini client on first use."""
        if self._client is not None:
            return

        try:
            from google import genai
        except ImportError as exc:
            raise RuntimeError(
                "google-genai is not installed. Run: pip install google-genai"
            ) from exc

        api_key = (
            self._api_key_override
            or os.environ.get("GEMINI_API_KEY")
            or os.environ.get("GOOGLE_API_KEY")
        )
        if not api_key:
            raise RuntimeError(
                "Gemini API key not set. Export GEMINI_API_KEY or GOOGLE_API_KEY."
            )

        self._client = genai.Client(api_key=api_key)

    def summarize(self, text: str, prompt: str, max_retries: int = 2) -> str:
        """Generate a summary via Gemini Flash."""
        self._init()
        full_prompt = f"{prompt}\n\n---\n\n{text}"

        for attempt in range(max_retries + 1):
            try:
                response = self._client.models.generate_content(
                    model=self._model,
                    contents=full_prompt,
                    config={"http_options": {"timeout": 60_000}},
                )
                return response.text.strip()
            except Exception as exc:
                if attempt < max_retries:
                    logger.warning("Gemini summarize retry (%d/%d): %s", attempt + 1, max_retries, exc)
                    time.sleep(2**attempt)
                else:
                    logger.error("Gemini summarize failed after %d retries: %s", max_retries, exc)
                    return "[summary generation failed]"
        return "[summary generation failed]"

    def ocr(
        self,
        image_bytes: bytes,
        page_num: int,
        proofread: bool | None = None,
        max_retries: int = 2,
    ) -> str:
        """OCR a single page image via Gemini Vision."""
        self._init()

        import PIL.Image

        img = PIL.Image.open(io.BytesIO(image_bytes))
        do_proofread = self._ocr_proofread if proofread is None else proofread
        for attempt in range(max_retries + 1):
            try:
                response = self._client.models.generate_content(
                    model=self._model,
                    contents=[_OCR_TRANSCRIBE_PROMPT, img],
                    config={"http_options": {"timeout": 60_000}},
                )
                result = response.text.strip()
                # Proofread whenever transcription succeeded — gating on
                # attempt == 0 skipped it for any page that needed a retry.
                # Only the explicit OCR failure sentinel counts — not any text
                # that happens to start with '[' (e.g. "[1] footnote …").
                if do_proofread and result and not result.strip().startswith(
                    ("[OCR failed", "[OCR 失败")
                ):
                    try:
                        review = self._client.models.generate_content(
                            model=self._model,
                            contents=[_OCR_PROOFREAD_PROMPT.format(draft=result), img],
                            config={"http_options": {"timeout": 60_000}},
                        )
                        reviewed = review.text.strip()
                        if reviewed and not reviewed.strip().startswith(
                            ("[OCR failed", "[OCR 失败")
                        ):
                            result = reviewed
                    except Exception as exc:
                        logger.warning(
                            "Gemini OCR proofread skipped for page %d: %s",
                            page_num,
                            exc,
                        )
                return result
            except Exception as exc:
                if attempt < max_retries:
                    logger.warning("Gemini OCR retry (%d/%d) for page %d: %s", attempt + 1, max_retries, page_num, exc)
                    time.sleep(2**attempt)
                else:
                    logger.warning("Gemini OCR failed for page %d after %d retries: %s", page_num, max_retries, exc)
                    return f"[OCR failed for page {page_num}]"
        return f"[OCR failed for page {page_num}]"
