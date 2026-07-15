"""Local OCR worker engine, Gemini OCR, and OCR page caching.

Owns the long-lived local OCR worker subprocess and its module-level state
(the singleton handle, its lock/ready/initializing events, and the
circuit-breaker timestamp) plus the worker timeout/breaker config. These
globals are mutated in place, so they must live in exactly one module: tests
patch them at ``mantisfetch_docreader.ocr.engines`` (not the package facade),
since a re-exported copy would not track reassignment.

Depends only on stdlib, i18n, the LLM provider factory (imported lazily inside
gemini_ocr), and models — never the package __init__ — to avoid a circular
import.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import selectors
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from i18n import t

from ..models import OCRPageBlocks, OCRTextBlock, _normalize_layout_bbox

logger = logging.getLogger("mantisfetch_docreader")

# ── Local OCR worker config (worker-only; tests patch these here) ──
# Master switch for the offline (PaddleOCR) worker. False (set in the slim image,
# which ships no PaddleOCR) makes OCR routing skip the local worker entirely and
# use the LLM/vision provider instead — so PDF page OCR falls back rather than
# emitting failed markers, and no missing worker is prewarmed/attempted.
LOCAL_OCR_ENABLED = os.environ.get("MANTISFETCH_LOCAL_OCR_ENABLED", "true").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}
LOCAL_OCR_WORKER_STARTUP_TIMEOUT_SEC = float(
    os.environ.get("MANTISFETCH_LOCAL_OCR_WORKER_STARTUP_TIMEOUT_SEC", "180")
)
LOCAL_OCR_WORKER_REQUEST_TIMEOUT_SEC = float(
    os.environ.get("MANTISFETCH_LOCAL_OCR_WORKER_REQUEST_TIMEOUT_SEC", "180")
)
LOCAL_OCR_CIRCUIT_BREAKER_SEC = float(
    os.environ.get("MANTISFETCH_LOCAL_OCR_CIRCUIT_BREAKER_SEC", "120")
)

# ── Local OCR worker state (mutated in place; single owner) ──
_local_ocr_worker: subprocess.Popen[str] | None = None
_local_ocr_worker_lock = threading.Lock()
_local_ocr_worker_ready = threading.Event()
_local_ocr_worker_initializing = threading.Event()
_local_ocr_disabled_until = 0.0


def gemini_ocr(image_bytes: bytes, page_num: int, *, proofread: bool | None = None) -> str:
    """OCR a single page image via the active LLM provider."""
    from providers import get_provider

    try:
        return get_provider().ocr(image_bytes, page_num, proofread=proofread)
    except Exception as exc:
        logger.warning("OCR unavailable for page %d: %s", page_num, exc)
        return t("ocr_failed", page=page_num)


def _is_ocr_failed_text(text: str | None) -> bool:
    if not text:
        return False
    value = text.strip()
    return value.startswith("[OCR failed") or value.startswith("[OCR 失败")



def _ocr_cache_path(doc_dir: Path, page_num: int) -> Path:
    cache_dir = doc_dir / ".cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / f"ocr_p{page_num:04d}.txt"


def _ocr_cache_variant_path(doc_dir: Path, key: str) -> Path:
    cache_dir = doc_dir / ".cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", key).strip("-") or "cache"
    return cache_dir / safe


def _ocr_cache_key(image_bytes: bytes) -> str:
    return hashlib.sha1(image_bytes).hexdigest()[:16]



def _local_ocr_worker_command() -> list[str]:
    raw = os.environ.get("MANTISFETCH_LOCAL_OCR_WORKER_CMD", "").strip()
    if raw:
        return shlex.split(raw)
    # engines.py lives at services/docreader/mantisfetch_docreader/ocr/; the
    # worker script sits at services/docreader/ — parents[2].
    worker = Path(__file__).resolve().parents[2] / "paddle_ocr_worker.py"
    return [sys.executable, str(worker)]


def _drain_local_ocr_worker_stderr(proc: subprocess.Popen[str]) -> None:
    assert proc.stderr is not None
    # Must never die: if this thread stops draining, the stderr pipe fills and
    # the worker blocks on write (deadlock). errors="replace" prevents decode
    # errors; this guard catches anything else.
    try:
        for line in proc.stderr:
            value = line.rstrip()
            if value:
                logger.info("[local-ocr-worker] %s", value)
    except Exception as exc:
        logger.warning("local OCR worker stderr drain stopped: %s", exc)


def _read_local_ocr_worker_message(proc: subprocess.Popen[str], timeout: float) -> dict[str, Any]:
    if proc.stdout is None:
        raise RuntimeError("local OCR worker stdout is unavailable")
    deadline = time.monotonic() + max(timeout, 0.1)
    # Read raw bytes from the fd (never the TextIOWrapper): a non-blocking
    # TextIOWrapper.read() surfaces the incremental decoder's TypeError on
    # Python 3.11 when no data is ready (only 3.12+ raises the catchable
    # BlockingIOError). Raw os.read + our own line buffering keeps the deadline
    # honoured regardless of runtime and can't block on a partial line (#45).
    fd = proc.stdout.fileno()
    os.set_blocking(fd, False)
    selector = selectors.DefaultSelector()
    selector.register(fd, selectors.EVENT_READ)
    buffer = b""
    try:
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(f"local OCR worker exited with code {proc.returncode}")
            remaining = max(deadline - time.monotonic(), 0.05)
            if not selector.select(timeout=remaining):
                continue  # nothing ready yet; re-check the deadline
            try:
                chunk = os.read(fd, 65536)
            except (BlockingIOError, InterruptedError):
                continue  # spurious wakeup, no data
            except OSError:
                continue
            if not chunk:  # b"" = EOF (surfaced next loop via proc.poll())
                continue
            buffer += chunk
            while b"\n" in buffer:
                raw_line, _, buffer = buffer.partition(b"\n")
                # Splitting on the \n byte is decode-safe: 0x0A never appears
                # inside a UTF-8 multibyte sequence.
                line = raw_line.decode("utf-8", errors="replace")
                if not line.strip():
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("Ignoring non-JSON local OCR worker output: %s", line.rstrip())
                    continue
                if isinstance(message, dict):
                    return message
        raise TimeoutError(f"local OCR worker timed out after {timeout:.1f}s")
    finally:
        selector.close()


def _mark_local_ocr_worker_unhealthy(reason: str) -> None:
    global _local_ocr_disabled_until
    _local_ocr_disabled_until = time.monotonic() + max(LOCAL_OCR_CIRCUIT_BREAKER_SEC, 0)
    logger.warning("Local OCR worker marked unhealthy: %s", reason)


def _stop_local_ocr_worker() -> None:
    global _local_ocr_worker
    proc = _local_ocr_worker
    _local_ocr_worker = None
    _local_ocr_worker_ready.clear()
    if not proc:
        return
    try:
        if proc.stdin:
            proc.stdin.close()
    except Exception:
        pass
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def _get_local_ocr_worker() -> subprocess.Popen[str]:
    global _local_ocr_worker
    if _local_ocr_disabled_until > time.monotonic():
        raise RuntimeError("local OCR worker is temporarily disabled after a crash")
    if _local_ocr_worker is not None and _local_ocr_worker.poll() is None:
        return _local_ocr_worker
    if _local_ocr_worker is not None:
        _stop_local_ocr_worker()

    _local_ocr_worker_initializing.set()
    try:
        cmd = _local_ocr_worker_command()
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            # Tolerant decode: native libs (Paddle/CUDA) can emit non-UTF-8 bytes
            # on stdout/stderr. Strict decoding would crash the protocol read
            # (#18) and kill the stderr drain thread, filling the pipe and
            # deadlocking the worker (#17).
            errors="replace",
            bufsize=1,
        )
        _local_ocr_worker = proc
        threading.Thread(
            target=_drain_local_ocr_worker_stderr,
            args=(proc,),
            daemon=True,
        ).start()
        message = _read_local_ocr_worker_message(
            proc, timeout=LOCAL_OCR_WORKER_STARTUP_TIMEOUT_SEC
        )
        if message.get("type") != "ready":
            error = message.get("error") or message
            _stop_local_ocr_worker()
            raise RuntimeError(f"local OCR worker startup failed: {error}")
        _local_ocr_worker_ready.set()
        return proc
    except Exception as exc:
        _stop_local_ocr_worker()
        _mark_local_ocr_worker_unhealthy(str(exc))
        raise
    finally:
        _local_ocr_worker_initializing.clear()


def _ocr_page_blocks_from_worker_response(
    page_num: int,
    response: dict[str, Any],
    source: str,
) -> OCRPageBlocks | None:
    raw_blocks = response.get("blocks")
    if not isinstance(raw_blocks, list):
        return None
    blocks: list[OCRTextBlock] = []
    for index, raw in enumerate(raw_blocks):
        if not isinstance(raw, dict):
            continue
        text = str(raw.get("text") or "").strip()
        if not text:
            continue
        try:
            bbox = tuple(_normalize_layout_bbox(raw.get("bbox") or [0, 0, 0, 0]))
        except (TypeError, ValueError):
            bbox = (0.0, 0.0, 0.0, 0.0)
        order = int(raw.get("order") if raw.get("order") is not None else index)
        line_index = int(raw.get("line_index") if raw.get("line_index") is not None else order)
        blocks.append(
            OCRTextBlock(
                block_id=f"p{page_num}-b{len(blocks) + 1:04d}",
                text=text,
                bbox=bbox,  # type: ignore[arg-type]
                confidence=float(raw.get("confidence") or 0.0),
                source=source,
                line_index=line_index,
                order=order,
            )
        )
    width = int(response.get("width") or 0)
    height = int(response.get("height") or 0)
    return OCRPageBlocks(page=page_num, width=width, height=height, blocks=tuple(blocks))


def local_ocr_with_layout(
    image_bytes: bytes,
    page_num: int,
    backend: str,
) -> tuple[str, OCRPageBlocks | None]:
    name = (backend or "").strip().lower()
    if name in {"", "none"}:
        return "", None
    if name != "paddleocr":
        raise RuntimeError(f"unsupported local OCR backend: {backend}")
    if not LOCAL_OCR_ENABLED:
        # Local OCR is switched off (e.g. the slim image ships no PaddleOCR). Never
        # spawn the worker — a defensive choke point so any caller, including ones
        # without their own LLM fallback, gets a clean miss instead of a crash.
        return t("ocr_failed", page=page_num), None
    with _local_ocr_worker_lock:
        try:
            proc = _get_local_ocr_worker()
            if proc.stdin is None:
                raise RuntimeError("local OCR worker stdin is unavailable")
            request = {
                "page_num": page_num,
                "image_b64": base64.b64encode(image_bytes).decode("ascii"),
            }
            proc.stdin.write(json.dumps(request) + "\n")
            proc.stdin.flush()
            response = _read_local_ocr_worker_message(
                proc, timeout=LOCAL_OCR_WORKER_REQUEST_TIMEOUT_SEC
            )
            if not response.get("ok"):
                logger.warning(
                    "Local OCR worker failed page %d via %s: %s",
                    page_num,
                    backend,
                    response.get("error") or response,
                )
                return t("ocr_failed", page=page_num), None
            text = str(response.get("text") or "").strip()
            page_blocks = _ocr_page_blocks_from_worker_response(
                page_num,
                response,
                source=f"local-{name}",
            )
            return text or t("ocr_failed", page=page_num), page_blocks
        except Exception as exc:
            _stop_local_ocr_worker()
            _mark_local_ocr_worker_unhealthy(str(exc))
            logger.warning("Local OCR unavailable for page %d via %s: %s", page_num, backend, exc)
            return t("ocr_failed", page=page_num), None


def local_ocr(image_bytes: bytes, page_num: int, backend: str) -> str:
    text, _page_blocks = local_ocr_with_layout(image_bytes, page_num, backend)
    return text
