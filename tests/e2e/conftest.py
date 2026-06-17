"""Shared fixtures and marker configuration for E2E tests.

E2E tests require a running MantisFetch server on localhost:9898.
They are decorated with ``@pytest.mark.live`` and are skipped by default.

To run them::

    # Start the server first
    python mantisfetch_server.py &

    # Then run with the live marker
    pytest tests/e2e/ -v -m live --timeout=60
"""

import httpx
import pytest


def pytest_configure(config: pytest.Config) -> None:
    """Register the ``live`` marker to suppress PytestUnknownMarkWarning."""
    config.addinivalue_line(
        "markers",
        "live: requires a running MantisFetch server (localhost:9898) and network access",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Skip live-marked tests unless ``-m live`` is explicitly requested."""
    markexpr: str = getattr(config.option, "markexpr", "") or ""
    if "live" not in markexpr:
        skip = pytest.mark.skip(
            reason="live test — start the server and pass -m live to run"
        )
        for item in items:
            if item.get_closest_marker("live"):
                item.add_marker(skip)


@pytest.fixture(scope="session")
def base_url() -> str:
    """Base URL of the running MantisFetch service."""
    return "http://localhost:9898"


@pytest.fixture(scope="session")
def http_client() -> httpx.Client:
    """Session-scoped httpx client for E2E requests."""
    with httpx.Client(timeout=60.0) as client:
        yield client
