"""On-demand pricing refresh: re-pull litellm → rebuild → validate → write S3.

Exposed via the admin endpoint POST /admin/pricing/refresh. All-or-nothing: the S3
write is the commit point, so any failure (fetch/parse/validation) leaves the
current live catalog untouched.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from datetime import UTC, datetime

from pricing import DEFAULT_PRICING
from pricing_catalog import build_catalog
from pricing_store import save_live_catalog

logger = logging.getLogger(__name__)

LITELLM_SOURCE_URL = os.environ.get(
    "LITELLM_SOURCE_URL",
    "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json",
)
_FETCH_TIMEOUT_S = 15
_MAX_BODY_BYTES = 10 * 1024 * 1024  # 10 MB cap — reject larger (fail closed)

# In-use cross-region inference-profile IDs (MEMORY.md / bedrock-model-access). The
# refreshed catalog must keep these priced and within a sane band of the baked-in
# rates. These are chat/completion models with nonzero input AND output rates, so the
# >0 check below is anchor-specific by design (token-priced embeddings have output 0).
_REQUIRED_MODEL_IDS = (
    "us.anthropic.claude-sonnet-4-6",
    "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    # bedrock-mantle anchor. Without it, an upstream regression that drops the
    # bedrock_mantle family would pass validate_catalog (the 80%-shrink guard barely
    # moves for 13 of 300+ entries), go live unpriced, and bill every Responses request
    # at the Opus-tier fallback — a ~11x over-count, silent except for a log field.
    "mantle/openai.gpt-5.6-luna",
)


def fetch_litellm() -> dict:
    """GET the litellm price map. Fails closed: HTTP 200 only, size-capped, must parse."""
    req = urllib.request.Request(
        LITELLM_SOURCE_URL, headers={"User-Agent": "bedrock-api-proxy/pricing-refresh"}
    )
    with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT_S) as resp:  # noqa: S310 (fixed https URL)
        if resp.status != 200:
            raise ValueError(f"litellm fetch returned HTTP {resp.status}")
        body = resp.read(_MAX_BODY_BYTES + 1)
    if len(body) > _MAX_BODY_BYTES:
        raise ValueError(f"litellm response exceeds {_MAX_BODY_BYTES} byte cap")
    return json.loads(body)


def validate_catalog(catalog: dict) -> None:
    """Raise ValueError if the rebuilt catalog is unsafe to make live (see DD6)."""
    if not catalog:
        raise ValueError("rebuilt catalog is empty")
    if len(catalog) < 0.8 * len(DEFAULT_PRICING):
        raise ValueError(
            f"rebuilt catalog shrank too far ({len(catalog)} < 80% of {len(DEFAULT_PRICING)})"
        )
    for mid in _REQUIRED_MODEL_IDS:
        if mid not in catalog:
            raise ValueError(f"required model {mid!r} missing from rebuilt catalog")
        rates = catalog[mid]["on_demand"]
        base = DEFAULT_PRICING[mid]["on_demand"]
        for key in ("input_usd_micros_per_1k", "output_usd_micros_per_1k"):
            value = rates.get(key, 0)
            if value <= 0:
                raise ValueError(f"{mid}.{key} is non-positive ({value})")
            if not (base[key] / 10 <= value <= base[key] * 10):
                raise ValueError(f"{mid}.{key}={value} outside 0.1x-10x of baked-in {base[key]}")


def refresh_pricing(s3) -> dict:
    """Fetch litellm → build → validate → write S3. Returns a summary; raises on failure."""
    raw = fetch_litellm()
    catalog = build_catalog(raw)
    validate_catalog(catalog)
    meta = {
        "fetched_at": datetime.now(UTC).isoformat(),
        "source": LITELLM_SOURCE_URL,
        "source_commit": None,
        "entry_count": len(catalog),
    }
    save_live_catalog(s3, catalog, meta)
    return {
        "entry_count": len(catalog),
        "fetched_at": meta["fetched_at"],
        "anchors": {mid: catalog[mid]["on_demand"] for mid in _REQUIRED_MODEL_IDS},
    }
