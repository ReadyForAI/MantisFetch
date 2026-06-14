"""Security tests for TASK-016: path traversal, SSRF, upload size limits."""

import io
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

# ── DocReader security ──────────────────────────────────────────────


@pytest.fixture()
def doc_client():
    """TestClient for the docreader sub-app."""
    from larkscout_docreader import app

    return TestClient(app, raise_server_exceptions=False)


class TestDocIdTraversal:
    """C5: doc_id path traversal must be rejected.

    Note: doc_ids containing '/' are naturally split by FastAPI routing and
    never reach the handler. The validation guards against form-field injection
    (POST /parse) and against names like '..' or 'back\\slash' that don't
    contain '/' but are still dangerous.
    """

    # IDs that reach the handler (no literal '/' or '..') but are invalid
    MALICIOUS_IDS = [
        "back\\slash",
        "not-valid",
        "-doc001",     # leading hyphen
        "doc001-",     # trailing hyphen
    ]

    @pytest.mark.parametrize("doc_id", MALICIOUS_IDS)
    def test_manifest_rejects_invalid_doc_id(self, doc_client, doc_id):
        resp = doc_client.get(f"/library/{doc_id}/manifest")
        assert resp.status_code == 400

    def test_parse_rejects_traversal_doc_id(self, doc_client):
        """POST /parse form field bypasses routing — test real traversal."""
        for malicious_id in ["../../etc/passwd", "../secret", "DOC-001/../../etc"]:
            fake_pdf = io.BytesIO(b"%PDF-1.4 fake")
            resp = doc_client.post(
                "/parse",
                files={"file": ("test.pdf", fake_pdf, "application/pdf")},
                data={"doc_id": malicious_id},
            )
            assert resp.status_code == 400, f"doc_id={malicious_id!r} was not rejected"

    def test_valid_doc_id_not_rejected(self, doc_client):
        """Valid doc_id format passes validation (may 200 or 404, never 400)."""
        for doc_id in ["DOC-001", "NBS250321", "doc001", "nbs250321"]:
            resp = doc_client.get(f"/library/{doc_id}/manifest")
            assert resp.status_code != 400, f"doc_id={doc_id!r} was wrongly rejected"


class TestTableIdTraversal:
    """H6: table_id path traversal must be rejected."""

    MALICIOUS_IDS = [
        "abc",
        "01-exploit",
        "table-exploit",
    ]

    @pytest.mark.parametrize("table_id", MALICIOUS_IDS)
    def test_table_rejects_invalid_id(self, doc_client, table_id):
        resp = doc_client.get(f"/library/DOC-001/table/{table_id}")
        assert resp.status_code == 400

    def test_valid_table_ids_not_rejected(self, doc_client):
        """Valid formats pass validation (may 404 since doc doesn't exist)."""
        for tid in ["01", "99", "table-01"]:
            resp = doc_client.get(f"/library/DOC-001/table/{tid}")
            assert resp.status_code != 400, f"table_id={tid!r} was wrongly rejected"


class TestUploadSizeLimit:
    """H3: oversized file uploads must return 413."""

    def test_oversized_upload(self, doc_client, monkeypatch):
        import larkscout_docreader

        monkeypatch.setattr(larkscout_docreader, "MAX_UPLOAD_BYTES", 1024)
        big_content = b"%PDF-1.4 " + b"x" * 2048
        resp = doc_client.post(
            "/parse",
            files={"file": ("big.pdf", io.BytesIO(big_content), "application/pdf")},
        )
        assert resp.status_code == 413

    def test_small_upload_passes_size_check(self, doc_client, monkeypatch):
        import larkscout_docreader

        monkeypatch.setattr(larkscout_docreader, "MAX_UPLOAD_BYTES", 10 * 1024 * 1024)
        small_content = b"%PDF-1.4 small file"
        resp = doc_client.post(
            "/parse",
            files={"file": ("small.pdf", io.BytesIO(small_content), "application/pdf")},
        )
        assert resp.status_code != 413


class TestUploadFilenameTraversal:
    """P0: the multipart upload filename must not escape the scratch dir."""

    @pytest.mark.parametrize(
        "evil",
        [
            "../../../../etc/passwd.pdf",
            "../../../../../../tmp/PWNED.pdf",
            "..\\..\\..\\PWNED.pdf",
            "/abs/PWNED.pdf",
            "..",
            "...pdf",
        ],
    )
    def test_safe_source_filename_cannot_escape(self, evil, tmp_path):
        from larkscout_docreader import _safe_source_filename

        safe = _safe_source_filename(evil)
        assert "/" not in safe and "\\" not in safe
        # The real invariant: joining the result onto a dir stays in that dir.
        assert (tmp_path / safe).resolve().parent == tmp_path.resolve()

    def test_parse_filename_cannot_escape_scratch_dir(self, doc_client, monkeypatch, tmp_path):
        import larkscout_common.storage as common_storage

        docs_dir = tmp_path / "docs"
        monkeypatch.setattr(common_storage, "DEFAULT_DOCS_DIR", docs_dir)
        # Enough "../" to land at tmp_path (outside docs_dir) if unsanitized.
        evil = "../../../../PWNED.pdf"
        doc_client.post(
            "/parse",
            files={"file": (evil, io.BytesIO(b"%PDF-1.4 fake"), "application/pdf")},
            data={"generate_summary": "false"},
        )
        docs_resolved = str(docs_dir.resolve())
        for hit in tmp_path.rglob("PWNED.pdf"):
            assert str(hit.resolve()).startswith(docs_resolved), f"upload escaped to {hit}"


