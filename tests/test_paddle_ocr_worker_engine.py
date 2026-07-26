"""Tests for local-OCR engine construction (oneDNN / PIR compatibility).

paddlepaddle 3.3.0+ raises NotImplementedError on every predict call when oneDNN
is enabled under the PIR executor (Paddle#77340), which silently turned every
scanned page into an empty OCR result. oneDNN is worth ~6x on CPU, so the worker
picks the default from the installed paddlepaddle version rather than giving it up
everywhere — on where it works, off where it crashes, with an env override.
"""

import sys
import types

import paddle_ocr_worker
import pytest


class _FakePaddleOCR:
    """Records the kwargs it was constructed with; satisfies the v3 check."""

    last_kwargs: dict = {}

    def __init__(self, **kwargs):
        type(self).last_kwargs = kwargs

    def predict(self, *args, **kwargs):  # marks the instance as the v3 API
        return []


def _install(monkeypatch: pytest.MonkeyPatch, paddle_version: str) -> type[_FakePaddleOCR]:
    module = types.ModuleType("paddleocr")
    module.PaddleOCR = _FakePaddleOCR
    monkeypatch.setitem(sys.modules, "paddleocr", module)
    versions = {"paddleocr": "3.7.0", "paddlepaddle": paddle_version}

    def _version(name: str) -> str:
        return versions[name]

    monkeypatch.setattr(paddle_ocr_worker.importlib.metadata, "version", _version)
    monkeypatch.delenv("MANTISFETCH_LOCAL_OCR_ENABLE_MKLDNN", raising=False)
    _FakePaddleOCR.last_kwargs = {}
    return _FakePaddleOCR


@pytest.mark.parametrize("paddle_version", ["3.3.0", "3.3.1", "3.4.0", "4.0.0"])
def test_mkldnn_off_by_default_on_broken_paddle(
    monkeypatch: pytest.MonkeyPatch, paddle_version: str
):
    fake = _install(monkeypatch, paddle_version)

    _engine, api_version = paddle_ocr_worker._build_engine()

    assert api_version == "v3"
    assert fake.last_kwargs["enable_mkldnn"] is False


@pytest.mark.parametrize("paddle_version", ["3.2.2", "3.2.0", "3.1.1", "3.0.0"])
def test_mkldnn_on_by_default_on_working_paddle(
    monkeypatch: pytest.MonkeyPatch, paddle_version: str
):
    fake = _install(monkeypatch, paddle_version)

    paddle_ocr_worker._build_engine()

    assert fake.last_kwargs["enable_mkldnn"] is True


def test_mkldnn_off_when_paddle_version_unreadable(monkeypatch: pytest.MonkeyPatch):
    """Unknown version must degrade to slow-but-working, never to crashing."""
    fake = _install(monkeypatch, "3.2.2")

    def _boom(name: str) -> str:
        if name == "paddlepaddle":
            raise RuntimeError("no metadata")
        return "3.7.0"

    monkeypatch.setattr(paddle_ocr_worker.importlib.metadata, "version", _boom)

    paddle_ocr_worker._build_engine()

    assert fake.last_kwargs["enable_mkldnn"] is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_env_forces_mkldnn_on_even_on_broken_paddle(monkeypatch: pytest.MonkeyPatch, value: str):
    fake = _install(monkeypatch, "3.3.1")
    monkeypatch.setenv("MANTISFETCH_LOCAL_OCR_ENABLE_MKLDNN", value)

    paddle_ocr_worker._build_engine()

    assert fake.last_kwargs["enable_mkldnn"] is True


@pytest.mark.parametrize("value", ["0", "false", "FALSE", "no", "off"])
def test_env_forces_mkldnn_off_even_on_working_paddle(monkeypatch: pytest.MonkeyPatch, value: str):
    fake = _install(monkeypatch, "3.2.2")
    monkeypatch.setenv("MANTISFETCH_LOCAL_OCR_ENABLE_MKLDNN", value)

    paddle_ocr_worker._build_engine()

    assert fake.last_kwargs["enable_mkldnn"] is False


def test_unrecognised_env_value_falls_back_to_version_detection(
    monkeypatch: pytest.MonkeyPatch,
):
    fake = _install(monkeypatch, "3.2.2")
    monkeypatch.setenv("MANTISFETCH_LOCAL_OCR_ENABLE_MKLDNN", "maybe")

    paddle_ocr_worker._build_engine()

    assert fake.last_kwargs["enable_mkldnn"] is True
