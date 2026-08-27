"""Tests for the bedrock-mantle transport (mantle.py).

Only the socket is faked (httpx.MockTransport). The real httpx client, the real SigV4
signing, and the real usage parsing all run — so these cover the wiring, not a mock of it.
"""

import json

import httpx
import pytest
from bedrock import BedrockError
from mantle import (
    RESPONSES_PATH,
    forward_responses,
    iter_responses_sse,
    open_responses_stream,
    reasoning_tokens_of,
    sign_headers,
)

# The exact usage payload observed from the live endpoint (issue #8).
OBSERVED_USAGE = {
    "input_tokens": 12,
    "input_tokens_details": {"cache_write_tokens": 0, "cached_tokens": 0},
    "output_tokens": 5,
    "output_tokens_details": {"reasoning_tokens": 0},
    "total_tokens": 17,
}


def _client(handler) -> httpx.Client:
    return httpx.Client(
        base_url="https://bedrock-mantle.us-east-1.api.aws",
        transport=httpx.MockTransport(handler),
    )


def _ok(payload: dict):
    return lambda request: httpx.Response(200, json=payload)


def _error(status: int, err_type: str, message: str = "boom"):
    return lambda request: httpx.Response(
        status, json={"error": {"type": err_type, "message": message, "code": err_type}}
    )


def _sse(lines: list[str]):
    body = "".join(f"{line}\n" for line in lines)
    return lambda request: httpx.Response(200, text=body)


# ---------------------------------------------------------------------------
# Usage parsing
# ---------------------------------------------------------------------------


def test_forward_responses_parses_observed_usage_shape():
    client = _client(_ok({"output": [], "usage": OBSERVED_USAGE}))
    _, usage = forward_responses(client, "openai.gpt-5.6-luna", {"input": "hi"})
    assert usage == {
        "input_tokens": 12,
        "output_tokens": 5,
        "cache_read_input_tokens": 0,
        "cache_write_input_tokens": 0,
    }


def test_nested_cache_counters_are_flattened():
    usage = {
        "input_tokens": 100,
        "input_tokens_details": {"cached_tokens": 40, "cache_write_tokens": 25},
        "output_tokens": 7,
    }
    client = _client(_ok({"usage": usage}))
    _, parsed = forward_responses(client, "openai.gpt-5.6-luna", {"input": "hi"})
    assert parsed["cache_read_input_tokens"] == 40
    assert parsed["cache_write_input_tokens"] == 25


def test_reasoning_tokens_are_reported_but_never_billed():
    """DD2: reasoning_tokens are a SUBSET of output_tokens — billing them would double-charge."""
    usage = {
        "input_tokens": 10,
        "output_tokens": 100,
        "output_tokens_details": {"reasoning_tokens": 80},
    }
    client = _client(_ok({"usage": usage}))
    _, parsed = forward_responses(client, "openai.gpt-5.6-luna", {"input": "hi"})
    # output_tokens is reported verbatim — NOT 100 + 80.
    assert parsed["output_tokens"] == 100
    assert "reasoning_tokens" not in parsed  # never a billing dimension
    assert reasoning_tokens_of(usage) == 80  # available for logging


def test_reasoning_tokens_of_tolerates_missing_details():
    assert reasoning_tokens_of({}) == 0
    assert reasoning_tokens_of(None) == 0
    assert reasoning_tokens_of({"output_tokens_details": None}) == 0
    assert reasoning_tokens_of({"output_tokens_details": {"reasoning_tokens": True}}) == 0


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "err_type", "expect_status", "expect_code"),
    [
        (400, "validation_error", 400, "BAD_REQUEST"),
        (404, "not_found_error", 404, "MODEL_NOT_FOUND"),
        (429, "rate_limit_error", 429, "THROTTLED"),
        (500, "server_error", 502, "BEDROCK_ERROR"),
    ],
)
def test_error_envelope_mapping(status, err_type, expect_status, expect_code):
    client = _client(_error(status, err_type))
    with pytest.raises(BedrockError) as exc:
        forward_responses(client, "openai.gpt-5.6-luna", {"input": "hi"})
    assert exc.value.status == expect_status
    assert exc.value.code == expect_code


def test_upstream_auth_failure_is_502_never_401():
    """Our SigV4/IAM being broken must not tell the caller their bearer token is bad."""
    client = _client(_error(401, "permission_denied_error", "Missing 'authorization' header"))
    with pytest.raises(BedrockError) as exc:
        forward_responses(client, "openai.gpt-5.6-luna", {"input": "hi"})
    assert exc.value.status == 502
    assert exc.value.code == "BEDROCK_ERROR"


def test_non_json_body_is_502():
    client = _client(lambda request: httpx.Response(200, text="<html>gateway</html>"))
    with pytest.raises(BedrockError) as exc:
        forward_responses(client, "openai.gpt-5.6-luna", {"input": "hi"})
    assert exc.value.status == 502


