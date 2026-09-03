"""Text uploads are decoded as UTF-8 when they are UTF-8, not as whatever a
detector guesses from the bytes.

Measured on a 117-byte JSON probe holding ``口足目JSON探针``: MarkItDown, given
only a path, asks charset-normalizer to name the encoding, and it answered
``cp1251``. The document stored — and every search over it afterwards — read
``еЏЈи¶із›®JSONжЋўй’€``.

The guess is also not stable across versions of the detector: the same bytes
read correctly under charset-normalizer 3.4.9 and as Cyrillic under 3.5.1, the
version the image ships. So the end-to-end assertions here would pass for the
wrong reason on a lucky detector; the mechanism tests below the first one do not
depend on which version is installed.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

CJK_JSON = '{\n  "kind": "probe",\n  "cjk": "口足目JSON探针",\n  "n": 5\n}\n'


def _write(tmp_path: Path, name: str, data: bytes) -> Path:
    p = tmp_path / name
    p.write_bytes(data)
    return p


# ── the reported failure, end to end ─────────────────────────────────────────────
def test_a_short_cjk_json_survives_the_round_trip(tmp_path: Path) -> None:
    import mantisfetch_docreader as dr

    path = _write(tmp_path, "probe.json", CJK_JSON.encode("utf-8"))
    out = dr._convert_to_markdown(path)

    assert "口足目JSON探针" in out
    assert "еЏЈ" not in out  # the cp1251 reading of the same bytes


@pytest.mark.parametrize("name", ["probe.txt", "probe.csv", "probe.html", "probe.xml"])
def test_the_other_text_formats_too(tmp_path: Path, name: str) -> None:
    import mantisfetch_docreader as dr

    path = _write(tmp_path, name, "口足目探针 probe\n".encode())
    assert "口足目探针" in dr._convert_to_markdown(path)


# ── the mechanism, independent of which detector version is installed ────────────
def test_valid_utf8_is_declared_rather_than_detected(tmp_path: Path) -> None:
    import mantisfetch_docreader as dr

    path = _write(tmp_path, "probe.json", CJK_JSON.encode("utf-8"))
    assert dr._utf8_charset_hint(path) == "utf-8"


def test_a_bom_is_consumed_not_left_in_the_text(tmp_path: Path) -> None:
    """utf-8 would decode the BOM to a stray ﻿ at the head of the document,
    where it lands in the title of the first section."""
    import mantisfetch_docreader as dr

    path = _write(tmp_path, "probe.txt", b"\xef\xbb\xbf" + "口足目探针".encode())
    assert dr._utf8_charset_hint(path) == "utf-8-sig"
    assert "﻿" not in dr._convert_to_markdown(path)


def test_bytes_that_are_not_utf8_are_left_to_the_detector(tmp_path: Path) -> None:
    """The hint must not claim UTF-8 for a genuine GBK upload — that is the one
    case where detection is the right answer and this must not pre-empt it."""
    import mantisfetch_docreader as dr

    gbk = ("口足目探针，用于验证中文编码检测。" * 20).encode("gbk")
    path = _write(tmp_path, "probe.txt", gbk)

    assert dr._utf8_charset_hint(path) is None
    assert "口足目探针" in dr._convert_to_markdown(path)


def test_the_hint_reaches_markitdown(tmp_path: Path) -> None:
    """The charset is only useful if it is handed over. Assert the call, so this
    keeps holding if MarkItDown's signature moves."""
    import mantisfetch_docreader as dr

    path = _write(tmp_path, "probe.json", CJK_JSON.encode("utf-8"))
    converter = MagicMock()
    converter.convert.return_value = MagicMock(text_content="ok")

    with patch.object(dr, "_get_converter", return_value=converter):
        dr._convert_to_markdown(path)

    stream_info = converter.convert.call_args.kwargs["stream_info"]
    assert stream_info is not None
    assert stream_info.charset == "utf-8"


def test_container_formats_get_no_charset(tmp_path: Path) -> None:
    """A .docx is a zip. Declaring a charset for it is meaningless, and the
    sniff would read a megabyte of compressed bytes to decide nothing."""
    import mantisfetch_docreader as dr

    path = _write(tmp_path, "probe.docx", b"PK\x03\x04rest-of-a-zip")
    converter = MagicMock()
    converter.convert.return_value = MagicMock(text_content="ok")

    with patch.object(dr, "_get_converter", return_value=converter):
        dr._convert_to_markdown(path)

    assert converter.convert.call_args.kwargs["stream_info"] is None


def test_an_unreadable_file_does_not_raise_from_the_sniff(tmp_path: Path) -> None:
    """The sniff opens the file before the converter does. If it cannot, that is
    the converter's error to report, not a new failure mode from this check."""
    import mantisfetch_docreader as dr

    assert dr._utf8_charset_hint(tmp_path / "absent.json") is None


def test_a_file_larger_than_one_chunk_is_fully_validated(tmp_path: Path) -> None:
    """The decoder runs incrementally, so a multi-byte character split across a
    chunk boundary must not read as invalid — nor must trailing garbage escape
    validation because only the first chunk was checked."""
    import mantisfetch_docreader as dr

    filler = "口" * (1024 * 1024)  # 3 MB of UTF-8, straddling chunk boundaries
    assert dr._utf8_charset_hint(_write(tmp_path, "big.txt", filler.encode())) == "utf-8"

    tail_broken = filler.encode() + b"\xff\xfe\xff"
    assert dr._utf8_charset_hint(_write(tmp_path, "broken.txt", tail_broken)) is None
