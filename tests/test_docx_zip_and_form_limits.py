"""B6: DOCX decompression-bomb guard. B7: /doc/parse numeric Form ranges."""

import io
import zipfile
from pathlib import Path

import pytest
from fastapi import HTTPException
from starlette.testclient import TestClient


# ── B6: DOCX zip-bomb pre-flight ────────────────────────────────────────────
def test_docx_budget_rejects_oversized_entry(tmp_path: Path) -> None:
    from mantisfetch_docreader.word import _check_docx_unzip_budget

    path = tmp_path / "bomb.docx"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        # 80 MB of zeros compresses to ~KB but reports 80 MB uncompressed (> 64 MB).
        zf.writestr("word/document.xml", b"\x00" * (80 * 1024 * 1024))

    with pytest.raises(HTTPException) as exc:
        _check_docx_unzip_budget(path)
    assert exc.value.status_code == 422


def test_docx_budget_rejects_oversized_total(tmp_path: Path, monkeypatch) -> None:
    import mantisfetch_docreader.word as word

    monkeypatch.setattr(word, "_MAX_DOCX_ENTRY_BYTES", 10 * 1024 * 1024)
    monkeypatch.setattr(word, "_MAX_DOCX_UNZIP_BYTES", 15 * 1024 * 1024)

    path = tmp_path / "bomb-total.docx"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for i in range(3):  # 3 x 8 MB = 24 MB total (each under the entry cap)
            zf.writestr(f"word/media/img{i}.bin", b"\x00" * (8 * 1024 * 1024))

    with pytest.raises(HTTPException) as exc:
        word._check_docx_unzip_budget(path)
    assert exc.value.status_code == 422


def test_count_refs_also_enforces_budget(tmp_path: Path) -> None:
    # The image-count pre-scan (run before parse_word) must enforce the budget too.
    from mantisfetch_docreader.word import _count_word_embedded_image_references

    path = tmp_path / "bomb-count.docx"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", b"\x00" * (80 * 1024 * 1024))

    with pytest.raises(HTTPException) as exc:
        _count_word_embedded_image_references(path)
    assert exc.value.status_code == 422


def test_docx_budget_allows_normal_document(tmp_path: Path) -> None:
    from mantisfetch_docreader.word import _check_docx_unzip_budget

    path = tmp_path / "ok.docx"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", b"<w:document/>")
    _check_docx_unzip_budget(path)  # must not raise


# ── B7: /doc/parse numeric Form ranges ──────────────────────────────────────
def _parse(client: TestClient, **data) -> int:
    resp = client.post(
        "/doc/parse",
        files={"file": ("x.pdf", io.BytesIO(b"%PDF-1.4 fake"), "application/pdf")},
        data={"generate_summary": "false", **data},
    )
    return resp.status_code


def test_parse_rejects_out_of_range_concurrency(client: TestClient) -> None:
    assert _parse(client, concurrency="0") == 422
    assert _parse(client, concurrency="999") == 422


def test_parse_rejects_negative_max_tables_per_page(client: TestClient) -> None:
    assert _parse(client, max_tables_per_page="-1") == 422
