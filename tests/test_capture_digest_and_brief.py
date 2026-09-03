"""The first hop answers "what is here and what do I read next", and the L2
tier exists without spending a token.

Before this, the digest was section snippets only, so an agent had to spend a
doc_sections round-trip just to learn the sids — and doc_brief 404'd for every
capture unless summary_mode="defer" had paid an LLM to write one, which left the
middle tier of a three-tier protocol missing on the default path.

A third case: the SSRF guard aborts with "addressunreachable", which Chromium
reports as net::ERR_ADDRESS_UNREACHABLE — indistinguishable from the network
being down. On a host behind a fake-ip proxy that is every page, and identifying
it cost a full debugging session.
"""

import json
from pathlib import Path

import mantisfetch_browser as lb
import pytest
from mantisfetch_browser.security import blocked_target_hint


def _sections() -> list[dict]:
    return [
        {"sid": "s_001", "h": "Description", "t": "First sentence here. Second one too. "
         "A third that the brief should drop.", "type": "text"},
        {"sid": "s_002", "h": "Eyes", "t": "Mantis shrimp have sixteen photoreceptor types.",
         "type": "text"},
        {"sid": "t_001", "h": "[Table] Revenue", "type": "table",
         "t": "| R | A |\n| --- | --- |\n| N | 1 |",
         "table_meta": {"rows": 223, "cols": 2, "caption": "Revenue by region"}},
    ]


# ── the digest carries the outline ──────────────────────────────────────────────
def test_digest_lists_sids_so_no_extra_round_trip_is_needed() -> None:
    digest = lb._build_web_digest("Mantis shrimp", _sections())
    assert "## Outline" in digest
    for sid in ("s_001", "s_002", "t_001"):
        assert sid in digest


def test_digest_lists_tables_too() -> None:
    """Tables were skipped, so a page whose substance was a table looked empty."""
    digest = lb._build_web_digest("GDP", _sections())
    assert "t_001" in digest
    assert "223 rows" in digest


def test_digest_still_carries_an_opening_snippet() -> None:
    digest = lb._build_web_digest("Mantis shrimp", _sections())
    assert "First sentence here." in digest


def test_digest_is_bounded_on_a_deep_heading_tree() -> None:
    """max_sections is unlimited for capture, so the outline needs its own cap or
    the cheap first hop stops being cheap."""
    many = [
        {"sid": f"s_{n:03d}", "h": f"Heading number {n} with a long title", "type": "text",
         "t": "Body text for this section."}
        for n in range(200)
    ]
    digest = lb._build_web_digest("Big", many)
    assert len(digest) <= 1400
    # The marker must close the outline block — not merely appear somewhere, which
    # a sliced tail would also satisfy while leaving a half-written sid behind.
    outline = digest.split("## Outline\n", 1)[1].split("\n\n", 1)[0].split("\n")
    assert outline[-1].startswith("- ... ") and "more" in outline[-1]
    # and every entry before it is whole
    for line in outline[:-1]:
        assert line.startswith("- s_") and ") " in line, f"truncated entry: {line!r}"


def test_digest_titles_are_truncated_not_dropped() -> None:
    long_title = "A" * 300
    digest = lb._build_web_digest("T", [{"sid": "s_1", "h": long_title, "t": "body", "type": "text"}])
    assert "s_1" in digest
    assert long_title not in digest


# ── the local L2 brief ──────────────────────────────────────────────────────────
def test_brief_is_written_without_an_llm(tmp_path: Path) -> None:
    lb._persist_web_capture(
        doc_id="WEB-910", url="https://example.com/a", title="Example",
        sections=_sections(), digest="d", tags=[], content_hash="sha256:a",
        docs_dir=tmp_path, content_type="General", extract_tables=True,
        requested_url="https://example.com/a", lang="en-US",
    )
    doc_dir = tmp_path / "General" / "WEB-910"
    brief = (doc_dir / "brief.md").read_text(encoding="utf-8")

    assert "Description" in brief and "Eyes" in brief
    assert "photoreceptor" in brief
    # tables contribute their shape, not their rows
    assert "223 rows" in brief
    assert "| N | 1 |" not in brief
    # and the manifest advertises it
    manifest = json.loads((doc_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["paths"]["brief"] == "brief.md"


def test_brief_keeps_the_opening_sentences_only() -> None:
    brief = lb._build_web_brief("T", "https://e.com", _sections(), [])
    assert "First sentence here. Second one too." in brief
    assert "A third that the brief should drop." not in brief


def test_brief_sits_between_digest_and_full() -> None:
    """It is the middle tier: richer than the digest, far cheaper than the body."""
    sections = _sections()
    digest = lb._build_web_digest("T", sections)
    brief = lb._build_web_brief("T", "https://e.com", [s for s in sections if s["type"] != "table"],
                                [s for s in sections if s["type"] == "table"])
    full = lb._build_web_full_text("T", "https://e.com",
                                   [s for s in sections if s["type"] != "table"],
                                   [s for s in sections if s["type"] == "table"])
    assert len(digest) < len(full)
    assert len(brief) < len(full)


def test_brief_is_bounded() -> None:
    many = [{"sid": f"s_{n}", "h": f"H{n}", "t": "Sentence one. " * 40, "type": "text"}
            for n in range(200)]
    assert len(lb._build_web_brief("T", "https://e.com", many, [])) <= 6001


# ── the guard says why, not just that it failed ─────────────────────────────────
def test_hint_names_the_resolved_address(monkeypatch) -> None:
    import mantisfetch_browser.security as sec

    monkeypatch.setattr(sec.socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("198.18.0.114", 0))])
    hint = blocked_target_hint("https://en.wikipedia.org/wiki/X")
    assert "198.18.0.114" in hint
    assert "en.wikipedia.org" in hint


