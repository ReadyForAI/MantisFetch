"""/parse's two default-on flags stay on when the request leaves them out.

From FastAPI 0.147 an omitted ``bool`` form field arrives as False (read as an
unchecked HTML checkbox), whatever its declared default. ``generate_summary``
and ``extract_tables`` default to True, so a caller that sent neither got no
summary and no tables. CI runs the newest FastAPI, which is where these bite.
"""

from __future__ import annotations

from pathlib import Path

import mantisfetch_docreader as dr
import pytest
from starlette.testclient import TestClient

import mantisfetch_common.doc_index_store as dis
import mantisfetch_common.storage as cs

HTML_WITH_TABLE = (
    b"<html><body><h1>Probe</h1><p>Prose before the table.</p>"
    b"<table><tr><th>Metric</th><th>Value</th></tr>"
    b"<tr><td>throughput</td><td>4.4</td></tr></table></body></html>"
)


@pytest.fixture()
def docs_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    d = tmp_path / "docs"
    d.mkdir()
    monkeypatch.setattr(cs, "DEFAULT_DOCS_DIR", d)
    return d


@pytest.fixture()
def deferred(monkeypatch: pytest.MonkeyPatch) -> list:
    """Each summary the upload hands to the background writer."""
    calls: list = []
    monkeypatch.setattr(dr, "_generate_deferred_summary", lambda *a, **k: calls.append(a))
    return calls


def _parse(**fields: str):
    client = TestClient(dr.app, raise_server_exceptions=False)
    return client.post(
        "/parse",
        files={"file": ("probe.html", HTML_WITH_TABLE, "text/html")},
        data={"summary_mode": "defer", **fields},
    )


def test_both_flags_left_out_mean_on(docs_dir: Path, deferred: list) -> None:
    r = _parse()
    assert r.status_code == 200, r.text
    assert len(deferred) == 1, "an upload that never said generate_summary got no summary"
    assert r.json()["table_count"] == 1, "an upload that never said extract_tables got no tables"


def test_an_empty_flag_is_left_out(docs_dir: Path, deferred: list) -> None:
    r = _parse(generate_summary="", extract_tables="")
    assert r.status_code == 200, r.text
    assert len(deferred) == 1
    assert r.json()["table_count"] == 1


@pytest.mark.parametrize("off", ["false", "False", "0", "no", "off"])
def test_each_flag_still_turns_off(docs_dir: Path, deferred: list, off: str) -> None:
    r = _parse(generate_summary=off, extract_tables=off)
    assert r.status_code == 200, r.text
    assert deferred == []
    assert r.json()["table_count"] == 0


@pytest.mark.parametrize("on", ["true", "True", "1", "yes", "on"])
def test_each_flag_still_turns_on(docs_dir: Path, deferred: list, on: str) -> None:
    r = _parse(generate_summary=on, extract_tables=on)
    assert r.status_code == 200, r.text
    assert len(deferred) == 1
    assert r.json()["table_count"] == 1


@pytest.mark.parametrize("field", ["generate_summary", "extract_tables"])
def test_a_flag_that_is_not_a_bool_is_refused_before_anything_is_written(
    docs_dir: Path, deferred: list, field: str
) -> None:
    r = _parse(**{field: "maybe"})
    assert r.status_code == 422, r.text
    [err] = r.json()["detail"]
    assert err["loc"] == ["body", field]
    assert err["type"] == "bool_parsing"
    assert dis.list_documents(docs_dir) == []
    assert not (docs_dir / ".counter").exists(), "a doc_id was minted for a refused upload"


def test_the_schema_still_says_boolean_default_true() -> None:
    body = dr.app.openapi()["components"]["schemas"]["Body_api_parse_doc_parse_post"]
    for field in ("generate_summary", "extract_tables"):
        prop = body["properties"][field]
        assert prop["type"] == "boolean", prop
        assert prop["default"] is True, prop
