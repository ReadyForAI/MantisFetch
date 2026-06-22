"""Tests for the MantisFetch Python SDK (TASK-009)."""

import ast
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

SDK_PATH = Path(__file__).parent.parent / "sdk" / "python"


# ── module structure ──────────────────────────────────────────────────────────


class TestSDKStructure:
    def test_client_file_exists(self):
        assert (SDK_PATH / "mantisfetch_client.py").is_file()

    def test_pyproject_exists(self):
        assert (SDK_PATH / "pyproject.toml").is_file()

    def test_examples_exist(self):
        examples = list((SDK_PATH / "examples").glob("*.py"))
        assert len(examples) >= 1

    def test_both_classes_in_ast(self):
        source = (SDK_PATH / "mantisfetch_client.py").read_text()
        tree = ast.parse(source)
        classes = {n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}
        assert "MantisFetchClient" in classes
        assert "AsyncMantisFetchClient" in classes

    def test_client_importable(self, monkeypatch):
        import sys

        monkeypatch.syspath_prepend(str(SDK_PATH))
        from mantisfetch_client import AsyncMantisFetchClient, MantisFetchClient  # noqa: F401

        assert MantisFetchClient is not None
        assert AsyncMantisFetchClient is not None


# ── sync client unit tests ────────────────────────────────────────────────────


@pytest.fixture()
def sync_client(monkeypatch, tmp_path):
    """Return a MantisFetchClient with httpx.Client mocked out."""
    import sys

    monkeypatch.syspath_prepend(str(SDK_PATH))
    from mantisfetch_client import MantisFetchClient

    mock_http = MagicMock()
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_http.get.return_value = mock_resp
    mock_http.post.return_value = mock_resp

    client = MantisFetchClient.__new__(MantisFetchClient)
    client._base = "http://localhost:9898"
    client._http = mock_http
    return client, mock_http, mock_resp


def test_api_error_surfaces_server_detail(monkeypatch):
    """C50: the SDK must surface the server's error detail (e.g. 409 message),
    not just the bare status code."""
    monkeypatch.syspath_prepend(str(SDK_PATH))
    from mantisfetch_client import MantisFetchAPIError, _raise_for_status

    class FakeResp:
        is_success = False
        status_code = 409
        reason_phrase = "Conflict"

        def json(self):
            return {"detail": "doc_id 'DOC-001' already exists. Pass replace=true"}

    with pytest.raises(MantisFetchAPIError) as exc:
        _raise_for_status(FakeResp())
    assert exc.value.status_code == 409
    assert "already exists" in str(exc.value)
    assert exc.value.detail and "already exists" in exc.value.detail


