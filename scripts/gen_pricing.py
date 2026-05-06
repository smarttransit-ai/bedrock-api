#!/usr/bin/env python3
"""Generate DEFAULT_PRICING dict for lambda/proxy/pricing.py.

Fetches on-demand us-east-1 token prices from two AWS public pricing APIs:
  - AmazonBedrock          : non-Anthropic models (per-1k-token pricing)
  - AmazonBedrockFoundationModels : Anthropic/marketplace models (per-1M-token pricing)

Cross-region inference profiles (us.*, global.*) are emitted alongside the
base model IDs using the mapping at the bottom of this file.

Run from repo root:
    python3 scripts/gen_pricing.py
"""

import json
import urllib.request

BEDROCK_API_URL = (
    "https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonBedrock/current/index.json"
)
FM_API_URL = (
    "https://pricing.us-east-1.amazonaws.com"
    "/offers/v1.0/aws/AmazonBedrockFoundationModels/current/index.json"
)

# ---------------------------------------------------------------------------
# Model name → Bedrock API IDs
# Each name maps to a list of (model_id, is_cross_region_profile) tuples.
# Sourced from: aws bedrock list-foundation-models + list-inference-profiles
# ---------------------------------------------------------------------------
NAME_TO_IDS: dict[str, list[str]] = {
    # Anthropic — Claude 4 Opus family
    "Claude Opus 4 (Amazon Bedrock Edition)": [
        "anthropic.claude-opus-4-20250514-v1:0",
        "us.anthropic.claude-opus-4-20250514-v1:0",
    ],
    "Claude Opus 4.1 (Amazon Bedrock Edition)": [
        "anthropic.claude-opus-4-1-20250805-v1:0",
        "us.anthropic.claude-opus-4-1-20250805-v1:0",
    ],
    "Claude Opus 4.5 (Amazon Bedrock Edition)": [
        "anthropic.claude-opus-4-5-20251101-v1:0",
        "us.anthropic.claude-opus-4-5-20251101-v1:0",
        "global.anthropic.claude-opus-4-5-20251101-v1:0",
    ],
    "Claude Opus 4.6 (Amazon Bedrock Edition)": [
        "anthropic.claude-opus-4-6-v1",
        "us.anthropic.claude-opus-4-6-v1",
        "global.anthropic.claude-opus-4-6-v1",
    ],
    # Anthropic — Claude 4 Sonnet family
    "Claude Sonnet 4 (Amazon Bedrock Edition)": [
        "anthropic.claude-sonnet-4-20250514-v1:0",
        "us.anthropic.claude-sonnet-4-20250514-v1:0",
        "global.anthropic.claude-sonnet-4-20250514-v1:0",
    ],
    "Claude Sonnet 4.5 (Amazon Bedrock Edition)": [
        "anthropic.claude-sonnet-4-5-20250929-v1:0",
        "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        "global.anthropic.claude-sonnet-4-5-20250929-v1:0",
    ],
    "Claude Sonnet 4.6 (Amazon Bedrock Edition)": [
        "anthropic.claude-sonnet-4-6",
        "us.anthropic.claude-sonnet-4-6",
        "global.anthropic.claude-sonnet-4-6",
    ],
    # Anthropic — Claude 4 Haiku family
    "Claude Haiku 4.5 (Amazon Bedrock Edition)": [
        "anthropic.claude-haiku-4-5-20251001-v1:0",
        "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        "global.anthropic.claude-haiku-4-5-20251001-v1:0",
    ],
    # Anthropic — Claude 3.x family
    "Claude 3.7 Sonnet (Amazon Bedrock Edition)": [
        "anthropic.claude-3-7-sonnet-20250219-v1:0",
        "us.anthropic.claude-3-7-sonnet-20250219-v1:0",
    ],
    "Claude 3.5 Sonnet v2 (Amazon Bedrock Edition)": [
        "anthropic.claude-3-5-sonnet-20241022-v2:0",
        "us.anthropic.claude-3-5-sonnet-20241022-v2:0",
    ],
    "Claude 3.5 Sonnet (Amazon Bedrock Edition)": [
        "anthropic.claude-3-5-sonnet-20240620-v1:0",
    ],
    "Claude 3.5 Haiku (Amazon Bedrock Edition)": [
        "anthropic.claude-3-5-haiku-20241022-v1:0",
        "us.anthropic.claude-3-5-haiku-20241022-v1:0",
    ],
    "Claude 3 Opus (Amazon Bedrock Edition)": [
        "anthropic.claude-3-opus-20240229-v1:0",
        "us.anthropic.claude-3-opus-20240229-v1:0",
    ],
    "Claude 3 Sonnet (Amazon Bedrock Edition)": [
        "anthropic.claude-3-sonnet-20240229-v1:0",
        "us.anthropic.claude-3-sonnet-20240229-v1:0",
    ],
    "Claude 3 Haiku (Amazon Bedrock Edition)": [
        "anthropic.claude-3-haiku-20240307-v1:0",
        "us.anthropic.claude-3-haiku-20240307-v1:0",
    ],
    "Claude 2.1 (Amazon Bedrock Edition)": [
        "anthropic.claude-v2:1",
    ],
    "Claude 2.0 (Amazon Bedrock Edition)": [
        "anthropic.claude-v2",
    ],
    "Claude Instant (Amazon Bedrock Edition)": [
        "anthropic.claude-instant-v1",
    ],
    # AI21
    "Jamba 1.5 Large (Amazon Bedrock Edition)": ["ai21.jamba-1-5-large-v1:0"],
    "Jamba 1.5 Mini (Amazon Bedrock Edition)": ["ai21.jamba-1-5-mini-v1:0"],
    # Cohere
    "Cohere Command R+ (Amazon Bedrock Edition)": ["cohere.command-r-plus-v1:0"],
    "Cohere Command R (Amazon Bedrock Edition)": ["cohere.command-r-v1:0"],
    "Cohere Generate Model - Command (Amazon Bedrock Edition)": [
        "cohere.command-text-v14",
    ],
    "Cohere Generate Model - Command-Light (Amazon Bedrock Edition)": [
        "cohere.command-light-text-v14",
    ],
    # Writer / Palmyra
    "Palmyra X4 (Amazon Bedrock Edition)": [
        "writer.palmyra-x4-v1:0",
        "us.writer.palmyra-x4-v1:0",
    ],
    "Palmyra X5 (Amazon Bedrock Edition)": [
        "writer.palmyra-x5-v1:0",
        "us.writer.palmyra-x5-v1:0",
    ],
    "Writer Palmyra Vision 7B (Amazon Bedrock Edition)": ["writer.palmyra-vision-7b"],
}

