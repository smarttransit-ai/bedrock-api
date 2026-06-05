"""Tests for Phase B streaming routes and bedrock streaming helpers.

Covers (amendment R4):
  - converse-stream happy path: chunks relayed, usage from metadata, no double count
  - invoke-with-response-stream happy path: chunks relayed, cache tokens (B2)
  - pre-flight rejects BEFORE bytes: revoked token → 401, over-rate-limit → 429
  - call-time SDK error (open_* raises): ThrottlingException→429, generic ClientError→502,
    ParamValidationError→400 — on HTTP response, not 200 (amendment B1)
  - mid-stream EventStream error member → 200 + terminal SSE error frame (B6)
  - output-cap on streaming routes (B5)
  - generator-level disconnect: gen.close() after partial iteration; finally writes usage
    when populated and does NOT write when empty

Plus bedrock-level unit tests:
  - EventStream error-member mapper (_check_eventstream_error)
  - iter_invoke_stream cache token extraction (B2)
"""

import json
import time
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError, ParamValidationError
from conftest import DEFAULT_MODEL, ENCODED_MODEL, _make_token

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _current_period() -> str:
    return datetime.now(UTC).strftime("%Y-%m")


def _converse_stream_path() -> str:
    return f"/model/{ENCODED_MODEL}/converse-stream"


def _invoke_stream_path() -> str:
    return f"/model/{ENCODED_MODEL}/invoke-with-response-stream"


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


def _converse_stream_events(in_tokens: int = 10, out_tokens: int = 5) -> list[dict]:
    """Fake Converse EventStream events matching boto3 documented shapes."""
    return [
        {"messageStart": {"role": "assistant"}},
        {"contentBlockStart": {"start": {"text": ""}, "contentBlockIndex": 0}},
        {"contentBlockDelta": {"delta": {"text": "Hello"}, "contentBlockIndex": 0}},
        {"contentBlockStop": {"contentBlockIndex": 0}},
        {"messageStop": {"stopReason": "end_turn"}},
        {
            "metadata": {
                "usage": {
                    "inputTokens": in_tokens,
                    "outputTokens": out_tokens,
                    "totalTokens": in_tokens + out_tokens,
                },
                "metrics": {"latencyMs": 100},
            }
        },
    ]


def _converse_stream_events_with_cache(
    in_tokens: int = 10,
    out_tokens: int = 5,
    cache_read: int = 20,
    cache_write: int = 30,
) -> list[dict]:
    """Fake Converse EventStream events with cache token counts."""
    return [
        {"messageStart": {"role": "assistant"}},
        {"contentBlockDelta": {"delta": {"text": "Hi"}, "contentBlockIndex": 0}},
        {"messageStop": {"stopReason": "end_turn"}},
        {
            "metadata": {
                "usage": {
                    "inputTokens": in_tokens,
                    "outputTokens": out_tokens,
                    "cacheReadInputTokenCount": cache_read,
                    "cacheWriteInputTokenCount": cache_write,
                },
                "metrics": {"latencyMs": 50},
            }
        },
    ]


def _invoke_stream_events(in_tokens: int = 10, out_tokens: int = 5) -> list[bytes]:
    """Fake InvokeModel streaming chunks — each is a bytes value for chunk["bytes"]."""
    message_start = json.dumps(
        {
            "type": "message_start",
            "message": {
                "usage": {
                    "input_tokens": in_tokens,
                }
            },
        }
    ).encode()
    content_block_delta = json.dumps(
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hello"}}
    ).encode()
    message_delta = json.dumps(
        {"type": "message_delta", "usage": {"output_tokens": out_tokens}}
    ).encode()
    message_stop = json.dumps({"type": "message_stop", "stop_reason": "end_turn"}).encode()
    return [message_start, content_block_delta, message_delta, message_stop]


def _invoke_stream_events_with_cache(
    in_tokens: int = 10,
    out_tokens: int = 5,
    cache_read: int = 15,
    cache_write: int = 25,
) -> list[bytes]:
    """Fake invoke stream events including cache tokens in message_start."""
    message_start = json.dumps(
        {
            "type": "message_start",
            "message": {
                "usage": {
                    "input_tokens": in_tokens,
                    "cache_read_input_tokens": cache_read,
                    "cache_creation_input_tokens": cache_write,
                }
            },
        }
    ).encode()
    message_delta = json.dumps(
        {"type": "message_delta", "usage": {"output_tokens": out_tokens}}
    ).encode()
    message_stop = json.dumps({"type": "message_stop", "stop_reason": "end_turn"}).encode()
    return [message_start, message_delta, message_stop]


def _make_converse_stream_mock(events: list[dict]) -> MagicMock:
    """Return a mock bedrock client whose converse_stream returns fake events."""
    mock_client = MagicMock()
    mock_client.converse_stream.return_value = {"stream": iter(events)}
    return mock_client


