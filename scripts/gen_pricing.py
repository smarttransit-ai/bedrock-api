#!/usr/bin/env python3
"""Generate DEFAULT_PRICING dict for lambda/proxy/pricing.py.

Reads Bedrock pricing data from scripts/vendor/litellm_model_prices.json
(a filtered snapshot of litellm's community pricing data).

Run from repo root:
    python3 scripts/gen_pricing.py

The output (DEFAULT_PRICING block) must be manually copied into
lambda/proxy/pricing.py, replacing the existing block between the top
comment and the _FALLBACK_BY_MODE definition.

--- Refresh procedure ---
1. Fetch the upstream litellm JSON (BerriAI/litellm raw
   model_prices_and_context_window.json on main) into /tmp/litellm_full.json.
   The full source URL is in scripts/vendor/litellm_model_prices.json (_meta.source).
2. Filter to routeable Bedrock entries (both input+output token cost fields present; no '/'
   in the key after stripping the bedrock/ prefix), strip the bedrock/ prefix from keys,
   keep only the pricing fields, sort keys, and prepend the _meta block; write the result to
   scripts/vendor/litellm_model_prices.json.
3. Update _meta.upstream_commit and _meta.fetched_date in the vendor file.
4. Run this script and paste the DEFAULT_PRICING block into lambda/proxy/pricing.py, then
   reformat (the generated dicts are single-line; ruff expands them to the file style):
       python3 scripts/gen_pricing.py > /tmp/pricing_block.py   # paste, then:
       ruff format lambda/proxy/pricing.py
5. If new model IDs appear, refresh the coverage snapshot so the D9 gate tracks them: set
   scripts/vendor/supported_model_ids.txt to the sorted DEFAULT_PRICING keys (the model IDs
   in the generated block).

litellm is MIT licensed. See scripts/vendor/litellm_model_prices.json for attribution.
"""

import json
import pathlib
import sys

VENDOR_JSON_PATH = pathlib.Path(__file__).parent / "vendor" / "litellm_model_prices.json"
SNAPSHOT_PATH = pathlib.Path(__file__).parent / "vendor" / "supported_model_ids.txt"

_BEDROCK_PROVIDERS = frozenset({"bedrock", "bedrock_converse"})

# Prefixes derived outward (base → regional) in Pass 2a.
_DERIVE_PREFIXES = ("us.", "global.")

# All regional prefixes used to detect a region-scoped ID. Sources for derivation
# must NOT start with any of these (a region-scoped ID is never a true base).
# Note: us-gov.* is region-scoped and non-derivable — never prepend a prefix to it,
# and never strip it to a "base" (there are no us-gov base IDs in the catalog).
_REGIONAL_PREFIXES = ("us.", "global.", "eu.", "ap.", "us-gov.")

# Prefix used to derive base IDs in Pass 2b (regional → base). Only us.* variants
# are used as derivation sources for determinism (D5); us.* exists for every model
# that needs a derived base (nova-premier, deepseek.r1, pixtral-large).
_BASE_DERIVE_PREFIX = "us."

# Provider prefixes that actually have AWS cross-region inference profiles
# (i.e. real us./global. inference-profile IDs exist). Derived from the committed
# snapshot supported_model_ids.txt: any provider appearing with a us./global./eu./ap.
# regional prefix there. Constraining Pass 2a to these prevents manufacturing phantom
# inference-profile IDs (e.g. us.ai21.*, global.cohere.*) that AWS does not expose.
_CROSS_REGION_PROVIDERS = frozenset(
    {"amazon", "anthropic", "deepseek", "meta", "mistral", "writer"}
)


def _provider_of(model_id: str) -> str:
    """Return the provider prefix of a base (non-region-scoped) model ID.

    The provider is the segment before the first '.' (e.g. "amazon.nova-lite-v1:0"
    -> "amazon"). IDs with no '.' return the whole string.
    """
    return model_id.split(".", 1)[0]


