"""Tests for the GET /usage self-service endpoint in lambda/proxy/app.py.

Migrated from handler-based tests to FastAPI TestClient.
"""

import time
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import patch

import pytest
from botocore.exceptions import ClientError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _current_period() -> str:
    return datetime.now(UTC).strftime("%Y-%m")


def _auth_headers(bearer_token: str) -> dict:
    return {"Authorization": f"Bearer {bearer_token}"}


def _set_limits(tokens_table, token_id: str, **kwargs):
    attr_map = {
        "monthly_requests": "limit_monthly_requests",
        "monthly_usd_micros": "limit_monthly_usd_micros",
        "max_input_tokens": "limit_max_input_tokens",
        "max_output_tokens": "limit_max_output_tokens",
        "rps": "limit_rps",
    }
    set_parts, values = [], {}
    for k, v in kwargs.items():
        placeholder = f":v{k}"
        set_parts.append(f"{attr_map[k]} = {placeholder}")
        values[placeholder] = Decimal(str(v))
    tokens_table.update_item(
        Key={"token_id": token_id},
        UpdateExpression="SET " + ", ".join(set_parts),
        ExpressionAttributeValues=values,
    )


def _seed_usage(usage_table, token_id: str, **kwargs):
    item = {"token_id": token_id, "period": _current_period()}
    item.update({k: Decimal(str(v)) for k, v in kwargs.items()})
    usage_table.put_item(Item=item)


@pytest.fixture()
def usage_client(test_token, bedrock_stub):
    """TestClient with dependency_overrides cleared at teardown."""
    from app import app
    from deps import get_bedrock, get_tables
    from fastapi.testclient import TestClient

    token_id, bearer_token, tables = test_token
    client_bedrock, _stubber = bedrock_stub
    app.dependency_overrides[get_tables] = lambda: tables
    app.dependency_overrides[get_bedrock] = lambda: client_bedrock
    http_client = TestClient(app, raise_server_exceptions=False)
    yield http_client, token_id, bearer_token, tables
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_no_usage_no_limits(usage_client):
    http_client, token_id, bearer_token, tables = usage_client
    resp = http_client.get("/usage", headers=_auth_headers(bearer_token))

    assert resp.status_code == 200
    body = resp.json()
    assert body["period"] == _current_period()
    assert body["usage"] == {
        "requests": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_write_input_tokens": 0,
        "usd": 0.0,
    }
    assert body["limits"] == {
        "monthly_requests": None,
        "monthly_usd": None,
        "max_input_tokens": None,
        "max_output_tokens": None,
        "rps": None,
    }
    assert "remaining" not in body


def test_with_usage_and_limits(usage_client):
    http_client, token_id, bearer_token, tables = usage_client
    tokens_table, usage_table, _ = tables

    _set_limits(tokens_table, token_id, monthly_requests=500, monthly_usd_micros=5_000_000)
    _seed_usage(
        usage_table,
        token_id,
        requests=42,
        input_tokens=18400,
        output_tokens=7200,
        cache_read_input_tokens=3100,
        cache_write_input_tokens=800,
        usd_micros=312000,
    )

    resp = http_client.get("/usage", headers=_auth_headers(bearer_token))

    assert resp.status_code == 200
    body = resp.json()
    assert body["period"] == _current_period()
    assert body["usage"] == {
        "requests": 42,
        "input_tokens": 18400,
        "output_tokens": 7200,
        "cache_read_input_tokens": 3100,
        "cache_write_input_tokens": 800,
        "usd": 0.312,
    }
    assert body["limits"]["monthly_requests"] == 500
    assert body["limits"]["monthly_usd"] == 5.0
    assert body["remaining"]["requests"] == 458
    assert body["remaining"]["usd"] == 4.688


def test_remaining_clamped_to_zero(usage_client):
    http_client, token_id, bearer_token, tables = usage_client
    tokens_table, usage_table, _ = tables

    _set_limits(tokens_table, token_id, monthly_requests=500)
    _seed_usage(usage_table, token_id, requests=600, usd_micros=0)

    resp = http_client.get("/usage", headers=_auth_headers(bearer_token))

    assert resp.status_code == 200
    body = resp.json()
    assert body["remaining"]["requests"] == 0
    assert "usd" not in body["remaining"]