def test_hint_blames_a_proxy_only_for_the_fake_ip_range(monkeypatch) -> None:
    """A host that genuinely names a private target is not a proxy problem, and
    saying so would send the reader down the wrong path."""
    import mantisfetch_browser.security as sec

    monkeypatch.setattr(sec.socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("198.18.0.114", 0))])
    assert "fake-ip" in blocked_target_hint("https://example.com")

    monkeypatch.setattr(sec.socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("192.168.1.10", 0))])
    hint = blocked_target_hint("https://internal.example")
    assert "192.168.1.10" in hint
    assert "fake-ip" not in hint


def test_hint_is_none_for_a_reachable_public_address(monkeypatch) -> None:
    """Then the failure was something else and the hint must not guess."""
    import mantisfetch_browser.security as sec

    monkeypatch.setattr(sec.socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))])
    assert blocked_target_hint("https://example.com") is None


def test_hint_is_none_when_only_some_addresses_are_private(monkeypatch) -> None:
    """A split-horizon name that also has a public address is not the fake-ip
    case, and the guard would have let the public one through."""
    import mantisfetch_browser.security as sec

    monkeypatch.setattr(sec.socket, "getaddrinfo", lambda *a, **k: [
        (2, 1, 6, "", ("10.0.0.5", 0)), (2, 1, 6, "", ("93.184.216.34", 0))])
    assert blocked_target_hint("https://example.com") is None


@pytest.mark.parametrize("url", ["not a url", "https://", "http://[bad", ""])
def test_hint_survives_junk_input(url: str) -> None:
    assert blocked_target_hint(url) is None


# ── the hint reaches the caller, not just the unit test ─────────────────────────
# The first version of this change added blocked_target_hint and its tests but
# never called it: the edit that wired it into the goto failure path was lost in
# a script that aborted before writing. The function was covered and the product
# behaviour was unchanged, which is exactly the shape of bug a unit test alone
# cannot catch.
def _blocked_browser(monkeypatch, tmp_path: Path):
    from unittest.mock import AsyncMock, MagicMock

    import mantisfetch_browser.security as sec

    monkeypatch.setattr(sec.socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("198.18.0.114", 0))])
    page = AsyncMock()
    page.goto = AsyncMock(side_effect=Exception(
        "Page.goto: net::ERR_ADDRESS_UNREACHABLE at https://example.com"))
    context = AsyncMock()
    context.new_page = AsyncMock(return_value=page)
    browser = MagicMock()
    browser.new_context = AsyncMock(return_value=context)
    monkeypatch.setattr(lb, "_browser", browser)
    monkeypatch.setattr(lb, "_setup_routing", AsyncMock())
    monkeypatch.setattr(lb, "_get_docs_dir", lambda: tmp_path)


def test_capture_502_explains_a_policy_refusal(client, monkeypatch, tmp_path: Path) -> None:
    _blocked_browser(monkeypatch, tmp_path)
    resp = client.post("/web/capture", json={"url": "https://example.com"})
    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert "198.18.0.114" in detail
    assert "fake-ip" in detail
    assert "ERR_ADDRESS_UNREACHABLE" not in detail


def test_session_goto_502_explains_a_policy_refusal(client, monkeypatch, tmp_path: Path) -> None:
    _blocked_browser(monkeypatch, tmp_path)
    sid = client.post("/web/session/new", json={}).json()["session_id"]
    try:
        resp = client.post(
            "/web/session/goto", json={"session_id": sid, "url": "https://example.com"}
        )
        assert resp.status_code == 502
        assert "198.18.0.114" in resp.json()["detail"]
    finally:
        client.post("/web/session/close", json={"session_id": sid})


def test_a_real_network_failure_keeps_its_own_message(client, monkeypatch, tmp_path: Path) -> None:
    """When the address is fine the hint must stay out of the way."""
    from unittest.mock import AsyncMock, MagicMock

    import mantisfetch_browser.security as sec

    monkeypatch.setattr(sec.socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))])
    page = AsyncMock()
    page.goto = AsyncMock(side_effect=Exception("Page.goto: Timeout 25000ms exceeded"))
    context = AsyncMock()
    context.new_page = AsyncMock(return_value=page)
    browser = MagicMock()
    browser.new_context = AsyncMock(return_value=context)
    monkeypatch.setattr(lb, "_browser", browser)
    monkeypatch.setattr(lb, "_setup_routing", AsyncMock())
    monkeypatch.setattr(lb, "_get_docs_dir", lambda: tmp_path)

    resp = client.post("/web/capture", json={"url": "https://example.com"})
    assert resp.status_code == 502
    assert "Timeout 25000ms" in resp.json()["detail"]
    assert "SSRF" not in resp.json()["detail"]