# Models in AmazonBedrock API (per-1k-token pricing, non-Anthropic)
# name in pricing API → list of Bedrock model IDs
BEDROCK_API_NAME_TO_IDS: dict[str, list[str]] = {
    # Amazon Nova
    "Nova Micro": ["amazon.nova-micro-v1:0", "us.amazon.nova-micro-v1:0"],
    "Nova Lite": ["amazon.nova-lite-v1:0", "us.amazon.nova-lite-v1:0"],
    "Nova Pro": ["amazon.nova-pro-v1:0", "us.amazon.nova-pro-v1:0"],
    "Nova Premier": ["amazon.nova-premier-v1:0", "us.amazon.nova-premier-v1:0"],
    "Nova 2.0 Lite": [
        "amazon.nova-2-lite-v1:0",
        "us.amazon.nova-2-lite-v1:0",
        "global.amazon.nova-2-lite-v1:0",
    ],
    "Nova Pro Latency Optimized": [],  # skip — latency tier
    # DeepSeek
    "DeepSeek v3.2": ["deepseek.v3.2"],
    "R1": ["deepseek.r1-v1:0", "us.deepseek.r1-v1:0"],
    # Google
    "Gemma 3 4B": ["google.gemma-3-4b-it"],
    "Gemma 3 12B": ["google.gemma-3-12b-it"],
    "Gemma 3 27B": ["google.gemma-3-27b-it"],
    # Kimi
    "Kimi K2 Thinking": ["moonshot.kimi-k2-thinking"],
    "Kimi K2.5": ["moonshotai.kimi-k2.5"],
    # Meta Llama
    "Llama 3 8B": ["meta.llama3-8b-instruct-v1:0"],
    "Llama 3 70B": ["meta.llama3-70b-instruct-v1:0"],
    "Llama 3.1 8B": ["meta.llama3-1-8b-instruct-v1:0", "us.meta.llama3-1-8b-instruct-v1:0"],
    "Llama 3.1 70B": ["meta.llama3-1-70b-instruct-v1:0", "us.meta.llama3-1-70b-instruct-v1:0"],
    "Llama 3.1 70B Latency Optimized": [],  # skip
    "Llama 3.2 1B": ["meta.llama3-2-1b-instruct-v1:0", "us.meta.llama3-2-1b-instruct-v1:0"],
    "Llama 3.2 3B": ["meta.llama3-2-3b-instruct-v1:0", "us.meta.llama3-2-3b-instruct-v1:0"],
    "Llama 3.2 11B": ["meta.llama3-2-11b-instruct-v1:0", "us.meta.llama3-2-11b-instruct-v1:0"],
    "Llama 3.2 90B": ["meta.llama3-2-90b-instruct-v1:0", "us.meta.llama3-2-90b-instruct-v1:0"],
    "Llama 3.3 70B": ["meta.llama3-3-70b-instruct-v1:0", "us.meta.llama3-3-70b-instruct-v1:0"],
    "Llama 4 Scout 17B": [
        "meta.llama4-scout-17b-instruct-v1:0",
        "us.meta.llama4-scout-17b-instruct-v1:0",
    ],
    "Llama 4 Maverick 17B": [
        "meta.llama4-maverick-17b-instruct-v1:0",
        "us.meta.llama4-maverick-17b-instruct-v1:0",
    ],
    # MiniMax
    "Minimax M2": ["minimax.minimax-m2"],
    "Minimax M2.1": ["minimax.minimax-m2.1"],
    "MiniMax M2.5": ["minimax.minimax-m2.5"],
    # Mistral
    "Mistral 7B": ["mistral.mistral-7b-instruct-v0:2"],
    "Mixtral 8x7B": ["mistral.mixtral-8x7b-instruct-v0:1"],
    "Mistral Small": ["mistral.mistral-small-2402-v1:0"],
    "Mistral Large": ["mistral.mistral-large-2402-v1:0"],
    "Mistral Large 3": ["mistral.mistral-large-3-675b-instruct"],
    "Ministral 3B 3.0": ["mistral.ministral-3-3b-instruct"],
    "Ministral 8B 3.0": ["mistral.ministral-3-8b-instruct"],
    "Ministral 14B 3.0": ["mistral.ministral-3-14b-instruct"],
    "Magistral Small 1.2": ["mistral.magistral-small-2509"],
    "Pixtral Large 25.02": [
        "mistral.pixtral-large-2502-v1:0",
        "us.mistral.pixtral-large-2502-v1:0",
    ],
    "Devstral": ["mistral.devstral-2-123b"],
    "Voxtral Mini 1.0": ["mistral.voxtral-mini-3b-2507"],
    "Voxtral Small 1.0": ["mistral.voxtral-small-24b-2507"],
    # NVIDIA
    "NVIDIA Nemotron Nano 2": ["nvidia.nemotron-nano-9b-v2"],
    "NVIDIA Nemotron Nano 2 VL": ["nvidia.nemotron-nano-12b-v2"],
    "Nemotron Nano 3 30B": ["nvidia.nemotron-nano-3-30b"],
    "NVIDIA Nemotron 3 Super 120B A12B": ["nvidia.nemotron-super-3-120b"],
    # OpenAI OSS
    "gpt-oss-20b": ["openai.gpt-oss-20b-1:0"],
    "gpt-oss-120b": ["openai.gpt-oss-120b-1:0"],
    "GPT OSS Safeguard 20B": ["openai.gpt-oss-safeguard-20b"],
    "GPT OSS Safeguard 120B": ["openai.gpt-oss-safeguard-120b"],
    # Qwen
    "Qwen3 32B": ["qwen.qwen3-32b-v1:0"],
    "Qwen3 Coder 30B A3B": ["qwen.qwen3-coder-30b-a3b-v1:0"],
    "Qwen3 Coder Next": ["qwen.qwen3-coder-next"],
    "Qwen3 VL 235B A22B": ["qwen.qwen3-vl-235b-a22b"],
    # Z.AI
    "GLM 4.7": ["zai.glm-4.7"],
    "GLM 4.7 Flash": ["zai.glm-4.7-flash"],
    "GLM 5": ["zai.glm-5"],
}


