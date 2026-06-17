"""Tests for TASK-019: unified doc-index v2 and manifest schema consistency."""

import hashlib
import json
import tempfile
from pathlib import Path

import pytest

# Required fields that every doc-index entry MUST contain
DOC_INDEX_REQUIRED_FIELDS = {
    "id", "filename", "file_type", "content_type", "storage_path", "source", "source_url",
    "pages", "sections", "ocr_pages", "tables",
    "digest", "digest_path", "tags", "created_at", "content_hash",
}

# Required fields that every manifest MUST contain
MANIFEST_REQUIRED_FIELDS = {
    "doc_id", "filename", "file_type", "source", "tags", "paths", "sections", "provenance",
}

PROVENANCE_REQUIRED_FIELDS = {
    "source", "source_url", "created_at", "content_hash",
}

SECTION_REQUIRED_FIELDS = {
    "sid", "index", "title", "char_count", "type", "file",
}


class TestDocIndexBrowserEntry:
    """Browser doc-index entries must have all required fields."""

    def test_browser_index_entry_has_all_fields(self):
        from mantisfetch_browser import _persist_web_capture

        with tempfile.TemporaryDirectory() as tmp:
            docs_dir = Path(tmp)
            _persist_web_capture(
                doc_id="WEB-001",
                url="https://example.com",
                title="Example",
                sections=[
                    {"sid": "s_001", "h": "Intro", "t": "Hello world", "type": "text"},
                    {"sid": "t_001", "h": "Table 1", "t": "| a | b |", "type": "table", "table_meta": {}},
                ],
                digest="Test digest",
                tags=["test"],
                content_hash="sha256:abc123",
                docs_dir=docs_dir,
            )

            index = json.loads((docs_dir / "doc-index.json").read_text(encoding="utf-8"))
            entry = index["documents"][0]
            missing = DOC_INDEX_REQUIRED_FIELDS - set(entry.keys())
            assert not missing, f"Browser index entry missing fields: {missing}"

    def test_browser_index_entry_values(self):
        from mantisfetch_browser import _persist_web_capture

        with tempfile.TemporaryDirectory() as tmp:
            docs_dir = Path(tmp)
            _persist_web_capture(
                doc_id="WEB-002", url="https://example.com", title="Test",
                sections=[{"sid": "s_001", "h": "A", "t": "text", "type": "text"}],
                digest="digest", tags=[], content_hash="sha256:x",
                docs_dir=docs_dir,
            )
            entry = json.loads((docs_dir / "doc-index.json").read_text(encoding="utf-8"))["documents"][0]
            assert entry["pages"] == 1
            assert entry["ocr_pages"] == 0
            assert entry["content_hash"] == "sha256:x"
            assert entry["source_url"] == "https://example.com"


