"""Tests for the a11y-first dual-strategy action model and the pre-click
occlusion guard (browser /web hardening).

All browser I/O is mocked — no real Playwright browser is started.
"""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import mantisfetch_browser as mb
import pytest
from mantisfetch_browser import (
    _a11y_actions_from_pairs,
    _click_blocker,
    _extract_actions_dom,
    _locate,
    _merge_actions,
)
from mantisfetch_browser.session import Session
from starlette.testclient import TestClient


def _loc(count: int) -> MagicMock:
    """A Playwright-locator mock whose async count() resolves to ``count``."""
    loc = MagicMock()
    loc.count = AsyncMock(return_value=count)
    return loc


# ── _merge_actions (pure) ──────────────────────────────────────────────────────


def test_merge_folds_dom_css_into_a11y_identity() -> None:
    a11y = [
        {
            "aid": "a1",
            "role": "button",
            "name": "Submit",
            "source": "a11y",
            "confidence": 0.85,
            "actions": ["click"],
            "strategy": {"type": "role", "role": "button", "name": "Submit", "nth": 0},
        }
    ]
    dom = [
        {
            "aid": "d1",
            "role": "button",
            "name": "Submit",
            "source": "dom",
            "confidence": 0.8,
            "actions": ["click"],
            "strategy": {
                "type": "role",
                "role": "button",
                "name": "Submit",
                "nth": 0,
                "css": "button.submit",
            },
        },
        {
            "aid": "d2",
            "role": "button",
            "name": "Submit",
            "source": "dom",
            "confidence": 0.8,
            "actions": ["click"],
            "strategy": {
                "type": "role",
                "role": "button",
                "name": "Submit",
                "nth": 1,
                "css": "button.submit2",
            },
        },
        {
            "aid": "d3",
            "role": "",
            "name": "",
            "source": "dom",
            "confidence": 0.8,
            "actions": ["click"],
            "strategy": {"type": "css", "selector": ".extra"},
        },
    ]

    merged = _merge_actions(a11y, dom)

    # a11y identity wins (same aid), DOM css folds in, dom-only entries appended.
    assert [m["aid"] for m in merged] == ["a1", "d2", "d3"]
    assert merged[0]["strategy"]["css"] == "button.submit"
    assert merged[0]["confidence"] == 0.85  # max(0.85, 0.8)
    assert merged[1]["strategy"]["nth"] == 1
    assert merged[2]["strategy"]["selector"] == ".extra"
    # inputs are not mutated
    assert "css" not in a11y[0]["strategy"]


def test_merge_keeps_existing_css_over_secondary() -> None:
    primary = [
        {
            "aid": "a1",
            "role": "link",
            "name": "Home",
            "confidence": 0.85,
            "strategy": {"type": "role", "role": "link", "name": "Home", "nth": 0, "css": "a#home"},
        }
    ]
    secondary = [
        {
            "aid": "d1",
            "role": "link",
            "name": "Home",
            "confidence": 0.8,
            "strategy": {
                "type": "role",
                "role": "link",
                "name": "Home",
                "nth": 0,
                "css": "a.other",
            },
        }
    ]
    merged = _merge_actions(primary, secondary)
    assert len(merged) == 1
    assert merged[0]["strategy"]["css"] == "a#home"  # primary css preserved


# ── a11y nth assignment ────────────────────────────────────────────────────────


def test_a11y_pairs_assign_nth_to_duplicates() -> None:
    pairs = [("button", "Add"), ("button", "Add"), ("link", "Docs")]
    actions = _a11y_actions_from_pairs(pairs, max_actions=10, confidence=0.85)

    assert [a["strategy"]["nth"] for a in actions] == [0, 1, 0]
    # duplicates stay individually addressable: distinct aids
    assert actions[0]["aid"] != actions[1]["aid"]
    assert all(a["source"] == "a11y" for a in actions)


def test_a11y_pairs_respect_max_actions() -> None:
    pairs = [("button", f"B{i}") for i in range(20)]
    actions = _a11y_actions_from_pairs(pairs, max_actions=5, confidence=0.85)
    assert len(actions) == 5


# ── DOM extraction keeps css as a fallback ─────────────────────────────────────


@pytest.mark.asyncio
async def test_dom_extraction_carries_css_fallback_and_nth() -> None:
    raw = [
        {"role": "button", "name": "Go", "strategy": {"css": "button#go"}, "actions": ["click"]},
        {"role": "button", "name": "Go", "strategy": {"css": "button.go2"}, "actions": ["click"]},
        {"role": "", "name": "", "strategy": {"css": ".bare"}, "actions": ["scroll_into_view"]},
    ]
    page = MagicMock()
    page.evaluate = AsyncMock(return_value=raw)

    actions = await _extract_actions_dom(page, max_actions=60)

    assert actions[0]["strategy"] == {
        "type": "role",
        "role": "button",
        "name": "Go",
        "nth": 0,
        "css": "button#go",
    }
    assert actions[1]["strategy"]["nth"] == 1
    assert actions[0]["aid"] != actions[1]["aid"]
    # unnamed element stays a css-only strategy
    assert actions[2]["strategy"] == {"type": "css", "selector": ".bare"}


# ── _locate resilient chain ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_locate_prefers_role_identity() -> None:
    page = MagicMock()
    base = MagicMock()
    nth0 = _loc(1)
    base.nth = MagicMock(return_value=nth0)
    page.get_by_role = MagicMock(return_value=base)

    result = await _locate(page, {"type": "role", "role": "button", "name": "Go", "nth": 0})
    assert result is nth0
    page.get_by_role.assert_called_once_with("button", name="Go")


