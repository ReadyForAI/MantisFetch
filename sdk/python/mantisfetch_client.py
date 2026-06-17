"""MantisFetch Python SDK.

Lightweight sync and async clients for the MantisFetch API.

Basic usage (sync)::

    from mantisfetch_client import MantisFetchClient

    client = MantisFetchClient("http://localhost:9898")
    result = client.capture("https://example.com")
    print(result["doc_id"])

Basic usage (async)::

    from mantisfetch_client import AsyncMantisFetchClient

    async with AsyncMantisFetchClient("http://localhost:9898") as client:
        result = await client.capture("https://example.com")
        print(result["doc_id"])
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

__version__ = "0.1.0"

_DEFAULT_BASE_URL = "http://localhost:9898"
_DEFAULT_TIMEOUT = 120.0  # seconds; large uploads / OCR can be slow

# Library categories accepted by /doc/parse and /web/capture. The server
# normalizes case-insensitively, but the SDK fails fast with a clear error
# so callers don't have to debug an opaque HTTP 422.
CONTENT_TYPES: tuple[str, ...] = ("General", "Contract", "Bid", "Knowledge")
_CONTENT_TYPE_LOOKUP = {v.lower(): v for v in CONTENT_TYPES}


# ── helpers ───────────────────────────────────────────────────────────────────


class MantisFetchAPIError(Exception):
    """Raised when the MantisFetch API returns an error response.

    Carries the server-provided ``detail`` (e.g. the 409 "already exists" or 422
    validation message) which httpx's ``raise_for_status()`` would otherwise drop.
    """

    def __init__(self, message: str, *, status_code: int, detail: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail


def _raise_for_status(resp: Any) -> None:
    """Like ``resp.raise_for_status()`` but surfaces the server's error detail."""
    if resp.is_success:
        return
    detail = None
    try:
        body = resp.json()
        if isinstance(body, dict):
            detail = body.get("detail")
    except Exception:
        detail = None
    message = f"MantisFetch API {resp.status_code} {resp.reason_phrase}"
    if detail:
        message += f": {detail}"
    raise MantisFetchAPIError(message, status_code=resp.status_code, detail=detail)


def _validate_content_type(value: str) -> str:
    """Return the canonical content_type value, or raise ValueError.

    Accepts the same forms the server accepts: case-insensitive matches
    against the documented enum, with leading/trailing whitespace stripped.
    """
    canonical = (
        _CONTENT_TYPE_LOOKUP.get(value.strip().lower()) if isinstance(value, str) else None
    )
    if canonical is None:
        raise ValueError(
            f"content_type must be one of {CONTENT_TYPES} (case-insensitive); got {value!r}"
        )
    return canonical


def _base_url(url: str) -> str:
    return url.rstrip("/")


def _tags_param(tags: list[str] | None) -> str | None:
    """Encode a tag list as a JSON-array string for form fields."""
    import json

    return json.dumps(tags) if tags else None


def _metadata_param(metadata: dict[str, Any] | None) -> str | None:
    """Encode metadata as a JSON-object string for form fields."""
    if not metadata:
        return None
    import json

    return json.dumps(metadata, ensure_ascii=False)