def _fetch_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=60) as resp:
        return json.loads(resp.read())


def _build_price_map(data: dict) -> dict[str, float]:
    """SKU → on-demand price per unit."""
    price_map: dict[str, float] = {}
    for sku, term_data in data["terms"]["OnDemand"].items():
        for term in term_data.values():
            for pd in term["priceDimensions"].values():
                price_map[sku] = float(pd["pricePerUnit"].get("USD", 0))
    return price_map


def _parse_bedrock_api(data: dict) -> dict[str, dict[str, tuple[float, float]]]:
    """AmazonBedrock API → {model_name: {mode: (input_per_1k_usd, output_per_1k_usd)}}.

    Prices are in USD per 1k tokens.
    Filter: us-east-1, on-demand Input/Output tokens only (no batch, no cache).
    """
    price_map = _build_price_map(data)
    by_model: dict[str, dict[str, dict[str, float]]] = {}

    for sku, prod in data["products"].items():
        attrs = prod["attributes"]
        if attrs.get("regionCode") != "us-east-1":
            continue
        inf_type = attrs.get("inferenceType", "")
        mode = "batch" if "Batch" in inf_type else "on_demand"
        if "Input tokens" in inf_type:
            token_class = "input"
        elif "Output tokens" in inf_type:
            token_class = "output"
        else:
            continue
        model = attrs.get("model", "")
        usd = price_map.get(sku, 0.0)
        by_model.setdefault(model, {}).setdefault(mode, {})
        by_model[model][mode][token_class] = usd

    output: dict[str, dict[str, tuple[float, float]]] = {}
    for name, modes in by_model.items():
        out_modes: dict[str, tuple[float, float]] = {}
        for mode, vals in modes.items():
            if "input" in vals and "output" in vals:
                out_modes[mode] = (vals["input"], vals["output"])
        if out_modes:
            output[name] = out_modes
    return output