def _make_invoke_stream_mock(chunk_bytes_list: list[bytes]) -> MagicMock:
    """Return a mock bedrock client whose invoke_model_with_response_stream returns fake chunks."""
    mock_client = MagicMock()
    # Each element of the body is {"chunk": {"bytes": <bytes>}}
    body_events = [{"chunk": {"bytes": b}} for b in chunk_bytes_list]
    mock_client.invoke_model_with_response_stream.return_value = {"body": iter(body_events)}
    return mock_client


def _parse_sse_frames(content: str) -> list[dict]:
    """Parse SSE-formatted text into a list of decoded JSON objects."""
    frames = []
    for line in content.splitlines():
        line = line.strip()
        if line.startswith("data: "):
            payload = line[len("data: ") :]
            frames.append(json.loads(payload))
    return frames


# ---------------------------------------------------------------------------
# app_client with overrideable bedrock mock
# ---------------------------------------------------------------------------


def _make_app_client_with_mock(test_token, mock_bedrock_client):
    """Build a TestClient with a custom bedrock mock (not a Stubber).

    O2: Returns a context manager (TestClient) that clears dependency_overrides
    at teardown when used as ``with http_client as client: ...``.
    Callers should always use it as a context manager so overrides are cleared,
    matching the pattern of the ``app_client`` fixture in conftest.
    """
    from app import app
    from deps import get_bedrock, get_tables
    from fastapi.testclient import TestClient

    token_id, bearer_token, tables = test_token
    app.dependency_overrides[get_tables] = lambda: tables
    app.dependency_overrides[get_bedrock] = lambda: mock_bedrock_client

    class _Client:
        """Thin wrapper that clears dependency_overrides on __exit__ (O2)."""

        def __init__(self):
            self._client = TestClient(app, raise_server_exceptions=False)

        def __enter__(self):
            return self._client.__enter__()

        def __exit__(self, *args):
            result = self._client.__exit__(*args)
            app.dependency_overrides.clear()
            return result

    http_client = _Client()
    return http_client, token_id, bearer_token, tables


# ===========================================================================
# Section 1: converse-stream happy path
# ===========================================================================


def test_converse_stream_happy_path(test_token):
    """Valid token → 200, SSE chunks relayed, usage row written from metadata."""
    events = _converse_stream_events(in_tokens=10, out_tokens=5)
    mock_client = _make_converse_stream_mock(events)
    http_client, token_id, bearer_token, tables = _make_app_client_with_mock(
        test_token, mock_client
    )
    _, usage_table, _ = tables

    with http_client as client:
        resp = client.post(
            _converse_stream_path(),
            json=_converse_body(),
            headers=_auth_headers(bearer_token),
        )

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")

    frames = _parse_sse_frames(resp.text)
    # At minimum: contentBlockDelta + metadata frames should be present
    frame_types = {list(f.keys())[0] for f in frames}
    assert "contentBlockDelta" in frame_types
    assert "metadata" in frame_types

    usage = usage_table.get_item(Key={"token_id": token_id, "period": _current_period()}).get(
        "Item", {}
    )
    assert int(usage["requests"]) == 1
    assert int(usage["input_tokens"]) == 10
    assert int(usage["output_tokens"]) == 5
    assert int(usage["usd_micros"]) > 0


def test_converse_stream_no_double_count(test_token):
    """Single metadata event → usage row written with exact token counts (no double count)."""
    events = _converse_stream_events(in_tokens=7, out_tokens=3)
    mock_client = _make_converse_stream_mock(events)
    http_client, token_id, bearer_token, tables = _make_app_client_with_mock(
        test_token, mock_client
    )
    _, usage_table, _ = tables

    with http_client as client:
        client.post(
            _converse_stream_path(),
            json=_converse_body(),
            headers=_auth_headers(bearer_token),
        )

    usage = usage_table.get_item(Key={"token_id": token_id, "period": _current_period()}).get(
        "Item", {}
    )
    assert int(usage["input_tokens"]) == 7
    assert int(usage["output_tokens"]) == 3


# ===========================================================================
# Section 2: invoke-with-response-stream happy path + cache tokens (B2)
# ===========================================================================


def test_invoke_stream_happy_path(test_token):
    """Valid token → 200, SSE chunks relayed, usage row written from message_start+delta."""
    chunks = _invoke_stream_events(in_tokens=12, out_tokens=6)
    mock_client = _make_invoke_stream_mock(chunks)
    http_client, token_id, bearer_token, tables = _make_app_client_with_mock(
        test_token, mock_client
    )
    _, usage_table, _ = tables

    with http_client as client:
        resp = client.post(
            _invoke_stream_path(),
            json=_invoke_body(),
            headers=_auth_headers(bearer_token),
        )

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")

    frames = _parse_sse_frames(resp.text)
    frame_types = {f.get("type") for f in frames}
    assert "content_block_delta" in frame_types

    usage = usage_table.get_item(Key={"token_id": token_id, "period": _current_period()}).get(
        "Item", {}
    )
    assert int(usage["input_tokens"]) == 12
    assert int(usage["output_tokens"]) == 6
    assert int(usage["usd_micros"]) > 0