# IDs intentionally removed from DEFAULT_PRICING coverage.
# Any ID in supported_model_ids.txt absent from generated entries AND absent from this set
# causes a hard failure (sys.exit(1)). Add to this set only when removal is deliberate.
ALLOWLISTED_REMOVALS: frozenset[str] = frozenset()


def _per_token_to_micros_per_1k(v: float) -> int:
    """USD per single token → integer µUSD per 1,000 tokens.

    Formula: v * 1e6 (USD→µUSD) * 1e3 (per-token→per-1k) = v * 1e9.
    Example: $15/1M = 15e-6/token → round(15e-6 * 1e9) = 15_000 µUSD/1k.

    Uses Python's built-in round() which applies banker's rounding (half-even)
    for half-integer ties. Sub-µUSD/1k ties are economically negligible.
    """
    return round(v * 1e9)


def _load_vendor(path: pathlib.Path = VENDOR_JSON_PATH) -> dict:
    """Load the vendor JSON; return the model map (excluding the _meta key)."""
    with path.open() as f:
        data = json.load(f)
    return {k: v for k, v in data.items() if k != "_meta"}


def _to_on_demand_rates(entry: dict) -> dict[str, int]:
    """Convert a litellm pricing entry to an on_demand rates dict.

    Cache fallback (D3): an absent or null cache field defaults to the output rate.
    entry.get(key) returns None for both absent keys and JSON null values; the
    is-not-None guard handles both identically.
    """
    inp = _per_token_to_micros_per_1k(entry["input_cost_per_token"])
    out = _per_token_to_micros_per_1k(entry["output_cost_per_token"])
    cache_read_raw = entry.get("cache_read_input_token_cost")
    cache_write_raw = entry.get("cache_creation_input_token_cost")
    return {
        "input_usd_micros_per_1k": inp,
        "output_usd_micros_per_1k": out,
        "cache_read_input_usd_micros_per_1k": _per_token_to_micros_per_1k(cache_read_raw)
        if cache_read_raw is not None
        else out,
        "cache_write_input_usd_micros_per_1k": _per_token_to_micros_per_1k(cache_write_raw)
        if cache_write_raw is not None
        else out,
    }


def _build_entries(
    vendor: dict,
) -> tuple[dict[str, dict[str, dict[str, int]]], list[str]]:
    """Build the pricing entries dict from the vendor map.

    Returns (entries, derived_ids) where derived_ids lists any IDs that were
    created by derivation (not present in vendor directly).

    Pass 2a derives us./global. variants from base IDs when absent, but only for
    providers with real AWS cross-region inference profiles (_CROSS_REGION_PROVIDERS).
    Pass 2b derives base IDs from us.* variants when absent (D5 generalization).
    Neither pass overwrites an explicit vendor entry.
    """
    entries: dict[str, dict[str, dict[str, int]]] = {}
    derived_ids: list[str] = []

    # Pass 1: emit all vendor entries.
    # The vendor file filter already enforces the constraints below; the guards
    # here are a belt-and-suspenders double-check against schema drift.
    for model_id, entry in vendor.items():
        if entry.get("litellm_provider") not in _BEDROCK_PROVIDERS:
            continue
        inp_cost = entry.get("input_cost_per_token")
        out_cost = entry.get("output_cost_per_token")
        if inp_cost is None or out_cost is None:
            continue
        # Drop entries with no billing value (both token costs 0) — e.g. rerank
        # models at 0/0. Including them would underbill (route to fallback at $0).
        # Token-priced embeddings (input > 0, output 0) ARE billable and kept.
        if inp_cost <= 0 and out_cost <= 0:
            continue
        on_demand = _to_on_demand_rates(entry)
        # D2: batch mirrors on_demand (litellm carries no Bedrock batch rates).
        # NOTE: This over-counts batch requests for models with AWS batch discounts
        # (e.g., Claude 3.x/4.x have a 50% batch discount). Accept this tradeoff.
        entries[model_id] = {"on_demand": on_demand, "batch": dict(on_demand)}

    # Pass 2a: for each true base ID, derive us./global. variants if absent (D5).
    # A "base" ID is NOT region-scoped (no us./global./eu./ap./us-gov. prefix), and its
    # provider must have real AWS cross-region inference profiles (_CROSS_REGION_PROVIDERS).
    # This prevents manufacturing phantom IDs (e.g. us.ai21.*, global.cohere.*) and never
    # prepends a prefix to an already-regional ID (incl. us-gov.*). dict() avoids aliasing.
    base_ids = [
        mid
        for mid in list(entries)
        if not any(mid.startswith(p) for p in _REGIONAL_PREFIXES)
        and _provider_of(mid) in _CROSS_REGION_PROVIDERS
    ]
    for base_id in base_ids:
        for prefix in _DERIVE_PREFIXES:
            derived_id = prefix + base_id
            if derived_id not in entries:
                entries[derived_id] = dict(entries[base_id])
                derived_ids.append(derived_id)

    # Pass 2b: for each us.* variant, derive the base ID if absent (D5 generalization).
    # Fixes cases where litellm has us.X but not X (e.g. amazon.nova-premier-v1:0,
    # deepseek.r1-v1:0, mistral.pixtral-large-2502-v1:0). Only us.* is used as the
    # derivation source (deterministic; us.* exists for every model needing a base —
    # avoids insertion-order nondeterminism if eu.X / ap.X ever diverge from us.X).
    for regional_id in list(entries):
        if regional_id.startswith(_BASE_DERIVE_PREFIX):
            base_id = regional_id[len(_BASE_DERIVE_PREFIX) :]
            if base_id not in entries:
                entries[base_id] = dict(entries[regional_id])
                derived_ids.append(base_id)

    return entries, derived_ids