@pytest.mark.asyncio
async def test_locate_falls_back_to_css_when_identity_empty() -> None:
    page = MagicMock()
    base = MagicMock()
    base.nth = MagicMock(return_value=_loc(0))  # identity matches nothing
    page.get_by_role = MagicMock(return_value=base)
    css_first = _loc(1)
    css_loc = MagicMock()
    css_loc.first = css_first
    page.locator = MagicMock(return_value=css_loc)

    result = await _locate(
        page, {"type": "role", "role": "button", "name": "Go", "nth": 0, "css": "button#go"}
    )
    assert result is css_first
    page.locator.assert_called_once_with("button#go")


@pytest.mark.asyncio
async def test_locate_keeps_identity_when_nothing_resolves_yet() -> None:
    """When neither identity nor css currently resolves (e.g. an SPA transiently
    detached the node between distill and act), _locate returns the identity
    locator so Playwright's own actionability wait still applies at click/fill
    time — it must not fail fast."""
    page = MagicMock()
    base = MagicMock()
    nth0 = _loc(0)
    base.nth = MagicMock(return_value=nth0)
    page.get_by_role = MagicMock(return_value=base)
    css_loc = MagicMock()
    css_loc.first = _loc(0)
    page.locator = MagicMock(return_value=css_loc)

    result = await _locate(
        page, {"type": "role", "role": "button", "name": "Gone", "nth": 0, "css": ".x"}
    )
    assert result is nth0  # identity locator returned, no RuntimeError


# ── _click_blocker (pre-click occlusion hit-test) ──────────────────────────────


@pytest.mark.asyncio
async def test_click_blocker_returns_occluder_description() -> None:
    handle = AsyncMock()
    handle.dispose = AsyncMock()
    locator = MagicMock()
    locator.element_handle = AsyncMock(return_value=handle)
    page = MagicMock()
    page.evaluate = AsyncMock(return_value="div#cookie-banner")

    blocker = await _click_blocker(page, locator)
    assert blocker == "div#cookie-banner"
    handle.dispose.assert_awaited_once()


@pytest.mark.asyncio
async def test_click_blocker_none_when_clear() -> None:
    handle = AsyncMock()
    handle.dispose = AsyncMock()
    locator = MagicMock()
    locator.element_handle = AsyncMock(return_value=handle)
    page = MagicMock()
    page.evaluate = AsyncMock(return_value=None)
    assert await _click_blocker(page, locator) is None


@pytest.mark.asyncio
async def test_click_blocker_best_effort_swallows_errors() -> None:
    locator = MagicMock()
    locator.element_handle = AsyncMock(side_effect=RuntimeError("detached"))
    page = MagicMock()
    assert await _click_blocker(page, locator) is None


# ── act() returns 409 on occlusion ─────────────────────────────────────────────


def _register_session(sid: str, aid: str) -> Session:
    page = MagicMock()
    page.url = "https://example.com"
    context = MagicMock()
    context.pages = [page]
    sess = Session(context=context, page=page, lang="en")
    sess.last_distill = {
        "url": "https://example.com",
        "title": "T",
        "content_hash": "sha256:x",
        "sections": [],
        "actions": [],
        "meta": {},
    }
    sess.action_map = {
        aid: {
            "aid": aid,
            "role": "button",
            "name": "Buy",
            "actions": ["click"],
            "strategy": {"type": "role", "role": "button", "name": "Buy", "nth": 0},
        }
    }
    mb.sessions._sessions[sid] = (time.time(), sess)
    return sess


def test_act_click_occluded_returns_409(client: TestClient) -> None:
    _register_session("S-OCCL", "A1")
    locator = MagicMock()
    locator.wait_for = AsyncMock()
    try:
        with (
            patch("mantisfetch_browser._locate", new=AsyncMock(return_value=locator)),
            patch("mantisfetch_browser._click_blocker", new=AsyncMock(return_value="div#overlay")),
        ):
            resp = client.post(
                "/web/session/act",
                json={"session_id": "S-OCCL", "aid": "A1", "action": "click"},
            )
    finally:
        mb.sessions._sessions.pop("S-OCCL", None)

    assert resp.status_code == 409
    assert "div#overlay" in resp.json()["detail"]


def test_act_click_proceeds_when_not_occluded(client: TestClient) -> None:
    _register_session("S-CLEAR", "A1")
    locator = MagicMock()
    locator.wait_for = AsyncMock()
    locator.click = AsyncMock()
    after = {
        "url": "https://example.com",
        "title": "T",
        "content_hash": "sha256:y",
        "sections": [],
        "actions": [],
        "meta": {},
    }
    try:
        with (
            patch("mantisfetch_browser._locate", new=AsyncMock(return_value=locator)),
            patch("mantisfetch_browser._click_blocker", new=AsyncMock(return_value=None)),
            patch("mantisfetch_browser._distill", new=AsyncMock(return_value=after)),
        ):
            resp = client.post(
                "/web/session/act",
                json={"session_id": "S-CLEAR", "aid": "A1", "action": "click"},
            )
    finally:
        mb.sessions._sessions.pop("S-CLEAR", None)

    assert resp.status_code == 200
    locator.click.assert_awaited_once()