def _parse_fm_api(data: dict) -> dict[str, dict[str, dict[str, float]]]:
    """AmazonBedrockFoundationModels API → nested mode/class price map (USD per 1M)."""
    price_map = _build_price_map(data)
    by_svc: dict[str, dict[str, dict[str, float]]] = {}

    for sku, prod in data["products"].items():
        attrs = prod["attributes"]
        if attrs.get("regionCode") != "us-east-1":
            continue
        usagetype = attrs.get("usagetype", "")
        svc = attrs.get("servicename", "")

        skip_tokens = ("Reserved", "TPM", "Latency")
        if any(tok in usagetype for tok in skip_tokens):
            continue

        mode = "batch" if "Batch" in usagetype else "on_demand"
        if "CacheReadInputTokenCount" in usagetype:
            tok_type = "cache_read_input"
        elif "CacheWriteInputTokenCount" in usagetype:
            tok_type = "cache_write_input"
        elif "InputTokenCount" in usagetype:
            tok_type = "input"
        elif "OutputTokenCount" in usagetype or "ResponseToken" in usagetype:
            tok_type = "output"
        else:
            continue

        is_global = "Global" in usagetype
        usd = price_map.get(sku, 0.0)

        key = svc
        entry = by_svc.setdefault(key, {}).setdefault(mode, {})
        # Prefer global price (cross-region inference profile rates)
        if tok_type not in entry or is_global:
            entry[tok_type] = usd
    return by_svc


def _micros_per_1k_from_per_1k(usd_per_1k: float) -> int:
    """USD per 1k tokens → integer µUSD per 1k tokens."""
    return int(round(usd_per_1k * 1_000_000))


def _micros_per_1k_from_per_1m(usd_per_1m: float) -> int:
    """USD per 1M tokens → integer µUSD per 1k tokens."""
    return int(round(usd_per_1m * 1_000))


