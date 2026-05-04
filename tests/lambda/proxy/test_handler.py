"""Tests for lambda/proxy/handler.py.

Uses moto for DynamoDB (real table creation via mock_aws in conftest) and
botocore.stub.Stubber for bedrock-runtime (boto3 not patched directly).
"""

import json
import time
from datetime import UTC

from conftest import (
    ENCODED_MODEL,
    converse_response,
    make_event,
)
from handler import handler

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _converse_path() -> str:
    return f"/model/{ENCODED_MODEL}/converse"


def _converse_event(bearer_token: str, prompt: str = "Hello") -> dict:
    return make_event(
        _converse_path(),
        {"messages": [{"role": "user", "content": [{"text": prompt}]}]},
        bearer_token,
    )


def _add_bedrock_success(stubber, in_tokens: int = 10, out_tokens: int = 5) -> None:
    stubber.add_response("converse", converse_response(in_tokens=in_tokens, out_tokens=out_tokens))


# ---------------------------------------------------------------------------
# Test 1: happy path
# ---------------------------------------------------------------------------


def test_happy_path(test_token, bedrock_stub):
    """Valid token + allowed model + under all limits → 200 + usage row updated."""
    token_id, bearer_token, tables = test_token
    _, usage_table, _ = tables
    client, stubber = bedrock_stub

    _add_bedrock_success(stubber, in_tokens=10, out_tokens=5)
    event = _converse_event(bearer_token)

    with stubber:
        resp = handler(event, None, _bedrock_client=client, _tables=tables)

    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["stopReason"] == "end_turn"

    # Usage row must be updated
    usage = usage_table.get_item(Key={"token_id": token_id, "period": _current_period()}).get(
        "Item", {}
    )
    assert int(usage["requests"]) == 1
    assert int(usage["input_tokens"]) == 10
    assert int(usage["output_tokens"]) == 5
    assert int(usage["usd_micros"]) > 0


# ---------------------------------------------------------------------------
# Test 2: revoked token → 401
# ---------------------------------------------------------------------------


def test_revoked_token(tables, bedrock_stub):
    """Token with status='revoked' is rejected before hitting Bedrock."""
    tokens_table, _, _ = tables
    from conftest import _make_token

    token_id, bearer_token, secret_hash = _make_token()
    tokens_table.put_item(
        Item={
            "token_id": token_id,
            "secret_hash": secret_hash,
            "owner": "alice",
            "created_at": "2026-01-01T00:00:00Z",
            "status": "revoked",
        }
    )
    client, stubber = bedrock_stub
    event = _converse_event(bearer_token)

    with stubber:
        resp = handler(event, None, _bedrock_client=client, _tables=tables)

    assert resp["statusCode"] == 401
    assert json.loads(resp["body"])["error"]["code"] == "INVALID_TOKEN"
    stubber.assert_no_pending_responses()


# ---------------------------------------------------------------------------
# Test 3: unknown token → 401
# ---------------------------------------------------------------------------


def test_unknown_token(tables, bedrock_stub):
    """Token ID not in DynamoDB returns 401."""
    client, stubber = bedrock_stub
    event = _converse_event("bk_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa." + "b" * 64)

    with stubber:
        resp = handler(event, None, _bedrock_client=client, _tables=tables)

    assert resp["statusCode"] == 401
    assert json.loads(resp["body"])["error"]["code"] == "INVALID_TOKEN"
    stubber.assert_no_pending_responses()


# ---------------------------------------------------------------------------
# Test 4: over rate limit → 429
# ---------------------------------------------------------------------------


def test_over_rate_limit(test_token, bedrock_stub):
    """Pre-filled rate_limit counter at limit → 429."""
    token_id, bearer_token, tables = test_token
    tokens_table, _, rate_limit_table = tables

    # Set limit_rps=2 on the token.
    tokens_table.update_item(
        Key={"token_id": token_id},
        UpdateExpression="SET limit_rps = :v",
        ExpressionAttributeValues={":v": 2},
    )

    # Pre-fill the current second (and the next, for robustness) at count=2.
    now_sec = int(time.time())
    for sec in (now_sec, now_sec + 1):
        rate_limit_table.put_item(
            Item={
                "token_id": token_id,
                "window_second": sec,
                "count": 2,
                "ttl": sec + 10,
            }
        )

    client, stubber = bedrock_stub
    event = _converse_event(bearer_token)

    with stubber:
        resp = handler(event, None, _bedrock_client=client, _tables=tables)

    assert resp["statusCode"] == 429
    assert json.loads(resp["body"])["error"]["code"] == "RATE_LIMIT_EXCEEDED"
    stubber.assert_no_pending_responses()


# ---------------------------------------------------------------------------
# Test 5: over monthly request quota → 429
# ---------------------------------------------------------------------------


def test_over_monthly_requests(test_token, bedrock_stub):
    """usage.requests == limit → 429 MONTHLY_REQUEST_QUOTA_EXCEEDED."""
    token_id, bearer_token, tables = test_token
    tokens_table, usage_table, _ = tables

    tokens_table.update_item(
        Key={"token_id": token_id},
        UpdateExpression="SET limit_monthly_requests = :v",
        ExpressionAttributeValues={":v": 100},
    )
    usage_table.put_item(
        Item={
            "token_id": token_id,
            "period": _current_period(),
            "requests": 100,
            "input_tokens": 0,
            "output_tokens": 0,
            "usd_micros": 0,
        }
    )

    client, stubber = bedrock_stub
    event = _converse_event(bearer_token)

    with stubber:
        resp = handler(event, None, _bedrock_client=client, _tables=tables)

    assert resp["statusCode"] == 429
    assert json.loads(resp["body"])["error"]["code"] == "MONTHLY_REQUEST_QUOTA_EXCEEDED"
    stubber.assert_no_pending_responses()


