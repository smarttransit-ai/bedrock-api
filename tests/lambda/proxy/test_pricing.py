import os

import boto3
import pricing
from pricing import compute_cost
from pricing_store import PRICING_OBJECT_KEY, save_live_catalog


def _set_live(catalog: dict) -> None:
    """Write a live catalog to S3 and force the pricing cache to re-read it."""
    save_live_catalog(
        boto3.client("s3", region_name="us-east-1"), catalog, {"entry_count": len(catalog)}
    )
    pricing.invalidate_cache()


# ---------------------------------------------------------------------------
# compute_cost math — driven through the live S3 catalog (ported from the old
# PRICING_JSON env-override tests).
# ---------------------------------------------------------------------------


def test_compute_cost_on_demand_rounding_and_components(pricing_bucket):
    _set_live(
        {
            "m": {
                "on_demand": {
                    "input_usd_micros_per_1k": 3000,
                    "output_usd_micros_per_1k": 15000,
                    "cache_read_input_usd_micros_per_1k": 1000,
                    "cache_write_input_usd_micros_per_1k": 2000,
                },
                "batch": {
                    "input_usd_micros_per_1k": 2000,
                    "output_usd_micros_per_1k": 10000,
                    "cache_read_input_usd_micros_per_1k": 500,
                    "cache_write_input_usd_micros_per_1k": 1000,
                },
            }
        }
    )
    total, components, fallback_applied, fallback_dimensions, _ = compute_cost(
        model_id="m",
        pricing_mode="on_demand",
        input_tokens=1001,
        output_tokens=500,
        cache_read_input_tokens=1000,
        cache_write_input_tokens=250,
    )
    assert components["input_usd_micros"] == 3003
    assert components["output_usd_micros"] == 7500
    assert components["cache_read_input_usd_micros"] == 1000
    assert components["cache_write_input_usd_micros"] == 500
    assert total == 12003
    assert fallback_applied is False
    assert fallback_dimensions == []


def test_compute_cost_batch_mode(pricing_bucket):
    _set_live(
        {
            "m2": {
                "on_demand": {
                    "input_usd_micros_per_1k": 3000,
                    "output_usd_micros_per_1k": 15000,
                    "cache_read_input_usd_micros_per_1k": 1000,
                    "cache_write_input_usd_micros_per_1k": 2000,
                },
                "batch": {
                    "input_usd_micros_per_1k": 1500,
                    "output_usd_micros_per_1k": 7500,
                    "cache_read_input_usd_micros_per_1k": 500,
                    "cache_write_input_usd_micros_per_1k": 1000,
                },
            }
        }
    )
    on_demand_total, _, _, _, _ = compute_cost(
        model_id="m2",
        pricing_mode="on_demand",
        input_tokens=1000,
        output_tokens=1000,
        cache_read_input_tokens=0,
        cache_write_input_tokens=0,
    )
    batch_total, _, _, _, _ = compute_cost(
        model_id="m2",
        pricing_mode="batch",
        input_tokens=1000,
        output_tokens=1000,
        cache_read_input_tokens=0,
        cache_write_input_tokens=0,
    )
    assert batch_total > 0
    assert batch_total < on_demand_total


def test_compute_cost_fallback_for_missing_mode(pricing_bucket):
    _set_live(
        {
            "m": {
                "on_demand": {
                    "input_usd_micros_per_1k": 1000,
                    "output_usd_micros_per_1k": 2000,
                }
            }
        }
    )
    _, _, fallback_applied, fallback_dimensions, _ = compute_cost(
        model_id="m",
        pricing_mode="batch",
        input_tokens=1,
        output_tokens=1,
        cache_read_input_tokens=1,
        cache_write_input_tokens=1,
    )
    assert fallback_applied is True
    assert "pricing_mode" in fallback_dimensions


# ---------------------------------------------------------------------------
# DEFAULT_PRICING fallback path (no live catalog object present)
# ---------------------------------------------------------------------------


def test_compute_cost_fallback_for_unknown_model():
    total, _, fallback_applied, fallback_dimensions, _ = compute_cost(
        model_id="unknown.model",
        pricing_mode="on_demand",
        input_tokens=1000,
        output_tokens=1000,
        cache_read_input_tokens=1000,
        cache_write_input_tokens=1000,
    )
    assert total == 240000
    assert fallback_applied is True
    assert "model_id" in fallback_dimensions


