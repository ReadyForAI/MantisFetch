"""An oversized upload stops being read; it is not spooled and then refused.

`MAX_UPLOAD_BYTES` is checked while copying the multipart spool into the parse
scratch file — which is after Starlette has already parsed the multipart body
and written the whole thing to that spool. So the limit bounded what MantisFetch
*kept*, never what a caller could make it *receive*: a 4 KiB body against a
16-byte limit landed in full before the 413 (the review measured exactly that),
and nothing bounded the disk a burst of oversized uploads could take.

The ceiling here is the outer one, in front of the form parser. The per-request
limits stay where they are: MAX_UPLOAD_BYTES for the parse channel and the
per-type raw ceilings for store_only, both of which need to know the filename
and therefore cannot run before the form is parsed.
"""

import pytest


def _multipart(body: bytes, boundary: str = "----ceiling") -> bytes:
    return (
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="big.txt"\r\n'
            f"Content-Type: text/plain\r\n\r\n"
        ).encode()
        + body
        + f"\r\n--{boundary}--\r\n".encode()
    )


@pytest.fixture()
def small_ceiling(monkeypatch):
    import mantisfetch_server as server

    monkeypatch.setattr(server, "_max_request_bytes", lambda: 4096)
    return 4096


def test_a_body_over_the_ceiling_is_refused(client, small_ceiling) -> None:
    resp = client.post(
        "/doc/parse",
        content=_multipart(b"x" * 8192),
        headers={"Content-Type": "multipart/form-data; boundary=----ceiling"},
    )
    assert resp.status_code == 413
    assert "request body" in resp.text.lower()


def test_the_body_is_not_read_past_the_ceiling(client, small_ceiling, monkeypatch) -> None:
    """The point of moving the check: a refusal must cost bounded memory and
    bounded disk, not a full spool followed by an error."""
    written: list[int] = []
    import starlette.datastructures as ds

    real_write = ds.UploadFile.write

    async def counting_write(self, data):
        written.append(len(data))
        return await real_write(self, data)

    monkeypatch.setattr(ds.UploadFile, "write", counting_write)

    client.post(
        "/doc/parse",
        content=_multipart(b"x" * 200_000),
        headers={"Content-Type": "multipart/form-data; boundary=----ceiling"},
    )

    assert sum(written) <= 4096 + 65536, f"{sum(written)} bytes reached the spool"


def test_a_body_within_the_ceiling_is_untouched(client, small_ceiling) -> None:
    """The boundary case has to pass, or the ceiling is just a smaller bug."""
    resp = client.post(
        "/doc/parse",
        content=_multipart(b"<h1>T</h1><p>Body worth keeping.</p>"),
        headers={"Content-Type": "multipart/form-data; boundary=----ceiling"},
        params={},
    )
    assert resp.status_code in (200, 422), resp.text
    assert resp.status_code != 413


def test_a_declared_content_length_is_refused_before_the_body_arrives(
    client, small_ceiling
) -> None:
    """A caller that says up front how much it is about to send should not have
    to send it."""
    resp = client.post(
        "/doc/parse",
        content=b"x" * 8192,
        headers={
            "Content-Type": "multipart/form-data; boundary=----ceiling",
            "Content-Length": "8192",
        },
    )
    assert resp.status_code == 413


def test_reads_and_health_are_not_affected(client, small_ceiling) -> None:
    assert client.get("/doc/health").status_code == 200
    assert client.get("/health").status_code == 200
    assert client.get("/doc/library/search", params={"q": "x"}).status_code == 200


def test_the_ceiling_is_derived_from_the_upload_limit(monkeypatch) -> None:
    """One number to raise, not two that can drift apart."""
    import mantisfetch_docreader as dr

    import mantisfetch_server as server

    assert server._max_request_bytes() > dr.MAX_UPLOAD_BYTES
    assert server._max_request_bytes() < dr.MAX_UPLOAD_BYTES * 2
