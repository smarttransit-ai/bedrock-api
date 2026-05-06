import json

from pricing import compute_cost


def test_compute_cost_on_demand_rounding_and_components(monkeypatch):
    monkeypatch.setenv(
        "PRICING_JSON",
        json.dumps(
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
        ),
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


def test_compute_cost_batch_mode(monkeypatch):
    monkeypatch.setenv(
        "PRICING_JSON",
        json.dumps(
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
        ),
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


def test_compute_cost_fallback_for_missing_mode(monkeypatch):
    monkeypatch.setenv(
        "PRICING_JSON",
        json.dumps(
            {
                "m": {
                    "on_demand": {
                        "input_usd_micros_per_1k": 1000,
                        "output_usd_micros_per_1k": 2000,
                    }
                }
            }
        ),
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
    assert batch_total < on_demand_total
    assert fallback_applied is False
    assert "pricing_mode" not in fallback_dimensions