def test_default_catalog_batch_mode_does_not_fallback_on_mode():
    on_demand_total, _, _, _, _ = compute_cost(
        model_id="us.anthropic.claude-sonnet-4-6",
        pricing_mode="on_demand",
        input_tokens=1000,
        output_tokens=1000,
        cache_read_input_tokens=0,
        cache_write_input_tokens=0,
    )
    batch_total, _, fallback_applied, fallback_dimensions, _ = compute_cost(
        model_id="us.anthropic.claude-sonnet-4-6",
        pricing_mode="batch",
        input_tokens=1000,
        output_tokens=1000,
        cache_read_input_tokens=0,
        cache_write_input_tokens=0,
    )
    # batch mirrors on_demand (D2)
    assert batch_total == on_demand_total
    assert fallback_applied is False
    assert "pricing_mode" not in fallback_dimensions


def test_active_model_ids_in_default_pricing():
    """Active in-use model IDs must be priced (not fallback-dependent)."""
    from pricing import _FALLBACK_BY_MODE, DEFAULT_PRICING

    active_ids = [
        "us.anthropic.claude-sonnet-4-6",
        "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        "global.anthropic.claude-sonnet-4-6",
    ]
    fallback_input = _FALLBACK_BY_MODE["on_demand"]["input_usd_micros_per_1k"]
    for mid in active_ids:
        assert mid in DEFAULT_PRICING, f"{mid!r} missing from DEFAULT_PRICING"
        rate = DEFAULT_PRICING[mid]["on_demand"]["input_usd_micros_per_1k"]
        assert rate < fallback_input, f"{mid!r} rate equals fallback (missing real entry?)"
        assert rate > 0, f"{mid!r} has zero input rate"


def test_compute_cost_litellm_known_model_math():
    """Spot-check conversion: anthropic.claude-3-5-sonnet-20241022-v2:0 at litellm rates.

    litellm: $3/1M input, $15/1M output = 3_000 / 15_000 µUSD/1k.
    1000 input + 1000 output → 3_000 + 15_000 = 18_000 µUSD total.
    Update the expected values if litellm upstream changes the rate.
    """
    total, components, fallback_applied, _, _ = compute_cost(
        model_id="anthropic.claude-3-5-sonnet-20241022-v2:0",
        pricing_mode="on_demand",
        input_tokens=1000,
        output_tokens=1000,
        cache_read_input_tokens=0,
        cache_write_input_tokens=0,
    )
    assert fallback_applied is False
    assert components["input_usd_micros"] == 3_000
    assert components["output_usd_micros"] == 15_000
    assert total == 18_000


# ---------------------------------------------------------------------------
# Live-catalog source resolution + cache behavior
# ---------------------------------------------------------------------------


def test_live_catalog_overrides_default(pricing_bucket):
    _set_live(
        {
            "us.anthropic.claude-sonnet-4-6": {
                "on_demand": {"input_usd_micros_per_1k": 1, "output_usd_micros_per_1k": 1},
                "batch": {"input_usd_micros_per_1k": 1, "output_usd_micros_per_1k": 1},
            }
        }
    )
    total, _, fallback_applied, _, _ = compute_cost(
        model_id="us.anthropic.claude-sonnet-4-6",
        pricing_mode="on_demand",
        input_tokens=1000,
        output_tokens=1000,
        cache_read_input_tokens=0,
        cache_write_input_tokens=0,
    )
    assert fallback_applied is False
    assert total == 2  # 1 + 1 from the live override, not the baked-in rate


def test_absent_live_catalog_uses_default(pricing_bucket):
    # Bucket exists, no object → authoritative-absent → DEFAULT_PRICING.
    _, _, fallback_applied, _, _ = compute_cost(
        model_id="us.anthropic.claude-sonnet-4-6",
        pricing_mode="on_demand",
        input_tokens=1000,
        output_tokens=1000,
        cache_read_input_tokens=0,
        cache_write_input_tokens=0,
    )
    assert fallback_applied is False  # priced from DEFAULT_PRICING


