#!/usr/bin/env python3
"""Generate DEFAULT_PRICING dict for lambda/proxy/pricing.py.

Reads Bedrock pricing data from scripts/vendor/litellm_model_prices.json
(a filtered snapshot of litellm's community pricing data).

Run from repo root:
    python3 scripts/gen_pricing.py

The output (DEFAULT_PRICING block) must be manually copied into
lambda/proxy/pricing.py, replacing the existing block between the top
comment and the _FALLBACK_BY_MODE definition.

The litellm → catalog transform itself lives in lambda/proxy/pricing_catalog.py
(shared with the runtime refresh endpoint); this script is the offline driver
(load vendor file → build → coverage gate → print).

--- Refresh procedure ---
1. Fetch the upstream litellm JSON (BerriAI/litellm raw
   model_prices_and_context_window.json on main) into /tmp/litellm_full.json.
   The full source URL is in scripts/vendor/litellm_model_prices.json (_meta.source).
2. Filter to routeable entries and key them for the catalog. The simplest correct way is to
   reuse the shipped transform rather than hand-rolling jq — it is the same function the
   runtime refresh uses, so the snapshot cannot drift from live behaviour:

       import json, sys
       sys.path.insert(0, "lambda/proxy")
       from pricing_catalog import filter_bedrock
       raw = json.load(open("/tmp/litellm_full.json"))
       KEEP = ("litellm_provider", "input_cost_per_token", "output_cost_per_token",
               "cache_read_input_token_cost", "cache_creation_input_token_cost",
               "output_cost_per_reasoning_token")
       out = {k: {f: e[f] for f in KEEP if f in e} for k, e in filter_bedrock(raw).items()}
       # then: sort keys, prepend the _meta block, write scripts/vendor/litellm_model_prices.json

   filter_bedrock keeps litellm_provider in {bedrock, bedrock_converse, bedrock_mantle} with
   both token costs non-null, strips the provider's key prefix (bedrock/ or bedrock_mantle/),
   drops keys still holding a '/' afterward, and namespaces mantle keys under mantle/.
   Retain output_cost_per_reasoning_token when present so _build_entries can warn that our
   rates would underbill (no priced provider carries it today).
   NOTE: mantle entries MUST be stored as mantle/<id>; prefixing them with bedrock/ instead
   leaves a slash in the stripped key and the whole mantle family is silently dropped.
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

# The transform is shared with the runtime; it ships only under lambda/proxy/.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "lambda" / "proxy"))
from pricing_catalog import ALLOWLISTED_REMOVALS, _build_entries  # noqa: E402

VENDOR_JSON_PATH = pathlib.Path(__file__).parent / "vendor" / "litellm_model_prices.json"
SNAPSHOT_PATH = pathlib.Path(__file__).parent / "vendor" / "supported_model_ids.txt"


def _load_vendor(path: pathlib.Path = VENDOR_JSON_PATH) -> dict:
    """Load the vendor JSON; return the model map (excluding the _meta key)."""
    with path.open() as f:
        data = json.load(f)
    return {k: v for k, v in data.items() if k != "_meta"}


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
