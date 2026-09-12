"""An upload is admitted by its size before any of it is read.

Starlette parses a multipart body — spooling each file past its first MiB to
the system temp dir — before the /parse handler runs, so the handler's upload
gate and the parse queue's byte cap only ever saw an upload that had already
landed. With the gate full, two waiting requests had spooled all of their
bytes: what a burst could put on disk was bounded by how many clients sent at
once, not by anything configured.

The size is now reserved from Content-Length before the body is touched,
against the parse queue's byte budget, and a request that does not fit gets 429
without a byte of it being read.
"""

import asyncio
import contextlib

import httpx
import pytest


async def _spin_until(predicate, timeout=5.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition never became true")
        await asyncio.sleep(0.01)


@pytest.fixture()
def spooled(monkeypatch):
    """How many upload bytes Starlette has written into multipart spools."""
    import starlette.datastructures as sd

    counter = {"bytes": 0, "closed": 0}
    real_write, real_close = sd.UploadFile.write, sd.UploadFile.close

    async def write(self, data):
        counter["bytes"] += len(data)
        return await real_write(self, data)

    async def close(self):
        counter["closed"] += 1
        return await real_close(self)

    monkeypatch.setattr(sd.UploadFile, "write", write)
    monkeypatch.setattr(sd.UploadFile, "close", close)
    return counter


def _surface(name):
    """Both ways in: the unified server, and the MCP front-end's in-process hop
    straight to doc_app, which never passes the unified server's middleware."""
    import mantisfetch_docreader as dr

    import mantisfetch_server as srv

    return {"unified": (srv.app, "/doc/parse"), "in-process": (dr.app, "/parse")}[name]


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


@pytest.mark.parametrize("surface", ["unified", "in-process"])
async def test_an_upload_that_does_not_fit_is_refused_unread(surface, monkeypatch, spooled):
    import mantisfetch_docreader as dr

    app, path = _surface(surface)
    monkeypatch.setenv("MANTISFETCH_PARSE_QUEUE_MAX_BYTES", "6000")
    monkeypatch.setattr(dr, "_upload_sem", asyncio.Semaphore(0))  # the copy stage is full

    async with _client(app) as c:
        first = asyncio.create_task(c.post(path, files={"file": ("a.txt", b"x" * 4096)}))
        await _spin_until(lambda: spooled["bytes"] == 4096)
        # The precondition: the first upload is received and still holds its
        # reservation while it waits for the gate.
        assert dr._receiving_bytes_held > 4096

        second = await c.post(path, files={"file": ("b.txt", b"y" * 4096)})
        assert second.status_code == 429, second.text
        assert second.headers["retry-after"] == "30"
        assert "being received" in second.json()["detail"]
        assert spooled["bytes"] == 4096, "the refused upload was read anyway"

        first.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await first
    # Cancelled before its copy: the reservation still comes back.
    await _spin_until(lambda: dr._receiving_bytes_held == 0)


async def test_the_reservation_comes_back_once_the_handler_has_its_copy(monkeypatch, spooled):
    """Not at the end of the request — for a parse that is minutes later, and
    the spool it was reserved for is already gone by then."""
    import mantisfetch_docreader as dr

    app, path = _surface("unified")
    parse_gate = asyncio.Semaphore(0)
    monkeypatch.setattr(dr, "_parse_sem", parse_gate)

    async with _client(app) as c:
        request = asyncio.create_task(
            c.post(
                path,
                files={"file": ("a.txt", b"a short text document worth parsing")},
                data={"generate_summary": "false", "summary_mode": "off"},
            )
        )
        # Parked at the parse slot: past the copy, long before the response.
        await _spin_until(lambda: dr._scratch_bytes_held > 0)
        assert dr._receiving_bytes_held == 0
        assert spooled["closed"] >= 1, "the multipart spool was left open"

        parse_gate.release()
        resp = await request
    assert resp.status_code == 200, resp.text
    assert dr._receiving_bytes_held == 0


async def test_a_full_parse_queue_refuses_before_the_body_is_read(monkeypatch, spooled):
    """What is queued counts too. The handler would refuse this upload once it
    arrived, so admission refuses it before it does."""
    import mantisfetch_docreader as dr

    app, path = _surface("unified")
    monkeypatch.setenv("MANTISFETCH_PARSE_QUEUE_MAX_BYTES", "100000")
    monkeypatch.setattr(dr, "_scratch_bytes_held", 100000 - 100)
    async with _client(app) as c:
        resp = await c.post(path, files={"file": ("a.txt", b"x" * 4096)})
    assert resp.status_code == 429, resp.text
    assert "queued for parse" in resp.json()["detail"]
    assert spooled["bytes"] == 0


async def test_a_negative_content_length_does_not_give_bytes_back():
    """Taken at face value it would be a negative reservation, and admitting it
    would lower the count every other request is measured against."""
    import mantisfetch_docreader as dr

    held_during: list[int] = []
    sent: list[dict] = []

    async def app(scope, receive, send):
        held_during.append(dr._receiving_bytes_held)

    async def send(message):
        sent.append(message)

    gate = dr._UploadAdmission(app)
    before = dr._receiving_bytes_held
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/parse",
        "root_path": "",
        "headers": [(b"content-length", b"-500000000")],
    }
    await gate(scope, None, send)

    # Reserved as the largest request instead, while it is in flight — which is
    # when a negative reservation would have let others in — and given back
    # once it ends.
    assert held_during == [before + dr._max_request_bytes()], held_during
    assert dr._receiving_bytes_held == before


async def test_a_request_the_handler_refuses_gives_its_reservation_back():
    import mantisfetch_docreader as dr

    app, path = _surface("unified")
    async with _client(app) as c:
        resp = await c.post(path, files={"file": ("a.unknown", b"whatever")})
    assert resp.status_code == 422
    assert dr._receiving_bytes_held == 0


async def test_an_upload_that_exactly_fits_is_admitted(monkeypatch):
    app, path = _surface("unified")
    async with _client(app) as c:
        request = c.build_request(
            "POST",
            path,
            files={"file": ("a.txt", b"exactly the budget, not a byte under")},
            data={"generate_summary": "false", "summary_mode": "off"},
        )
        monkeypatch.setenv("MANTISFETCH_PARSE_QUEUE_MAX_BYTES", request.headers["content-length"])
        resp = await c.send(request)
    assert resp.status_code == 200, resp.text


async def test_a_body_with_no_declared_length_reserves_the_most_a_request_may_be(
    monkeypatch, spooled
):
    """Chunked: nothing says how big it will be, so it is admitted as the
    largest request the service accepts."""
    app, path = _surface("unified")
    monkeypatch.setenv("MANTISFETCH_PARSE_QUEUE_MAX_BYTES", "1000000")
    boundary = "b0undary"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="a.txt"\r\n'
        "Content-Type: text/plain\r\n\r\n"
        "small, but it does not say so\r\n"
        f"--{boundary}--\r\n"
    ).encode()

    async def chunks():
        yield body

    async with _client(app) as c:
        resp = await c.post(
            path,
            content=chunks(),
            headers={"content-type": f"multipart/form-data; boundary={boundary}"},
        )
    assert resp.status_code == 429, resp.text
    assert spooled["bytes"] == 0
