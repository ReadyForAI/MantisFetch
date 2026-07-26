"""Tests for local-OCR engine construction (oneDNN / PIR compatibility).

paddlepaddle 3.x raises NotImplementedError on every predict call when oneDNN is
enabled under the PIR executor, which silently turned every scanned page into an
empty OCR result. The worker must therefore build the v3 engine with oneDNN off
unless an operator explicitly opts back in.
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


@pytest.fixture
def fake_paddleocr(monkeypatch: pytest.MonkeyPatch) -> type[_FakePaddleOCR]:
    module = types.ModuleType("paddleocr")
    module.PaddleOCR = _FakePaddleOCR
    monkeypatch.setitem(sys.modules, "paddleocr", module)
    monkeypatch.setattr(paddle_ocr_worker.importlib.metadata, "version", lambda _name: "3.7.0")
    _FakePaddleOCR.last_kwargs = {}
    return _FakePaddleOCR


def test_mkldnn_disabled_by_default(fake_paddleocr, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("MANTISFETCH_LOCAL_OCR_ENABLE_MKLDNN", raising=False)

    _engine, api_version = paddle_ocr_worker._build_engine()

    assert api_version == "v3"
    assert fake_paddleocr.last_kwargs["enable_mkldnn"] is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_mkldnn_opt_in(fake_paddleocr, monkeypatch: pytest.MonkeyPatch, value: str):
    monkeypatch.setenv("MANTISFETCH_LOCAL_OCR_ENABLE_MKLDNN", value)

    paddle_ocr_worker._build_engine()

    assert fake_paddleocr.last_kwargs["enable_mkldnn"] is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off"])
def test_mkldnn_stays_off_for_non_truthy_values(
    fake_paddleocr, monkeypatch: pytest.MonkeyPatch, value: str
):
    monkeypatch.setenv("MANTISFETCH_LOCAL_OCR_ENABLE_MKLDNN", value)

    paddle_ocr_worker._build_engine()

    assert fake_paddleocr.last_kwargs["enable_mkldnn"] is False
