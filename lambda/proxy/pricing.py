import json
import os

# Prices as of 2025-05-14. Source: https://aws.amazon.com/bedrock/pricing/
# All values are integer USD-micros (µUSD) per 1,000 tokens.
# 1 µUSD = $0.000001.  $3.00/1M tokens = 3,000 µUSD/1k tokens.
DEFAULT_PRICING: dict[str, dict[str, int]] = {
    "us.anthropic.claude-sonnet-4-6-20250514-v1:0": {
        "input_usd_micros_per_1k": 3_000,  # $3.00 / 1M input tokens
        "output_usd_micros_per_1k": 15_000,  # $15.00 / 1M output tokens
    },
    "us.anthropic.claude-haiku-4-5-20250207-v1:0": {
        "input_usd_micros_per_1k": 800,  # $0.80 / 1M input tokens
        "output_usd_micros_per_1k": 4_000,  # $4.00 / 1M output tokens
    },
}

# Fallback for unknown models — conservative (Sonnet 4.6 rates).
_FALLBACK: dict[str, int] = {
    "input_usd_micros_per_1k": 3_000,
    "output_usd_micros_per_1k": 15_000,
}


def _load_pricing() -> dict[str, dict[str, int]]:
    raw = os.environ.get("PRICING_JSON")
    if raw:
        return json.loads(raw)
    return DEFAULT_PRICING


def compute_cost(model_id: str, input_tokens: int, output_tokens: int) -> int:
    """Return total cost as integer USD-micros. No floats used."""
    rates = _load_pricing().get(model_id, _FALLBACK)
    input_cost = (input_tokens * rates["input_usd_micros_per_1k"]) // 1000
    output_cost = (output_tokens * rates["output_usd_micros_per_1k"]) // 1000
    return input_cost + output_cost
