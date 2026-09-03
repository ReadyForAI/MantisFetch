"""Text extracted from a document is the document's text, not a markdown
rendering of it.

From a doc-parse evaluation against v1.7.2: a DOCX cell holding
``MFPARSE_DOCX_TABLE_W4`` was stored as ``MFPARSE\\_DOCX\\_TABLE\\_W4``, and
``search_text`` for the string that is in the document returned nothing. Same
for XLSX and HTML. PDF, CSV, TXT and PPTX were unaffected, which is the clue:
those do not go through markdownify, whose defaults escape ``_`` and ``*``.
"""

import io
import zipfile
from pathlib import Path

import pytest
from starlette.testclient import TestClient

PROBE = "MFPARSE_DOCX_TABLE_W4"
STAR_PROBE = "build*fast"


def _docx(body_rows: list[str]) -> bytes:
    """A minimal but real .docx — MarkItDown reads it through markdownify."""
    paragraphs = "".join(
        f"<w:p><w:r><w:t>{row}</w:t></w:r></w:p>" for row in body_rows
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{paragraphs}</w:body></w:document>"
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.'
        'openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>'
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/'
        '2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>'
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", document)
    return buf.getvalue()


HTML = (
    "<html><body><h1>Probe</h1>"
    f"<p>Unique paragraph probe: {PROBE} appears here.</p>"
    f"<p>Also {STAR_PROBE} with a star.</p>"
    "</body></html>"
).encode()


def test_an_identifier_in_a_docx_keeps_its_underscores(tmp_path: Path) -> None:
    import mantisfetch_docreader as dr

    path = tmp_path / "probe.docx"
    path.write_bytes(_docx([f"Cell probe: {PROBE}"]))

    out = dr._convert_to_markdown(path)
    assert PROBE in out
    assert "\\_" not in out


def test_html_too(tmp_path: Path) -> None:
    import mantisfetch_docreader as dr

    path = tmp_path / "probe.html"
    path.write_bytes(HTML)

    out = dr._convert_to_markdown(path)
    assert PROBE in out
    assert "\\_" not in out


def test_asterisks_are_not_escaped_either(tmp_path: Path) -> None:
    """The evaluation only had underscores in its probes, which made the escape
    look narrower than it is: markdownify escapes * on the same default."""
    import mantisfetch_docreader as dr

    path = tmp_path / "probe.html"
    path.write_bytes(HTML)

    out = dr._convert_to_markdown(path)
    assert STAR_PROBE in out
    assert "\\*" not in out


@pytest.mark.parametrize(
    ("name", "payload"),
    [
        ("plain.txt", f"probe {PROBE} here\n".encode()),
        ("data.csv", f"metric,value\nunique_cell,{PROBE}\n".encode()),
        ("data.json", ('{"probe": "' + PROBE + '"}').encode()),
    ],
)
def test_the_formats_that_were_never_escaped_are_unchanged(
    tmp_path: Path, name: str, payload: bytes
) -> None:
    import mantisfetch_docreader as dr

    path = tmp_path / name
    path.write_bytes(payload)
    assert PROBE in dr._convert_to_markdown(path)


def test_the_probe_is_findable_through_the_library(client: TestClient, tmp_path: Path) -> None:
    """The point of the change: the string in the document is the string the
    library can be searched for."""
    resp = client.post(
        "/doc/parse",
        files={"file": ("probe.html", HTML, "application/octet-stream")},
        data={"summary_mode": "off", "generate_summary": "false", "content_type": "General"},
    )
    assert resp.status_code == 200

    hits = client.get("/doc/library/search_text", params={"q": PROBE})
    assert hits.status_code == 200
    body = hits.json()
    assert body["total"] >= 1, f"searching for the document's own text found nothing: {body}"
    assert body.get("skipped", 0) == 0
