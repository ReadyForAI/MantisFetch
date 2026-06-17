"""Tests for TASK-020: atomic writes, encoding, KeyError guards."""

import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


class TestBrowserWriteTextAtomic:
    """_write_text_atomic must write via temp + os.replace."""

    def test_basic_write(self):
        from mantisfetch_browser import _write_text_atomic

        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "test.txt"
            _write_text_atomic(p, "hello")
            assert p.read_text(encoding="utf-8") == "hello"
            assert not p.with_suffix(".tmp").exists()

    def test_unicode_content(self):
        from mantisfetch_browser import _write_text_atomic

        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "uni.txt"
            _write_text_atomic(p, "中文测试 日本語 한국어")
            assert p.read_text(encoding="utf-8") == "中文测试 日本語 한국어"


class TestBrowserCounterAtomic:
    """_next_web_doc_id must use atomic write with encoding."""

    def test_counter_uses_utf8(self):
        from mantisfetch_browser import _next_web_doc_id

        with tempfile.TemporaryDirectory() as tmp:
            docs_dir = Path(tmp)
            _next_web_doc_id(docs_dir)
            counter_path = docs_dir / ".web_counter"
            assert counter_path.exists()
            val = counter_path.read_text(encoding="utf-8").strip()
            assert val.isdigit()


class TestDocreaderCounterAtomic:
    """_next_doc_id must use atomic write with encoding."""

    def test_counter_uses_utf8(self):
        from mantisfetch_docreader import _next_doc_id

        with tempfile.TemporaryDirectory() as tmp:
            docs_dir = Path(tmp)
            _next_doc_id(docs_dir)
            counter_path = docs_dir / ".counter"
            assert counter_path.exists()
            val = counter_path.read_text(encoding="utf-8").strip()
            assert val.isdigit()


class TestDocIndexDocumentsKeyValidation:
    """doc-index must handle missing or corrupt 'documents' key."""

    def test_docreader_handles_missing_documents_key(self):
        from mantisfetch_docreader import _update_doc_index

        with tempfile.TemporaryDirectory() as tmp:
            docs_dir = Path(tmp)
            index_path = docs_dir / "doc-index.json"
            index_path.write_text(json.dumps({"version": 2}), encoding="utf-8")

            meta = {
                "doc_id": "DOC-001",
                "filename": "test.pdf",
                "file_type": "pdf",
                "total_pages": 1,
                "section_count": 1,
                "ocr_page_count": 0,
                "table_count": 0,
                "created_at": "2026-01-01T00:00:00Z",
            }
            _update_doc_index(docs_dir, meta, "digest text")

            index = json.loads(index_path.read_text(encoding="utf-8"))
            assert isinstance(index["documents"], list)
            assert len(index["documents"]) == 1
            assert index["documents"][0]["id"] == "DOC-001"

    def test_docreader_handles_non_list_documents(self):
        from mantisfetch_docreader import _update_doc_index

        with tempfile.TemporaryDirectory() as tmp:
            docs_dir = Path(tmp)
            index_path = docs_dir / "doc-index.json"
            index_path.write_text(
                json.dumps({"version": 2, "documents": "corrupted"}), encoding="utf-8"
            )

            meta = {
                "doc_id": "DOC-002",
                "filename": "test.pdf",
                "file_type": "pdf",
                "total_pages": 1,
                "section_count": 1,
                "ocr_page_count": 0,
                "table_count": 0,
                "created_at": "2026-01-01T00:00:00Z",
            }
            _update_doc_index(docs_dir, meta, "digest text")

            index = json.loads(index_path.read_text(encoding="utf-8"))
            assert isinstance(index["documents"], list)
            assert len(index["documents"]) == 1


class TestWebMCPToolNameGuard:
    """WebMCP action must safely handle missing tool_name via .get()."""

    def test_get_used_instead_of_subscript(self):
        """Verify the code uses .get() so missing tool_name won't KeyError."""
        import ast
        import inspect

        from mantisfetch_browser import act

        source = inspect.getsource(act)
        tree = ast.parse(source)

        # Walk AST: confirm no Subscript access to "tool_name" on strategy
        # (i.e. ad["strategy"]["tool_name"] should not appear)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Subscript)
                and isinstance(node.slice, ast.Constant)
                and node.slice.value == "tool_name"
            ):
                pytest.fail(
                    "Found ad['strategy']['tool_name'] subscript — should use .get() to avoid KeyError"
                )
