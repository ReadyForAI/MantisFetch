"""Abstract base class for LLM providers."""

from abc import ABC, abstractmethod

# Shared OCR prompts — identical across providers; keep one source of truth so an
# edit can't silently change behaviour for only one backend.
OCR_TRANSCRIBE_PROMPT = (
    "Transcribe this document page exactly as written. "
    "Preserve names, numbers, dates, account numbers, email addresses, and punctuation exactly. "
    "Do not summarize, translate, infer, normalize, or correct the source. "
    "Ignore only obvious scanner borders or decorative watermarks. "
    "If the page contains a table, return the table as a complete GitHub-flavored Markdown table. "
    "Return only the transcribed page text."
)
OCR_PROOFREAD_PROMPT = (
    "Proofread the following OCR draft against the document page image. "
    "Fix OCR mistakes only where the image clearly supports the correction. "
    "Pay extra attention to company names, amounts, percentages, dates, account numbers, email addresses, and table cells. "
    "Keep the same layout style, including Markdown tables where present. "
    "Return only the corrected page text.\n\n"
    "OCR draft:\n{draft}"
)


class LLMProvider(ABC):
    """Unified interface for LLM backends (summarisation + OCR).

    Implementations must override both ``summarize`` and ``ocr``.
    All concrete providers are expected to handle retries internally.
    """

    @abstractmethod
    def summarize(self, text: str, prompt: str, max_retries: int = 2) -> str:
        """Generate a text summary.

        Args:
            text:        The source text to summarise.
            prompt:      System/user prompt that instructs the model.
            max_retries: Number of retries on transient errors.

        Returns:
            The generated summary string.
        """

    @abstractmethod
    def ocr(self, image_bytes: bytes, page_num: int, proofread: bool | None = None) -> str:
        """Extract text from a page image via vision.

        Args:
            image_bytes: Raw image bytes (PNG/JPEG).
            page_num:    1-based page number (used only for logging).
            proofread:   Override provider default OCR proofreading behaviour.

        Returns:
            Extracted text string.
        """
