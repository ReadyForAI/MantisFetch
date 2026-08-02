"""Shared pytest fixtures for MantisFetch tests.

Provides a session-scoped TestClient that covers the unified app
(browser + docreader mounted) without launching real external services.
Playwright is mocked so no browser is started during the test run.
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.testclient import TestClient

ROOT = Path(__file__).parent.parent

# Make mantisfetch_server and service modules importable.
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "services" / "browser"))
sys.path.insert(0, str(ROOT / "services" / "docreader"))
sys.path.insert(0, str(ROOT / "services" / "mcp"))


@pytest.fixture(autouse=True)
def _hermetic_mcp_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep MANTISFETCH_MCP_TOKEN unset per test.

    magika (a transitive MarkItDown dependency) calls
    ``dotenv.load_dotenv(dotenv.find_dotenv())`` at import time, which walks up to
    the repo ``.env`` and leaks the developer's MANTISFETCH_MCP_TOKEN into
    os.environ mid-session (lazily, on the first MarkItDown use). With that token
    set, the REST/MCP auth gate would reject the non-loopback TestClient and break
    unrelated tests. Tests that exercise auth set the token explicitly via their
    own monkeypatch (same instance), which wins over this delenv.
    """
    monkeypatch.delenv("MANTISFETCH_MCP_TOKEN", raising=False)


@pytest.fixture(scope="session", autouse=True)
def _hermetic_docs_dir_session(tmp_path_factory: pytest.TempPathFactory):
    """Redirect the library before anything session-scoped can touch it.

    The per-test fixture below is function-scoped, so it is set up *after* the
    session-scoped ``client`` — whose TestClient entry runs the docreader
    lifespan, and whose startup manifest-tags backfill reads and patches
    whatever library ``_get_docs_dir()`` points at. That would be the
    developer's real one. ``client`` depends on this fixture explicitly so the
    ordering is enforced rather than assumed.
    """
    import mantisfetch_common.storage as storage  # noqa: PLC0415

    mp = pytest.MonkeyPatch()
    docs_dir = tmp_path_factory.mktemp("session-docs")
    mp.setattr(storage, "DEFAULT_DOCS_DIR", docs_dir)
    yield docs_dir
    mp.undo()


@pytest.fixture(autouse=True)
def _hermetic_docs_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep the document library per-test, never the developer's real one.

    A test that posts to /parse without redirecting the library mints a doc_id
    from the real ``~/.mantisfetch/docs`` counter and creates the directory
    there — and if the parse then fails, that directory stays behind, empty.
    That is how a full test run was quietly adding a document to the developer's
    own library on every invocation.

    Tests that need their own path still monkeypatch DEFAULT_DOCS_DIR; theirs
    runs after this one and wins.
    """
    import mantisfetch_common.storage as storage  # noqa: PLC0415

    docs_dir = tmp_path / "hermetic-docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(storage, "DEFAULT_DOCS_DIR", docs_dir)


def _make_playwright_mock() -> MagicMock:
    """Build a mock satisfying ``await async_playwright().start()``."""
    mock_browser = AsyncMock()
    mock_browser.close = AsyncMock()

    mock_pw = AsyncMock()
    mock_pw.chromium.launch = AsyncMock(return_value=mock_browser)
    mock_pw.stop = AsyncMock()

    mock_api = MagicMock()
    mock_api.start = AsyncMock(return_value=mock_pw)
    return mock_api


@pytest.fixture(scope="session")
def client(_hermetic_docs_dir_session) -> TestClient:
    """Session-scoped TestClient for the unified MantisFetch app.

    Playwright is mocked so the test suite runs without a real browser. The
    docs-dir redirect is a declared dependency, not an assumption: entering the
    TestClient runs the docreader lifespan, which touches the library.
    """
    with patch("mantisfetch_browser.async_playwright", return_value=_make_playwright_mock()):
        from mantisfetch_server import app  # noqa: PLC0415

        # Present as a loopback peer: the REST gate is loopback-only by default,
        # and a TestClient simulates a same-host caller. Auth-gating for
        # non-loopback peers is exercised separately in test_rest_auth.
        with TestClient(app, client=("127.0.0.1", 50000), raise_server_exceptions=True) as c:
            yield c
