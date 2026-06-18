"""Tests for the unified server's optional TLS env wiring (_ssl_kwargs)."""

import mantisfetch_server as srv


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