def test_invoke_stream_cache_tokens_propagate(test_token):
    """Cache tokens from message_start reach the DynamoDB write (amendment B2)."""
    chunks = _invoke_stream_events_with_cache(
        in_tokens=10, out_tokens=5, cache_read=15, cache_write=25
    )
    mock_client = _make_invoke_stream_mock(chunks)
    http_client, token_id, bearer_token, tables = _make_app_client_with_mock(
        test_token, mock_client
    )
    _, usage_table, _ = tables

    with http_client as client:
        client.post(
            _invoke_stream_path(),
            json=_invoke_body(),
            headers=_auth_headers(bearer_token),
        )

    usage = usage_table.get_item(Key={"token_id": token_id, "period": _current_period()}).get(
        "Item", {}
    )
    assert int(usage["cache_read_input_tokens"]) == 15
    assert int(usage["cache_write_input_tokens"]) == 25
    # Main token counts also correct
    assert int(usage["input_tokens"]) == 10
    assert int(usage["output_tokens"]) == 5


# ===========================================================================
# Section 3: pre-flight rejects BEFORE bytes (amendment B1)
# ===========================================================================


def test_converse_stream_revoked_token_401(test_token):
    """Revoked token → 401 on HTTP response (not 200 + SSE)."""
    tokens_table, _, _ = test_token[2]
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

    mock_client = MagicMock()
    http_client, _, _, _ = _make_app_client_with_mock(test_token, mock_client)

    with http_client as client:
        resp = client.post(
            _converse_stream_path(),
            json=_converse_body(),
            headers=_auth_headers(new_bearer_token),
        )

    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "INVALID_TOKEN"
    mock_client.converse_stream.assert_not_called()


def test_converse_stream_over_rate_limit_429(test_token):
    """Pre-filled rate limit → 429 on HTTP response before any streaming."""
    token_id, bearer_token, tables = test_token
    tokens_table, _, rate_limit_table = tables

    tokens_table.update_item(
        Key={"token_id": token_id},
        UpdateExpression="SET limit_rps = :v",
        ExpressionAttributeValues={":v": 1},
    )
    now_sec = int(time.time())
    rate_limit_table.put_item(
        Item={"token_id": token_id, "window_second": now_sec, "count": 1, "ttl": now_sec + 10}
    )

    mock_client = MagicMock()
    http_client, _, _, _ = _make_app_client_with_mock(test_token, mock_client)

    with http_client as client:
        resp = client.post(
            _converse_stream_path(),
            json=_converse_body(),
            headers=_auth_headers(bearer_token),
        )

    assert resp.status_code == 429
    assert resp.json()["error"]["code"] == "RATE_LIMIT_EXCEEDED"
    mock_client.converse_stream.assert_not_called()


def test_invoke_stream_revoked_token_401(test_token):
    """Revoked token → 401 on /invoke-with-response-stream (preflight, not SSE)."""
    tokens_table, _, _ = test_token[2]
    new_token_id, new_bearer_token, new_secret_hash = _make_token()
    tokens_table.put_item(
        Item={
            "token_id": new_token_id,
            "secret_hash": new_secret_hash,
            "owner": "bob",
            "created_at": "2026-01-01T00:00:00Z",
            "status": "revoked",
        }
    )

    mock_client = MagicMock()
    http_client, _, _, _ = _make_app_client_with_mock(test_token, mock_client)

    with http_client as client:
        resp = client.post(
            _invoke_stream_path(),
            json=_invoke_body(),
            headers=_auth_headers(new_bearer_token),
        )

    assert resp.status_code == 401
    mock_client.invoke_model_with_response_stream.assert_not_called()


# ===========================================================================
# Section 4: call-time SDK errors → real 4xx/5xx (amendment B1)
# ===========================================================================


def _make_client_error(code: str, message: str = "err", http_status: int = 400) -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": message}, "ResponseMetadata": {}},
        "converse_stream",
    )


def test_converse_stream_throttle_before_response(test_token):
    """ThrottlingException from open_converse_stream → 429 on HTTP response (not 200)."""
    mock_client = MagicMock()
    mock_client.converse_stream.side_effect = _make_client_error(
        "ThrottlingException", "Rate exceeded", 429
    )
    http_client, _, bearer_token, _ = _make_app_client_with_mock(test_token, mock_client)

    with http_client as client:
        resp = client.post(
            _converse_stream_path(),
            json=_converse_body(),
            headers=_auth_headers(bearer_token),
        )

    assert resp.status_code == 429
    assert resp.json()["error"]["code"] == "BEDROCK_THROTTLED"


