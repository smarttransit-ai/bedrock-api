"""Tests for the shared litellm → catalog transform (pricing_catalog)."""

import pytest
from pricing_catalog import (
    _build_entries,
    build_catalog,
    filter_bedrock,
    normalize_service_tier,
)


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
    converse = cat["openai.gpt-oss-safeguard-20b"]["standard"]
    mantle = cat["mantle/openai.gpt-oss-safeguard-20b"]["standard"]
    assert converse["input_usd_micros_per_1k"] == 70
    assert converse["output_usd_micros_per_1k"] == 200
    assert mantle["input_usd_micros_per_1k"] == 75
    assert mantle["output_usd_micros_per_1k"] == 300


def test_mantle_rates_and_no_region_derivation():
    cat = build_catalog(_raw())
    luna = cat["mantle/openai.gpt-5.6-luna"]["standard"]
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
    assert set(cat["anthropic.claude-sonnet-4-6"]) == {"standard"}  # no tiers, no batch
    sonnet = cat["anthropic.claude-sonnet-4-6"]["standard"]
    # µUSD/1k conversion: 3.0e-06/token → 3000; 1.5e-05 → 15000
    assert sonnet["input_usd_micros_per_1k"] == 3000
    assert sonnet["output_usd_micros_per_1k"] == 15000
    # Pass 2a: base anthropic → us./global. derived (same rates)
    assert cat["us.anthropic.claude-sonnet-4-6"]["standard"] == sonnet
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
    assert entries["anthropic.claude-x"]["standard"]["input_usd_micros_per_1k"] == 2000
    assert "us.anthropic.claude-x" in entries  # derived
    assert "us.anthropic.claude-x" in derived


# ---------------------------------------------------------------------------
# Service tiers (*_flex / *_priority cost fields)
# ---------------------------------------------------------------------------


def _tiered(**extra) -> dict:
    """A vendored-shape entry ($1/$2 per 1M) plus the given tier/cache cost fields."""
    return {
        "litellm_provider": "bedrock_converse",
        "input_cost_per_token": 1.0e-06,
        "output_cost_per_token": 2.0e-06,
        **extra,
    }


def _tiers(entry: dict) -> dict:
    return _build_entries({"openai.m": entry})[0]["openai.m"]


def test_tiers_built_from_suffixed_keys():
    tiers = _tiers(
        _tiered(
            input_cost_per_token_flex=5.0e-07,
            output_cost_per_token_flex=1.0e-06,
            input_cost_per_token_priority=1.75e-06,
            output_cost_per_token_priority=3.5e-06,
        )
    )
    assert set(tiers) == {"standard", "flex", "priority"}
    assert tiers["flex"]["input_usd_micros_per_1k"] == 500
    assert tiers["flex"]["output_usd_micros_per_1k"] == 1000
    assert tiers["priority"]["input_usd_micros_per_1k"] == 1750
    assert tiers["priority"]["output_usd_micros_per_1k"] == 3500


@pytest.mark.parametrize(
    "extra",
    [
        {"input_cost_per_token_flex": 5.0e-07},  # output missing
        {"output_cost_per_token_flex": 1.0e-06},  # input missing
    ],
)
def test_tier_dropped_when_input_or_output_missing(extra):
    assert set(_tiers(_tiered(**extra))) == {"standard"}


def test_embedding_keeps_zero_output_tier():
    entry = _tiered(
        output_cost_per_token=0, input_cost_per_token_flex=5.0e-07, output_cost_per_token_flex=0
    )
    flex = _tiers(entry)["flex"]
    assert flex["input_usd_micros_per_1k"] == 500
    assert flex["output_usd_micros_per_1k"] == 0


@pytest.mark.parametrize("tier_cache", [0, 3.0e-08, 9e-04])
def test_tier_cache_ignored_when_standard_cache_absent(tier_cache):
    """With no Standard cache cost to bound it against, a tier cache cost is not trusted:
    it is scaled from Standard's filled (output) cache rate, as Standard itself is."""
    entry = _tiered(
        input_cost_per_token_flex=5.0e-07,
        output_cost_per_token_flex=1.0e-06,
        cache_read_input_token_cost_flex=tier_cache,
    )
    assert _tiers(entry)["flex"]["cache_read_input_usd_micros_per_1k"] == 1000  # 2000 x 0.5


def test_tier_cache_fallback_scales_with_input_ratio():
    """nova-2-pro shape: no Priority cache cost, and an output ratio far from the input one.

    Falling back to the tier OUTPUT rate (the Standard rule) would over-bill ~32x.
    """
    entry = _tiered(
        input_cost_per_token=1.25e-06,
        output_cost_per_token=1.0e-05,
        cache_read_input_token_cost=3.12e-07,
        input_cost_per_token_priority=2.1875e-06,
        output_cost_per_token_priority=1.75e-05,
    )
    priority = _tiers(entry)["priority"]
    assert priority["cache_read_input_usd_micros_per_1k"] == 546  # round(312 x 1.75)
    assert priority["cache_write_input_usd_micros_per_1k"] == 17500  # 10000 (filled) x 1.75