def test_cache_reused_within_ttl_then_reread_after_invalidate(pricing_bucket):
    s3 = boto3.client("s3", region_name="us-east-1")

    def model_rates(rate):
        return {
            "x": {
                "on_demand": {"input_usd_micros_per_1k": rate, "output_usd_micros_per_1k": rate},
                "batch": {"input_usd_micros_per_1k": rate, "output_usd_micros_per_1k": rate},
            }
        }

    _set_live(model_rates(1000))
    first, _, _, _, _ = compute_cost("x", "on_demand", 1000, 1000, 0, 0)
    assert first == 2000
    # Change S3 WITHOUT invalidating → cached value still served.
    save_live_catalog(s3, model_rates(9000), {"entry_count": 1})
    cached, _, _, _, _ = compute_cost("x", "on_demand", 1000, 1000, 0, 0)
    assert cached == first
    # After invalidate, the new rates are read.
    pricing.invalidate_cache()
    fresh, _, _, _, _ = compute_cost("x", "on_demand", 1000, 1000, 0, 0)
    assert fresh == 18000


def test_corrupt_live_catalog_falls_back_to_default(pricing_bucket):
    boto3.client("s3", region_name="us-east-1").put_object(
        Bucket=os.environ["PRICING_BUCKET"], Key=PRICING_OBJECT_KEY, Body=b"not json"
    )
    pricing.invalidate_cache()
    _, _, fallback_applied, _, _ = compute_cost(
        model_id="us.anthropic.claude-sonnet-4-6",
        pricing_mode="on_demand",
        input_tokens=1000,
        output_tokens=1000,
        cache_read_input_tokens=0,
        cache_write_input_tokens=0,
    )
    assert fallback_applied is False  # DEFAULT_PRICING used despite the corrupt object
    # Corrupt/transient → short retry TTL so a later good refresh isn't masked for 60s.
    assert pricing._PRICING_CACHE_TTL == pricing._PRICING_RETRY_TTL


def test_transient_s3_error_falls_back_and_short_retry(monkeypatch):
    from botocore.exceptions import ClientError

    class _FakeS3:
        def get_object(self, **kwargs):
            raise ClientError({"Error": {"Code": "InternalError"}}, "GetObject")

    monkeypatch.setattr(pricing, "get_s3", lambda: _FakeS3())
    pricing.invalidate_cache()
    _, _, fallback_applied, _, _ = compute_cost(
        model_id="us.anthropic.claude-sonnet-4-6",
        pricing_mode="on_demand",
        input_tokens=1000,
        output_tokens=1000,
        cache_read_input_tokens=0,
        cache_write_input_tokens=0,
    )
    assert fallback_applied is False  # DEFAULT used on a transient (non-absent) S3 error
    assert pricing._PRICING_CACHE_TTL == pricing._PRICING_RETRY_TTL


def test_compute_cost_mantle_namespaced_model(pricing_bucket):
    """mantle/openai.gpt-5.6-luna prices from the catalog, not the Opus-tier fallback."""
    total, components, fallback_applied, fallback_dimensions, rates = compute_cost(
        model_id="mantle/openai.gpt-5.6-luna",
        pricing_mode="on_demand",
        input_tokens=1000,
        output_tokens=1000,
        cache_read_input_tokens=0,
        cache_write_input_tokens=0,
    )
    # litellm: $1.10/1M input, $6.60/1M output → 1_100 / 6_600 µUSD per 1k.
    assert rates["input_usd_micros_per_1k"] == 1100
    assert rates["output_usd_micros_per_1k"] == 6600
    assert total == 7700
    assert fallback_applied is False
    assert fallback_dimensions == []
    assert components["output_usd_micros"] == 6600


def test_bare_mantle_model_id_falls_back(pricing_bucket):
    """The wire ID is NOT a catalog key — billing must go through pricing_model_id.

    This pins the failure mode the mantle/ namespace exists to make visible: billing a
    Responses call by its bare wire ID misses the catalog entirely.
    """
    _, _, fallback_applied, fallback_dimensions, _ = compute_cost(
        model_id="openai.gpt-5.6-luna",
        pricing_mode="on_demand",
        input_tokens=1000,
        output_tokens=1000,
        cache_read_input_tokens=0,
        cache_write_input_tokens=0,
    )
    assert fallback_applied is True
    assert "model_id" in fallback_dimensions


