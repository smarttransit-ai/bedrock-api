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
        # bedrock_mantle → kept, but namespaced under mantle/ (never merged flat)
        "bedrock_mantle/openai.gpt-5.6-luna": {
            "litellm_provider": "bedrock_mantle",
            "input_cost_per_token": 1.1e-06,
            "output_cost_per_token": 6.6e-06,
            "cache_read_input_token_cost": 1.1e-07,
            "cache_creation_input_token_cost": 1.375e-06,
        },
        # Same ID under BOTH providers at DIFFERENT rates — the collision that forces
        # the mantle/ namespace. A flat merge would let dict order pick the winner.
        "openai.gpt-oss-safeguard-20b": {
            "litellm_provider": "bedrock_converse",
            "input_cost_per_token": 7.0e-08,
            "output_cost_per_token": 2.0e-07,
        },
        "bedrock_mantle/openai.gpt-oss-safeguard-20b": {
            "litellm_provider": "bedrock_mantle",
            "input_cost_per_token": 7.5e-08,
            "output_cost_per_token": 3.0e-07,
        },
        # provider says mantle but the key carries no bedrock_mantle/ prefix →
        # a leftover '/' remains after the strip, so it is dropped as non-routeable.
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
    assert "mantle-thing" not in f  # mantle provider, but key kept a leftover '/'
    assert "_meta" not in f and "sample_spec" not in f
    assert "amazon.incomplete" not in f  # null output cost
    # The only '/' permitted in a key is the mantle namespace itself.
    assert all("/" not in k.removeprefix("mantle/") for k in f)


def test_filter_bedrock_namespaces_mantle():
    f = filter_bedrock(_raw())
    assert "mantle/openai.gpt-5.6-luna" in f
    assert "openai.gpt-5.6-luna" not in f  # never leaks into the flat namespace


def test_mantle_collision_leaves_converse_rates_untouched():
    """The dual-provider ID must yield two distinct entries, not one racy winner."""
    cat = build_catalog(_raw())
    converse = cat["openai.gpt-oss-safeguard-20b"]["on_demand"]
    mantle = cat["mantle/openai.gpt-oss-safeguard-20b"]["on_demand"]
    assert converse["input_usd_micros_per_1k"] == 70
    assert converse["output_usd_micros_per_1k"] == 200
    assert mantle["input_usd_micros_per_1k"] == 75
    assert mantle["output_usd_micros_per_1k"] == 300


def test_mantle_rates_and_no_region_derivation():
    cat = build_catalog(_raw())
    luna = cat["mantle/openai.gpt-5.6-luna"]["on_demand"]
    assert luna["input_usd_micros_per_1k"] == 1100
    assert luna["output_usd_micros_per_1k"] == 6600
    assert luna["cache_read_input_usd_micros_per_1k"] == 110
    assert luna["cache_write_input_usd_micros_per_1k"] == 1375
    # mantle does not support geo/global inference IDs — no phantom regional variants.
    assert not [k for k in cat if k.startswith(("us.mantle/", "global.mantle/"))]
    assert not [k for k in cat if k.startswith("mantle/") and ".us." in k]


def test_reasoning_cost_field_warns_rather_than_dropping(caplog):
    """Upstream pricing reasoning separately must be loud, not silent (never dropped)."""
    raw = {
        "bedrock_mantle/openai.gpt-future": {
            "litellm_provider": "bedrock_mantle",
            "input_cost_per_token": 1.0e-06,
            "output_cost_per_token": 2.0e-06,
            "output_cost_per_reasoning_token": 8.0e-06,
        }
    }
    with caplog.at_level("WARNING"):
        cat = build_catalog(raw)
    assert "mantle/openai.gpt-future" in cat  # kept: fallback would be far worse
    assert "pricing_reasoning_cost_ignored" in caplog.text


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