# ---------------------------------------------------------------------------
# Test 6: over monthly USD budget → 429
# ---------------------------------------------------------------------------


def test_over_monthly_budget(test_token, bedrock_stub):
    """usage.usd_micros == limit → 429 MONTHLY_BUDGET_EXCEEDED."""
    token_id, bearer_token, tables = test_token
    tokens_table, usage_table, _ = tables

    tokens_table.update_item(
        Key={"token_id": token_id},
        UpdateExpression="SET limit_monthly_usd_micros = :v",
        ExpressionAttributeValues={":v": 50_000_000},  # $50
    )
    usage_table.put_item(
        Item={
            "token_id": token_id,
            "period": _current_period(),
            "requests": 1,
            "input_tokens": 0,
            "output_tokens": 0,
            "usd_micros": 50_000_000,  # already at limit
        }
    )

    client, stubber = bedrock_stub
    event = _converse_event(bearer_token)

    with stubber:
        resp = handler(event, None, _bedrock_client=client, _tables=tables)

    assert resp["statusCode"] == 429
    assert json.loads(resp["body"])["error"]["code"] == "MONTHLY_BUDGET_EXCEEDED"
    stubber.assert_no_pending_responses()


# ---------------------------------------------------------------------------
# Test 7: input token cap exceeded → 413
# ---------------------------------------------------------------------------


def test_input_token_cap_exceeded(test_token, bedrock_stub):
    """Prompt larger than limit_max_input_tokens (heuristic) → 413."""
    token_id, bearer_token, tables = test_token
    tokens_table, _, _ = tables

    # cap = 1 token; prompt is "Hello" (5 chars → ceil(5/4)=2 tokens > 1)
    tokens_table.update_item(
        Key={"token_id": token_id},
        UpdateExpression="SET limit_max_input_tokens = :v",
        ExpressionAttributeValues={":v": 1},
    )

    client, stubber = bedrock_stub
    event = _converse_event(bearer_token, prompt="Hello")

    with stubber:
        resp = handler(event, None, _bedrock_client=client, _tables=tables)

    assert resp["statusCode"] == 413
    assert json.loads(resp["body"])["error"]["code"] == "INPUT_TOKEN_LIMIT_EXCEEDED"
    stubber.assert_no_pending_responses()


# ---------------------------------------------------------------------------
# Test 8: model not in allowlist → 403
# ---------------------------------------------------------------------------


def test_model_not_allowed(test_token, bedrock_stub):
    """Model ID not in allowed_models String Set → 403."""
    token_id, bearer_token, tables = test_token
    tokens_table, _, _ = tables

    tokens_table.update_item(
        Key={"token_id": token_id},
        UpdateExpression="SET allowed_models = :v",
        ExpressionAttributeValues={":v": {"us.anthropic.claude-haiku-4-5-20251001-v1:0"}},
    )

    client, stubber = bedrock_stub
    # Request targets Sonnet, but only Haiku is allowed.
    event = _converse_event(bearer_token)

    with stubber:
        resp = handler(event, None, _bedrock_client=client, _tables=tables)

    assert resp["statusCode"] == 403
    assert json.loads(resp["body"])["error"]["code"] == "MODEL_NOT_ALLOWED"
    stubber.assert_no_pending_responses()


# ---------------------------------------------------------------------------
# Test 9: Bedrock throttle → 429 passthrough
# ---------------------------------------------------------------------------


def test_bedrock_throttle(test_token, bedrock_stub):
    """Bedrock ThrottlingException is mapped to 429 BEDROCK_THROTTLED."""
    _, bearer_token, tables = test_token
    client, stubber = bedrock_stub

    stubber.add_client_error(
        "converse",
        service_error_code="ThrottlingException",
        service_message="Rate exceeded",
        http_status_code=429,
    )

    event = _converse_event(bearer_token)
    with stubber:
        resp = handler(event, None, _bedrock_client=client, _tables=tables)

    assert resp["statusCode"] == 429
    assert json.loads(resp["body"])["error"]["code"] == "BEDROCK_THROTTLED"


# ---------------------------------------------------------------------------
# Test 10: usage ADD failure → still 200, error logged
# ---------------------------------------------------------------------------


def test_usage_write_failure(test_token, bedrock_stub, caplog):
    """If the usage write fails after a successful Bedrock call, still return 200."""
    token_id, bearer_token, tables = test_token
    tokens_table, usage_table, rate_limit_table = tables
    client, stubber = bedrock_stub

    class _FailingUsageTable:
        """Wrapper: reads pass through; writes raise."""

        def get_item(self, **kwargs):
            return usage_table.get_item(**kwargs)

        def update_item(self, **kwargs):
            raise RuntimeError("Simulated DynamoDB write failure")

    failing_tables = (tokens_table, _FailingUsageTable(), rate_limit_table)

    _add_bedrock_success(stubber)
    event = _converse_event(bearer_token)

    import logging

    with stubber, caplog.at_level(logging.ERROR):
        resp = handler(event, None, _bedrock_client=client, _tables=failing_tables)

    # Client still receives the Bedrock response.
    assert resp["statusCode"] == 200
    # Error must have been logged.
    assert any("usage_write_failed" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def _current_period() -> str:
    from datetime import datetime

    return datetime.now(UTC).strftime("%Y-%m")