def test_dual_provider_model_keeps_distinct_rates():
    """openai.gpt-oss-safeguard-20b exists on BOTH providers at different rates.

    A flat merge of bedrock_mantle into the Converse namespace would silently re-price the
    Converse entry (+50% output) depending on dict order. Guard both sides.
    """
    from pricing import DEFAULT_PRICING

    converse = DEFAULT_PRICING["openai.gpt-oss-safeguard-20b"]["on_demand"]
    mantle = DEFAULT_PRICING["mantle/openai.gpt-oss-safeguard-20b"]["on_demand"]
    assert converse["input_usd_micros_per_1k"] == 70
    assert converse["output_usd_micros_per_1k"] == 200
    assert mantle["input_usd_micros_per_1k"] == 75
    assert mantle["output_usd_micros_per_1k"] == 300


def test_mantle_family_priced_and_not_region_derived():
    from pricing import _FALLBACK_BY_MODE, DEFAULT_PRICING

    mantle_ids = [m for m in DEFAULT_PRICING if m.startswith("mantle/")]
    assert len(mantle_ids) == 13
    fallback_input = _FALLBACK_BY_MODE["on_demand"]["input_usd_micros_per_1k"]
    for mid in mantle_ids:
        assert DEFAULT_PRICING[mid]["on_demand"]["input_usd_micros_per_1k"] < fallback_input
    # mantle does not support geo/global inference profiles — no phantom regional IDs.
    assert not [m for m in DEFAULT_PRICING if m.startswith(("us.mantle/", "global.mantle/"))]


# ---------------------------------------------------------------------------
# The mantle route feeding compute_cost: the whole point of the fix is what the
# request COSTS, so assert dollars, not just the intermediate usage dict.
# ---------------------------------------------------------------------------


def _mantle_cost(raw_usage: dict) -> tuple[int, int]:
    """Return (billable_input_tokens, usd_micros) for a Responses-shaped usage payload."""
    from bedrock import normalize_usage
    from mantle import USAGE_KEY_MAP, _flatten_usage, _to_exclusive_input

    usage = _to_exclusive_input(normalize_usage(_flatten_usage(raw_usage), USAGE_KEY_MAP))
    total, _, _, _, _ = compute_cost(
        model_id="luna",
        pricing_mode="on_demand",
        input_tokens=usage["input_tokens"],
        output_tokens=usage["output_tokens"],
        cache_read_input_tokens=usage["cache_read_input_tokens"],
        cache_write_input_tokens=usage["cache_write_input_tokens"],
    )
    return usage["input_tokens"], total


def test_a_cached_responses_request_costs_less_than_an_uncached_one(pricing_bucket):
    """Luna's real shape: 3090 of 3092 input tokens cached on the second identical call.

    Before the fix both calls billed all 3092 tokens at the full input rate, so caching
    made requests MORE expensive. Rates below are Luna's published per-token prices scaled
    to micros-per-1k.
    """
    _set_live(
        {
            "luna": {
                "on_demand": {
                    "input_usd_micros_per_1k": 1100,
                    "output_usd_micros_per_1k": 6600,
                    "cache_read_input_usd_micros_per_1k": 110,
                    "cache_write_input_usd_micros_per_1k": 1375,
                }
            }
        }
    )
    cold_in, cold_cost = _mantle_cost(
        {
            "input_tokens": 3092,
            "input_tokens_details": {"cache_write_tokens": 3090, "cached_tokens": 0},
            "output_tokens": 5,
        }
    )
    warm_in, warm_cost = _mantle_cost(
        {
            "input_tokens": 3092,
            "input_tokens_details": {"cache_write_tokens": 0, "cached_tokens": 3090},
            "output_tokens": 5,
        }
    )

    # Only two tokens are ever new; the rest is cache traffic.
    assert cold_in == 2 and warm_in == 2

    # The property that was inverted: a hit must be cheaper than a miss.
    assert warm_cost < cold_cost

    # And the hit must be far cheaper than billing all 3092 at the full input rate would be.
    naive_full_rate = (3092 * 1100) // 1000
    assert warm_cost < naive_full_rate