class TestDocIndexDocreaderEntry:
    """Docreader doc-index entries must have all required fields."""

    def test_docreader_index_entry_has_all_fields(self):
        from mantisfetch_docreader import _update_doc_index

        with tempfile.TemporaryDirectory() as tmp:
            docs_dir = Path(tmp)
            meta = {
                "doc_id": "DOC-001", "filename": "test.pdf", "file_type": "pdf",
                "total_pages": 5, "section_count": 3, "ocr_page_count": 1,
                "table_count": 2, "created_at": "2026-01-01T00:00:00Z",
            }
            _update_doc_index(docs_dir, meta, "Test digest",
                              tags=["demo"], source="upload",
                              content_hash="sha256:abc", source_url="test.pdf")

            index = json.loads((docs_dir / "doc-index.json").read_text(encoding="utf-8"))
            entry = index["documents"][0]
            missing = DOC_INDEX_REQUIRED_FIELDS - set(entry.keys())
            assert not missing, f"Docreader index entry missing fields: {missing}"

    def test_docreader_index_entry_without_optional_args(self):
        """Even without source_url/content_hash args, entry must have all fields."""
        from mantisfetch_docreader import _update_doc_index

        with tempfile.TemporaryDirectory() as tmp:
            docs_dir = Path(tmp)
            meta = {
                "doc_id": "DOC-002", "filename": "test.csv", "file_type": "csv",
                "total_pages": 1, "section_count": 1, "ocr_page_count": 0,
                "table_count": 1, "created_at": "2026-01-01T00:00:00Z",
            }
            _update_doc_index(docs_dir, meta, "CSV digest", tags=[], source="upload")

            index = json.loads((docs_dir / "doc-index.json").read_text(encoding="utf-8"))
            entry = index["documents"][0]
            missing = DOC_INDEX_REQUIRED_FIELDS - set(entry.keys())
            assert not missing, f"Docreader index entry missing fields (no optionals): {missing}"
            assert entry["content_hash"] == ""
            assert entry["source_url"] == ""

    def test_docreader_index_entry_includes_metadata_and_source_ref(self):
        from mantisfetch_docreader import _update_doc_index

        with tempfile.TemporaryDirectory() as tmp:
            docs_dir = Path(tmp)
            meta = {
                "doc_id": "DOC-003", "filename": "contract.pdf", "file_type": "pdf",
                "total_pages": 2, "section_count": 1, "ocr_page_count": 0,
                "table_count": 0, "created_at": "2026-01-01T00:00:00Z",
            }
            _update_doc_index(
                docs_dir,
                meta,
                "Contract digest",
                metadata={"customer": "ACME", "nested": {"ignored": True}},
                source_record={"ref": "source/contract.pdf", "filename": "contract.pdf", "sha256": "abc"},
            )

            entry = json.loads((docs_dir / "doc-index.json").read_text(encoding="utf-8"))["documents"][0]
            assert entry["metadata"]["customer"] == "ACME"
            assert "nested" not in entry["metadata"]
            assert entry["source_ref"] == "source/contract.pdf"


