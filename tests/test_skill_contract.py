"""The SKILL files agents load must describe the surface that actually exists.

Three invariants, each one a drift that has happened:

- every REST endpoint a SKILL names (``POST /doc/parse`` …) is a route the
  server serves, under the mount prefix it is documented with;
- the Chinese twin of each SKILL names the same endpoints as the English one —
  the ``-cn`` file is the one that gets loaded, and edits used to land only in
  the English file;
- the MCP SKILL's tool catalog lists exactly the tools the server registers.
"""

from __future__ import annotations

import asyncio
import importlib
import re
from pathlib import Path

import mantisfetch_mcp as mm
import pytest
from starlette.testclient import TestClient

SKILLS = Path(__file__).resolve().parent.parent / "skills"
PAIRS = ["mantisfetch-browser", "mantisfetch-docreader", "mantisfetch-mcp"]

# `POST /doc/parse`, `GET /doc/library/{doc_id}/table/{table_id}?fmt=md`, …
_ENDPOINT = re.compile(r"\b(GET|POST|PUT|DELETE|PATCH)\s+(/[A-Za-z0-9_{}/.-]*)")
# A catalog row: | `doc_parse` | … or | `web_search` † | …
_CATALOG_ROW = re.compile(r"^\|\s*`([a-z][a-z0-9_]*)`\s*(†)?\s*\|", re.MULTILINE)


def _norm(path: str) -> str:
    """Path template with every parameter written ``{}`` and no trailing dot/slash."""
    return re.sub(r"\{[^}]*\}", "{}", path.rstrip("./") or "/")


def _documented(name: str) -> set[tuple[str, str]]:
    text = (SKILLS / name).read_text(encoding="utf-8")
    return {(method, _norm(path)) for method, path in _ENDPOINT.findall(text)}


def _served(client: TestClient) -> set[tuple[str, str]]:
    routes: set[tuple[str, str]] = set()
    for prefix, spec in (
        ("", "/openapi.json"),
        ("/web", "/web/openapi.json"),
        ("/doc", "/doc/openapi.json"),
    ):
        for path, ops in client.get(spec).json()["paths"].items():
            for method in ops:
                routes.add((method.upper(), _norm(prefix + path)))
    return routes


@pytest.mark.parametrize("name", [f"{p}-SKILL{s}.md" for p in PAIRS for s in ("", "-cn")])
def test_every_documented_endpoint_is_served(client: TestClient, name: str) -> None:
    missing = _documented(name) - _served(client)
    assert not missing, f"{name} documents routes the server does not serve: {sorted(missing)}"


@pytest.mark.parametrize("pair", PAIRS)
def test_the_chinese_twin_names_the_same_endpoints(pair: str) -> None:
    en = _documented(f"{pair}-SKILL.md")
    cn = _documented(f"{pair}-SKILL-cn.md")
    assert en == cn, f"only in English: {sorted(en - cn)}; only in Chinese: {sorted(cn - en)}"


def _catalog(name: str) -> tuple[set[str], set[str]]:
    """(always-registered tools, search-provider-only tools marked †)."""
    text = (SKILLS / name).read_text(encoding="utf-8")
    rows = [(tool, bool(dagger)) for tool, dagger in _CATALOG_ROW.findall(text)]
    rows = [(tool, dagger) for tool, dagger in rows if tool.startswith(("web_", "doc_"))]
    return {t for t, d in rows if not d}, {t for t, d in rows if d}


def _registered() -> set[str]:
    return {t.name for t in asyncio.run(mm.mcp.list_tools())}


@pytest.mark.parametrize("name", ["mantisfetch-mcp-SKILL.md", "mantisfetch-mcp-SKILL-cn.md"])
def test_the_mcp_catalog_matches_the_registered_tools(monkeypatch, name: str) -> None:
    always, search_only = _catalog(name)
    monkeypatch.delenv("MANTISFETCH_SEARCH_PROVIDER", raising=False)
    importlib.reload(mm)
    without_search = _registered()
    try:
        monkeypatch.setenv("MANTISFETCH_SEARCH_PROVIDER", "searxng")
        importlib.reload(mm)
        with_search = _registered()
    finally:
        monkeypatch.delenv("MANTISFETCH_SEARCH_PROVIDER", raising=False)
        importlib.reload(mm)
    assert always == without_search, (
        f"{name}: catalogued but not registered {sorted(always - without_search)}; "
        f"registered but not catalogued {sorted(without_search - always)}"
    )
    assert search_only == with_search - without_search
