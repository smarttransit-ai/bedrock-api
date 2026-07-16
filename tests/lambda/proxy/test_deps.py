"""Tests for dependency providers (deps.py) — issue #8 mantle client lifecycle."""

import deps


def test_get_mantle_returns_cached_singleton(monkeypatch):
    """httpx.Client owns a pool and has no __del__; a per-request client would leak it.

    The provider must hand back the same instance so the connection pool is reused and
    never orphaned. (dependency_overrides replaces the provider outright in tests, so the
    cache cannot leak state between tests.)
    """
    monkeypatch.setattr(deps, "_MANTLE_CLIENT", None)
    first = deps.get_mantle()
    second = deps.get_mantle()
    assert first is second


def test_get_mantle_base_url_from_bedrock_region(monkeypatch):
    monkeypatch.setattr(deps, "_MANTLE_CLIENT", None)
    monkeypatch.setenv("BEDROCK_REGION", "eu-west-1")
    monkeypatch.delenv("MANTLE_ENDPOINT_URL", raising=False)
    assert str(deps.get_mantle().base_url) == "https://bedrock-mantle.eu-west-1.api.aws"


def test_get_mantle_endpoint_url_override(monkeypatch):
    monkeypatch.setattr(deps, "_MANTLE_CLIENT", None)
    monkeypatch.setenv("MANTLE_ENDPOINT_URL", "https://mantle.local.test")
    assert str(deps.get_mantle().base_url) == "https://mantle.local.test"
