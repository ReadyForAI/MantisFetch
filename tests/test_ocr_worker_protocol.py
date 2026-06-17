"""Tests for the local-OCR worker stdout protocol reader (deadline safety)."""

import subprocess
import sys
import time

import pytest
from mantisfetch_docreader.ocr.engines import _read_local_ocr_worker_message


def _proc(script: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
        bufsize=1,
    )


def test_reads_complete_message():
    p = _proc(
        "import sys,time; sys.stdout.write('{\"ok\": true, \"page_num\": 3}\\n');"
        " sys.stdout.flush(); time.sleep(1)"
    )
    try:
        assert _read_local_ocr_worker_message(p, timeout=2.0) == {"ok": True, "page_num": 3}
    finally:
        p.kill()


def test_buffers_partial_then_complete_line():
    p = _proc(
        "import sys,time; sys.stdout.write('{\"ok\":'); sys.stdout.flush();"
        " time.sleep(0.2); sys.stdout.write(' true}\\n'); sys.stdout.flush(); time.sleep(1)"
    )
    try:
        assert _read_local_ocr_worker_message(p, timeout=3.0) == {"ok": True}
    finally:
        p.kill()


def test_partial_line_does_not_block_past_deadline():
    # #45: a partial line (no newline) followed by a long pause must time out
    # near the deadline, not block until the worker eventually finishes.
    p = _proc("import sys,time; sys.stdout.write('{\"partial\":'); sys.stdout.flush(); time.sleep(5)")
    try:
        t0 = time.monotonic()
        with pytest.raises(TimeoutError):
            _read_local_ocr_worker_message(p, timeout=0.5)
        assert time.monotonic() - t0 < 2.0  # did not block for the worker's 5s
    finally:
        p.kill()