def test_standard_cache_fallback_is_output_rate():
    std = _tiers(_tiered())["standard"]
    assert std["cache_read_input_usd_micros_per_1k"] == 2000
    assert std["cache_write_input_usd_micros_per_1k"] == 2000


def test_derived_ids_carry_tiers():
    vendor = {
        "anthropic.claude-x": _tiered(
            input_cost_per_token_flex=5.0e-07, output_cost_per_token_flex=1.0e-06
        )
    }
    entries, _ = _build_entries(vendor)
    assert entries["us.anthropic.claude-x"]["flex"] == entries["anthropic.claude-x"]["flex"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, "standard"),
        ("", "standard"),
        ("default", "standard"),
        ("auto", "standard"),
        ("STANDARD", "standard"),
        ({"type": "flex"}, "flex"),
        ({"type": None}, "standard"),
        ("priority", "priority"),
        ("reserved", "reserved"),
        (42, "standard"),
    ],
)
def test_normalize_service_tier(raw, expected):
    assert normalize_service_tier(raw) == expected


_FIELDS = [
    "input_cost_per_token",
    "cache_read_input_token_cost",
    "input_cost_per_token_flex",
    "cache_creation_input_token_cost_priority",
]


@pytest.mark.parametrize("field", _FIELDS)
@pytest.mark.parametrize(
    "bad", [-1e-07, "5e-7", True, float("inf"), float("nan"), 10**400, 3.0, 3e-13]
)
def test_corrupt_upstream_cost_fails_the_build(field, bad):
    """Any corrupt price field rejects the whole build: refresh fails closed, no guessing."""
    with pytest.raises(ValueError, match=f"openai.m: {field}="):
        _tiers(_tiered(**{field: bad}))


@pytest.mark.parametrize(
    ("extra", "field"),
    [
        ({"input_cost_per_token_flex": 1.5e-12}, "input_cost_per_token_flex"),  # below bound
        ({"input_cost_per_token_priority": 1.0e-03}, "input_cost_per_token_priority"),  # 1000x
        ({"input_cost_per_token_flex": 1.0e-07}, "input_cost_per_token_flex"),  # 0.1x
        ({"input_cost_per_token": 0}, "input_cost_per_token"),  # free input, paid output
        # Tier zero-ness disagrees with Standard (either way round).
        ({"input_cost_per_token_flex": 0}, "input_cost_per_token_flex"),
        ({"output_cost_per_token_flex": 0}, "output_cost_per_token_flex"),
        (
            {"output_cost_per_token": 0, "output_cost_per_token_flex": 1.0e-07},
            "output_cost_per_token_flex",
        ),
        (
            {"cache_read_input_token_cost": 1.0e-07, "cache_read_input_token_cost_flex": 0},
            "cache_read_input_token_cost_flex",
        ),
    ],
)
def test_implausible_upstream_cost_fails_the_build(extra, field):
    entry = _tiered(input_cost_per_token_flex=5.0e-07, output_cost_per_token_flex=1.0e-06)
    entry.update(input_cost_per_token_priority=2.0e-06, output_cost_per_token_priority=4.0e-06)
    with pytest.raises(ValueError, match=f"openai.m: {field}="):
        _tiers(entry | extra)


@pytest.mark.parametrize("cost", [3e-10, 5e-10])  # 5e-10 → 0.5 µUSD/1k rounds to 0
def test_cost_that_would_round_to_zero_is_rejected(cost):
    with pytest.raises(ValueError, match="input_cost_per_token="):
        _build_entries({"openai.m": _tiered(input_cost_per_token=cost)})


def test_cost_just_above_the_floor_is_kept():
    entry = _build_entries({"openai.m": _tiered(input_cost_per_token=1.5e-9)})[0]["openai.m"]
    assert entry["standard"]["input_usd_micros_per_1k"] == 2


def test_scaled_tier_cache_rate_never_rounds_a_paid_rate_to_free():
    tiers = _build_entries(
        {
            "openai.m": _tiered(
                cache_read_input_token_cost=1e-09,
                input_cost_per_token_flex=4.3e-07,
                output_cost_per_token_flex=8.6e-07,
            )
        }
    )[0]["openai.m"]
    assert tiers["standard"]["cache_read_input_usd_micros_per_1k"] == 1
    assert tiers["flex"]["cache_read_input_usd_micros_per_1k"] == 1