def test_converse_stream_client_error_502(test_token):
    """Generic ClientError from open_converse_stream → 502 on HTTP response."""
    mock_client = MagicMock()
    mock_client.converse_stream.side_effect = _make_client_error(
        "ServiceException", "Internal error", 500
    )
    http_client, _, bearer_token, _ = _make_app_client_with_mock(test_token, mock_client)

    with http_client as client:
        resp = client.post(
            _converse_stream_path(),
            json=_converse_body(),
            headers=_auth_headers(bearer_token),
        )

    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "BEDROCK_ERROR"


def test_converse_stream_param_validation_400(test_token):
    """ParamValidationError from open_converse_stream → 400 on HTTP response."""
    mock_client = MagicMock()
    mock_client.converse_stream.side_effect = ParamValidationError(report="Missing required param")
    http_client, _, bearer_token, _ = _make_app_client_with_mock(test_token, mock_client)

    with http_client as client:
        resp = client.post(
            _converse_stream_path(),
            json=_converse_body(),
            headers=_auth_headers(bearer_token),
        )

    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "BAD_REQUEST"


def test_invoke_stream_throttle_before_response(test_token):
    """ThrottlingException from open_invoke_stream → 429 on HTTP response."""
    mock_client = MagicMock()
    mock_client.invoke_model_with_response_stream.side_effect = _make_client_error(
        "ThrottlingException", "Rate exceeded", 429
    )
    http_client, _, bearer_token, _ = _make_app_client_with_mock(test_token, mock_client)

    with http_client as client:
        resp = client.post(
            _invoke_stream_path(),
            json=_invoke_body(),
            headers=_auth_headers(bearer_token),
        )

    assert resp.status_code == 429
    assert resp.json()["error"]["code"] == "BEDROCK_THROTTLED"


def test_invoke_stream_param_validation_400(test_token):
    """ParamValidationError from open_invoke_stream → 400 on HTTP response."""
    mock_client = MagicMock()
    mock_client.invoke_model_with_response_stream.side_effect = ParamValidationError(
        report="Invalid param"
    )
    http_client, _, bearer_token, _ = _make_app_client_with_mock(test_token, mock_client)

    with http_client as client:
        resp = client.post(
            _invoke_stream_path(),
            json=_invoke_body(),
            headers=_auth_headers(bearer_token),
        )

    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "BAD_REQUEST"


def test_invoke_stream_client_error_502(test_token):
    """Generic ClientError from open_invoke_stream → 502 on HTTP response."""
    mock_client = MagicMock()
    mock_client.invoke_model_with_response_stream.side_effect = _make_client_error(
        "ServiceException", "Internal error", 500
    )
    http_client, _, bearer_token, _ = _make_app_client_with_mock(test_token, mock_client)

    with http_client as client:
        resp = client.post(
            _invoke_stream_path(),
            json=_invoke_body(),
            headers=_auth_headers(bearer_token),
        )

    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "BEDROCK_ERROR"


# ===========================================================================
# Section 5: mid-stream EventStream error members → 200 + terminal SSE error frame (B6)
# ===========================================================================


def test_converse_stream_mid_stream_throttle(test_token):
    """Mid-stream throttlingException → 200 HTTP + terminal SSE error frame."""
    # Mix a good event then a throttle error member (real dict shape)
    events = [
        {"contentBlockDelta": {"delta": {"text": "Par"}, "contentBlockIndex": 0}},
        {"throttlingException": {"message": "Too many requests"}},
    ]
    mock_client = _make_converse_stream_mock(events)
    http_client, _, bearer_token, _ = _make_app_client_with_mock(test_token, mock_client)

    with http_client as client:
        resp = client.post(
            _converse_stream_path(),
            json=_converse_body(),
            headers=_auth_headers(bearer_token),
        )

    assert resp.status_code == 200  # Headers already sent
    frames = _parse_sse_frames(resp.text)
    error_frames = [f for f in frames if "error" in f]
    assert len(error_frames) >= 1
    assert error_frames[-1]["error"]["code"] == "BEDROCK_THROTTLED"