class TestManifestBrowserFormat:
    """Browser manifest must have all required fields."""

    def test_browser_manifest_has_required_fields(self):
        from mantisfetch_browser import _persist_web_capture

        with tempfile.TemporaryDirectory() as tmp:
            docs_dir = Path(tmp)
            _persist_web_capture(
                doc_id="WEB-010", url="https://example.com", title="Test",
                sections=[{"sid": "s_001", "h": "Intro", "t": "Hello", "type": "text"}],
                digest="digest", tags=[], content_hash="sha256:x",
                docs_dir=docs_dir,
            )
            manifest = json.loads(
                (docs_dir / "General" / "WEB-010" / "manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            missing = MANIFEST_REQUIRED_FIELDS - set(manifest.keys())
            assert not missing, f"Browser manifest missing fields: {missing}"

    def test_browser_manifest_provenance_fields(self):
        from mantisfetch_browser import _persist_web_capture

        with tempfile.TemporaryDirectory() as tmp:
            docs_dir = Path(tmp)
            _persist_web_capture(
                doc_id="WEB-011", url="https://example.com", title="Test",
                sections=[{"sid": "s_001", "h": "A", "t": "text", "type": "text"}],
                digest="d", tags=[], content_hash="sha256:y",
                docs_dir=docs_dir,
            )
            manifest = json.loads(
                (docs_dir / "General" / "WEB-011" / "manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            prov = manifest["provenance"]
            missing = PROVENANCE_REQUIRED_FIELDS - set(prov.keys())
            assert not missing, f"Browser provenance missing fields: {missing}"
            assert "created_at" in prov  # not capture_time
            assert "capture_time" not in prov

    def test_browser_manifest_section_fields(self):
        from mantisfetch_browser import _persist_web_capture

        with tempfile.TemporaryDirectory() as tmp:
            docs_dir = Path(tmp)
            _persist_web_capture(
                doc_id="WEB-012", url="https://example.com", title="Test",
                sections=[
                    {"sid": "s_001", "h": "Intro", "t": "Hello", "type": "text"},
                    {"sid": "t_001", "h": "Table", "t": "| a |", "type": "table"},
                ],
                digest="d", tags=[], content_hash="sha256:z",
                docs_dir=docs_dir,
            )
            manifest = json.loads(
                (docs_dir / "General" / "WEB-012" / "manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            for sec in manifest["sections"]:
                missing = SECTION_REQUIRED_FIELDS - set(sec.keys())
                assert not missing, f"Browser manifest section missing fields: {missing} in {sec}"


class TestManifestDocreaderFormat:
    """Docreader manifest (both full and extract-only) must have all required fields."""

    def _make_parsed(self):
        from mantisfetch_docreader import ParsedDocument, Section

        return ParsedDocument(
            filename="test.pdf", file_type="pdf", total_pages=2,
            pages=[], sections=[
                Section(index=1, title="Intro", level=1, text="Hello world",
                        page_range="1-1", sid="s_abc", summary="Short summary"),
            ], ocr_page_count=0, table_count=0,
        )

    def test_full_write_manifest_has_all_fields(self):
        from mantisfetch_docreader import write_output

        parsed = self._make_parsed()
        with tempfile.TemporaryDirectory() as tmp:
            docs_dir = Path(tmp)
            write_output("DOC-010", parsed, "digest text", "brief text",
                         docs_dir, tags=["t"], source="upload", original_path="test.pdf")
            manifest = json.loads((docs_dir / "DOC-010" / "manifest.json").read_text(encoding="utf-8"))
            missing = MANIFEST_REQUIRED_FIELDS - set(manifest.keys())
            assert not missing, f"Docreader full manifest missing: {missing}"
            assert manifest["tags"] == ["t"]

            prov = manifest["provenance"]
            missing_prov = PROVENANCE_REQUIRED_FIELDS - set(prov.keys())
            assert not missing_prov, f"Docreader full provenance missing: {missing_prov}"
            assert "created_at" in prov  # not upload_time
            assert "upload_time" not in prov

    def test_extract_only_manifest_has_all_fields(self):
        from mantisfetch_docreader import write_output_extract_only

        parsed = self._make_parsed()
        with tempfile.TemporaryDirectory() as tmp:
            docs_dir = Path(tmp)
            write_output_extract_only("DOC-011", parsed, docs_dir, tags=["招标文件"], source="upload")
            manifest = json.loads((docs_dir / "DOC-011" / "manifest.json").read_text(encoding="utf-8"))
            missing = MANIFEST_REQUIRED_FIELDS - set(manifest.keys())
            assert not missing, f"Extract-only manifest missing: {missing}"
            assert manifest["tags"] == ["招标文件"]

            prov = manifest["provenance"]
            missing_prov = PROVENANCE_REQUIRED_FIELDS - set(prov.keys())
            assert not missing_prov, f"Extract-only provenance missing: {missing_prov}"

    def test_extract_only_manifest_has_file_type_and_source(self):
        from mantisfetch_docreader import write_output_extract_only

        parsed = self._make_parsed()
        with tempfile.TemporaryDirectory() as tmp:
            docs_dir = Path(tmp)
            write_output_extract_only("DOC-012", parsed, docs_dir, tags=[], source="upload")
            manifest = json.loads((docs_dir / "DOC-012" / "manifest.json").read_text(encoding="utf-8"))
            assert manifest["file_type"] == "pdf"
            assert manifest["source"] == "upload"

    def test_rewrite_removes_stale_generated_section_files(self):
        from mantisfetch_docreader import ParsedDocument, Section, write_output_extract_only

        with tempfile.TemporaryDirectory() as tmp:
            docs_dir = Path(tmp)
            first = ParsedDocument(
                filename="test.pdf",
                file_type="pdf",
                total_pages=1,
                pages=[],
                sections=[
                    Section(index=1, title="Old", level=1, text="old", page_range="1-1", sid="old"),
                ],
            )
            second = ParsedDocument(
                filename="test.pdf",
                file_type="pdf",
                total_pages=1,
                pages=[],
                sections=[
                    Section(index=1, title="New", level=1, text="new", page_range="1-1", sid="new"),
                ],
            )

            write_output_extract_only("DOC-012", first, docs_dir, tags=[], source="upload")
            write_output_extract_only("DOC-012", second, docs_dir, tags=[], source="upload")

            section_names = sorted(p.name for p in (docs_dir / "DOC-012" / "sections").iterdir())
            assert section_names == ["01-new-New.md"]

    def test_docreader_manifest_includes_metadata_and_source_file(self):
        from mantisfetch_docreader import write_output

        parsed = self._make_parsed()
        with tempfile.TemporaryDirectory() as tmp:
            docs_dir = Path(tmp)
            write_output(
                "DOC-014",
                parsed,
                "digest",
                "brief",
                docs_dir,
                tags=[],
                source="upload",
                metadata={"customer": "ACME"},
                source_record={"ref": "source/contract.pdf", "filename": "contract.pdf", "sha256": "abc"},
            )
            manifest = json.loads((docs_dir / "DOC-014" / "manifest.json").read_text(encoding="utf-8"))
            assert manifest["metadata"]["customer"] == "ACME"
            assert manifest["source_file"]["ref"] == "source/contract.pdf"
            assert manifest["sections"][0]["page_start"] == 1
            assert manifest["sections"][0]["page_end"] == 1

    def test_docreader_manifest_section_has_type(self):
        from mantisfetch_docreader import write_output

        parsed = self._make_parsed()
        with tempfile.TemporaryDirectory() as tmp:
            docs_dir = Path(tmp)
            write_output("DOC-013", parsed, "digest", "brief",
                         docs_dir, tags=[], source="upload")
            manifest = json.loads((docs_dir / "DOC-013" / "manifest.json").read_text(encoding="utf-8"))
            for sec in manifest["sections"]:
                assert "type" in sec, f"Section missing 'type': {sec}"


class TestManifestTagsBackfill:
    """Startup backfill restores `tags` on legacy manifests from doc-index.json."""

    @staticmethod
    def _write_legacy_manifest(docs_dir: Path, storage_rel: str, doc_id: str) -> Path:
        doc_dir = docs_dir / storage_rel
        doc_dir.mkdir(parents=True, exist_ok=True)
        manifest = {
            "doc_id": doc_id,
            "filename": f"{doc_id}.pdf",
            "file_type": "pdf",
            "source": "upload",
            "paths": {"sections": "sections.json"},
            "sections": [],
            "provenance": {"source": "upload", "source_url": "", "created_at": "", "content_hash": ""},
        }
        path = doc_dir / "manifest.json"
        path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    @staticmethod
    def _write_index(docs_dir: Path, entries: list[dict]) -> None:
        (docs_dir / "doc-index.json").write_text(
            json.dumps({"version": 2, "documents": entries}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def test_backfill_populates_missing_tags_from_index(self):
        from mantisfetch_docreader import _backfill_manifest_tags

        with tempfile.TemporaryDirectory() as tmp:
            docs_dir = Path(tmp)
            m_path = self._write_legacy_manifest(docs_dir, "Bid/DOC-100", "DOC-100")
            self._write_index(docs_dir, [{"id": "DOC-100", "tags": ["招标文件"]}])

            stats = _backfill_manifest_tags(docs_dir)
            assert stats["patched"] == 1
            assert stats["skipped"] == 0
            patched = json.loads(m_path.read_text(encoding="utf-8"))
            assert patched["tags"] == ["招标文件"]

    def test_backfill_is_idempotent(self):
        from mantisfetch_docreader import _backfill_manifest_tags

        with tempfile.TemporaryDirectory() as tmp:
            docs_dir = Path(tmp)
            m_path = self._write_legacy_manifest(docs_dir, "Bid/DOC-101", "DOC-101")
            self._write_index(docs_dir, [{"id": "DOC-101", "tags": ["x"]}])

            first = _backfill_manifest_tags(docs_dir)
            assert first["patched"] == 1
            second = _backfill_manifest_tags(docs_dir)
            assert second["patched"] == 0
            assert second["skipped"] == 1
            patched = json.loads(m_path.read_text(encoding="utf-8"))
            assert patched["tags"] == ["x"]

    def test_backfill_writes_empty_when_index_missing_entry(self):
        from mantisfetch_docreader import _backfill_manifest_tags

        with tempfile.TemporaryDirectory() as tmp:
            docs_dir = Path(tmp)
            m_path = self._write_legacy_manifest(docs_dir, "Bid/DOC-102", "DOC-102")
            self._write_index(docs_dir, [])  # no entry for DOC-102

            stats = _backfill_manifest_tags(docs_dir)
            assert stats["patched"] == 1
            assert stats["missing_index"] == 1
            patched = json.loads(m_path.read_text(encoding="utf-8"))
            assert patched["tags"] == []

    def test_backfill_preserves_existing_tags(self):
        from mantisfetch_docreader import _backfill_manifest_tags

        with tempfile.TemporaryDirectory() as tmp:
            docs_dir = Path(tmp)
            doc_dir = docs_dir / "Bid" / "DOC-103"
            doc_dir.mkdir(parents=True)
            manifest = {
                "doc_id": "DOC-103",
                "filename": "x.pdf", "file_type": "pdf", "source": "upload",
                "tags": ["keep-me"],  # already present, must not be overwritten
                "paths": {}, "sections": [],
                "provenance": {"source": "upload", "source_url": "", "created_at": "", "content_hash": ""},
            }
            m_path = doc_dir / "manifest.json"
            m_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
            self._write_index(docs_dir, [{"id": "DOC-103", "tags": ["overwrite-me"]}])

            stats = _backfill_manifest_tags(docs_dir)
            assert stats["patched"] == 0
            assert stats["skipped"] == 1
            patched = json.loads(m_path.read_text(encoding="utf-8"))
            assert patched["tags"] == ["keep-me"]


class TestDocEntryFromManifestTags:
    """`_doc_entry_from_manifest` fallback must read tags from manifest, not just .meta.json."""

    def test_fallback_reads_tags_from_manifest(self):
        from mantisfetch_docreader import _doc_entry_from_manifest

        with tempfile.TemporaryDirectory() as tmp:
            docs_dir = Path(tmp)
            doc_dir = docs_dir / "Bid" / "DOC-200"
            doc_dir.mkdir(parents=True)
            manifest = {
                "doc_id": "DOC-200",
                "filename": "x.pdf", "file_type": "pdf", "source": "upload",
                "content_type": "Bid", "storage_path": "Bid/DOC-200",
                "tags": ["招标文件"],
                "paths": {}, "sections": [],
                "provenance": {"source": "upload", "source_url": "x.pdf",
                               "created_at": "2026-06-01T00:00:00Z", "content_hash": ""},
            }
            (doc_dir / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
            )
            # No .meta.json on disk — manifest is the only source.
            entry = _doc_entry_from_manifest(docs_dir, "DOC-200")
            assert entry is not None
            assert entry["tags"] == ["招标文件"]

    def test_fallback_falls_back_to_meta_when_manifest_lacks_tags(self):
        from mantisfetch_docreader import _doc_entry_from_manifest

        with tempfile.TemporaryDirectory() as tmp:
            docs_dir = Path(tmp)
            doc_dir = docs_dir / "Bid" / "DOC-201"
            doc_dir.mkdir(parents=True)
            manifest = {  # legacy manifest, no tags
                "doc_id": "DOC-201",
                "filename": "x.pdf", "file_type": "pdf", "source": "upload",
                "content_type": "Bid", "storage_path": "Bid/DOC-201",
                "paths": {}, "sections": [],
                "provenance": {"source": "upload", "source_url": "x.pdf",
                               "created_at": "2026-06-01T00:00:00Z", "content_hash": ""},
            }
            (doc_dir / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
            )
            (doc_dir / ".meta.json").write_text(
                json.dumps({"tags": ["legacy-tag"]}, ensure_ascii=False), encoding="utf-8"
            )
            entry = _doc_entry_from_manifest(docs_dir, "DOC-201")
            assert entry is not None
            assert entry["tags"] == ["legacy-tag"]
