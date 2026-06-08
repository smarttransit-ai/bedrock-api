"""S3-backed live pricing catalog — the single runtime source of pricing.

The live catalog is one JSON object in S3 (``PRICING_BUCKET`` / ``PRICING_OBJECT_KEY``):
    {"catalog": {<model_id>: {on_demand, batch}}, "meta": {...}}

``pricing.py`` reads it (TTL-cached) and falls back to the baked-in DEFAULT_PRICING
when it is absent or unreachable. ``pricing_refresh.py`` writes it.
"""

from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger(__name__)

PRICING_OBJECT_KEY = os.environ.get("PRICING_OBJECT_KEY", "pricing/current.json")


def load_live_catalog(s3) -> dict | None:
    """Return the live catalog dict, or None if the object is authoritatively absent.

    Returns None only for an authoritative "no object" (NoSuchKey/NoSuchBucket).
    RAISES on any transient/unreachable/corrupt condition (other ClientError,
    timeout, unparseable body, missing ``catalog`` key) so the caller can apply a
    short retry cadence — a corrupt object is treated like a transient fault (fall
    back to baked-in rates and re-check soon, rather than permanently masking it).
    """
    from botocore.exceptions import ClientError

    try:
        resp = s3.get_object(Bucket=os.environ["PRICING_BUCKET"], Key=PRICING_OBJECT_KEY)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code in ("NoSuchKey", "NoSuchBucket", "404"):
            return None
        raise
    data = json.loads(resp["Body"].read())
    return data["catalog"]


def save_live_catalog(s3, catalog: dict, meta: dict) -> None:
    """Write the live catalog object (full replace)."""
    s3.put_object(
        Bucket=os.environ["PRICING_BUCKET"],
        Key=PRICING_OBJECT_KEY,
        Body=json.dumps({"catalog": catalog, "meta": meta}).encode("utf-8"),
        ContentType="application/json",
    )