def test_converse_stream_mid_stream_model_error(test_token):
    """Mid-stream modelStreamErrorException → terminal SSE error frame with 502 code."""
    events = [
        {"contentBlockDelta": {"delta": {"text": "Hi"}, "contentBlockIndex": 0}},
        {"modelStreamErrorException": {"message": "Model stream error", "originalStatusCode": 500}},
    ]
    mock_client = _make_converse_stream_mock(events)
    http_client, _, bearer_token, _ = _make_app_client_with_mock(test_token, mock_client)

    with http_client as client:
        resp = client.post(
            _converse_stream_path(),
            json=_converse_body(),
            headers=_auth_headers(bearer_token),
        )

    assert resp.status_code == 200
    frames = _parse_sse_frames(resp.text)
    error_frames = [f for f in frames if "error" in f]
    assert len(error_frames) >= 1
    assert error_frames[-1]["error"]["code"] == "BEDROCK_STREAM_ERROR"


def test_invoke_stream_mid_stream_model_timeout(test_token):
    """Mid-stream modelTimeoutException in invoke stream → terminal SSE error frame (BB1)."""
    good_chunk = json.dumps(
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hi"}}
    ).encode()
    events = [
        {"chunk": {"bytes": good_chunk}},
        {"modelTimeoutException": {"message": "Model timed out"}},
    ]
    mock_client = MagicMock()
    mock_client.invoke_model_with_response_stream.return_value = {"body": iter(events)}
    http_client, _, bearer_token, _ = _make_app_client_with_mock(test_token, mock_client)

    with http_client as client:
        resp = client.post(
            _invoke_stream_path(),
            json=_invoke_body(),
            headers=_auth_headers(bearer_token),
        )

    assert resp.status_code == 200
    frames = _parse_sse_frames(resp.text)
    error_frames = [f for f in frames if "error" in f]
    assert len(error_frames) >= 1
    assert error_frames[-1]["error"]["code"] == "BEDROCK_ERROR"
    assert error_frames[-1]["error"]["message"] == "Model timed out"


def test_converse_stream_bytes_event_relayed_as_base64(test_token):
    """Bytes-valued event (e.g. redactedContent) is relayed as base64, not a crash (BB2)."""
    events = [
        # Simulates a reasoningContent.redactedContent or similar bytes field
        {
            "contentBlockDelta": {
                "delta": {"redactedContent": b"\x00\xff\xfe"},
                "contentBlockIndex": 0,
            }
        },
        {
            "metadata": {
                "usage": {"inputTokens": 3, "outputTokens": 2, "totalTokens": 5},
                "metrics": {"latencyMs": 10},
            }
        },
    ]
    mock_client = _make_converse_stream_mock(events)
    http_client, _, bearer_token, _ = _make_app_client_with_mock(test_token, mock_client)

    with http_client as client:
        resp = client.post(
            _converse_stream_path(),
            json=_converse_body(),
            headers=_auth_headers(bearer_token),
        )

    assert resp.status_code == 200
    frames = _parse_sse_frames(resp.text)
    # Should not contain a STREAM_ERROR frame — bytes were base64-encoded transparently
    error_frames = [f for f in frames if "error" in f]
    assert not error_frames, f"Unexpected error frames: {error_frames}"
    # The bytes field should be base64-encoded in the SSE frame
    import base64

    expected_b64 = base64.b64encode(b"\x00\xff\xfe").decode("ascii")
    delta_frames = [f for f in frames if "contentBlockDelta" in f]
    assert len(delta_frames) >= 1
    assert delta_frames[0]["contentBlockDelta"]["delta"]["redactedContent"] == expected_b64


def test_invoke_stream_mid_stream_throttle(test_token):
    """Mid-stream throttlingException in invoke stream → terminal SSE error frame."""
    good_chunk = json.dumps(
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hi"}}
    ).encode()
    # An EventStream error member doesn't have "chunk" key — it's top-level
    events = [
        {"chunk": {"bytes": good_chunk}},
        {"throttlingException": {"message": "Too many requests"}},
    ]
    mock_client = MagicMock()
    mock_client.invoke_model_with_response_stream.return_value = {"body": iter(events)}
    http_client, _, bearer_token, _ = _make_app_client_with_mock(test_token, mock_client)

    with http_client as client:
        resp = client.post(
            _invoke_stream_path(),
            json=_invoke_body(),
            headers=_auth_headers(bearer_token),
        )

    assert resp.status_code == 200
    frames = _parse_sse_frames(resp.text)
    error_frames = [f for f in frames if "error" in f]
    assert len(error_frames) >= 1
    assert error_frames[-1]["error"]["code"] == "BEDROCK_THROTTLED"


# ===========================================================================
# Section 6: output-cap on streaming routes (B5)
# ===========================================================================


