"""MantisFetch SDK — web-search research example.

Requires a MantisFetch service with a search provider configured, e.g. the
zero-cost self-hosted SearXNG profile:

    MANTISFETCH_SEARCH_PROVIDER=searxng docker compose --profile search up
    # then, from the repo root:
    python sdk/python/examples/search_research.py

If search is disabled server-side, /web/search returns 404 (the SDK raises).
"""

import sys
from pathlib import Path

# Allow running directly from the repo root without installing the package.
sys.path.insert(0, str(Path(__file__).parent.parent))

from mantisfetch_client import MantisFetchClient

BASE_URL = "http://localhost:9898"


def example_search() -> None:
    """Pure search — a ranked list, nothing captured. The agent decides next."""
    with MantisFetchClient(BASE_URL) as client:
        result = client.web_search("2026 AI agent governance whitepaper", max_results=5)
        print(f"provider={result['provider']} — {len(result['results'])} results:")
        for r in result["results"]:
            print(f"  - {r['title']}\n    {r['url']}")


def example_search_and_capture_then_read() -> None:
    """The differentiator vs a plain search API: search → capture → deep read.

    One call returns library doc_ids already parsed into the three-tier model, so
    the agent reads a ~200-token digest before pulling any section.
    """
    with MantisFetchClient(BASE_URL) as client:
        # 1. search + capture the top 2 hits (serial; each stamped source=web_search)
        res = client.web_search_and_capture(
            "competitor X 2026 pricing", capture_top=2, tags=["research"]
        )
        print(f"captured {len(res['captured'])}, skipped {len(res['skipped'])}")

        for item in res["captured"]:
            doc_id = item["doc_id"]
            reused = item["reused"]
            print(f"\n[rank {item['rank']}] {doc_id} (reused={reused}) {item['url']}")
            # 2. three-tier deep read — digest first (cheapest), sections on demand
            digest = client.get_digest(doc_id).get("content", "")
            print(f"  digest: {digest[:120].strip()}...")

        for skip in res["skipped"]:
            print(f"[skipped rank {skip['rank']}] {skip['url']} — {skip['reason']}")


if __name__ == "__main__":
    example_search()
    print("\n" + "=" * 60 + "\n")
    example_search_and_capture_then_read()