# ── Browser security (SSRF) ────────────────────────────────────────


class TestSSRF:
    """C6: SSRF via goto and capture must be blocked."""

    def test_file_scheme_blocked(self):
        from larkscout_browser import _validate_url

        with pytest.raises(Exception, match="scheme not allowed"):
            _validate_url("file:///etc/passwd")

    def test_ftp_scheme_blocked(self):
        from larkscout_browser import _validate_url

        with pytest.raises(Exception, match="scheme not allowed"):
            _validate_url("ftp://evil.com/malware")

    def test_metadata_ip_blocked(self):
        from larkscout_browser import _validate_url

        with pytest.raises(Exception, match="private/reserved"):
            _validate_url("http://169.254.169.254/latest/meta-data")

    def test_loopback_blocked(self):
        from larkscout_browser import _validate_url

        with pytest.raises(Exception, match="private/reserved"):
            _validate_url("http://127.0.0.1:9898/health")

    def test_private_10_blocked(self):
        from larkscout_browser import _validate_url

        with pytest.raises(Exception, match="private/reserved"):
            _validate_url("http://10.0.0.1/admin")

    def test_private_192_blocked(self):
        from larkscout_browser import _validate_url

        with pytest.raises(Exception, match="private/reserved"):
            _validate_url("http://192.168.1.1/")

    def test_localhost_name_blocked(self):
        from larkscout_browser import _validate_url

        with pytest.raises(Exception, match="not allowed"):
            _validate_url("http://localhost:8080/secret")

    def test_ipv6_loopback_blocked(self):
        from larkscout_browser import _validate_url

        with pytest.raises(Exception, match="private/reserved"):
            _validate_url("http://[::1]/")

    def test_valid_https_passes(self):
        from larkscout_browser import _validate_url

        _validate_url("https://example.com/page")

    def test_valid_http_passes(self):
        from larkscout_browser import _validate_url

        _validate_url("http://example.com:8080/api")

    @pytest.mark.parametrize(
        "url",
        [
            "http://2130706433/",            # decimal 127.0.0.1
            "http://0x7f000001/",            # hex 127.0.0.1
            "http://0177.0.0.1/",            # octal 127.0.0.1
            "http://0xa9fea9fe/latest/",     # hex 169.254.169.254 (metadata)
        ],
    )
    def test_encoded_ip_forms_blocked(self, url):
        from larkscout_browser import _validate_url

        with pytest.raises(Exception, match="private/reserved"):
            _validate_url(url)

    def test_trailing_dot_localhost_blocked(self):
        from larkscout_browser import _validate_url

        with pytest.raises(Exception, match="not allowed"):
            _validate_url("http://localhost./")

    def test_metadata_fqdn_blocked(self):
        from larkscout_browser import _validate_url

        with pytest.raises(Exception, match="not allowed"):
            _validate_url("http://metadata.google.internal/computeMetadata/v1/")

    def test_url_allowed_blocks_domain_resolving_to_private(self, monkeypatch):
        import larkscout_browser.security as sec

        def fake_getaddrinfo(host, *a, **k):
            return [(2, 1, 6, "", ("169.254.169.254", 0))]

        monkeypatch.setattr(sec.socket, "getaddrinfo", fake_getaddrinfo)
        assert sec._url_allowed("http://rebind.attacker.example/") is False

    def test_url_allowed_passes_domain_resolving_to_public(self, monkeypatch):
        import larkscout_browser.security as sec

        def fake_getaddrinfo(host, *a, **k):
            return [(2, 1, 6, "", ("93.184.216.34", 0))]

        monkeypatch.setattr(sec.socket, "getaddrinfo", fake_getaddrinfo)
        assert sec._url_allowed("http://public.example/") is True

    def test_url_allowed_allows_unresolvable(self, monkeypatch):
        import larkscout_browser.security as sec

        def boom(host, *a, **k):
            raise OSError("nxdomain")

        monkeypatch.setattr(sec.socket, "getaddrinfo", boom)
        # Cannot resolve => browser cannot reach it => not an SSRF => allowed.
        assert sec._url_allowed("http://nonexistent.invalid/") is True