def test_converse_stream_output_cap(test_token):
    """limit_max_output_tokens clamps inferenceConfig.maxTokens on converse-stream."""
    token_id, bearer_token, tables = test_token
    tokens_table, _, _ = tables

    tokens_table.update_item(
        Key={"token_id": token_id},
        UpdateExpression="SET limit_max_output_tokens = :v",
        ExpressionAttributeValues={":v": 50},
    )

    captured = {}

    def _fake_open_converse_stream(client, model_id, body):
        captured["body"] = body
        events = _converse_stream_events(in_tokens=5, out_tokens=3)
        return {"stream": iter(events)}

    mock_client = MagicMock()
    http_client, _, _, _ = _make_app_client_with_mock(test_token, mock_client)

    with patch("app.open_converse_stream", side_effect=_fake_open_converse_stream):
        with http_client as client:
            resp = client.post(
                _converse_stream_path(),
                json={
                    "messages": [{"role": "user", "content": [{"text": "Hi"}]}],
                    "inferenceConfig": {"maxTokens": 200},
                },
                headers=_auth_headers(bearer_token),
            )

    assert resp.status_code == 200
    assert captured["body"].get("inferenceConfig", {}).get("maxTokens") == 50


def test_invoke_stream_output_cap(test_token):
    """limit_max_output_tokens clamps max_tokens on invoke-with-response-stream."""
    token_id, bearer_token, tables = test_token
    tokens_table, _, _ = tables

    tokens_table.update_item(
        Key={"token_id": token_id},
        UpdateExpression="SET limit_max_output_tokens = :v",
        ExpressionAttributeValues={":v": 30},
    )

    captured = {}

    def _fake_open_invoke_stream(client, model_id, body):
        captured["body"] = body
        chunks = _invoke_stream_events(in_tokens=5, out_tokens=3)
        return {"body": iter([{"chunk": {"bytes": b}} for b in chunks])}

    mock_client = MagicMock()
    http_client, _, _, _ = _make_app_client_with_mock(test_token, mock_client)

    with patch("app.open_invoke_stream", side_effect=_fake_open_invoke_stream):
        with http_client as client:
            resp = client.post(
                _invoke_stream_path(),
                json={
                    "messages": [{"role": "user", "content": "Hi"}],
                    "max_tokens": 100,
                    "anthropic_version": "bedrock-2023-05-31",
                },
                headers=_auth_headers(bearer_token),
            )

    assert resp.status_code == 200
    assert captured["body"].get("max_tokens") == 30


# ===========================================================================
# Section 7: generator-level disconnect (amendment R4)
# ===========================================================================


def test_converse_stream_disconnect_no_usage_write():
    """Generator closed before metadata event → _post_flight_write skips write (usage_out empty)."""
    from bedrock import iter_converse_stream

    events = [
        {"contentBlockDelta": {"delta": {"text": "Hello"}, "contentBlockIndex": 0}},
        # metadata would come last but we close before it
    ]
    usage_out = {}
    gen = iter_converse_stream(iter(events), usage_out)

    # Consume first event then close (simulates client disconnect)
    first = next(gen)
    assert "contentBlockDelta" in first
    gen.close()

    # usage_out is still empty — no billing write should happen
    assert usage_out == {}


def test_converse_stream_disconnect_with_partial_usage_writes(test_token):
    """Generator closed after metadata → _post_flight_write writes partial usage."""
    from app import _post_flight_write

    token_id, bearer_token, tables = test_token
    _, usage_table, _ = tables

    # Build a minimal preflight result substitute
    from datetime import UTC, datetime

    from app import PreflightResult

    period = datetime.now(UTC).strftime("%Y-%m")
    pf = PreflightResult(
        token_id=token_id,
        token_row={},
        period=period,
        body={},
        model_id=DEFAULT_MODEL,
        pricing_mode="on_demand",
        log_ctx={"token_id": token_id, "model_id": DEFAULT_MODEL, "pricing_mode": "on_demand"},
        start_ms=time.monotonic() * 1000,
        usage_table=usage_table,
    )

    # Simulate usage populated before disconnect
    usage_out = {
        "input_tokens": 8,
        "output_tokens": 4,
        "cache_read_input_tokens": 0,
        "cache_write_input_tokens": 0,
    }
    log_ctx = dict(pf.log_ctx)

    _post_flight_write(usage_out, pf, log_ctx)

    usage = usage_table.get_item(Key={"token_id": token_id, "period": period}).get("Item", {})
    assert int(usage.get("input_tokens", 0)) == 8
    assert int(usage.get("output_tokens", 0)) == 4


