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
