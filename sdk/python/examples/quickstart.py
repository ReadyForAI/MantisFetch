"""MantisFetch SDK — Quick Start Examples.

Run with a MantisFetch service already started on http://localhost:9898:

    python mantisfetch_server.py &
    python sdk/python/examples/quickstart.py
"""

import sys
from pathlib import Path

# Allow running directly from the repo root without installing the package.
sys.path.insert(0, str(Path(__file__).parent.parent))

from mantisfetch_client import AsyncMantisFetchClient, MantisFetchClient

BASE_URL = "http://localhost:9898"


# ── Sync examples ─────────────────────────────────────────────────────────────


def example_health_check() -> None:
    """Verify the service is running."""
    with MantisFetchClient(BASE_URL) as client:
        status = client.health()
        print(f"Service health: ok={status.get('ok')}, version={status.get('version')}")


def example_web_capture() -> None:
    """Capture a web page and save it to the document library."""
    with MantisFetchClient(BASE_URL) as client:
        result = client.capture(
            "https://en.wikipedia.org/wiki/Python_(programming_language)",
            tags=["wikipedia", "python"],
            extract_tables=True,
        )
        doc_id = result["doc_id"]
        print(f"Captured → {doc_id}")
        print(f"  Digest preview: {result['digest'][:120]}...")
        print(f"  Sections: {result['section_count']}, Tables: {result['table_count']}")
        return doc_id


def example_parse_document(file_path: str) -> None:
    """Parse a local PDF or DOCX file."""
    with MantisFetchClient(BASE_URL) as client:
        result = client.parse(
            file_path,
            generate_summary=True,
            extract_tables=True,
            tags=["example"],
        )
        doc_id = result["doc_id"]
        print(f"Parsed → {doc_id} ({result['file_type']})")
        print(f"  Pages: {result.get('total_pages', 'N/A')}, Sections: {result['section_count']}")
        print(f"  Digest preview: {result['digest'][:120]}...")
        return doc_id


def example_search_and_read(query: str) -> None:
    """Search the document library and read a digest."""
    with MantisFetchClient(BASE_URL) as client:
        results = client.search(query, limit=5)
        print(f"Search '{query}' → {results['total']} results")
        for item in results["results"]:
            print(f"  {item['doc_id']}: {item['filename']} ({item['file_type']})")

        if results["results"]:
            doc_id = results["results"][0]["doc_id"]
            digest = client.get_digest(doc_id)
            print(f"\nDigest for {doc_id}:\n{digest['content'][:400]}")


def example_read_section(doc_id: str) -> None:
    """List sections and read the first one."""
    with MantisFetchClient(BASE_URL) as client:
        sections_resp = client.list_sections(doc_id)
        sections = sections_resp.get("sections", [])
        if not sections:
            print(f"No sections found for {doc_id}")
            return

        first = sections[0]
        print(f"Section '{first['title']}' (sid={first['sid']})")
        section = client.get_section(doc_id, first["sid"])
        print(section["content"][:400])


# ── Async examples ────────────────────────────────────────────────────────────


async def example_async_capture() -> None:
    """Async version of web capture."""
    import asyncio  # noqa: F401 — import only needed in async context

    async with AsyncMantisFetchClient(BASE_URL) as client:
        result = await client.capture(
            "https://en.wikipedia.org/wiki/FastAPI",
            tags=["wikipedia", "fastapi"],
        )
        print(f"[async] Captured → {result['doc_id']}")
        return result["doc_id"]


async def example_async_parallel_digests(doc_ids: list[str]) -> None:
    """Fetch multiple digests concurrently."""
    import asyncio

    async with AsyncMantisFetchClient(BASE_URL) as client:
        digests = await asyncio.gather(*[client.get_digest(d) for d in doc_ids])
        for d in digests:
            print(f"{d['doc_id']}: {d['content'][:80]}...")


# ── Entry point ───────────────────────────────────────────────────────────────


def main() -> None:
    print("=== MantisFetch SDK Quick Start ===\n")

    print("-- Health check --")
    example_health_check()

    print("\n-- Search --")
    example_search_and_read("python")

    print("\nDone. Start the service and provide a real URL/file to run capture/parse.")


if __name__ == "__main__":
    main()