def test_invoke_stream_disconnect_partial_state():
    """iter_invoke_stream closed after message_start but before message_delta.

    Generator state after close: input_tokens is set (from message_start) but
    output_tokens is absent.  This documents the partial-usage state; whether
    billing occurs for input-only disconnects is a policy decision — the current
    _post_flight_write guard is ``if not usage_out`` (empty dict), so a partial
    disconnect DOES bill (usage_out is non-empty with input_tokens).
    """
    from bedrock import iter_invoke_stream

    # Only emit message_start (input tokens) then close
    message_start_bytes = json.dumps(
        {"type": "message_start", "message": {"usage": {"input_tokens": 5}}}
    ).encode()
    events = [{"chunk": {"bytes": message_start_bytes}}]
    # No message_delta so output_tokens never set

    usage_out = {}
    gen = iter_invoke_stream(iter(events), usage_out)

    first = next(gen)
    assert first.get("type") == "message_start"
    gen.close()

    # input_tokens was populated but output_tokens wasn't
    assert usage_out.get("input_tokens") == 5
    assert "output_tokens" not in usage_out
    # usage_out is non-empty, so _post_flight_write will attempt to bill
    assert usage_out  # non-empty → billing path taken


# ===========================================================================
# Section 7b: _sse_stream wrapper-level disconnect tests (R6)
# Tests drive the real _sse_stream helper directly (not through HTTP) so the
# real finally:→_post_flight_write path on disconnect is covered.
# ===========================================================================


def test_sse_stream_disconnect_empty_usage_no_write(test_token):
    """_sse_stream closed before any usage events → _post_flight_write skips write (R6-i)."""
    from app import _sse_stream

    token_id, bearer_token, tables = test_token
    _, usage_table, _ = tables

    from app import PreflightResult

    period = _current_period()
    pf = PreflightResult(
        token_id=token_id,
        token_row={},
        period=period,
        body={},
        model_id=DEFAULT_MODEL,
        pricing_mode="on_demand",
        log_ctx={"token_id": token_id, "model_id": DEFAULT_MODEL, "pricing_mode": "on_demand"},
        start_ms=time.monotonic() * 1000,
        usage_table=usage_table,
    )
    log_ctx = dict(pf.log_ctx)
    usage_out: dict = {}

    # An event iterator that never completes — we close before the first event
    def _never_ending():
        yield {"contentBlockDelta": {"delta": {"text": "Hi"}}}

    gen = _sse_stream(_never_ending(), usage_out, pf, log_ctx)

    # Consume one frame then close (simulates disconnect)
    next(gen)
    gen.close()

    # usage_out is still empty, so _post_flight_write should NOT write
    usage = usage_table.get_item(Key={"token_id": token_id, "period": period}).get("Item", {})
    assert not usage, f"Expected no usage write, got: {usage}"


def test_sse_stream_disconnect_with_usage_writes(test_token):
    """_sse_stream closed after usage is populated → _post_flight_write writes usage (R6-ii)."""
    from app import _sse_stream
    from bedrock import iter_converse_stream

    token_id, bearer_token, tables = test_token
    _, usage_table, _ = tables

    from app import PreflightResult

    period = _current_period()
    pf = PreflightResult(
        token_id=token_id,
        token_row={},
        period=period,
        body={},
        model_id=DEFAULT_MODEL,
        pricing_mode="on_demand",
        log_ctx={"token_id": token_id, "model_id": DEFAULT_MODEL, "pricing_mode": "on_demand"},
        start_ms=time.monotonic() * 1000,
        usage_table=usage_table,
    )
    log_ctx = dict(pf.log_ctx)
    usage_out: dict = {}

    # Provide events including a metadata event so usage_out is populated
    events = _converse_stream_events(in_tokens=11, out_tokens=4)
    event_iter = iter_converse_stream(iter(events), usage_out)
    gen = _sse_stream(event_iter, usage_out, pf, log_ctx)

    # Consume all frames (metadata is last, so iterate fully to populate usage_out)
    frames = list(gen)
    assert frames  # should have yielded some SSE frames

    # _post_flight_write should have written usage
    usage = usage_table.get_item(Key={"token_id": token_id, "period": period}).get("Item", {})
    assert int(usage.get("input_tokens", 0)) == 11
    assert int(usage.get("output_tokens", 0)) == 4


# ===========================================================================
# Section 8: bedrock-level unit tests — error-member mapper + cache extraction
# ===========================================================================


def test_check_eventstream_error_throttling():
    """throttlingException member raises BEDROCK_THROTTLED 429."""
    from bedrock import BedrockError, _check_eventstream_error

    with pytest.raises(BedrockError) as exc_info:
        _check_eventstream_error({"throttlingException": {"message": "Throttled"}})
    assert exc_info.value.code == "BEDROCK_THROTTLED"
    assert exc_info.value.status == 429


def test_check_eventstream_error_model_stream_error():
    """modelStreamErrorException → BEDROCK_STREAM_ERROR 502."""
    from bedrock import BedrockError, _check_eventstream_error

    with pytest.raises(BedrockError) as exc_info:
        _check_eventstream_error(
            {"modelStreamErrorException": {"message": "stream failed", "originalStatusCode": 500}}
        )
    assert exc_info.value.code == "BEDROCK_STREAM_ERROR"
    assert exc_info.value.status == 502