def test_transport_error_is_502():
    def boom(request):
        raise httpx.ConnectError("connection refused")

    with pytest.raises(BedrockError) as exc:
        forward_responses(_client(boom), "openai.gpt-5.6-luna", {"input": "hi"})
    assert exc.value.status == 502


# ---------------------------------------------------------------------------
# SigV4
# ---------------------------------------------------------------------------


def test_sigv4_signs_with_bedrock_service_name():
    """Issue #8 verified SigV4 with service name 'bedrock' is what mantle accepts."""
    seen = {}

    def handler(request):
        seen.update(request.headers)
        return httpx.Response(200, json={"usage": OBSERVED_USAGE})

    forward_responses(_client(handler), "openai.gpt-5.6-luna", {"input": "hi"})
    auth = seen["authorization"]
    assert auth.startswith("AWS4-HMAC-SHA256 ")
    assert "/bedrock/aws4_request" in auth
    assert "x-amz-date" in seen


def test_sigv4_region_follows_bedrock_region_not_aws_region(monkeypatch):
    """Signing region must match the endpoint host (BEDROCK_REGION), not the Lambda's
    AWS_REGION — a mismatch names the wrong credential scope and mantle returns 403."""
    monkeypatch.setenv("BEDROCK_REGION", "eu-west-1")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    seen = {}

    def handler(request):
        seen.update(request.headers)
        return httpx.Response(200, json={"usage": OBSERVED_USAGE})

    forward_responses(_client(handler), "openai.gpt-5.6-luna", {"input": "hi"})
    assert "/eu-west-1/bedrock/aws4_request" in seen["authorization"]


def test_sign_headers_signs_the_exact_body():
    """The signature must cover the payload actually sent, or mantle returns 403."""
    client = _client(_ok({}))
    body = json.dumps({"model": "openai.gpt-5.6-luna", "input": "hi"}).encode()
    first = sign_headers(client, body)
    second = sign_headers(client, body + b" ")
    assert first["Authorization"] != second["Authorization"]


def test_model_is_sent_in_body_not_path():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"usage": OBSERVED_USAGE})

    forward_responses(_client(handler), "openai.gpt-5.6-luna", {"input": "hi"})
    assert seen["url"].endswith(RESPONSES_PATH)
    assert seen["body"]["model"] == "openai.gpt-5.6-luna"
    assert seen["body"]["stream"] is False


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------


COMPLETED = {
    "type": "response.completed",
    "response": {
        "usage": {
            "input_tokens": 12,
            "input_tokens_details": {"cached_tokens": 3, "cache_write_tokens": 0},
            "output_tokens": 100,
            "output_tokens_details": {"reasoning_tokens": 80},
        }
    },
}


def test_stream_relays_event_lines_verbatim_and_harvests_usage():
    """`event:` lines must survive — OpenAI SDK clients dispatch on the event type."""
    lines = [
        "event: response.output_text.delta",
        'data: {"type": "response.output_text.delta", "delta": "hi"}',
        "",
        "event: response.completed",
        f"data: {json.dumps(COMPLETED)}",
        "",
    ]
    resp = open_responses_stream(_client(_sse(lines)), "openai.gpt-5.6-luna", {"input": "hi"})
    usage_out: dict = {}
    relayed = "".join(iter_responses_sse(resp, usage_out))

    # Byte-for-byte relay: event: lines, data: lines, AND the blank-line separators that
    # delimit one SSE event from the next. Drop the blanks and clients stop dispatching.
    assert relayed == "".join(f"{line}\n" for line in lines)
    assert "event: response.output_text.delta\n" in relayed
    assert "event: response.completed\n" in relayed
    assert relayed.count("\n\n") == 2  # one frame terminator per event
    # 9, not the 12 the payload reports: input_tokens is inclusive of cached_tokens on the
    # Responses API, and billable input is the remainder. The streaming path must apply the
    # same exclusion as the non-streaming one or usage differs by transport.
    assert usage_out["input_tokens"] == 9
    assert usage_out["output_tokens"] == 100
    assert usage_out["cache_read_input_tokens"] == 3
    assert usage_out["reasoning_tokens"] == 80  # logged, not billed


def test_stream_sets_stream_true_in_body():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, text="")

    resp = open_responses_stream(_client(handler), "openai.gpt-5.6-luna", {"input": "hi"})
    list(iter_responses_sse(resp, {}))
    assert seen["body"]["stream"] is True


def test_stream_closes_response_even_on_early_exit():
    """httpx responses are not GC-managed like boto3 EventStreams — the connection leaks."""
    lines = ["event: a", 'data: {"type": "x"}', "", "event: b", 'data: {"type": "y"}', ""]
    resp = open_responses_stream(_client(_sse(lines)), "openai.gpt-5.6-luna", {"input": "hi"})
    gen = iter_responses_sse(resp, {})
    next(gen)  # consume one frame, then abandon (client disconnect)
    gen.close()
    assert resp.is_closed


