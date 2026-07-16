"""Shared litellm → Bedrock pricing-catalog transform.

This is the single source of the pricing transform, imported by BOTH:
  - scripts/gen_pricing.py (offline: vendored litellm JSON → DEFAULT_PRICING block)
  - lambda/proxy/pricing_refresh.py (runtime: live litellm pull → S3 catalog)

so the baked-in catalog and a live-refreshed catalog are computed identically.

``filter_bedrock`` reproduces, in Python, the jq filter that is applied when the
vendor snapshot is produced (see scripts/vendor/litellm_model_prices.json _meta).
``_build_entries`` then applies the µUSD conversion and cross-region derivation.

litellm is MIT licensed. See scripts/vendor/litellm_model_prices.json for attribution.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

_BEDROCK_PROVIDERS = frozenset({"bedrock", "bedrock_converse"})

# bedrock-mantle (the OpenAI-compatible endpoint) is priced too, but its IDs are
# namespaced under "mantle/" rather than merged into the flat Converse namespace.
# Two IDs — openai.gpt-oss-safeguard-120b/20b — exist under BOTH bedrock_converse and
# bedrock_mantle at DIFFERENT rates (safeguard-20b: $0.07/$0.20 vs $0.075/$0.30 per 1M).
# A flat merge would let dict order decide the winner and silently re-price an existing
# Converse model. Namespacing keeps them distinct; the caller selects by route, never by
# parsing the model string (openai.* legitimately spans both providers).
_MANTLE_PROVIDER = "bedrock_mantle"
MANTLE_NAMESPACE = "mantle/"

_PRICED_PROVIDERS = _BEDROCK_PROVIDERS | {_MANTLE_PROVIDER}

# Upstream litellm key prefix per provider. Stripped before namespacing; a key that still
# holds a '/' afterward is a non-routeable region/routing/image-size variant and is dropped.
_UPSTREAM_PREFIX = {_MANTLE_PROVIDER: "bedrock_mantle/"}
_DEFAULT_UPSTREAM_PREFIX = "bedrock/"

# litellm prices reasoning separately via this field (e.g. dashscope/qwen-plus: $1.20
# output vs $4.00 reasoning per 1M). No bedrock/bedrock_converse/bedrock_mantle entry
# carries it today, so reasoning tokens bill at the standard output rate — and since the
# Responses API counts them WITHIN output_tokens, billing output_tokens is complete.
# If upstream ever adds it to a priced model we would silently underbill, so warn loudly
# rather than fail closed (failing closed would drop the model to the Opus-tier fallback,
# which over-counts ~11x — strictly worse than a known-stale rate).
_REASONING_COST_FIELD = "output_cost_per_reasoning_token"

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

# IDs intentionally removed from DEFAULT_PRICING coverage.
# Any ID in supported_model_ids.txt absent from generated entries AND absent from this set
# causes a hard failure in gen_pricing.py. Add to this set only when removal is deliberate.
ALLOWLISTED_REMOVALS: frozenset[str] = frozenset()


def _provider_of(model_id: str) -> str:
    """Return the provider prefix of a base (non-region-scoped) model ID.

    The provider is the segment before the first '.' (e.g. "amazon.nova-lite-v1:0"
    -> "amazon"). IDs with no '.' return the whole string.
    """
    return model_id.split(".", 1)[0]


def _per_token_to_micros_per_1k(v: float) -> int:
    """USD per single token → integer µUSD per 1,000 tokens.

    Formula: v * 1e6 (USD→µUSD) * 1e3 (per-token→per-1k) = v * 1e9.
    Example: $15/1M = 15e-6/token → round(15e-6 * 1e9) = 15_000 µUSD/1k.

    Uses Python's built-in round() which applies banker's rounding (half-even)
    for half-integer ties. Sub-µUSD/1k ties are economically negligible.
    """
    return round(v * 1e9)


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


def filter_bedrock(raw: dict) -> dict:
    """Filter a raw litellm price map down to routeable entries, keyed for the catalog.

    Reproduces in Python the jq filter applied at vendor time (see the vendor
    file's _meta.filter). Keeps entries whose ``litellm_provider`` is in
    ``_PRICED_PROVIDERS`` and whose input+output token costs are both non-null.

    Two provider families are kept, keyed differently:
      - ``bedrock`` / ``bedrock_converse`` → ``bedrock/`` stripped, key used as-is
        (e.g. ``anthropic.claude-sonnet-4-6``).
      - ``bedrock_mantle`` → ``bedrock_mantle/`` stripped, key namespaced under
        ``mantle/`` (e.g. ``mantle/openai.gpt-5.6-luna``). See MANTLE_NAMESPACE for
        why these are namespaced rather than merged.

    Keys still containing a ``/`` after the provider prefix is stripped are dropped
    (non-routeable region/routing/image-size prefixes).
    """
    filtered: dict = {}
    for key, entry in raw.items():
        if key == "_meta" or key == "sample_spec":
            continue
        if not isinstance(entry, dict):
            continue
        provider = entry.get("litellm_provider")
        if provider not in _PRICED_PROVIDERS:
            continue
        if entry.get("input_cost_per_token") is None or entry.get("output_cost_per_token") is None:
            continue
        stripped = key.removeprefix(_UPSTREAM_PREFIX.get(provider, _DEFAULT_UPSTREAM_PREFIX))
        if "/" in stripped:
            continue
        namespace = MANTLE_NAMESPACE if provider == _MANTLE_PROVIDER else ""
        filtered[namespace + stripped] = entry
    return filtered


def _build_entries(
    vendor: dict,
) -> tuple[dict[str, dict[str, dict[str, int]]], list[str]]:
    """Build the pricing entries dict from the (already-filtered) vendor map.

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
        if entry.get("litellm_provider") not in _PRICED_PROVIDERS:
            continue
        inp_cost = entry.get("input_cost_per_token")
        out_cost = entry.get("output_cost_per_token")
        if inp_cost is None or out_cost is None:
            continue
        # Upstream started pricing reasoning separately for this model — our rates no
        # longer capture its full cost. Warn (do not drop: the fallback is far worse).
        if entry.get(_REASONING_COST_FIELD) is not None:
            logger.warning(
                json.dumps(
                    {
                        "event": "pricing_reasoning_cost_ignored",
                        "model_id": model_id,
                        "reasoning_cost_per_token": entry[_REASONING_COST_FIELD],
                    }
                )
            )
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


def build_catalog(raw: dict) -> dict[str, dict[str, dict[str, int]]]:
    """Full transform: raw litellm price map → pricing catalog.

    Filters to routeable bedrock-runtime + bedrock-mantle entries (the latter namespaced
    under mantle/, see filter_bedrock) then builds the µUSD on_demand/batch map.
    Same shape as DEFAULT_PRICING in pricing.py.
    """
    return _build_entries(filter_bedrock(raw))[0]
