"""The catch-all parser must hand over what it found.

Two findings from a doc-parse evaluation against v1.7.2:

- an HTML page with one table reported ``table_count`` 0 and wrote no sidecar,
  while DOCX, XLSX and CSV all produced one. The count was being computed and
  then dropped: the sidecar writer reads ``PageContent.tables``, and
  ``parse_generic`` never filled it.
- a 2-slide PPTX reported ``total_pages`` 1, and every page citation read
  p.1-1. The number came from ``len(markdown) // 3000``.
"""

from pathlib import Path

import pytest
from starlette.testclient import TestClient

HTML_WITH_TABLE = (
    b"<html><body><h1>Probe</h1>"
    b"<p>Prose before the table.</p>"
    b"<table><tr><th>Metric</th><th>Value</th></tr>"
    b"<tr><td>throughput</td><td>4.4</td></tr>"
    b"<tr><td>errors</td><td>0</td></tr></table>"
    b"</body></html>"
)

TWO_SLIDES = """# Deck

<!-- Slide number: 1 -->
First slide body.

<!-- Slide number: 2 -->
Second slide body.
"""


def _parse_generic(tmp_path: Path, name: str, data: bytes, **kw):
    from mantisfetch_docreader.tabular import parse_generic

    path = tmp_path / name
    path.write_bytes(data)
    return parse_generic(path, **kw)


# ── tables reach the page, not just the counter ──────────────────────────────────
def test_an_html_table_becomes_page_content(tmp_path: Path) -> None:
    parsed = _parse_generic(tmp_path, "probe.html", HTML_WITH_TABLE)

    assert parsed.table_count == 1
    assert [len(p.tables) for p in parsed.pages] == [1]
    assert "throughput" in parsed.pages[0].tables[0]


def test_extract_tables_false_hands_over_nothing(tmp_path: Path) -> None:
    """A caller that turned tables off must not get sidecars anyway — and the
    count has to agree with what was handed over, which is the bug in the other
    direction."""
    parsed = _parse_generic(tmp_path, "probe.html", HTML_WITH_TABLE, extract_tables=False)

    assert parsed.table_count == 0
    assert parsed.pages[0].tables == []


def test_the_table_reaches_the_library_as_a_sidecar(client: TestClient) -> None:
    """The end the evaluation measured: table_count in the response, and a
    readable sidecar behind it."""
    resp = client.post(
        "/doc/parse",
        files={"file": ("probe.html", HTML_WITH_TABLE, "application/octet-stream")},
        data={
            "summary_mode": "off",
            "generate_summary": "false",
            "extract_tables": "true",
            "content_type": "General",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["table_count"] == 1

    doc_id = body["doc_id"]
    manifest = client.get(f"/doc/library/{doc_id}/manifest").json()
    table_ids = [t["table_id"] for t in manifest.get("tables", [])]
    assert table_ids, "the table was counted but no sidecar was written"

    table = client.get(f"/doc/library/{doc_id}/table/{table_ids[0]}")
    assert table.status_code == 200
    assert "throughput" in table.json()["content"]


# ── a deck's page count is its slide count ───────────────────────────────────────
def test_slide_markers_are_the_page_count(tmp_path: Path, monkeypatch) -> None:
    import mantisfetch_docreader as dr

    monkeypatch.setattr(dr, "_convert_to_markdown", lambda _p: TWO_SLIDES)
    parsed = _parse_generic(tmp_path, "deck.pptx", b"unused, conversion is patched")

    assert parsed.total_pages == 2


@pytest.mark.parametrize("slides", [1, 3, 17])
def test_the_count_follows_the_deck(tmp_path: Path, monkeypatch, slides: int) -> None:
    import mantisfetch_docreader as dr

    text = "# Deck\n" + "".join(
        f"\n<!-- Slide number: {i} -->\nBody {i}.\n" for i in range(1, slides + 1)
    )
    monkeypatch.setattr(dr, "_convert_to_markdown", lambda _p: text)
    assert _parse_generic(tmp_path, "deck.pptx", b"x").total_pages == slides


def test_a_format_with_no_slide_markers_keeps_the_estimate(
    tmp_path: Path, monkeypatch
) -> None:
    """parse_generic is the catch-all. HTML has no slides, so it falls back to
    the length estimate rather than reporting zero pages."""
    import mantisfetch_docreader as dr

    monkeypatch.setattr(dr, "_convert_to_markdown", lambda _p: "x" * 9500)
    parsed = _parse_generic(tmp_path, "page.html", b"x")

    assert parsed.total_pages == 3


def test_a_short_document_is_still_one_page(tmp_path: Path, monkeypatch) -> None:
    import mantisfetch_docreader as dr

    monkeypatch.setattr(dr, "_convert_to_markdown", lambda _p: "short")
    assert _parse_generic(tmp_path, "page.html", b"x").total_pages == 1