def _doc_id_like(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", value.strip()))


# ── sync client ───────────────────────────────────────────────────────────────


class MantisFetchClient:
    """Synchronous MantisFetch API client.

    Args:
        base_url: Base URL of the MantisFetch service (default: http://localhost:9898).
        timeout:  HTTP timeout in seconds (default: 120).
        api_key:  Optional API key passed as ``Authorization: Bearer <key>`` header.
                  Also read from the ``MANTISFETCH_API_KEY`` environment variable.
    """

    def __init__(
        self,
        base_url: str = _DEFAULT_BASE_URL,
        *,
        timeout: float = _DEFAULT_TIMEOUT,
        api_key: str | None = None,
    ) -> None:
        import httpx

        self._base = _base_url(base_url)
        key = api_key or os.environ.get("MANTISFETCH_API_KEY", "")
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        self._http = httpx.Client(timeout=timeout, headers=headers)

    # ── context-manager support ──────────────────────────────────────────────

    def __enter__(self) -> MantisFetchClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the underlying HTTP connection pool."""
        self._http.close()

    # ── internal ─────────────────────────────────────────────────────────────

    def _get(self, path: str, **params: Any) -> dict[str, Any]:
        resp = self._http.get(f"{self._base}{path}", params={k: v for k, v in params.items() if v is not None})
        _raise_for_status(resp)
        return resp.json()

    def _post_json(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        resp = self._http.post(f"{self._base}{path}", json=body)
        _raise_for_status(resp)
        return resp.json()

    def _post_multipart(self, path: str, data: dict[str, Any], file_path: Path) -> dict[str, Any]:
        with file_path.open("rb") as fh:
            resp = self._http.post(
                f"{self._base}{path}",
                data={k: v for k, v in data.items() if v is not None},
                files={"file": (file_path.name, fh)},
            )
        _raise_for_status(resp)
        return resp.json()

    # ── public API ────────────────────────────────────────────────────────────

    def capture(
        self,
        url: str,
        *,
        content_type: str = "General",
        tags: list[str] | None = None,
        extract_tables: bool = True,
    ) -> dict[str, Any]:
        """Capture a web page and persist it to the document library.

        Args:
            url:            The URL to capture.
            content_type:   Library category: General, Contract, Bid, or Knowledge.
            tags:           Optional list of tags to attach to the document.
            extract_tables: Whether to extract HTML tables (default: True).

        Returns:
            dict with ``doc_id``, ``digest``, ``section_count``, ``table_count``.
        """
        content_type = _validate_content_type(content_type)
        return self._post_json(
            "/web/capture",
            {
                "url": url,
                "content_type": content_type,
                "tags": tags or [],
                "extract_tables": extract_tables,
            },
        )

    def parse(
        self,
        file_path: str | Path,
        *,
        generate_summary: bool = True,
        summary_mode: str | None = None,
        profile: str | None = None,
        extract_tables: bool = True,
        content_type: str = "General",
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        doc_type: str | None = None,
        project_id: str | None = None,
        source_role: str | None = None,
        id_strategy: str | None = None,
        skip_ocr_pages: str | list[int | str] | None = None,
        doc_id: str | None = None,
        force_ocr: bool = False,
    ) -> dict[str, Any]:
        """Upload and parse a document (PDF, DOCX, XLSX, or CSV).

        Args:
            file_path:        Local path to the document.
            generate_summary: Generate LLM summaries (default: True).
            summary_mode:     Optional summary mode: ``"sync"``, ``"defer"``, or ``"off"``.
            profile:          Optional document profile name, e.g. ``"contract_cn"``.
            extract_tables:   Extract tables as Markdown (default: True).
            content_type:     Library category: General, Contract, Bid, or Knowledge.
            tags:             Optional list of tags.
            metadata:         Optional JSON-serializable metadata attached to the document.
            doc_type:         Optional caller-defined document type.
            project_id:       Optional caller-defined project id.
            source_role:      Optional caller-defined source role.
            id_strategy:      Optional document id strategy: ``"counter"`` or ``"source_filename"``.
            skip_ocr_pages:   Optional page range/list treated as manually skipped OCR pages.
            doc_id:           Optional explicit document id.
            force_ocr:        Force OCR on all pages (default: False).

        Returns:
            dict with ``doc_id``, ``digest``, ``section_count``, ``table_count``, etc.
        """
        content_type = _validate_content_type(content_type)
        path = Path(file_path)
        merged_metadata = dict(metadata or {})
        for key, value in {
            "summary_mode": summary_mode,
            "document_profile": profile,
            "doc_type": doc_type,
            "project_id": project_id,
            "source_role": source_role,
            "id_strategy": id_strategy,
            "skip_ocr_pages": skip_ocr_pages,
        }.items():
            if value is not None:
                merged_metadata.setdefault(key, value)
        return self._post_multipart(
            "/doc/parse",
            {
                "doc_id": doc_id,
                "content_type": content_type,
                "generate_summary": str(generate_summary).lower(),
                "summary_mode": summary_mode,
                "document_profile": profile,
                "extract_tables": str(extract_tables).lower(),
                "force_ocr": str(force_ocr).lower(),
                "id_strategy": id_strategy,
                "skip_ocr_pages": skip_ocr_pages if isinstance(skip_ocr_pages, str) else None,
                "tags": _tags_param(tags),
                "metadata": _metadata_param(merged_metadata),
            },
            path,
        )

    def ensure_doc(self, value: str | Path, **parse_kwargs: Any) -> str:
        """Return a doc_id, parsing ``value`` first when it is a local file path."""
        text = str(value)
        path = Path(text).expanduser()
        if path.exists() and path.is_file():
            return str(self.parse(path, **parse_kwargs)["doc_id"])
        if _doc_id_like(text):
            return text
        raise ValueError(f"not a doc_id or readable file path: {value}")

    def search(
        self,
        query: str,
        *,
        tags: str | None = None,
        file_type: str | None = None,
        content_type: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Search the document library.

        Args:
            query:     Full-text keyword query (searches filename, digest, tags).
            tags:      Comma-separated tag filter, e.g. ``"Q3,financial"``.
            file_type: Filter by file type: ``"pdf"``, ``"docx"``, or ``"web"``.
            content_type: Filter by library category: General, Contract, Bid, or Knowledge.
            limit:     Maximum number of results (default: 20).

        Returns:
            dict with ``results`` list and ``total`` count.
        """
        if content_type is not None:
            content_type = _validate_content_type(content_type)
        return self._get(
            "/doc/library/search",
            q=query,
            tags=tags,
            file_type=file_type,
            content_type=content_type,
            limit=limit,
        )

    def search_text(
        self,
        query: str,
        *,
        doc_id: str | None = None,
        scope: str = "all",
        tags: str | None = None,
        file_type: str | None = None,
        content_type: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Search full text and/or section text across the document library."""
        if content_type is not None:
            content_type = _validate_content_type(content_type)
        return self._get(
            "/doc/library/search_text",
            q=query,
            doc_id=doc_id,
            scope=scope,
            tags=tags,
            file_type=file_type,
            content_type=content_type,
            limit=limit,
        )

    def get_manifest(self, doc_id: str) -> dict[str, Any]:
        """Retrieve a document manifest."""
        return self._get(f"/doc/library/{doc_id}/manifest")

    def get_digest(self, doc_id: str) -> dict[str, Any]:
        """Retrieve the digest (~200 tokens) for a document.

        Args:
            doc_id: Document ID, e.g. ``"DOC-001"`` or ``"WEB-001"``.

        Returns:
            dict with ``doc_id`` and ``content`` (Markdown string).
        """
        return self._get(f"/doc/library/{doc_id}/digest")

    def get_section(self, doc_id: str, sid: str) -> dict[str, Any]:
        """Retrieve the full text of a specific document section.

        Args:
            doc_id: Document ID.
            sid:    Section ID obtained from ``GET /doc/library/{doc_id}/sections``.

        Returns:
            dict with ``doc_id``, ``sid``, and ``content`` (Markdown string).
        """
        return self._get(f"/doc/library/{doc_id}/section/{sid}")

    def get_brief(self, doc_id: str) -> dict[str, Any]:
        """Retrieve the brief (~1500 tokens) for a document.

        Args:
            doc_id: Document ID.

        Returns:
            dict with ``doc_id`` and ``content`` (Markdown string).
        """
        return self._get(f"/doc/library/{doc_id}/brief")

    def get_full(self, doc_id: str) -> dict[str, Any]:
        """Retrieve the full document text."""
        return self._get(f"/doc/library/{doc_id}/full")

    def list_sections(self, doc_id: str) -> dict[str, Any]:
        """List all sections of a document with their sids and metadata.

        Args:
            doc_id: Document ID.

        Returns:
            dict with ``doc_id`` and ``sections`` list.
        """
        return self._get(f"/doc/library/{doc_id}/sections")

    def search_sections(
        self,
        doc_id: str,
        query: str,
        *,
        limit: int = 20,
        include_content: bool = False,
        case_sensitive: bool = False,
    ) -> dict[str, Any]:
        """Search within one document's sections and return sid/page provenance."""
        return self._post_json(
            f"/doc/library/{doc_id}/search_sections",
            {
                "q": query,
                "limit": limit,
                "include_content": include_content,
                "case_sensitive": case_sensitive,
            },
        )

    def chunk_doc(
        self,
        doc_id: str,
        *,
        max_tokens_per_chunk: int = 4000,
        overlap_tokens: int = 200,
        merge_short_sections: bool = True,
        merge_threshold_tokens: int = 500,
        include_text: bool = True,
    ) -> dict[str, Any]:
        """Build generic section-boundary chunks for a document."""
        return self._post_json(
            f"/doc/library/{doc_id}/chunks",
            {
                "max_tokens_per_chunk": max_tokens_per_chunk,
                "overlap_tokens": overlap_tokens,
                "merge_short_sections": merge_short_sections,
                "merge_threshold_tokens": merge_threshold_tokens,
                "include_text": include_text,
            },
        )

    def health(self) -> dict[str, Any]:
        """Return the service health status.

        Returns:
            dict with ``ok``, ``version``, and sub-app statuses.
        """
        return self._get("/health")


# ── async client ──────────────────────────────────────────────────────────────


class AsyncMantisFetchClient:
    """Asynchronous MantisFetch API client.

    Identical API surface to :class:`MantisFetchClient` but all methods are
    coroutines.  Use as an async context manager or call :meth:`aclose`
    explicitly when done.

    Args:
        base_url: Base URL of the MantisFetch service (default: http://localhost:9898).
        timeout:  HTTP timeout in seconds (default: 120).
        api_key:  Optional API key.  Also read from ``MANTISFETCH_API_KEY``.
    """

    def __init__(
        self,
        base_url: str = _DEFAULT_BASE_URL,
        *,
        timeout: float = _DEFAULT_TIMEOUT,
        api_key: str | None = None,
    ) -> None:
        import httpx

        self._base = _base_url(base_url)
        key = api_key or os.environ.get("MANTISFETCH_API_KEY", "")
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        self._http = httpx.AsyncClient(timeout=timeout, headers=headers)

    # ── context-manager support ──────────────────────────────────────────────

    async def __aenter__(self) -> AsyncMantisFetchClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the underlying async HTTP connection pool."""
        await self._http.aclose()

    # ── internal ─────────────────────────────────────────────────────────────

    async def _get(self, path: str, **params: Any) -> dict[str, Any]:
        resp = await self._http.get(
            f"{self._base}{path}",
            params={k: v for k, v in params.items() if v is not None},
        )
        _raise_for_status(resp)
        return resp.json()

    async def _post_json(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        resp = await self._http.post(f"{self._base}{path}", json=body)
        _raise_for_status(resp)
        return resp.json()

    async def _post_multipart(self, path: str, data: dict[str, Any], file_path: Path) -> dict[str, Any]:
        with file_path.open("rb") as fh:
            resp = await self._http.post(
                f"{self._base}{path}",
                data={k: v for k, v in data.items() if v is not None},
                files={"file": (file_path.name, fh)},
            )
        _raise_for_status(resp)
        return resp.json()

    # ── public API ────────────────────────────────────────────────────────────

    async def capture(
        self,
        url: str,
        *,
        content_type: str = "General",
        tags: list[str] | None = None,
        extract_tables: bool = True,
    ) -> dict[str, Any]:
        """Capture a web page and persist it to the document library."""
        content_type = _validate_content_type(content_type)
        return await self._post_json(
            "/web/capture",
            {
                "url": url,
                "content_type": content_type,
                "tags": tags or [],
                "extract_tables": extract_tables,
            },
        )

    async def parse(
        self,
        file_path: str | Path,
        *,
        generate_summary: bool = True,
        summary_mode: str | None = None,
        profile: str | None = None,
        extract_tables: bool = True,
        content_type: str = "General",
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        doc_type: str | None = None,
        project_id: str | None = None,
        source_role: str | None = None,
        id_strategy: str | None = None,
        skip_ocr_pages: str | list[int | str] | None = None,
        doc_id: str | None = None,
        force_ocr: bool = False,
    ) -> dict[str, Any]:
        """Upload and parse a document (PDF, DOCX, XLSX, or CSV)."""
        content_type = _validate_content_type(content_type)
        path = Path(file_path)
        merged_metadata = dict(metadata or {})
        for key, value in {
            "summary_mode": summary_mode,
            "document_profile": profile,
            "doc_type": doc_type,
            "project_id": project_id,
            "source_role": source_role,
            "id_strategy": id_strategy,
            "skip_ocr_pages": skip_ocr_pages,
        }.items():
            if value is not None:
                merged_metadata.setdefault(key, value)
        return await self._post_multipart(
            "/doc/parse",
            {
                "doc_id": doc_id,
                "content_type": content_type,
                "generate_summary": str(generate_summary).lower(),
                "summary_mode": summary_mode,
                "document_profile": profile,
                "extract_tables": str(extract_tables).lower(),
                "force_ocr": str(force_ocr).lower(),
                "id_strategy": id_strategy,
                "skip_ocr_pages": skip_ocr_pages if isinstance(skip_ocr_pages, str) else None,
                "tags": _tags_param(tags),
                "metadata": _metadata_param(merged_metadata),
            },
            path,
        )

    async def ensure_doc(self, value: str | Path, **parse_kwargs: Any) -> str:
        """Return a doc_id, parsing ``value`` first when it is a local file path."""
        text = str(value)
        path = Path(text).expanduser()
        if path.exists() and path.is_file():
            return str((await self.parse(path, **parse_kwargs))["doc_id"])
        if _doc_id_like(text):
            return text
        raise ValueError(f"not a doc_id or readable file path: {value}")

    async def search(
        self,
        query: str,
        *,
        tags: str | None = None,
        file_type: str | None = None,
        content_type: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Search the document library."""
        if content_type is not None:
            content_type = _validate_content_type(content_type)
        return await self._get(
            "/doc/library/search",
            q=query,
            tags=tags,
            file_type=file_type,
            content_type=content_type,
            limit=limit,
        )

    async def search_text(
        self,
        query: str,
        *,
        doc_id: str | None = None,
        scope: str = "all",
        tags: str | None = None,
        file_type: str | None = None,
        content_type: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Search full text and/or section text across the document library."""
        if content_type is not None:
            content_type = _validate_content_type(content_type)
        return await self._get(
            "/doc/library/search_text",
            q=query,
            doc_id=doc_id,
            scope=scope,
            tags=tags,
            file_type=file_type,
            content_type=content_type,
            limit=limit,
        )

    async def get_manifest(self, doc_id: str) -> dict[str, Any]:
        """Retrieve a document manifest."""
        return await self._get(f"/doc/library/{doc_id}/manifest")

    async def get_digest(self, doc_id: str) -> dict[str, Any]:
        """Retrieve the digest (~200 tokens) for a document."""
        return await self._get(f"/doc/library/{doc_id}/digest")

    async def get_section(self, doc_id: str, sid: str) -> dict[str, Any]:
        """Retrieve the full text of a specific document section."""
        return await self._get(f"/doc/library/{doc_id}/section/{sid}")

    async def get_brief(self, doc_id: str) -> dict[str, Any]:
        """Retrieve the brief (~1500 tokens) for a document."""
        return await self._get(f"/doc/library/{doc_id}/brief")

    async def get_full(self, doc_id: str) -> dict[str, Any]:
        """Retrieve the full document text."""
        return await self._get(f"/doc/library/{doc_id}/full")

    async def list_sections(self, doc_id: str) -> dict[str, Any]:
        """List all sections of a document with their sids and metadata."""
        return await self._get(f"/doc/library/{doc_id}/sections")

    async def search_sections(
        self,
        doc_id: str,
        query: str,
        *,
        limit: int = 20,
        include_content: bool = False,
        case_sensitive: bool = False,
    ) -> dict[str, Any]:
        """Search within one document's sections and return sid/page provenance."""
        return await self._post_json(
            f"/doc/library/{doc_id}/search_sections",
            {
                "q": query,
                "limit": limit,
                "include_content": include_content,
                "case_sensitive": case_sensitive,
            },
        )

    async def chunk_doc(
        self,
        doc_id: str,
        *,
        max_tokens_per_chunk: int = 4000,
        overlap_tokens: int = 200,
        merge_short_sections: bool = True,
        merge_threshold_tokens: int = 500,
        include_text: bool = True,
    ) -> dict[str, Any]:
        """Build generic section-boundary chunks for a document."""
        return await self._post_json(
            f"/doc/library/{doc_id}/chunks",
            {
                "max_tokens_per_chunk": max_tokens_per_chunk,
                "overlap_tokens": overlap_tokens,
                "merge_short_sections": merge_short_sections,
                "merge_threshold_tokens": merge_threshold_tokens,
                "include_text": include_text,
            },
        )

    async def health(self) -> dict[str, Any]:
        """Return the service health status."""
        return await self._get("/health")
