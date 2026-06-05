"""Tests for lambda/proxy/app.py (FastAPI application).

Replaces test_handler.py. Uses FastAPI TestClient (httpx-based) for HTTP-level
testing, moto for DynamoDB, and botocore Stubber for bedrock-runtime.

All 13 tests from test_handler.py are ported here, plus the output-cap parity
test (amendment B5).
"""

import io
import json
import logging
import time
from datetime import UTC, datetime

from botocore.exceptions import ParamValidationError
from conftest import DEFAULT_MODEL, ENCODED_MODEL, converse_response

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _current_period() -> str:
    return datetime.now(UTC).strftime("%Y-%m")


def _converse_path() -> str:
    return f"/model/{ENCODED_MODEL}/converse"


def _invoke_path() -> str:
    return f"/model/{ENCODED_MODEL}/invoke"


def _auth_headers(bearer_token: str) -> dict:
    return {"Authorization": f"Bearer {bearer_token}"}


def _converse_body(prompt: str = "Hello") -> dict:
    return {"messages": [{"role": "user", "content": [{"text": prompt}]}]}


def _invoke_body(prompt: str = "Hello") -> dict:
    return {
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 256,
        "anthropic_version": "bedrock-2023-05-31",
    }


def _invoke_response(in_tokens: int = 10, out_tokens: int = 5) -> dict:
    """Build a minimal Bedrock InvokeModel response for the Stubber."""
    body_bytes = json.dumps(
        {
            "content": [{"type": "text", "text": "Hello!"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": in_tokens, "output_tokens": out_tokens},
        }
    ).encode()
    return {
        "contentType": "application/json",
        "body": io.BytesIO(body_bytes),
    }


def _add_bedrock_success(stubber, in_tokens: int = 10, out_tokens: int = 5) -> None:
    stubber.add_response("converse", converse_response(in_tokens=in_tokens, out_tokens=out_tokens))


# ---------------------------------------------------------------------------
# Test 1: happy path — converse
# ---------------------------------------------------------------------------


def test_happy_path(app_client):
    """Valid token + allowed model + under all limits → 200 + usage row updated."""
    http_client, token_id, bearer_token, tables, stubber = app_client
    _, usage_table, _ = tables

    _add_bedrock_success(stubber, in_tokens=10, out_tokens=5)

    with stubber:
        resp = http_client.post(
            _converse_path(), json=_converse_body(), headers=_auth_headers(bearer_token)
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["stopReason"] == "end_turn"

    usage = usage_table.get_item(Key={"token_id": token_id, "period": _current_period()}).get(
        "Item", {}
    )
    assert int(usage["requests"]) == 1
    assert int(usage["input_tokens"]) == 10
    assert int(usage["output_tokens"]) == 5
    assert int(usage["cache_read_input_tokens"]) == 0
    assert int(usage["cache_write_input_tokens"]) == 0
    assert int(usage["usd_micros"]) > 0


# ---------------------------------------------------------------------------
# Test 2: happy path with cache tokens
# ---------------------------------------------------------------------------


def test_happy_path_with_cache_tokens(app_client):
    http_client, token_id, bearer_token, tables, stubber = app_client
    _, usage_table, _ = tables

    stubber.add_response(
        "converse",
        converse_response(in_tokens=10, out_tokens=5, cache_read_tokens=100, cache_write_tokens=20),
    )

    with stubber:
        resp = http_client.post(
            _converse_path(), json=_converse_body(), headers=_auth_headers(bearer_token)
        )

    assert resp.status_code == 200
    usage = usage_table.get_item(Key={"token_id": token_id, "period": _current_period()}).get(
        "Item", {}
    )
    assert int(usage["cache_read_input_tokens"]) == 100
    assert int(usage["cache_write_input_tokens"]) == 20


# ---------------------------------------------------------------------------
# Test 3: invalid pricing mode → 500
# ---------------------------------------------------------------------------


def test_invalid_pricing_mode_rejected(app_client):
    http_client, token_id, bearer_token, tables, stubber = app_client
    tokens_table, _, _ = tables

    tokens_table.update_item(
        Key={"token_id": token_id},
        UpdateExpression="SET pricing_mode = :v",
        ExpressionAttributeValues={":v": "oops"},
    )

    with stubber:
        resp = http_client.post(
            _converse_path(), json=_converse_body(), headers=_auth_headers(bearer_token)
        )

    assert resp.status_code == 500


# ---------------------------------------------------------------------------
# Test 4: revoked token → 401
# ---------------------------------------------------------------------------


def test_revoked_token(app_client):
    """Token with status='revoked' is rejected before hitting Bedrock."""
    http_client, token_id, bearer_token, tables, stubber = app_client
    tokens_table, _, _ = tables

    from conftest import _make_token

    new_token_id, new_bearer_token, new_secret_hash = _make_token()
    tokens_table.put_item(
        Item={
            "token_id": new_token_id,
            "secret_hash": new_secret_hash,
            "owner": "alice",
            "created_at": "2026-01-01T00:00:00Z",
            "status": "revoked",
        }
    )

    with stubber:
        resp = http_client.post(
            _converse_path(),
            json=_converse_body(),
            headers=_auth_headers(new_bearer_token),
        )

    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "INVALID_TOKEN"
    stubber.assert_no_pending_responses()


# ---------------------------------------------------------------------------
# Test 5: unknown token → 401
# ---------------------------------------------------------------------------


def test_unknown_token(app_client):
    """Token ID not in DynamoDB returns 401."""
    http_client, _, _, _, stubber = app_client

    with stubber:
        resp = http_client.post(
            _converse_path(),
            json=_converse_body(),
            headers={"Authorization": "Bearer bk_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa." + "b" * 64},
        )

    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "INVALID_TOKEN"
    stubber.assert_no_pending_responses()


# ---------------------------------------------------------------------------
# Test 6: over rate limit → 429
# ---------------------------------------------------------------------------


def test_over_rate_limit(app_client):
    """Pre-filled rate_limit counter at limit → 429."""
    http_client, token_id, bearer_token, tables, stubber = app_client
    tokens_table, _, rate_limit_table = tables

    tokens_table.update_item(
        Key={"token_id": token_id},
        UpdateExpression="SET limit_rps = :v",
        ExpressionAttributeValues={":v": 2},
    )

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

    with stubber:
        resp = http_client.post(
            _converse_path(), json=_converse_body(), headers=_auth_headers(bearer_token)
        )

    assert resp.status_code == 429
    assert resp.json()["error"]["code"] == "RATE_LIMIT_EXCEEDED"
    stubber.assert_no_pending_responses()


# ---------------------------------------------------------------------------
# Test 7: over monthly request quota → 429
# ---------------------------------------------------------------------------


def test_over_monthly_requests(app_client):
    """usage.requests == limit → 429 MONTHLY_REQUEST_QUOTA_EXCEEDED."""
    http_client, token_id, bearer_token, tables, stubber = app_client
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

    with stubber:
        resp = http_client.post(
            _converse_path(), json=_converse_body(), headers=_auth_headers(bearer_token)
        )

    assert resp.status_code == 429
    assert resp.json()["error"]["code"] == "MONTHLY_REQUEST_QUOTA_EXCEEDED"
    stubber.assert_no_pending_responses()


# ---------------------------------------------------------------------------
# Test 8: over monthly budget → 429
# ---------------------------------------------------------------------------


def test_over_monthly_budget(app_client):
    """usage.usd_micros == limit → 429 MONTHLY_BUDGET_EXCEEDED."""
    http_client, token_id, bearer_token, tables, stubber = app_client
    tokens_table, usage_table, _ = tables

    tokens_table.update_item(
        Key={"token_id": token_id},
        UpdateExpression="SET limit_monthly_usd_micros = :v",
        ExpressionAttributeValues={":v": 50_000_000},
    )
    usage_table.put_item(
        Item={
            "token_id": token_id,
            "period": _current_period(),
            "requests": 1,
            "input_tokens": 0,
            "output_tokens": 0,
            "usd_micros": 50_000_000,
        }
    )

    with stubber:
        resp = http_client.post(
            _converse_path(), json=_converse_body(), headers=_auth_headers(bearer_token)
        )

    assert resp.status_code == 429
    assert resp.json()["error"]["code"] == "MONTHLY_BUDGET_EXCEEDED"
    stubber.assert_no_pending_responses()


# ---------------------------------------------------------------------------
# Test 9: input token cap exceeded → 413
# ---------------------------------------------------------------------------


def test_input_token_cap_exceeded(app_client):
    """Prompt larger than limit_max_input_tokens (heuristic) → 413."""
    http_client, token_id, bearer_token, tables, stubber = app_client
    tokens_table, _, _ = tables

    # cap = 1 token; prompt is "Hello" (5 chars → ceil(5/4)=2 tokens > 1)
    tokens_table.update_item(
        Key={"token_id": token_id},
        UpdateExpression="SET limit_max_input_tokens = :v",
        ExpressionAttributeValues={":v": 1},
    )

    with stubber:
        resp = http_client.post(
            _converse_path(),
            json=_converse_body(prompt="Hello"),
            headers=_auth_headers(bearer_token),
        )

    assert resp.status_code == 413
    assert resp.json()["error"]["code"] == "INPUT_TOKEN_LIMIT_EXCEEDED"
    stubber.assert_no_pending_responses()


# ---------------------------------------------------------------------------
# Test 10: model not in allowlist → 403
# ---------------------------------------------------------------------------


def test_model_not_allowed(app_client):
    """Model ID not in allowed_models String Set → 403."""
    http_client, token_id, bearer_token, tables, stubber = app_client
    tokens_table, _, _ = tables

    tokens_table.update_item(
        Key={"token_id": token_id},
        UpdateExpression="SET allowed_models = :v",
        ExpressionAttributeValues={":v": {"us.anthropic.claude-haiku-4-5-20251001-v1:0"}},
    )

    with stubber:
        resp = http_client.post(
            _converse_path(), json=_converse_body(), headers=_auth_headers(bearer_token)
        )

    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "MODEL_NOT_ALLOWED"
    stubber.assert_no_pending_responses()


# ---------------------------------------------------------------------------
# Test 11: Bedrock throttle → 429
# ---------------------------------------------------------------------------


def test_bedrock_throttle(app_client):
    """Bedrock ThrottlingException is mapped to 429 BEDROCK_THROTTLED."""
    http_client, _, bearer_token, _, stubber = app_client

    stubber.add_client_error(
        "converse",
        service_error_code="ThrottlingException",
        service_message="Rate exceeded",
        http_status_code=429,
    )

    with stubber:
        resp = http_client.post(
            _converse_path(), json=_converse_body(), headers=_auth_headers(bearer_token)
        )

    assert resp.status_code == 429
    assert resp.json()["error"]["code"] == "BEDROCK_THROTTLED"


# ---------------------------------------------------------------------------
# Test 12: invalid Bedrock request body → 400 BAD_REQUEST
# ---------------------------------------------------------------------------


def test_bedrock_param_validation_maps_to_bad_request(app_client):
    """Bedrock SDK parameter validation errors are mapped to 400 BAD_REQUEST."""
    http_client, _, bearer_token, tables, stubber = app_client

    from app import app
    from deps import get_bedrock

    class _ClientWithInvalidParams:
        def converse(self, **kwargs):
            raise ParamValidationError(report="Missing required parameter: messages")

    app.dependency_overrides[get_bedrock] = lambda: _ClientWithInvalidParams()
    try:
        resp = http_client.post(
            _converse_path(),
            json={"messages": [{"role": "user", "content": []}]},
            headers=_auth_headers(bearer_token),
        )
    finally:
        app.dependency_overrides[get_bedrock] = lambda: stubber.client  # restore stubber client

    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "BAD_REQUEST"


# ---------------------------------------------------------------------------
# Test 13: usage ADD failure → still 200, error logged
# ---------------------------------------------------------------------------


def test_usage_write_failure(app_client, caplog):
    """If the usage write fails after a successful Bedrock call, still return 200."""
    http_client, token_id, bearer_token, tables, stubber = app_client
    tokens_table, usage_table, rate_limit_table = tables

    from app import app
    from deps import get_tables

    class _FailingUsageTable:
        def get_item(self, **kwargs):
            return usage_table.get_item(**kwargs)

        def update_item(self, **kwargs):
            raise RuntimeError("Simulated DynamoDB write failure")

    failing_tables = (tokens_table, _FailingUsageTable(), rate_limit_table)
    app.dependency_overrides[get_tables] = lambda: failing_tables

    try:
        _add_bedrock_success(stubber)
        with stubber, caplog.at_level(logging.ERROR):
            resp = http_client.post(
                _converse_path(), json=_converse_body(), headers=_auth_headers(bearer_token)
            )
    finally:
        app.dependency_overrides[get_tables] = lambda: tables

    assert resp.status_code == 200
    assert any("usage_write_failed" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Test 14: output-cap parity (amendment B5)
# ---------------------------------------------------------------------------


def test_output_cap_converse(app_client):
    """limit_max_output_tokens clamps inferenceConfig.maxTokens on /converse."""
    from unittest.mock import patch

    http_client, token_id, bearer_token, tables, stubber = app_client
    tokens_table, _, _ = tables

    tokens_table.update_item(
        Key={"token_id": token_id},
        UpdateExpression="SET limit_max_output_tokens = :v",
        ExpressionAttributeValues={":v": 50},
    )

    captured = {}

    def _fake_forward_converse(client, model_id, body):
        captured["body"] = body
        # Return minimal usage dict
        return {"stopReason": "end_turn", "output": {}}, {
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_read_input_tokens": 0,
            "cache_write_input_tokens": 0,
        }

    with patch("app.forward_converse", side_effect=_fake_forward_converse):
        resp = http_client.post(
            _converse_path(),
            # Request with maxTokens=200 but cap is 50
            json={
                "messages": [{"role": "user", "content": [{"text": "Hello"}]}],
                "inferenceConfig": {"maxTokens": 200},
            },
            headers=_auth_headers(bearer_token),
        )

    assert resp.status_code == 200
    # The clamped value (50) must have been sent to Bedrock
    assert captured["body"].get("inferenceConfig", {}).get("maxTokens") == 50


def test_output_cap_invoke(app_client):
    """limit_max_output_tokens clamps max_tokens on /invoke."""
    from unittest.mock import patch

    http_client, token_id, bearer_token, tables, stubber = app_client
    tokens_table, _, _ = tables

    tokens_table.update_item(
        Key={"token_id": token_id},
        UpdateExpression="SET limit_max_output_tokens = :v",
        ExpressionAttributeValues={":v": 30},
    )

    captured = {}

    def _fake_forward_invoke(client, model_id, body):
        captured["body"] = body
        return {"content": [{"type": "text", "text": "Hi"}], "stop_reason": "end_turn"}, {
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_read_input_tokens": 0,
            "cache_write_input_tokens": 0,
        }

    with patch("app.forward_invoke_model", side_effect=_fake_forward_invoke):
        resp = http_client.post(
            _invoke_path(),
            # Request with max_tokens=100 but cap is 30
            json={
                "messages": [{"role": "user", "content": "Hello"}],
                "max_tokens": 100,
                "anthropic_version": "bedrock-2023-05-31",
            },
            headers=_auth_headers(bearer_token),
        )

    assert resp.status_code == 200
    assert captured["body"].get("max_tokens") == 30


# ---------------------------------------------------------------------------
# Test 15: INFO logging level (B1) — request_complete logged at INFO
# ---------------------------------------------------------------------------


def test_info_logging_level_set(app_client, caplog):
    """app.py must set the root logger to INFO so request_complete events emit."""
    # The basicConfig + setLevel(INFO) call at module load must have propagated.
    assert logging.getLogger().getEffectiveLevel() <= logging.INFO

    http_client, token_id, bearer_token, tables, stubber = app_client
    _add_bedrock_success(stubber)

    with stubber, caplog.at_level(logging.INFO, logger="app"):
        resp = http_client.post(
            _converse_path(), json=_converse_body(), headers=_auth_headers(bearer_token)
        )

    assert resp.status_code == 200
    events = [r.message for r in caplog.records]
    assert any("request_complete" in e for e in events), (
        "request_complete INFO event not found in caplog"
    )


# ---------------------------------------------------------------------------
# Test 16: real /invoke happy path — usage row written + 200
# ---------------------------------------------------------------------------


def test_invoke_happy_path(app_client):
    """Real invoke_model Stubber response → 200 + usage row written (no forwarder patch)."""
    http_client, token_id, bearer_token, tables, stubber = app_client
    _, usage_table, _ = tables

    stubber.add_response("invoke_model", _invoke_response(in_tokens=12, out_tokens=6))

    with stubber:
        resp = http_client.post(
            _invoke_path(), json=_invoke_body(), headers=_auth_headers(bearer_token)
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["stop_reason"] == "end_turn"

    usage = usage_table.get_item(Key={"token_id": token_id, "period": _current_period()}).get(
        "Item", {}
    )
    assert int(usage["requests"]) == 1
    assert int(usage["input_tokens"]) == 12
    assert int(usage["output_tokens"]) == 6
    assert int(usage["usd_micros"]) > 0


# ---------------------------------------------------------------------------
# Test 17: /invoke usage-write failure → still 200 (no forwarder patch)
# ---------------------------------------------------------------------------


def test_invoke_usage_write_failure_still_200(app_client, caplog):
    """Usage write failure after a real invoke_model call must still return 200."""
    http_client, token_id, bearer_token, tables, stubber = app_client
    tokens_table, usage_table, rate_limit_table = tables

    from app import app
    from deps import get_tables

    class _FailingUsageTable:
        def get_item(self, **kwargs):
            return usage_table.get_item(**kwargs)

        def update_item(self, **kwargs):
            raise RuntimeError("Simulated DynamoDB write failure")

    failing_tables = (tokens_table, _FailingUsageTable(), rate_limit_table)
    app.dependency_overrides[get_tables] = lambda: failing_tables

    try:
        stubber.add_response("invoke_model", _invoke_response())
        with stubber, caplog.at_level(logging.ERROR):
            resp = http_client.post(
                _invoke_path(), json=_invoke_body(), headers=_auth_headers(bearer_token)
            )
    finally:
        app.dependency_overrides[get_tables] = lambda: tables

    assert resp.status_code == 200
    assert any("usage_write_failed" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Test 18: %3A colon-encoding — decoded model_id reaches Bedrock (R3c)
# ---------------------------------------------------------------------------


def test_percent_encoded_colon_decoded_for_bedrock(app_client):
    """%3A in the URL path is decoded to ':' before being forwarded to Bedrock.

    The Stubber is pre-configured expecting the DECODED model id (DEFAULT_MODEL
    with a literal colon). If FastAPI passed the encoded form to forward_converse,
    the Stubber would raise an assertion error (unexpected model id).
    """
    http_client, token_id, bearer_token, tables, stubber = app_client

    # Stubber expects invoke with modelId = decoded model id (colon, not %3A).
    # botocore Stubber validates expected params against actual call params.
    # Note: bedrock.py passes body as json.dumps(body) (a str); botocore's
    # Stubber receives it as-is before serialisation, so expected body is str.
    stubber.add_response(
        "invoke_model",
        _invoke_response(in_tokens=5, out_tokens=3),
        expected_params={
            "modelId": DEFAULT_MODEL,
            "body": json.dumps(_invoke_body()),
            "contentType": "application/json",
            "accept": "application/json",
        },
    )

    with stubber:
        resp = http_client.post(
            _invoke_path(),  # path contains %3A
            json=_invoke_body(),
            headers=_auth_headers(bearer_token),
        )

    assert resp.status_code == 200