def main() -> None:
    # Read module attrs at call time so monkeypatching VENDOR_JSON_PATH /
    # SNAPSHOT_PATH in tests is honored (a default arg would bind at def time).
    vendor = _load_vendor(VENDOR_JSON_PATH)
    entries, derived_ids = _build_entries(vendor)

    # D9: coverage regression gate — fail hard on removed IDs.
    # Any ID in the snapshot absent from entries (and not allowlisted) would fall back
    # to the Opus-tier rate at runtime (~400x over-count). Treat it as a build error.
    if SNAPSHOT_PATH.exists():
        snapshot_ids = set(SNAPSHOT_PATH.read_text().splitlines())
        regressions = snapshot_ids - set(entries) - ALLOWLISTED_REMOVALS
        if regressions:
            print(
                "ERROR: IDs from supported_model_ids.txt are missing from generated entries.",
                file=sys.stderr,
            )
            print(
                "They would fall back to the Opus-tier rate (~400x over-count) at runtime.",
                file=sys.stderr,
            )
            print(
                "Fix: update D5 derivation, add a manual override, or add to ALLOWLISTED_REMOVALS.",
                file=sys.stderr,
            )
            for mid in sorted(regressions):
                print(f"  MISSING: {mid}", file=sys.stderr)
            sys.exit(1)

    print("# " + "=" * 75)
    print("# Generated by scripts/gen_pricing.py")
    print("# Source: scripts/vendor/litellm_model_prices.json (litellm community data, MIT)")
    print("# All values: integer USD-micros (µUSD) per 1,000 tokens.")
    print("# $15.00/1M tokens = 15_000 µUSD/1k tokens.")
    print("# Batch rates mirror on_demand (litellm carries no Bedrock batch rates).")
    print("# " + "=" * 75)
    print("DEFAULT_PRICING: dict[str, dict[str, dict[str, int]]] = {")
    for mid in sorted(entries):
        v = entries[mid]
        on_demand = v["on_demand"]
        inp = on_demand["input_usd_micros_per_1k"]
        out = on_demand["output_usd_micros_per_1k"]
        comment = f"  # ${inp / 1000:.4g}/${out / 1000:.4g} per 1M"
        print(f'    "{mid}": {v},{comment}')
    print("}")
    print()
    print(f"# Total entries: {len(entries)}")

    if derived_ids:
        print()
        print("# Cross-region / base IDs derived (not in vendor JSON directly):")
        for d in sorted(derived_ids):
            print(f"#   {d}")


if __name__ == "__main__":
    main()
