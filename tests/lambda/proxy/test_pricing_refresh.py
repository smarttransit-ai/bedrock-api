"""Tests for the pricing refresh orchestrator (pricing_refresh)."""

import boto3
import pricing_refresh
import pytest
from conftest import litellm_raw_from_catalog
from pricing import DEFAULT_PRICING
from pricing_catalog import build_catalog
from pricing_refresh import refresh_pricing, validate_catalog
from pricing_store import load_live_catalog


def _s3():
    return boto3.client("s3", region_name="us-east-1")


def _fixture_raw() -> dict:
    """Raw litellm-shaped map derived from DEFAULT_PRICING (passes size gate + anchors)."""
    return litellm_raw_from_catalog(DEFAULT_PRICING)


def _valid_catalog() -> dict:
    return build_catalog(_fixture_raw())


def test_refresh_writes_s3_and_returns_summary(pricing_bucket, monkeypatch):
    monkeypatch.setattr(pricing_refresh, "fetch_litellm", _fixture_raw)
    summary = refresh_pricing(_s3())
    assert summary["entry_count"] > 0
    assert "us.anthropic.claude-sonnet-4-6" in summary["anchors"]
    assert load_live_catalog(_s3()) is not None  # written


def test_refresh_failure_leaves_s3_untouched(pricing_bucket, monkeypatch):
    def boom():
        raise RuntimeError("network down")

    monkeypatch.setattr(pricing_refresh, "fetch_litellm", boom)
    with pytest.raises(RuntimeError):
        refresh_pricing(_s3())
    assert load_live_catalog(_s3()) is None  # nothing written


def test_validate_rejects_empty():
    with pytest.raises(ValueError):
        validate_catalog({})


def test_validate_rejects_shrunk():
    anchor = "us.anthropic.claude-sonnet-4-6"
    with pytest.raises(ValueError):
        validate_catalog({anchor: DEFAULT_PRICING[anchor]})


def test_validate_rejects_missing_anchor():
    cat = {
        f"m{i}": {"on_demand": {"input_usd_micros_per_1k": 1, "output_usd_micros_per_1k": 1}}
        for i in range(len(DEFAULT_PRICING))
    }
    with pytest.raises(ValueError):
        validate_catalog(cat)


def test_validate_rejects_zeroed_anchor():
    cat = _valid_catalog()
    cat["us.anthropic.claude-sonnet-4-6"]["on_demand"]["output_usd_micros_per_1k"] = 0
    with pytest.raises(ValueError):
        validate_catalog(cat)


def test_validate_rejects_out_of_band_anchor():
    cat = _valid_catalog()
    base = DEFAULT_PRICING["us.anthropic.claude-sonnet-4-6"]["on_demand"]["input_usd_micros_per_1k"]
    cat["us.anthropic.claude-sonnet-4-6"]["on_demand"]["input_usd_micros_per_1k"] = base * 100
    with pytest.raises(ValueError):
        validate_catalog(cat)


def test_validate_accepts_good_catalog():
    validate_catalog(_valid_catalog())  # no raise