class TestSyncClient:
    def test_health(self, sync_client):
        client, mock_http, mock_resp = sync_client
        mock_resp.json.return_value = {"ok": True, "version": "3.0.0"}
        result = client.health()
        assert result["ok"] is True
        mock_http.get.assert_called_once_with(
            "http://localhost:9898/health", params={}
        )

    def test_capture(self, sync_client):
        client, mock_http, mock_resp = sync_client
        mock_resp.json.return_value = {
            "doc_id": "WEB-001", "digest": "test", "section_count": 3, "table_count": 1
        }
        result = client.capture("https://example.com", content_type="Knowledge", tags=["test"])
        assert result["doc_id"] == "WEB-001"
        call_kwargs = mock_http.post.call_args
        assert "capture" in call_kwargs.args[0]
        body = call_kwargs.kwargs["json"]
        assert body["url"] == "https://example.com"
        assert body["content_type"] == "Knowledge"
        assert body["tags"] == ["test"]

    def test_capture_normalizes_lowercase_content_type(self, sync_client):
        client, mock_http, mock_resp = sync_client
        mock_resp.json.return_value = {"doc_id": "WEB-002"}
        client.capture("https://example.com", content_type="  bid  ")
        body = mock_http.post.call_args.kwargs["json"]
        # Server strips + lowercases before lookup; the SDK matches that.
        assert body["content_type"] == "Bid"

    def test_capture_rejects_unknown_content_type(self, sync_client):
        client, _mock_http, _mock_resp = sync_client
        import pytest

        with pytest.raises(ValueError, match="content_type must be one of"):
            client.capture("https://example.com", content_type="Cntract")

    def test_search(self, sync_client):
        client, mock_http, mock_resp = sync_client
        mock_resp.json.return_value = {"results": [], "total": 0}
        result = client.search("revenue", content_type="Bid", limit=5)
        assert result["total"] == 0
        call_kwargs = mock_http.get.call_args
        assert "search" in call_kwargs.args[0]
        assert call_kwargs.kwargs["params"]["q"] == "revenue"
        assert call_kwargs.kwargs["params"]["content_type"] == "Bid"

    def test_parse_accepts_skill_metadata_options(self, sync_client, tmp_path):
        client, mock_http, mock_resp = sync_client
        sample = tmp_path / "tender.pdf"
        sample.write_bytes(b"%PDF-1.4")
        mock_resp.json.return_value = {"doc_id": "DOC-001"}

        result = client.parse(
            sample,
            summary_mode="defer",
            profile="bid_cn",
            content_type="Bid",
            metadata={"app": "bid-manage"},
            project_id="P-001",
            source_role="tender_file",
            id_strategy="source_filename",
            skip_ocr_pages=[1, "3-4"],
        )

        assert result["doc_id"] == "DOC-001"
        data = mock_http.post.call_args.kwargs["data"]
        assert data["content_type"] == "Bid"
        assert data["summary_mode"] == "defer"
        assert data["document_profile"] == "bid_cn"
        assert data["id_strategy"] == "source_filename"
        assert "bid-manage" in data["metadata"]
        assert "tender_file" in data["metadata"]

    def test_ensure_doc_returns_existing_id(self, sync_client):
        client, _, _ = sync_client
        assert client.ensure_doc("DOC-001") == "DOC-001"

    def test_get_digest(self, sync_client):
        client, mock_http, mock_resp = sync_client
        mock_resp.json.return_value = {"doc_id": "DOC-001", "content": "# Summary"}
        result = client.get_digest("DOC-001")
        assert result["doc_id"] == "DOC-001"
        assert "DOC-001/digest" in mock_http.get.call_args.args[0]

    def test_get_section(self, sync_client):
        client, mock_http, mock_resp = sync_client
        mock_resp.json.return_value = {"doc_id": "DOC-001", "sid": "abc", "content": "text"}
        result = client.get_section("DOC-001", "abc")
        assert result["sid"] == "abc"
        assert "DOC-001/section/abc" in mock_http.get.call_args.args[0]

    def test_table_and_image_methods(self, sync_client):
        client, mock_http, mock_resp = sync_client
        mock_resp.json.return_value = {"ok": True}

        client.get_table("DOC-001", "01")
        assert "DOC-001/table/01" in mock_http.get.call_args.args[0]

        client.get_table_json("DOC-001", "01")
        assert "DOC-001/table/01/json" in mock_http.get.call_args.args[0]

        client.list_images("DOC-001")
        assert "DOC-001/images" in mock_http.get.call_args.args[0]

        client.get_image("DOC-001", "IMG-001")
        assert "DOC-001/image/IMG-001" in mock_http.get.call_args.args[0]

    def test_get_image_bytes(self, sync_client):
        client, mock_http, mock_resp = sync_client
        mock_resp.content = b"\x89PNGdata"
        data = client.get_image_bytes("DOC-001", "IMG-001")
        assert data == b"\x89PNGdata"
        call = mock_http.get.call_args
        assert "DOC-001/image/IMG-001/raw" in call.args[0]
        assert call.kwargs["params"]["variant"] == "rendered"
        # explicit variant is threaded through
        client.get_image_bytes("DOC-001", "IMG-001", variant="original")
        assert mock_http.get.call_args.kwargs["params"]["variant"] == "original"

    def test_skill_support_helpers(self, sync_client):
        client, mock_http, mock_resp = sync_client
        mock_resp.json.return_value = {"ok": True}

        client.get_manifest("DOC-001")
        assert "DOC-001/manifest" in mock_http.get.call_args.args[0]

        client.search_sections("DOC-001", "payment", include_content=True)
        post_call = mock_http.post.call_args
        assert "DOC-001/search_sections" in post_call.args[0]
        assert post_call.kwargs["json"]["include_content"] is True

        client.chunk_doc("DOC-001", include_text=False)
        post_call = mock_http.post.call_args
        assert "DOC-001/chunks" in post_call.args[0]
        assert post_call.kwargs["json"]["include_text"] is False

    def test_context_manager_closes(self, monkeypatch):
        import sys

        monkeypatch.syspath_prepend(str(SDK_PATH))

        mock_httpx = MagicMock()
        mock_client_instance = MagicMock()
        mock_httpx.Client.return_value = mock_client_instance

        with patch.dict("sys.modules", {"httpx": mock_httpx}):
            from mantisfetch_client import MantisFetchClient

            with MantisFetchClient("http://localhost:9898"):
                pass

        mock_client_instance.close.assert_called_once()