def test_check_eventstream_error_validation():
    """validationException → BAD_REQUEST 400."""
    from bedrock import BedrockError, _check_eventstream_error

    with pytest.raises(BedrockError) as exc_info:
        _check_eventstream_error({"validationException": {"message": "bad param"}})
    assert exc_info.value.code == "BAD_REQUEST"
    assert exc_info.value.status == 400


def test_check_eventstream_error_internal():
    """internalServerException → BEDROCK_ERROR 502."""
    from bedrock import BedrockError, _check_eventstream_error

    with pytest.raises(BedrockError) as exc_info:
        _check_eventstream_error({"internalServerException": {"message": "boom"}})
    assert exc_info.value.code == "BEDROCK_ERROR"
    assert exc_info.value.status == 502


def test_check_eventstream_error_service_unavailable():
    """serviceUnavailableException → BEDROCK_ERROR 502 (aligned to rest of Bedrock surface, R2)."""
    from bedrock import BedrockError, _check_eventstream_error

    with pytest.raises(BedrockError) as exc_info:
        _check_eventstream_error({"serviceUnavailableException": {"message": "down"}})
    assert exc_info.value.code == "BEDROCK_ERROR"
    assert exc_info.value.status == 502


def test_check_eventstream_error_model_timeout():
    """modelTimeoutException → BEDROCK_ERROR 504 (BB1)."""
    from bedrock import BedrockError, _check_eventstream_error

    with pytest.raises(BedrockError) as exc_info:
        _check_eventstream_error({"modelTimeoutException": {"message": "Model timed out"}})
    assert exc_info.value.code == "BEDROCK_ERROR"
    assert exc_info.value.status == 504


def test_check_eventstream_error_no_error():
    """Normal events pass through without raising."""
    from bedrock import _check_eventstream_error

    # Should not raise
    _check_eventstream_error({"contentBlockDelta": {"delta": {"text": "hi"}}})
    _check_eventstream_error({"metadata": {"usage": {}}})


def test_iter_invoke_stream_cache_extraction():
    """iter_invoke_stream extracts cache tokens from message_start.message.usage (B2)."""
    from bedrock import iter_invoke_stream

    chunks = _invoke_stream_events_with_cache(
        in_tokens=10, out_tokens=5, cache_read=15, cache_write=25
    )
    events = [{"chunk": {"bytes": b}} for b in chunks]
    usage_out = {}
    list(iter_invoke_stream(iter(events), usage_out))  # fully consume

    assert usage_out["input_tokens"] == 10
    assert usage_out["output_tokens"] == 5
    assert usage_out["cache_read_input_tokens"] == 15
    assert usage_out["cache_write_input_tokens"] == 25


def test_iter_invoke_stream_fallback_from_metrics():
    """invocationMetrics fills in tokens only when message_start/delta absent."""
    from bedrock import iter_invoke_stream

    # Only message_stop with invocationMetrics (no message_start / message_delta)
    message_stop = json.dumps(
        {
            "type": "message_stop",
            "stop_reason": "end_turn",
            "amazon-bedrock-invocationMetrics": {
                "inputTokenCount": 20,
                "outputTokenCount": 10,
            },
        }
    ).encode()
    events = [{"chunk": {"bytes": message_stop}}]
    usage_out = {}
    list(iter_invoke_stream(iter(events), usage_out))

    assert usage_out["input_tokens"] == 20
    assert usage_out["output_tokens"] == 10
    # Cache counters remain unset
    assert usage_out.get("cache_read_input_tokens", 0) == 0
    assert usage_out.get("cache_write_input_tokens", 0) == 0


def test_iter_invoke_stream_fallback_does_not_overwrite_primary():
    """invocationMetrics does NOT overwrite values already set from message_start/delta."""
    from bedrock import iter_invoke_stream

    message_start = json.dumps(
        {"type": "message_start", "message": {"usage": {"input_tokens": 5}}}
    ).encode()
    message_delta = json.dumps({"type": "message_delta", "usage": {"output_tokens": 3}}).encode()
    # invocationMetrics provides different (higher) counts — should be ignored
    message_stop = json.dumps(
        {
            "type": "message_stop",
            "stop_reason": "end_turn",
            "amazon-bedrock-invocationMetrics": {
                "inputTokenCount": 99,
                "outputTokenCount": 99,
            },
        }
    ).encode()
    events = [
        {"chunk": {"bytes": message_start}},
        {"chunk": {"bytes": message_delta}},
        {"chunk": {"bytes": message_stop}},
    ]
    usage_out = {}
    list(iter_invoke_stream(iter(events), usage_out))

    # Primary values win
    assert usage_out["input_tokens"] == 5
    assert usage_out["output_tokens"] == 3
