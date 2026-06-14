"""Gemini LLM provider (default).

Reads credentials from the environment:
  GEMINI_API_KEY  or  GOOGLE_API_KEY  — required
  LARKSCOUT_LLM_MODEL                 — optional; defaults to gemini-2.5-flash
"""

import io
import logging
import os
import time

from providers.base import LLMProvider

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "gemini-2.5-flash"
_OCR_TRANSCRIBE_PROMPT = (
    "Transcribe this document page exactly as written. "
    "Preserve names, numbers, dates, account numbers, email addresses, and punctuation exactly. "
    "Do not summarize, translate, infer, normalize, or correct the source. "
    "Ignore only obvious scanner borders or decorative watermarks. "
    "If the page contains a table, return the table as a complete GitHub-flavored Markdown table. "
    "Return only the transcribed page text."
)
_OCR_PROOFREAD_PROMPT = (
    "Proofread the following OCR draft against the document page image. "
    "Fix OCR mistakes only where the image clearly supports the correction. "
    "Pay extra attention to company names, amounts, percentages, dates, account numbers, email addresses, and table cells. "
    "Keep the same layout style, including Markdown tables where present. "
    "Return only the corrected page text.\n\n"
    "OCR draft:\n{draft}"
)


class GeminiProvider(LLMProvider):
    """LLM provider backed by the Google Gemini API (google-genai SDK)."""

    def __init__(self) -> None:
        self._client = None
        self._model = os.environ.get("LARKSCOUT_LLM_MODEL") or _DEFAULT_MODEL
        self._ocr_proofread = os.environ.get("LARKSCOUT_OCR_PROOFREAD", "true").strip().lower() not in {
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

        api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
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
                if do_proofread and result and not result.startswith("["):
                    try:
                        review = self._client.models.generate_content(
                            model=self._model,
                            contents=[_OCR_PROOFREAD_PROMPT.format(draft=result), img],
                            config={"http_options": {"timeout": 60_000}},
                        )
                        reviewed = review.text.strip()
                        if reviewed and not reviewed.startswith("["):
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