# ── async client unit tests ───────────────────────────────────────────────────


@pytest.fixture()
def async_client(monkeypatch):
    """Return an AsyncMantisFetchClient with httpx.AsyncClient mocked out."""
    monkeypatch.syspath_prepend(str(SDK_PATH))
    from mantisfetch_client import AsyncMantisFetchClient

    mock_http = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_http.get.return_value = mock_resp
    mock_http.post.return_value = mock_resp
    mock_http.aclose = AsyncMock()

    client = AsyncMantisFetchClient.__new__(AsyncMantisFetchClient)
    client._base = "http://localhost:9898"
    client._http = mock_http
    return client, mock_http, mock_resp


class TestAsyncClient:
    @pytest.mark.asyncio
    async def test_async_health(self, async_client):
        client, mock_http, mock_resp = async_client
        mock_resp.json.return_value = {"ok": True, "version": "3.0.0"}
        result = await client.health()
        assert result["ok"] is True

    @pytest.mark.asyncio
    async def test_async_capture(self, async_client):
        client, mock_http, mock_resp = async_client
        mock_resp.json.return_value = {
            "doc_id": "WEB-002", "digest": "d", "section_count": 1, "table_count": 0
        }
        result = await client.capture("https://example.org", content_type="Knowledge")
        assert result["doc_id"] == "WEB-002"
        assert mock_http.post.call_args.kwargs["json"]["content_type"] == "Knowledge"

    @pytest.mark.asyncio
    async def test_async_capture_validates_content_type(self, async_client):
        client, _mock_http, _mock_resp = async_client
        with pytest.raises(ValueError, match="content_type must be one of"):
            await client.capture("https://example.org", content_type="Cntract")

    @pytest.mark.asyncio
    async def test_async_get_digest(self, async_client):
        client, mock_http, mock_resp = async_client
        mock_resp.json.return_value = {"doc_id": "DOC-002", "content": "digest text"}
        result = await client.get_digest("DOC-002")
        assert result["content"] == "digest text"

    @pytest.mark.asyncio
    async def test_async_get_section(self, async_client):
        client, mock_http, mock_resp = async_client
        mock_resp.json.return_value = {"doc_id": "DOC-002", "sid": "s1", "content": "section"}
        result = await client.get_section("DOC-002", "s1")
        assert result["sid"] == "s1"

    @pytest.mark.asyncio
    async def test_async_table_and_image_methods(self, async_client):
        client, mock_http, mock_resp = async_client
        mock_resp.json.return_value = {"ok": True}
        await client.get_table("DOC-002", "01")
        assert "DOC-002/table/01" in mock_http.get.call_args.args[0]
        await client.get_table_json("DOC-002", "01")
        assert "DOC-002/table/01/json" in mock_http.get.call_args.args[0]
        await client.list_images("DOC-002")
        assert "DOC-002/images" in mock_http.get.call_args.args[0]
        await client.get_image("DOC-002", "IMG-001")
        assert "DOC-002/image/IMG-001" in mock_http.get.call_args.args[0]

    @pytest.mark.asyncio
    async def test_async_get_image_bytes(self, async_client):
        client, mock_http, mock_resp = async_client
        mock_resp.content = b"\x89PNGasync"
        data = await client.get_image_bytes("DOC-002", "IMG-001", variant="original")
        assert data == b"\x89PNGasync"
        call = mock_http.get.call_args
        assert "DOC-002/image/IMG-001/raw" in call.args[0]
        assert call.kwargs["params"]["variant"] == "original"

    @pytest.mark.asyncio
    async def test_async_search(self, async_client):
        client, mock_http, mock_resp = async_client
        mock_resp.json.return_value = {"results": [{"doc_id": "DOC-001"}], "total": 1}
        result = await client.search("profit", content_type="Contract")
        assert result["total"] == 1
        assert mock_http.get.call_args.kwargs["params"]["content_type"] == "Contract"