def main() -> None:
    print("Fetching AmazonBedrock pricing API ...", flush=True)
    bedrock_data = _fetch_json(BEDROCK_API_URL)
    print("Fetching AmazonBedrockFoundationModels pricing API ...", flush=True)
    fm_data = _fetch_json(FM_API_URL)

    bedrock_prices = _parse_bedrock_api(bedrock_data)
    fm_prices = _parse_fm_api(fm_data)

    entries: dict[str, dict[str, dict[str, int]]] = {}
    missing: list[str] = []

    # --- Marketplace / Anthropic models (AmazonBedrockFoundationModels API) ---
    for svc_name, model_ids in NAME_TO_IDS.items():
        if svc_name not in fm_prices:
            missing.append(f"FM API: {svc_name!r}")
            continue
        mode_map = fm_prices[svc_name]
        on_demand = mode_map.get("on_demand", {})
        batch = mode_map.get("batch", on_demand)
        inp_u = _micros_per_1k_from_per_1m(on_demand.get("input", 0.0))
        out_u = _micros_per_1k_from_per_1m(on_demand.get("output", 0.0))
        batch_in_u = _micros_per_1k_from_per_1m(batch.get("input", on_demand.get("input", 0.0)))
        batch_out_u = _micros_per_1k_from_per_1m(batch.get("output", on_demand.get("output", 0.0)))
        cache_read_u = _micros_per_1k_from_per_1m(
            on_demand.get("cache_read_input", on_demand.get("output", 0.0))
        )
        cache_write_u = _micros_per_1k_from_per_1m(
            on_demand.get("cache_write_input", on_demand.get("output", 0.0))
        )
        batch_cache_read_u = _micros_per_1k_from_per_1m(
            batch.get("cache_read_input", batch.get("output", on_demand.get("output", 0.0)))
        )
        batch_cache_write_u = _micros_per_1k_from_per_1m(
            batch.get("cache_write_input", batch.get("output", on_demand.get("output", 0.0)))
        )
        for mid in model_ids:
            entries[mid] = {
                "on_demand": {
                    "input_usd_micros_per_1k": inp_u,
                    "output_usd_micros_per_1k": out_u,
                    "cache_read_input_usd_micros_per_1k": cache_read_u,
                    "cache_write_input_usd_micros_per_1k": cache_write_u,
                },
                "batch": {
                    "input_usd_micros_per_1k": batch_in_u,
                    "output_usd_micros_per_1k": batch_out_u,
                    "cache_read_input_usd_micros_per_1k": batch_cache_read_u,
                    "cache_write_input_usd_micros_per_1k": batch_cache_write_u,
                },
            }

    # --- Standard models (AmazonBedrock API) ---
    for api_name, model_ids in BEDROCK_API_NAME_TO_IDS.items():
        if not model_ids:
            continue
        if api_name not in bedrock_prices:
            missing.append(f"Bedrock API: {api_name!r}")
            continue
        mode_map = bedrock_prices[api_name]
        on_demand = mode_map.get("on_demand")
        if not on_demand:
            missing.append(f"Bedrock API (on_demand): {api_name!r}")
            continue
        batch = mode_map.get("batch", on_demand)
        inp_u = _micros_per_1k_from_per_1k(on_demand[0])
        out_u = _micros_per_1k_from_per_1k(on_demand[1])
        batch_in_u = _micros_per_1k_from_per_1k(batch[0])
        batch_out_u = _micros_per_1k_from_per_1k(batch[1])
        for mid in model_ids:
            entries[mid] = {
                "on_demand": {
                    "input_usd_micros_per_1k": inp_u,
                    "output_usd_micros_per_1k": out_u,
                    "cache_read_input_usd_micros_per_1k": out_u,
                    "cache_write_input_usd_micros_per_1k": out_u,
                },
                "batch": {
                    "input_usd_micros_per_1k": batch_in_u,
                    "output_usd_micros_per_1k": batch_out_u,
                    "cache_read_input_usd_micros_per_1k": batch_out_u,
                    "cache_write_input_usd_micros_per_1k": batch_out_u,
                },
            }

    # --- Opus 4.7: not in pricing API yet — use Opus 4 rate ($15/$75 per 1M) ---
    for mid in [
        "anthropic.claude-opus-4-7",
        "us.anthropic.claude-opus-4-7",
        "global.anthropic.claude-opus-4-7",
    ]:
        entries[mid] = {
            "on_demand": {
                "input_usd_micros_per_1k": 15_000,
                "output_usd_micros_per_1k": 75_000,
                "cache_read_input_usd_micros_per_1k": 75_000,
                "cache_write_input_usd_micros_per_1k": 75_000,
            },
            "batch": {
                "input_usd_micros_per_1k": 15_000,
                "output_usd_micros_per_1k": 75_000,
                "cache_read_input_usd_micros_per_1k": 75_000,
                "cache_write_input_usd_micros_per_1k": 75_000,
            },
        }

    # --- Emit Python source ---
    print()
    print("# " + "=" * 75)
    print("# Generated by scripts/gen_pricing.py")
    print("# Source: https://aws.amazon.com/bedrock/pricing/ (public pricing API)")
    print("# All values: integer USD-micros (µUSD) per 1,000 tokens.")
    print("# $15.00/1M tokens = 15_000 µUSD/1k tokens.")
    print("# " + "=" * 75)
    print("DEFAULT_PRICING: dict[str, dict[str, dict[str, int]]] = {")
    for mid in sorted(entries):
        v = entries[mid]
        on_demand = v["on_demand"]
        inp = on_demand["input_usd_micros_per_1k"]
        out = on_demand["output_usd_micros_per_1k"]
        inp_dollar = inp / 1000
        out_dollar = out / 1000
        comment = f"  # ${inp_dollar:.4g}/${out_dollar:.4g} per 1M"
        print(f'    "{mid}": {v},{comment}')
    print("}")
    print()
    print(f"# Total entries: {len(entries)}")

    if missing:
        print()
        print("# WARNING — not found in pricing APIs (manual price needed):")
        for m in missing:
            print(f"#   {m}")


if __name__ == "__main__":
    main()
