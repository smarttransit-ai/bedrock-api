"""Tests for POST /admin/pricing/refresh."""

import boto3
import pricing_refresh
import pytest
from conftest import _make_token
from pricing import DEFAULT_PRICING
from pricing_store import load_live_catalog


def _raw_scaled(factor: float = 1.0) -> dict:
    """litellm-shaped map from DEFAULT_PRICING with rates scaled by ``factor`` (stays in-band)."""
    return {
        f"bedrock/{mid}": {
            "litellm_provider": "bedrock_converse",
            "input_cost_per_token": v["on_demand"]["input_usd_micros_per_1k"] / 1e9 * factor,
            "output_cost_per_token": v["on_demand"]["output_usd_micros_per_1k"] / 1e9 * factor,
        }
        for mid, v in DEFAULT_PRICING.items()
    }


@pytest.fixture()
def admin_client(tables, pricing_bucket):
    from app import app
    from deps import get_tables
    from fastapi.testclient import TestClient

    tokens_table, _, _ = tables
    aid, abearer, ahash = _make_token()
    tokens_table.put_item(
        Item={
            "token_id": aid,
            "secret_hash": ahash,
            "owner": "admin",
            "created_at": "2026-01-01T00:00:00Z",
            "status": "active",
            "admin": True,
        }
    )
    nid, nbearer, nhash = _make_token()
    tokens_table.put_item(
        Item={
            "token_id": nid,
            "secret_hash": nhash,
            "owner": "user",
            "created_at": "2026-01-01T00:00:00Z",
            "status": "active",
        }
    )
    app.dependency_overrides[get_tables] = lambda: tables
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, abearer, nbearer
    app.dependency_overrides.clear()


def test_no_token_401(admin_client):
    client, _, _ = admin_client
    assert client.post("/admin/pricing/refresh").status_code == 401


def test_non_admin_403(admin_client):
    client, _, nbearer = admin_client
    resp = client.post("/admin/pricing/refresh", headers={"Authorization": f"Bearer {nbearer}"})
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "FORBIDDEN"


def test_admin_happy_path_writes_and_busts_cache(admin_client, monkeypatch):
    from pricing import compute_cost

    client, abearer, _ = admin_client
    anchor = "us.anthropic.claude-sonnet-4-6"
    # Baseline (no live object yet) → served from DEFAULT_PRICING.
    base, _, _, _, _ = compute_cost(anchor, "on_demand", 1000, 1000, 0, 0)

    # Refresh with all rates doubled (in-band), then the new rates must be live.
    monkeypatch.setattr(pricing_refresh, "fetch_litellm", lambda: _raw_scaled(2.0))
    resp = client.post("/admin/pricing/refresh", headers={"Authorization": f"Bearer {abearer}"})
    assert resp.status_code == 200
    assert resp.json()["entry_count"] > 0
    assert load_live_catalog(boto3.client("s3", region_name="us-east-1")) is not None

    after, _, _, _, _ = compute_cost(anchor, "on_demand", 1000, 1000, 0, 0)
    assert after == base * 2  # cache was busted; the live (doubled) catalog is served


def test_admin_fetch_failure_502_pricing_unchanged(admin_client, monkeypatch):
    client, abearer, _ = admin_client

    def boom():
        raise RuntimeError("down")

    monkeypatch.setattr(pricing_refresh, "fetch_litellm", boom)
    resp = client.post("/admin/pricing/refresh", headers={"Authorization": f"Bearer {abearer}"})
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "REFRESH_FAILED"
    assert load_live_catalog(boto3.client("s3", region_name="us-east-1")) is None


# --- GET /admin/pricing (status) ---


def test_status_no_token_401(admin_client):
    client, _, _ = admin_client
    assert client.get("/admin/pricing").status_code == 401


def test_status_non_admin_403(admin_client):
    client, _, nbearer = admin_client
    resp = client.get("/admin/pricing", headers={"Authorization": f"Bearer {nbearer}"})
    assert resp.status_code == 403


def test_status_absent_reports_default(admin_client):
    client, abearer, _ = admin_client
    resp = client.get("/admin/pricing", headers={"Authorization": f"Bearer {abearer}"})
    assert resp.status_code == 200
    assert resp.json() == {"source": "default", "meta": None}


def test_status_after_refresh_reports_live(admin_client, monkeypatch):
    client, abearer, _ = admin_client
    monkeypatch.setattr(pricing_refresh, "fetch_litellm", lambda: _raw_scaled(1.0))
    client.post("/admin/pricing/refresh", headers={"Authorization": f"Bearer {abearer}"})
    resp = client.get("/admin/pricing", headers={"Authorization": f"Bearer {abearer}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "live"
    assert body["meta"]["entry_count"] > 0
    assert "fetched_at" in body["meta"]
