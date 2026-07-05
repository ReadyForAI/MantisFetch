"""Three-tier summary generation: per-section → brief → digest, via the LLM.

`generate_summaries` turns a parsed document into a (digest, brief, sections)
triple: it batches sections by token budget, summarizes each batch (with a
single-item JSON fallback), then rolls those up into a brief and a digest. All
LLM calls go through `gemini_summarize`, which serializes requests behind a
process-wide lock and a minimum-interval rate limiter.

`gemini_summarize` is monkeypatched by tests off the facade, so the generation
functions call it via a function-level `from . import gemini_summarize` (the
patched-helper pattern). `_parsed_document_locale` / `_estimate_tokens` are
shared helpers that stay in the package `__init__`; they're reached the same
way, which also breaks the import cycle. The rate-limit state
(`_summary_llm_lock`, `_summary_llm_next_allowed_at`) is module-local: read and
written only here, so it stays self-consistent without re-export.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeoutError
from datetime import UTC, datetime

from i18n import prompt_for_locale, t

from .models import DocumentProfile, ParsedDocument, Section

logger = logging.getLogger("mantisfetch_docreader")

SUMMARY_MAX_CHARS = 500
SUMMARY_BATCH_CONCURRENCY = max(
    1,
    int(os.environ.get("MANTISFETCH_SUMMARY_BATCH_CONCURRENCY", "1")),
)
SUMMARY_REQUEST_MIN_INTERVAL_SEC = max(
    0.0,
    float(os.environ.get("MANTISFETCH_SUMMARY_REQUEST_MIN_INTERVAL_SEC", "2.0")),
)
SUMMARY_SECTION_DETAIL_LIMIT = max(
    1,
    int(os.environ.get("MANTISFETCH_SUMMARY_SECTION_DETAIL_LIMIT", "10")),
)
SUMMARY_BRIEF_SECTION_EXCERPT_CHARS = max(
    200,
    int(os.environ.get("MANTISFETCH_SUMMARY_BRIEF_SECTION_EXCERPT_CHARS", "1200")),
)
SUMMARY_BRIEF_MAX_INPUT_CHARS = max(
    4000,
    int(os.environ.get("MANTISFETCH_SUMMARY_BRIEF_MAX_INPUT_CHARS", "32000")),
)

# Rate-limit state (min-interval between request *starts*) and a separate
# concurrency cap. The lock guards only the slot reservation, never the network
# call — see gemini_summarize.
_summary_llm_lock = threading.Lock()
_summary_llm_next_allowed_at = 0.0
_summary_llm_sem = threading.BoundedSemaphore(SUMMARY_BATCH_CONCURRENCY)


def gemini_summarize(text: str, summarize_prompt: str, max_retries: int = 2) -> str:
    """Generate summary via the active LLM provider.

    D1: the previous version held _summary_llm_lock for the entire network call
    (plus retries + backoff), so SUMMARY_BATCH_CONCURRENCY and the batch worker
    pool collapsed to serial execution. Now concurrency is bounded by
    _summary_llm_sem and the lock only reserves this request's start slot; the
    sleep and the network call run outside the lock. Throughput scales with the
    configured concurrency while min-interval-between-starts is preserved.

    The semaphore is acquired *before* the slot reservation so the reservation is
    coupled to the actual call: otherwise a burst of semaphore releases could let
    several already-reserved calls start within less than the min-interval.
    """
    from providers import get_provider

    global _summary_llm_next_allowed_at
    with _summary_llm_sem:
        with _summary_llm_lock:
            start_at = max(time.monotonic(), _summary_llm_next_allowed_at)
            _summary_llm_next_allowed_at = start_at + SUMMARY_REQUEST_MIN_INTERVAL_SEC
        wait_sec = start_at - time.monotonic()
        if wait_sec > 0:
            time.sleep(wait_sec)
        return get_provider().summarize(text, summarize_prompt, max_retries=max_retries)


def _summary_failed_text(text: str | None) -> bool:
    if not text:
        return True
    compact = text.strip().lower()
    return compact in {
        "[summary generation failed]",
        "summary generation failed",
    }


def _local_section_preview(sec: Section, limit: int = SUMMARY_MAX_CHARS) -> str:
    text = re.sub(r"\s+", " ", sec.text).strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def _sections_overview_from_text(sections: list[Section]) -> str:
    parts: list[str] = []
    total_chars = 0
    for sec in sections:
        excerpt = _local_section_preview(sec, SUMMARY_BRIEF_SECTION_EXCERPT_CHARS)
        part = f"## {sec.title} ({sec.page_range})\n{excerpt}".strip()
        if not part:
            continue
        if total_chars + len(part) > SUMMARY_BRIEF_MAX_INPUT_CHARS and parts:
            parts.append(
                f"\n[Truncated after {len(parts)} sections due to summary input budget]"
            )
            break
        parts.append(part)
        total_chars += len(part)
    return "\n\n".join(parts)


def _sections_overview_for_brief(sections: list[Section]) -> str:
    if len(sections) > 60:
        overview = _compress_sections_for_brief(sections)
    else:
        overview = "\n\n".join(
            f"## {sec.title} ({sec.page_range})\n{sec.summary[:SUMMARY_MAX_CHARS]}"
            for sec in sections
            if sec.summary and not _summary_failed_text(sec.summary)
        )
    if overview.strip():
        return overview
    return _sections_overview_from_text(sections)


def _should_skip_section_summaries(parsed: ParsedDocument) -> bool:
    # Read the limit off the facade so tests that patch
    # `docreader.SUMMARY_SECTION_DETAIL_LIMIT` take effect (a module-local read
    # would not see the monkeypatch).
    from . import SUMMARY_SECTION_DETAIL_LIMIT

    return len(parsed.sections) > SUMMARY_SECTION_DETAIL_LIMIT


def generate_summaries(
    parsed: ParsedDocument, concurrency: int = 3, allow_single_fallback: bool = True
) -> tuple[str, str, list[Section]]:
    # Facade imports: gemini_summarize is monkeypatched off the facade by tests;
    # _parsed_document_locale / _estimate_tokens live in __init__. Function-level
    # `from . import` resolves them off the fully-loaded facade at call time.
    from . import _estimate_tokens, _parsed_document_locale, gemini_summarize

    logger.info("Generating summaries...")
    summary_locale = _parsed_document_locale(parsed)

    if _should_skip_section_summaries(parsed):
        logger.info(
            "Skipping per-section summaries for long document: %s sections > limit %s",
            len(parsed.sections),
            SUMMARY_SECTION_DETAIL_LIMIT,
        )
        sections_overview = _sections_overview_from_text(parsed.sections)
    else:
        # Dynamic batching by token estimate
        BATCH_TOKEN_LIMIT = 10000
        batches: list[list[Section]] = []
        current_batch: list[Section] = []
        current_tokens = 0

        for sec in parsed.sections:
            sec_tokens = _estimate_tokens(sec.text) + _estimate_tokens(sec.title) + 20
            if current_tokens + sec_tokens > BATCH_TOKEN_LIMIT and current_batch:
                batches.append(current_batch)
                current_batch = []
                current_tokens = 0
            current_batch.append(sec)
            current_tokens += sec_tokens
        if current_batch:
            batches.append(current_batch)

        summary_workers = min(max(1, concurrency), SUMMARY_BATCH_CONCURRENCY, len(batches))
        if len(batches) > 1 and summary_workers > 1:
            logger.info(f"{len(batches)} batches, {summary_workers} summary workers")
            with ThreadPoolExecutor(max_workers=summary_workers) as pool:
                futures = {
                    pool.submit(_summarize_batch, batch, allow_single_fallback, summary_locale): batch
                    for batch in batches
                }
                for fut in as_completed(futures):
                    fut.result()
        else:
            for batch in batches:
                _summarize_batch(batch, allow_single_fallback, summary_locale)

        logger.info(f"{len(parsed.sections)} section summaries complete")
        sections_overview = _sections_overview_for_brief(parsed.sections)

    brief = gemini_summarize(
        f"Document: {parsed.filename}\nTotal pages: {parsed.total_pages}\n\n{sections_overview}",
        prompt_for_locale(summary_locale, "brief"),
    )
    if _summary_failed_text(brief):
        raise RuntimeError("upstream brief generation failed")
    logger.info("Brief generation complete")

    digest = gemini_summarize(
        f"Document: {parsed.filename}\n\nBriefing:\n{brief}",
        prompt_for_locale(summary_locale, "digest"),
    )
    if _summary_failed_text(digest):
        raise RuntimeError("upstream digest generation failed")
    logger.info("Digest generation complete")

    return digest, brief, parsed.sections


def _summarize_batch(
    sections: list[Section], allow_single_fallback: bool = True, summary_locale: str = "en"
):
    """Batch summarize with JSON output + single fallback."""
    from . import gemini_summarize

    n = len(sections)

    if n == 1:
        sec = sections[0]
        summary = gemini_summarize(
            f"## {sec.title} ({sec.page_range})\n\n{sec.text}",
            prompt_for_locale(summary_locale, "section_summary"),
        )
        # Treat a failure sentinel as a hard failure (like the batch path) so the
        # summary is marked failed/retryable instead of silently storing it.
        if _summary_failed_text(summary):
            raise RuntimeError("upstream summary generation failed")
        sec.summary = summary
        logger.info(f"Section {sec.index}: {sec.title[:30]}... done")
        return

    batch_text = ""
    for sec in sections:
        batch_text += f"\n\n## Section {sec.index}: {sec.title} ({sec.page_range})\n\n{sec.text}"

    result = gemini_summarize(batch_text, prompt_for_locale(summary_locale, "batch_summary", n=n))
    if _summary_failed_text(result):
        raise RuntimeError("upstream summary generation failed")

    # JSON parse
    parsed_ok = False
    try:
        clean = result.strip()
        if clean.startswith("```"):
            clean = re.sub(r"^```(?:json)?\s*", "", clean)
            clean = re.sub(r"\s*```$", "", clean)
        items = json.loads(clean)
        if isinstance(items, list) and len(items) >= n:
            for sec in sections:
                match = next((it for it in items if it.get("index") == sec.index), None)
                if match and match.get("summary"):
                    sec.summary = match["summary"]
                else:
                    sec.summary = t("summary_missing")
            parsed_ok = True
    except (json.JSONDecodeError, KeyError, TypeError):
        pass

    if parsed_ok:
        for sec in sections:
            logger.info(f"Section {sec.index}: {sec.title[:30]}... done")
        return

    # Fallback
    if not allow_single_fallback:
        logger.warning(
            "Batch JSON parse failed for %d sections; using local section previews",
            n,
        )
        for sec in sections:
            sec.summary = _local_section_preview(sec)
        return

    logger.warning(f"Batch JSON parse failed, falling back to single ({n} items)")
    for sec in sections:
        summary = gemini_summarize(
            f"## {sec.title} ({sec.page_range})\n\n{sec.text}",
            prompt_for_locale(summary_locale, "section_summary"),
        )
        # Don't persist the failure sentinel — degrade to a local preview so the
        # stored summary is usable and the brief overview filter behaves.
        sec.summary = (
            summary if not _summary_failed_text(summary) else _local_section_preview(sec)
        )
        logger.info(f"Section {sec.index}: {sec.title[:30]}... done (single)")


def _compress_sections_for_brief(sections: list[Section]) -> str:
    groups = []
    for i in range(0, len(sections), 10):
        group = sections[i : i + 10]
        group_text = "; ".join(
            f"{s.title}: {s.summary[:150]}"
            for s in group
            if s.summary and not _summary_failed_text(s.summary)
        )
        groups.append(f"**Sections {group[0].index}-{group[-1].index}**: {group_text}")
    return "\n\n".join(groups)


# ═══════════════════════════════════════════
# Summary orchestration metadata
# ═══════════════════════════════════════════


def _resolve_summary_mode(
    *,
    profile: DocumentProfile | None,
    parse_mode: str | None,
    generate_summary: bool,
    requested_mode: str | None,
) -> str:
    if not generate_summary:
        return "off"

    mode = (requested_mode or "").strip().lower()
    if not mode:
        mode = os.environ.get("MANTISFETCH_SUMMARY_MODE", "").strip().lower()

    if mode in {"off", "sync", "defer"}:
        return mode

    selected_parse_mode = (parse_mode or "").strip().lower()
    if profile:
        if selected_parse_mode and selected_parse_mode in profile.summary_policy.async_modes:
            return "defer"
        if selected_parse_mode and selected_parse_mode in profile.summary_policy.sync_modes:
            return "sync"
        if profile.summary_policy.default_mode in {"off", "sync", "defer"}:
            return profile.summary_policy.default_mode

    return "sync"


def _set_summary_metadata(
    parsed: ParsedDocument,
    *,
    mode: str,
    status: str,
    error: str | None = None,
    error_code: str | None = None,
    attempts: int | None = None,
) -> None:
    metadata = parsed.metadata if isinstance(parsed.metadata, dict) else {}
    existing = metadata.get("summary") if isinstance(metadata.get("summary"), dict) else {}
    metadata["summary"] = {
        "mode": mode,
        "status": status,
        "updated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "attempts": int(attempts if attempts is not None else existing.get("attempts", 0)),
    }
    if status == "running":
        metadata["summary"]["started_at"] = metadata["summary"]["updated_at"]
    elif existing.get("started_at"):
        metadata["summary"]["started_at"] = existing.get("started_at")
    if status in {"completed", "failed"}:
        metadata["summary"]["finished_at"] = metadata["summary"]["updated_at"]
    if error:
        metadata["summary"]["error"] = error
    if error_code:
        metadata["summary"]["error_code"] = error_code
    parsed.metadata = metadata


def _summary_placeholder_text(
    status: str, error: str | None = None, locale: str | None = None
) -> str:
    output_locale = "zh" if str(locale or "").lower().startswith("zh") else "en"
    if status == "running":
        return "(摘要生成中)" if output_locale == "zh" else "(Summary running)"
    if status == "failed":
        if error:
            if output_locale == "zh":
                return f"(摘要生成失败: {error})"
            return f"(Summary failed: {error})"
        return "(摘要生成失败)" if output_locale == "zh" else "(Summary failed)"
    return "(摘要待生成)" if output_locale == "zh" else "(Summary pending)"


def _current_summary_attempts(parsed: ParsedDocument) -> int:
    metadata = parsed.metadata if isinstance(parsed.metadata, dict) else {}
    summary = metadata.get("summary") if isinstance(metadata.get("summary"), dict) else {}
    try:
        return int(summary.get("attempts", 0))
    except (TypeError, ValueError):
        return 0


def _classify_summary_error(exc: Exception) -> tuple[str, str]:
    from . import DEFERRED_SUMMARY_TIMEOUT_SEC

    if isinstance(exc, FuturesTimeoutError):
        return "timeout", f"summary timed out after {int(DEFERRED_SUMMARY_TIMEOUT_SEC)}s"

    text = str(exc).strip() or exc.__class__.__name__
    lower = text.lower()
    if "attempt limit" in lower:
        return "attempt_limit", text
    if "429" in text or "rate limit" in lower or "速率限制" in text:
        return "rate_limit", "upstream rate limit"
    if "timeout" in lower or "timed out" in lower:
        return "timeout", text
    return "provider_error", text