def test_stream_without_usage_leaves_usage_out_empty():
    """No terminal event (disconnect) → empty usage → _post_flight_write skips the write."""
    resp = open_responses_stream(
        _client(_sse(["event: response.output_text.delta", 'data: {"delta": "hi"}', ""])),
        "openai.gpt-5.6-luna",
        {"input": "hi"},
    )
    usage_out: dict = {}
    list(iter_responses_sse(resp, usage_out))
    assert usage_out == {}


def test_stream_open_maps_error_status():
    client = _client(_error(400, "validation_error", "does not support the '/v1/responses' API"))
    with pytest.raises(BedrockError) as exc:
        open_responses_stream(client, "openai.gpt-5.6-luna", {"input": "hi"})
    assert exc.value.status == 400


def test_stream_open_closes_response_on_error():
    """The eager-open error path must release the connection before raising."""
    closed = {"value": False}
    real_response = httpx.Response(
        400, json={"error": {"type": "validation_error", "message": "nope"}}
    )
    original_close = real_response.close

    def spy():
        closed["value"] = True
        original_close()

    real_response.close = spy
    client = _client(lambda request: real_response)
    with pytest.raises(BedrockError):
        open_responses_stream(client, "openai.gpt-5.6-luna", {"input": "hi"})
    assert closed["value"]


def test_stream_closes_response_when_iter_raises_midstream():
    """A mid-stream transport error must still release the connection (finally: close)."""

    class ExplodingResponse:
        def __init__(self):
            self.is_closed = False

        def iter_lines(self):
            yield "event: response.output_text.delta"
            raise httpx.ReadError("connection reset mid-stream")

        def close(self):
            self.is_closed = True

    resp = ExplodingResponse()
    gen = iter_responses_sse(resp, {})
    assert next(gen) == "event: response.output_text.delta\n"
    with pytest.raises(httpx.ReadError):
        next(gen)
    assert resp.is_closed


def test_stream_ignores_malformed_data_lines():
    lines = [
        "data: not-json",
        "",
        "event: response.completed",
        f"data: {json.dumps(COMPLETED)}",
        "",
    ]
    resp = open_responses_stream(_client(_sse(lines)), "openai.gpt-5.6-luna", {"input": "hi"})
    usage_out: dict = {}
    relayed = "".join(iter_responses_sse(resp, usage_out))
    assert "data: not-json" in relayed  # relayed verbatim, not dropped
    assert usage_out["output_tokens"] == 100

# ---------------------------------------------------------------------------
# Cached tokens are a SUBSET of input_tokens on the Responses API (issue: cache
# hits cost more than misses). Numbers below are from the live endpoint.
# ---------------------------------------------------------------------------

# Same request sent twice. input_tokens is identical; only the details flip.
COLD_CALL = {
    "input_tokens": 3092,
    "input_tokens_details": {"cache_write_tokens": 3090, "cached_tokens": 0},
    "output_tokens": 5,
}
WARM_CALL = {
    "input_tokens": 3092,
    "input_tokens_details": {"cache_write_tokens": 0, "cached_tokens": 3090},
    "output_tokens": 5,
}


def test_warm_call_bills_only_the_uncached_remainder():
    """The regression: 3092 tokens billed at full rate when only 2 were new."""
    client = _client(_ok({"usage": WARM_CALL}))
    _, usage = forward_responses(client, "openai.gpt-5.6-luna", {"input": "hi"})

    assert usage["cache_read_input_tokens"] == 3090
    assert usage["input_tokens"] == 2, "cached tokens must not also be billed as input"


def test_cold_call_bills_only_the_unwritten_remainder():
    client = _client(_ok({"usage": COLD_CALL}))
    _, usage = forward_responses(client, "openai.gpt-5.6-luna", {"input": "hi"})

    assert usage["cache_write_input_tokens"] == 3090
    assert usage["input_tokens"] == 2


def test_a_cache_hit_is_cheaper_than_a_miss():
    """The property that was inverted: caching must reduce cost, never raise it.

    Asserted on the components rather than a dollar figure so it does not break when the
    price table moves.
    """
    cold_client = _client(_ok({"usage": COLD_CALL}))
    warm_client = _client(_ok({"usage": WARM_CALL}))
    _, cold = forward_responses(cold_client, "openai.gpt-5.6-luna", {"input": "hi"})
    _, warm = forward_responses(warm_client, "openai.gpt-5.6-luna", {"input": "hi"})

    # Cache reads are billed well below cache writes on every model in the table, and the
    # billable input is identical, so the warm call must be strictly cheaper.
    assert warm["input_tokens"] == cold["input_tokens"]
    assert warm["cache_read_input_tokens"] == cold["cache_write_input_tokens"]
    assert warm["cache_write_input_tokens"] == 0


def test_exclusive_shape_is_not_double_subtracted():
    """A provider reporting the Converse convention must not go negative."""
    usage = {
        "input_tokens": 10,
        "input_tokens_details": {"cached_tokens": 40, "cache_write_tokens": 25},
        "output_tokens": 7,
    }
    client = _client(_ok({"usage": usage}))
    _, parsed = forward_responses(client, "openai.gpt-5.6-luna", {"input": "hi"})
    assert parsed["input_tokens"] == 0
