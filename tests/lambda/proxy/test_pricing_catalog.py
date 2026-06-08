"""Tests for the shared litellm → catalog transform (pricing_catalog)."""

from pricing_catalog import _build_entries, build_catalog, filter_bedrock


def _raw() -> dict:
    """A raw litellm-shaped map: ``bedrock/``-prefixed keys, ``litellm_provider`` present."""
    return {
        "_meta": {"source": "x"},
        "sample_spec": {
            "litellm_provider": "bedrock",
            "input_cost_per_token": 1e-06,
            "output_cost_per_token": 1e-06,
        },
        # base anthropic model (no region prefix) → Pass 2a derives us./global.
        "bedrock/anthropic.claude-sonnet-4-6": {
            "litellm_provider": "bedrock_converse",
            "input_cost_per_token": 3.0e-06,
            "output_cost_per_token": 1.5e-05,
        },
        # us.-only model → Pass 2b derives the base
        "bedrock/us.deepseek.r1-v1:0": {
            "litellm_provider": "bedrock",
            "input_cost_per_token": 1.0e-06,
            "output_cost_per_token": 2.0e-06,
        },
        # non-bedrock provider → filtered out
        "openai/gpt-4o": {
            "litellm_provider": "openai",
            "input_cost_per_token": 5.0e-06,
            "output_cost_per_token": 1.0e-05,
        },
        # other bedrock_* provider → filtered out (do not broaden the set)
        "bedrock/mantle-thing": {
            "litellm_provider": "bedrock_mantle",
            "input_cost_per_token": 1.0e-06,
            "output_cost_per_token": 1.0e-06,
        },
        # key still containing '/' after bedrock/ strip → dropped (image/region prefix)
        "bedrock/1024-x-1024/stability.foo": {
            "litellm_provider": "bedrock",
            "input_cost_per_token": 1.0e-06,
            "output_cost_per_token": 1.0e-06,
        },
        # missing output cost → dropped
        "bedrock/amazon.incomplete": {
            "litellm_provider": "bedrock",
            "input_cost_per_token": 1.0e-06,
            "output_cost_per_token": None,
        },
        # both costs zero → dropped by _build_entries Pass 1
        "bedrock/cohere.rerank": {
            "litellm_provider": "bedrock",
            "input_cost_per_token": 0,
            "output_cost_per_token": 0,
        },
    }


def test_filter_bedrock_keeps_only_routeable_bedrock():
    f = filter_bedrock(_raw())
    assert "anthropic.claude-sonnet-4-6" in f  # bedrock/ stripped
    assert "us.deepseek.r1-v1:0" in f
    assert "gpt-4o" not in f  # non-bedrock provider
    assert "mantle-thing" not in f  # bedrock_mantle intentionally excluded
    assert all("/" not in k for k in f)  # no leftover-slash keys
    assert "_meta" not in f and "sample_spec" not in f
    assert "amazon.incomplete" not in f  # null output cost


def test_build_catalog_rates_and_derivation():
    cat = build_catalog(_raw())
    sonnet = cat["anthropic.claude-sonnet-4-6"]["on_demand"]
    # µUSD/1k conversion: 3.0e-06/token → 3000; 1.5e-05 → 15000
    assert sonnet["input_usd_micros_per_1k"] == 3000
    assert sonnet["output_usd_micros_per_1k"] == 15000
    # batch mirrors on_demand
    assert cat["anthropic.claude-sonnet-4-6"]["batch"] == sonnet
    # Pass 2a: base anthropic → us./global. derived (same rates)
    assert cat["us.anthropic.claude-sonnet-4-6"]["on_demand"] == sonnet
    assert "global.anthropic.claude-sonnet-4-6" in cat
    # Pass 2b: us.deepseek.r1 → base deepseek.r1 derived
    assert "deepseek.r1-v1:0" in cat
    # rerank (0/0) dropped
    assert "cohere.rerank" not in cat


def test_build_catalog_anchor_present_post_derivation():
    # The deployment anchor is a DERIVED id; assert it survives build_catalog from raw.
    assert "us.anthropic.claude-sonnet-4-6" in build_catalog(_raw())


def test_build_entries_pins_existing_behavior():
    # Vendored-shape input (already bedrock/-stripped) flows through _build_entries.
    vendor = {
        "anthropic.claude-x": {
            "litellm_provider": "bedrock_converse",
            "input_cost_per_token": 2.0e-06,
            "output_cost_per_token": 4.0e-06,
        }
    }
    entries, derived = _build_entries(vendor)
    assert entries["anthropic.claude-x"]["on_demand"]["input_usd_micros_per_1k"] == 2000
    assert "us.anthropic.claude-x" in entries  # derived
    assert "us.anthropic.claude-x" in derived