def test_only_one_monthly_limit(usage_client):
    http_client, token_id, bearer_token, tables = usage_client
    tokens_table, usage_table, _ = tables

    _set_limits(tokens_table, token_id, monthly_usd_micros=5_000_000)
    _seed_usage(usage_table, token_id, usd_micros=100_000)

    resp = http_client.get("/usage", headers=_auth_headers(bearer_token))

    assert resp.status_code == 200
    body = resp.json()
    assert "usd" in body["remaining"]
    assert "requests" not in body["remaining"]


def test_wrong_period_row_ignored(usage_client):
    http_client, token_id, bearer_token, tables = usage_client
    _, usage_table, _ = tables

    usage_table.put_item(
        Item={
            "token_id": token_id,
            "period": "2020-01",
            "requests": Decimal("99"),
            "usd_micros": Decimal("999999"),
        }
    )

    resp = http_client.get("/usage", headers=_auth_headers(bearer_token))

    assert resp.status_code == 200
    body = resp.json()
    assert body["usage"]["requests"] == 0
    assert body["usage"]["usd"] == 0.0


def test_invalid_token(usage_client):
    http_client, _, _, _ = usage_client
    resp = http_client.get("/usage", headers={"Authorization": "Bearer invalid.token"})

    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "INVALID_TOKEN"


def test_revoked_token(usage_client):
    http_client, token_id, bearer_token, tables = usage_client
    tokens_table, _, _ = tables

    tokens_table.update_item(
        Key={"token_id": token_id},
        UpdateExpression="SET #s = :r",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":r": "revoked"},
    )

    resp = http_client.get("/usage", headers=_auth_headers(bearer_token))

    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "INVALID_TOKEN"


def test_usage_does_not_write_usage_row(usage_client):
    http_client, token_id, bearer_token, tables = usage_client
    _, usage_table, _ = tables

    _seed_usage(usage_table, token_id, requests=5, usd_micros=1000)

    resp = http_client.get("/usage", headers=_auth_headers(bearer_token))

    assert resp.status_code == 200
    assert "period" in resp.json()

    row = usage_table.get_item(Key={"token_id": token_id, "period": _current_period()}).get(
        "Item", {}
    )
    assert int(row["requests"]) == 5
    assert int(row["usd_micros"]) == 1000


def test_usage_does_not_write_rate_limit(usage_client):
    http_client, token_id, bearer_token, tables = usage_client
    tokens_table, _, rate_limit_table = tables

    _set_limits(tokens_table, token_id, rps=1)
    now_second = int(time.time())
    rate_limit_table.put_item(
        Item={
            "token_id": token_id,
            "window_second": now_second,
            "count": Decimal("1"),
            "ttl": now_second + 10,
        }
    )

    resp = http_client.get("/usage", headers=_auth_headers(bearer_token))

    assert resp.status_code == 200
    assert "period" in resp.json()

    row = rate_limit_table.get_item(Key={"token_id": token_id, "window_second": now_second}).get(
        "Item", {}
    )
    assert int(row["count"]) == 1


def test_rps_zero_token_can_check_usage(usage_client):
    http_client, token_id, bearer_token, tables = usage_client
    tokens_table, _, _ = tables

    _set_limits(tokens_table, token_id, rps=0)

    resp = http_client.get("/usage", headers=_auth_headers(bearer_token))

    assert resp.status_code == 200
    assert "period" in resp.json()


def test_post_usage_not_dispatched(usage_client):
    """POST /usage → 405 Method Not Allowed (FastAPI behaviour change from handler's 400)."""
    http_client, _, bearer_token, _ = usage_client
    resp = http_client.post("/usage", headers=_auth_headers(bearer_token))

    assert resp.status_code == 405


def test_get_model_path_not_dispatched(usage_client):
    """GET /model/x/badroute → 404 (FastAPI returns 404 for unregistered paths)."""
    http_client, _, bearer_token, _ = usage_client
    resp = http_client.get("/model/somemodel/badroute", headers=_auth_headers(bearer_token))

    assert resp.status_code == 404


def test_usage_getitem_failure_returns_500(usage_client):
    http_client, token_id, bearer_token, tables = usage_client
    _, usage_table, _ = tables

    error_response = {"Error": {"Code": "InternalServerError", "Message": "DynamoDB unavailable"}}
    with patch.object(usage_table, "get_item", side_effect=ClientError(error_response, "GetItem")):
        resp = http_client.get("/usage", headers=_auth_headers(bearer_token))

    assert resp.status_code == 500
    assert resp.json()["error"]["code"] == "INTERNAL_ERROR"
