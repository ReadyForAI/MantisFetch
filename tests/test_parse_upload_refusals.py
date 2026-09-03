"""An upload that cannot become a document is refused, with a status that says
whose problem it is.

From a doc-parse evaluation run against v1.7.2:

- a 0-byte .txt returned 200 and DOC-010, a document whose only section was
  titled "Full document" with char_count 0, indexed under the caller's tags
- 68 bytes of binary named .pdf returned 500. Nothing was stored, which is
  right, but 500 tells the caller to retry a file that will never parse

Both are the same judgement /web/capture already makes: an error page and a
page that yields nothing are not documents, and neither is an empty or
unreadable upload.
"""

import io
import zipfile

import pytest
from starlette.testclient import TestClient


def _parse(client: TestClient, name: str, data: bytes, **form: str):
    return client.post(
        "/doc/parse",
        files={"file": (name, data, "application/octet-stream")},
        data={
            "summary_mode": "off",
            "generate_summary": "false",
            "content_type": "General",
            **form,
        },
    )


def _library(client: TestClient) -> list[dict]:
    r = client.get("/doc/library")
    return r.json().get("documents", []) if r.status_code == 200 else []


# ── an empty upload ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize("name", ["empty.txt", "empty.json", "empty.csv", "empty.pdf"])
def test_a_zero_byte_upload_is_refused(client: TestClient, name: str) -> None:
    before = len(_library(client))
    resp = _parse(client, name, b"")

    assert resp.status_code == 422
    assert "empty" in resp.json()["detail"]
    assert len(_library(client)) == before


def test_the_empty_refusal_names_the_file(client: TestClient) -> None:
    """A batch ingest refusing one of forty uploads has to say which."""
    assert "quarterly-report.txt" in _parse(client, "quarterly-report.txt", b"").json()["detail"]


def test_an_empty_upload_mints_no_doc_id_and_leaves_no_directory(
    client: TestClient, tmp_path
) -> None:
    """Size is known before any parser runs, so this refuses ahead of the
    counter — no id burned, and no .parse-failed.json, because a file with
    nothing in it is a rejected request rather than a parse that failed."""
    import mantisfetch_docreader as dr

    docs_dir = dr._get_docs_dir()
    _parse(client, "empty.txt", b"")

    assert not list((docs_dir / "General").glob("DOC-*")) if (docs_dir / "General").exists() else True
    assert not (docs_dir / ".counter").exists() or (docs_dir / ".counter").read_text().strip() in ("", "0")


def test_a_single_byte_is_not_empty(client: TestClient) -> None:
    """The predicate is 0 bytes, not "looks empty". A file holding one character
    is a real if tiny document and still parses."""
    assert _parse(client, "tiny.txt", b"x").status_code == 200


# ── an unreadable upload ─────────────────────────────────────────────────────────
def test_binary_named_pdf_is_refused_as_the_callers_problem(client: TestClient) -> None:
    resp = _parse(client, "not-a-pdf.pdf", b"MZ\x90\x00this is a windows binary, not a pdf\n")
    assert resp.status_code == 422


@pytest.mark.parametrize("name", ["broken.docx", "broken.xlsx", "broken.pptx"])
def test_ooxml_that_is_not_a_zip_is_refused(client: TestClient, name: str) -> None:
    """Not reported, found while reproducing the fake PDF. Nothing downstream
    catches this: MarkItDown falls back to reading the bytes as plain text, so
    40 bytes of junk named .docx came back 200 with a one-section document
    holding the junk — worse than the fake PDF's 500, because it looks like it
    worked. The zip check runs before the doc_id, so nothing is stored."""
    before = len(_library(client))
    resp = _parse(client, name, b"PK\x03\x04 truncated, not actually a zip archive")

    assert resp.status_code == 422
    assert "not a zip archive" in resp.json()["detail"]
    assert len(_library(client)) == before


def test_a_valid_zip_gets_past_the_zip_check(client: TestClient) -> None:
    """Guard the other direction. The check asks one question — is this a zip —
    and a file that is one goes on to the parser, whatever it makes of it. A
    stricter check here would refuse OOXML variants nobody tested."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("hello.txt", "a zip, if not much of a docx")

    resp = _parse(client, "valid-zip.docx", buf.getvalue())
    assert "not a zip archive" not in str(resp.json().get("detail", ""))


def test_legacy_binary_office_is_not_zip_checked(client: TestClient) -> None:
    """.doc/.ppt/.xls are OLE2 compound files, not zips. Applying the check to
    them would refuse every legacy upload."""
    ole2 = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64
    for name in ("legacy.doc", "legacy.ppt", "legacy.xls"):
        detail = str(_parse(client, name, ole2).json().get("detail", ""))
        assert "not a zip archive" not in detail


# ── the mapping itself, without going through a parser ───────────────────────────
def test_only_the_unreadable_classes_are_remapped() -> None:
    """A missing LibreOffice, an OOM or a parser bug is a server fault. Remapping
    those to 422 would tell every caller their file was bad whenever this
    service was."""
    import fitz
    import mantisfetch_docreader as dr

    assert dr._is_unreadable_document(fitz.FileDataError("no pdf here"))

    assert not dr._is_unreadable_document(MemoryError())
    assert not dr._is_unreadable_document(RuntimeError("soffice not found"))
    assert not dr._is_unreadable_document(TimeoutError())


def test_a_wrapped_cause_is_still_found() -> None:
    """Parsers re-raise. The chain is walked so wrapping does not put a document
    back on 500."""
    import fitz
    import mantisfetch_docreader as dr

    try:
        try:
            raise fitz.FileDataError("cannot open broken document")
        except fitz.FileDataError as inner:
            raise RuntimeError("failed to open file") from inner
    except RuntimeError as outer:
        assert dr._is_unreadable_document(outer)


def test_a_self_referential_chain_terminates() -> None:
    """__context__ can point back at an exception already seen. Walking it must
    not hang the request thread."""
    import mantisfetch_docreader as dr

    a = RuntimeError("a")
    b = RuntimeError("b")
    a.__context__ = b
    b.__context__ = a
    assert dr._is_unreadable_document(a) is False
