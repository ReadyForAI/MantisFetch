"""MantisFetch SDK — Async parallel document processing example.

Demonstrates fetching digests for multiple documents concurrently with
AsyncMantisFetchClient.

Usage::

    python mantisfetch_server.py &
    python sdk/python/examples/async_example.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from mantisfetch_client import AsyncMantisFetchClient


async def main() -> None:
    base_url = "http://localhost:9898"

    async with AsyncMantisFetchClient(base_url) as client:
        # 1. Health check
        health = await client.health()
        print(f"Service ok={health.get('ok')}, version={health.get('version')}")

        # 2. Search for all documents
        results = await client.search("", limit=10)
        doc_ids = [r["doc_id"] for r in results.get("results", [])]
        print(f"Found {len(doc_ids)} documents in library: {doc_ids}")

        if not doc_ids:
            print("No documents yet — run a capture or parse first.")
            return

        # 3. Fetch all digests concurrently
        print("\nFetching digests in parallel...")
        digests = await asyncio.gather(*[client.get_digest(d) for d in doc_ids])
        for digest in digests:
            preview = digest["content"][:100].replace("\n", " ")
            print(f"  {digest['doc_id']}: {preview}...")


if __name__ == "__main__":
    asyncio.run(main())
