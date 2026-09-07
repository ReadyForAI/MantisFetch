"""Abstract base class for LLM providers."""

import hashlib
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


def _short_digest(value: str) -> str:
    """A stable, credential-free stand-in for a string in a fingerprint.

    Fingerprints end up in cache filenames, so anything that might carry a key —
    a base_url with a token in its query, a request body — goes in hashed rather
    than verbatim, while still making two different values two different keys.
    """
    return hashlib.sha1(value.encode("utf-8", errors="ignore")).hexdigest()[:8]


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

    def check_configuration(self) -> None:
        """Raise if this provider cannot run as configured. Never touches the network.

        Construction proves the *model* resolved; it does not prove a key was
        supplied, because every backend opens its client lazily. So a role could
        report healthy at boot and 401 on the first document. This is the hook
        that answers "would a call work" using only what is already in hand.
        """
        return None

    def describe_model(self, role: str = "summary") -> str:
        """Model this provider would use for ``role`` — for /health and logs."""
        return type(self).__name__

    def ocr_fingerprint(self) -> str:
        """What identifies this backend's OCR output, for cache validity.

        Two runs that share a fingerprint are expected to produce the same text
        for the same image; anything that would change the text — the vendor,
        the model, the proofread setting — belongs in here. Credentials never
        do: the string is written into a filename.

        The default names only the class, which is the conservative answer for
        a backend that has not said more: it changes when the backend does.
        """
        return type(self).__name__

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
