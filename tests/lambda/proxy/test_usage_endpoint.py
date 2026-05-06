"""Tests for the GET /usage self-service endpoint in lambda/proxy/handler.py."""

import json
import time
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import patch

from botocore.exceptions import ClientError
from conftest import make_event
from handler import handler

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _current_period() -> str:
    return datetime.now(UTC).strftime("%Y-%m")


def _usage_event(bearer_token: str) -> dict:
    return make_event("/usage", {}, bearer_token, method="GET")


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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_no_usage_no_limits(test_token):
    token_id, bearer_token, tables = test_token
    resp = handler(_usage_event(bearer_token), None, _tables=tables)

    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["period"] == _current_period()
    assert body["usage"] == {
        "requests": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_write_input_tokens": 0,
        "usd_micros": 0,
    }
    assert body["limits"] == {
        "monthly_requests": None,
        "monthly_usd_micros": None,
        "max_input_tokens": None,
        "max_output_tokens": None,
        "rps": None,
    }
    assert "remaining" not in body


def test_with_usage_and_limits(test_token):
    token_id, bearer_token, tables = test_token
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

    resp = handler(_usage_event(bearer_token), None, _tables=tables)

    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["period"] == _current_period()
    assert body["usage"] == {
        "requests": 42,
        "input_tokens": 18400,
        "output_tokens": 7200,
        "cache_read_input_tokens": 3100,
        "cache_write_input_tokens": 800,
        "usd_micros": 312000,
    }
    assert body["limits"]["monthly_requests"] == 500
    assert body["limits"]["monthly_usd_micros"] == 5_000_000
    assert body["remaining"]["requests"] == 458
    assert body["remaining"]["usd_micros"] == 4_688_000


def test_remaining_clamped_to_zero(test_token):
    token_id, bearer_token, tables = test_token
    tokens_table, usage_table, _ = tables

    _set_limits(tokens_table, token_id, monthly_requests=500)
    _seed_usage(usage_table, token_id, requests=600, usd_micros=0)

    resp = handler(_usage_event(bearer_token), None, _tables=tables)

    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["remaining"]["requests"] == 0
    assert "usd_micros" not in body["remaining"]


def test_only_one_monthly_limit(test_token):
    token_id, bearer_token, tables = test_token
    tokens_table, usage_table, _ = tables

    _set_limits(tokens_table, token_id, monthly_usd_micros=5_000_000)
    _seed_usage(usage_table, token_id, usd_micros=100_000)

    resp = handler(_usage_event(bearer_token), None, _tables=tables)

    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert "usd_micros" in body["remaining"]
    assert "requests" not in body["remaining"]


def test_wrong_period_row_ignored(test_token):
    token_id, bearer_token, tables = test_token
    _, usage_table, _ = tables

    usage_table.put_item(
        Item={
            "token_id": token_id,
            "period": "2020-01",
            "requests": Decimal("99"),
            "usd_micros": Decimal("999999"),
        }
    )

    resp = handler(_usage_event(bearer_token), None, _tables=tables)

    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["usage"]["requests"] == 0
    assert body["usage"]["usd_micros"] == 0


def test_invalid_token(test_token):
    _, _, tables = test_token
    event = make_event("/usage", {}, "Bearer invalid.token", method="GET")
    resp = handler(event, None, _tables=tables)

    assert resp["statusCode"] == 401
    assert json.loads(resp["body"])["error"]["code"] == "INVALID_TOKEN"


def test_revoked_token(test_token):
    token_id, bearer_token, tables = test_token
    tokens_table, _, _ = tables

    tokens_table.update_item(
        Key={"token_id": token_id},
        UpdateExpression="SET #s = :r",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":r": "revoked"},
    )

    resp = handler(_usage_event(bearer_token), None, _tables=tables)

    assert resp["statusCode"] == 401
    assert json.loads(resp["body"])["error"]["code"] == "INVALID_TOKEN"


def test_usage_does_not_write_usage_row(test_token):
    token_id, bearer_token, tables = test_token
    _, usage_table, _ = tables

    _seed_usage(usage_table, token_id, requests=5, usd_micros=1000)

    resp = handler(_usage_event(bearer_token), None, _tables=tables)

    assert resp["statusCode"] == 200
    assert "period" in json.loads(resp["body"])

    row = usage_table.get_item(Key={"token_id": token_id, "period": _current_period()}).get(
        "Item", {}
    )
    assert int(row["requests"]) == 5
    assert int(row["usd_micros"]) == 1000


def test_usage_does_not_write_rate_limit(test_token):
    token_id, bearer_token, tables = test_token
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

    resp = handler(_usage_event(bearer_token), None, _tables=tables)

    assert resp["statusCode"] == 200
    assert "period" in json.loads(resp["body"])

    row = rate_limit_table.get_item(Key={"token_id": token_id, "window_second": now_second}).get(
        "Item", {}
    )
    assert int(row["count"]) == 1


def test_rps_zero_token_can_check_usage(test_token):
    token_id, bearer_token, tables = test_token
    tokens_table, _, _ = tables

    _set_limits(tokens_table, token_id, rps=0)

    resp = handler(_usage_event(bearer_token), None, _tables=tables)

    assert resp["statusCode"] == 200
    assert "period" in json.loads(resp["body"])


def test_post_usage_not_dispatched(test_token):
    _, bearer_token, tables = test_token
    event = make_event("/usage", {}, bearer_token, method="POST")
    resp = handler(event, None, _tables=tables)

    assert resp["statusCode"] == 400
    assert json.loads(resp["body"])["error"]["code"] == "BAD_REQUEST"


def test_get_model_path_not_dispatched(test_token):
    """GET /model/somemodel/badroute — parse_route rejects unsupported suffix → 400.

    Only reachable via direct Lambda injection; no APIGW route exists for GET on model
    paths. Defense-in-depth unit test.
    """
    _, bearer_token, tables = test_token
    event = make_event("/model/somemodel/badroute", {}, bearer_token, method="GET")
    resp = handler(event, None, _tables=tables)

    assert resp["statusCode"] == 400
    assert json.loads(resp["body"])["error"]["code"] == "BAD_REQUEST"


def test_usage_getitem_failure_returns_500(test_token):
    token_id, bearer_token, tables = test_token
    _, usage_table, _ = tables

    error_response = {"Error": {"Code": "InternalServerError", "Message": "DynamoDB unavailable"}}
    with patch.object(usage_table, "get_item", side_effect=ClientError(error_response, "GetItem")):
        resp = handler(_usage_event(bearer_token), None, _tables=tables)

    assert resp["statusCode"] == 500
    assert json.loads(resp["body"])["error"]["code"] == "INTERNAL_ERROR"
