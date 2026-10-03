"""A local OCR cache hit gives the page back with its layout (F07).

The page cache held only the text. A second parse of the same PDF found every
page cached, restored the text and nothing else, and so produced no
ocr_blocks — and the replace that followed deleted the ocr_blocks.json the
first parse had written. A cache is supposed to change what a parse costs,
not what it produces.
"""

from __future__ import annotations

import json
from pathlib import Path

import mantisfetch_docreader as dr
import pytest
from mantisfetch_docreader.models import OCRPageBlocks, OCRTextBlock
from starlette.testclient import TestClient

import mantisfetch_common.storage as cs


@pytest.fixture()
def docs_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    d = tmp_path / "docs"
    d.mkdir()
    monkeypatch.setattr(cs, "DEFAULT_DOCS_DIR", d)
    return d


def _scan(path: Path) -> bytes:
    import fitz

    doc = fitz.open()
    page = doc.new_page()
    page.draw_rect(fitz.Rect(50, 50, 300, 120), color=(0, 0, 0), fill=(0.2, 0.2, 0.2))
    doc.save(str(path))
    doc.close()
    return path.read_bytes()


@pytest.fixture()
def ocr_calls(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls: list[int] = []

    def fake_local(img_bytes, page_num, backend):
        calls.append(page_num)
        return (
            "甲方：测试公司",
            OCRPageBlocks(
                page=page_num,
                width=612,
                height=792,
                blocks=(
                    OCRTextBlock(
                        block_id=f"p{page_num}-b0001",
                        text="甲方：测试公司",
                        bbox=(50.0, 50.0, 300.0, 120.0),
                        confidence=0.93,
                        source="local-paddleocr",
                    ),
                ),
            ),
        )

    monkeypatch.setattr(dr, "local_ocr_with_layout", fake_local)
    monkeypatch.setattr(dr, "PREWARM_LOCAL_OCR", False)
    return calls


def _parse(client: TestClient, pdf: bytes, **extra):
    return client.post(
        "/parse",
        files={"file": ("contract.pdf", pdf, "application/pdf")},
        data={
            "doc_id": "DOC-9101",
            "summary_mode": "off",
            "document_profile": "contract_cn",
            "parse_mode": "fast",
            **extra,
        },
    )


def test_reparsing_a_cached_pdf_keeps_its_layout(
    docs_dir: Path, tmp_path: Path, ocr_calls: list[int]
) -> None:
    client = TestClient(dr.app, raise_server_exceptions=False)
    pdf = _scan(tmp_path / "contract.pdf")
    sidecar = docs_dir / "General" / "DOC-9101" / "ocr_blocks.json"

    first = _parse(client, pdf)
    assert first.status_code == 200, first.text
    assert ocr_calls == [1], "the first parse did not OCR the page"
    before = json.loads(sidecar.read_text(encoding="utf-8"))

    second = _parse(client, pdf, replace="true")
    assert second.status_code == 200, second.text
    assert ocr_calls == [1], "the second parse missed the cache"
    assert sidecar.exists(), "a cache hit deleted the layout sidecar"
    assert json.loads(sidecar.read_text(encoding="utf-8"))["pages"] == before["pages"]


def test_an_old_text_only_entry_is_re_ocred_not_trusted(
    docs_dir: Path, tmp_path: Path, ocr_calls: list[int]
) -> None:
    """The entries written before this fix cannot answer for the layout."""
    client = TestClient(dr.app, raise_server_exceptions=False)
    pdf = _scan(tmp_path / "contract.pdf")
    assert _parse(client, pdf).status_code == 200
    cache = docs_dir / "General" / "DOC-9101" / ".cache"
    for entry in cache.glob("ocr_p*.page.json"):
        entry.with_name(entry.name.replace(".page.json", ".txt")).write_text(
            "甲方：测试公司", encoding="utf-8"
        )
        entry.unlink()

    assert _parse(client, pdf, replace="true").status_code == 200
    assert ocr_calls == [1, 1], "a text-only entry was served as if it held the layout"
    assert (docs_dir / "General" / "DOC-9101" / "ocr_blocks.json").exists()


def test_a_damaged_entry_is_a_miss(docs_dir: Path, tmp_path: Path, ocr_calls: list[int]) -> None:
    client = TestClient(dr.app, raise_server_exceptions=False)
    pdf = _scan(tmp_path / "contract.pdf")
    assert _parse(client, pdf).status_code == 200
    for entry in (docs_dir / "General" / "DOC-9101" / ".cache").glob("ocr_p*.page.json"):
        entry.write_text('{"schema": 1, "text": "x", "layout": {"page": "one"}}', encoding="utf-8")

    assert _parse(client, pdf, replace="true").status_code == 200
    assert ocr_calls == [1, 1]
