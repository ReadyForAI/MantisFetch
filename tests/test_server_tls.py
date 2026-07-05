"""Tests for the unified server's optional TLS env wiring (_ssl_kwargs)."""

import logging
import os

import mantisfetch_server as srv


def test_warn_legacy_env_logs_when_larkscout_set(monkeypatch, caplog) -> None:
    monkeypatch.setenv("LARKSCOUT_LLM_API_KEY", "leftover")
    with caplog.at_level(logging.WARNING, logger="mantisfetch"):
        srv._warn_legacy_env()
    assert any("LARKSCOUT_LLM_API_KEY" in r.message for r in caplog.records)


def test_warn_legacy_env_silent_without_legacy(monkeypatch, caplog) -> None:
    for key in list(os.environ):
        if key.startswith("LARKSCOUT_"):
            monkeypatch.delenv(key, raising=False)
    with caplog.at_level(logging.WARNING, logger="mantisfetch"):
        srv._warn_legacy_env()
    assert not caplog.records


def test_ssl_kwargs_empty_without_env(monkeypatch) -> None:
    monkeypatch.delenv("MANTISFETCH_TLS_CERTFILE", raising=False)
    monkeypatch.delenv("MANTISFETCH_TLS_KEYFILE", raising=False)
    assert srv._ssl_kwargs() == {}


def test_ssl_kwargs_set_when_both_present(monkeypatch) -> None:
    monkeypatch.setenv("MANTISFETCH_TLS_CERTFILE", "/certs/cert.pem")
    monkeypatch.setenv("MANTISFETCH_TLS_KEYFILE", "/certs/key.pem")
    assert srv._ssl_kwargs() == {
        "ssl_certfile": "/certs/cert.pem",
        "ssl_keyfile": "/certs/key.pem",
    }


def test_ssl_kwargs_ignores_half_config(monkeypatch) -> None:
    # only the cert (no key) → treated as unset (plain http), not a boot failure
    monkeypatch.setenv("MANTISFETCH_TLS_CERTFILE", "/certs/cert.pem")
    monkeypatch.delenv("MANTISFETCH_TLS_KEYFILE", raising=False)
    assert srv._ssl_kwargs() == {}
