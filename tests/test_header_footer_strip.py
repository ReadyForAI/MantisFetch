"""Tests for cross-page running header/footer stripping (native PDF text)."""

from larkscout_docreader.ocr_text import _strip_repeated_headers_footers


def _pages(*texts: str) -> dict[int, str]:
    return {i + 1: t for i, t in enumerate(texts)}


def test_repeated_header_removed_body_kept():
    banner = "魏桥新能源汽车科技建设项目"
    pages = _pages(
        f"{banner}\n第一章 总则\n正文一。",
        f"{banner}\n第二章 范围\n正文二。",
        f"{banner}\n第三章 要求\n正文三。",
        f"{banner}\n第四章 验收\n正文四。",
    )
    out = _strip_repeated_headers_footers(pages, 4)
    for pn in range(1, 5):
        assert banner not in out[pn]
        assert "正文" in out[pn]
    # Per-page chapter titles (each different) must survive.
    assert "第一章 总则" in out[1]


def test_page_number_footer_and_header_banner_removed():
    pages = _pages(
        "banner\nbody1\n1",
        "banner\nbody2\n2",
        "banner\nbody3\n3",
        "banner\nbody4\n4",
    )
    out = _strip_repeated_headers_footers(pages, 4)
    for pn in range(1, 5):
        # banner (top, repeated) and the bare page number (bottom) both go;
        # the per-page body line stays.
        assert out[pn].strip().splitlines() == [f"body{pn}"]


def test_numbered_body_lines_not_collapsed():
    # Edge lines that differ only by digits but carry text (item1/item2) are
    # distinct body content and must NOT be treated as a repeating template.
    pages = _pages(
        "item1\nmiddle a",
        "item2\nmiddle b",
        "item3\nmiddle c",
        "item4\nmiddle d",
    )
    out = _strip_repeated_headers_footers(pages, 4)
    for pn in range(1, 5):
        assert f"item{pn}" in out[pn]


def test_short_doc_untouched():
    pages = _pages("banner\nbody1", "banner\nbody2")
    out = _strip_repeated_headers_footers(pages, 2)
    assert out == pages  # below _HF_MIN_PAGES -> no-op


def test_mid_page_repetition_kept_edges_stripped():
    pages = _pages(
        "banner\nA1\ncommon line\nB1\nfoot",
        "banner\nA2\ncommon line\nB2\nfoot",
        "banner\nA3\ncommon line\nB3\nfoot",
        "banner\nA4\ncommon line\nB4\nfoot",
    )
    out = _strip_repeated_headers_footers(pages, 4)
    for pn in range(1, 5):
        assert "common line" in out[pn]  # mid-page (outside edge window) -> kept
        assert "banner" not in out[pn]   # top edge, repeated -> dropped
        assert "foot" not in out[pn]     # bottom edge, repeated -> dropped
        assert f"A{pn}" in out[pn]       # edge but unique -> kept
        assert f"B{pn}" in out[pn]


def test_numeric_heading_not_page_number_kept():
    # Standalone numeric headings whose value does not track the page index
    # must NOT be collapsed to the page-number sentinel and deleted.
    pages = _pages(
        "banner\n10\nScope text",
        "banner\n20\nDefinitions text",
        "banner\n30\nRequirements text",
        "banner\n40\nAcceptance text",
    )
    out = _strip_repeated_headers_footers(pages, 4)
    assert "10" in out[1]  # value 10 != page 1 -> body numbering, kept
    assert "20" in out[2]
    assert "banner" not in out[1]  # the real running header still goes


def test_odd_page_count_uses_ceiling_threshold():
    # 5 pages: an edge line on only 2 pages (40%) is below "at least half"
    # and must be kept (ceil(5*0.5) == 3, not floor == 2).
    pages = _pages(
        "edge x\nbody1",
        "edge x\nbody2",
        "uniq3\nbody3",
        "uniq4\nbody4",
        "uniq5\nbody5",
    )
    out = _strip_repeated_headers_footers(pages, 5)
    assert "edge x" in out[1]
    assert "edge x" in out[2]


def test_rare_edge_line_kept():
    pages = _pages(
        "banner\nbody1",
        "banner\nbody2",
        "banner\nbody3",
        "unique top\nbody4",
    )
    out = _strip_repeated_headers_footers(pages, 4)
    assert "unique top" in out[4]
    assert "banner" not in out[1]
