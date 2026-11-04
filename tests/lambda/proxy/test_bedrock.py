import json

import pytest
from bedrock import (
    apply_output_cap,
    forward_converse,
    forward_invoke_model,
    iter_converse_stream,
    iter_invoke_stream,
    normalize_usage,
    open_invoke_stream,
)
from routes import ROUTE_CONVERSE, ROUTE_INVOKE, ROUTE_RESPONSES


def test_normalize_usage_non_dict_defaults_zero():
    usage = normalize_usage(
        None,
        {
            "input_tokens": "a",
            "output_tokens": "b",
            "cache_read_input_tokens": "c",
            "cache_write_input_tokens": "d",
        },
    )
    assert usage == {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_write_input_tokens": 0,
    }


def test_normalize_usage_parsing_and_clamping():
    usage = normalize_usage(
        {
            "a": "10",
            "b": "-5",
            "c": "not-int",
            "d": 10**30,
        },
        {
            "input_tokens": "a",
            "output_tokens": "b",
            "cache_read_input_tokens": "c",
            "cache_write_input_tokens": "d",
        },
    )
    assert usage["input_tokens"] == 10
    assert usage["output_tokens"] == 0
    assert usage["cache_read_input_tokens"] == 0
    assert usage["cache_write_input_tokens"] == 9_223_372_036_854_775_807


def test_normalize_usage_supports_alias_keys():
    usage = normalize_usage(
        {"count_key": 12, "token_key": 34},
        {
            "input_tokens": "count_key",
            "output_tokens": "token_key",
            "cache_read_input_tokens": ["missing", "count_key"],
            "cache_write_input_tokens": ["token_key", "missing"],
        },
    )
    assert usage["cache_read_input_tokens"] == 12
    assert usage["cache_write_input_tokens"] == 34


# ---------------------------------------------------------------------------
# Usage extraction and service tier (fakes: a canned-response _Client)
# ---------------------------------------------------------------------------


class _Body:
    def __init__(self, payload: dict):
        self._raw = json.dumps(payload).encode()

    def read(self):
        return self._raw


class _Client:
    """Records the kwargs of each call and returns a canned response."""

    def __init__(self, response: dict):
        self.response = response
        self.kwargs: dict = {}

    def converse(self, **kwargs):
        self.kwargs = kwargs
        return dict(self.response)

    def invoke_model(self, **kwargs):
        self.kwargs = kwargs
        return dict(self.response)

    def invoke_model_with_response_stream(self, **kwargs):
        self.kwargs = kwargs
        return dict(self.response)


_CONVERSE = {"output": {}, "usage": {"inputTokens": 3, "outputTokens": 4}}
_CLAUDE_BODY = {"usage": {"input_tokens": 10, "output_tokens": 5}}

# A real gpt-oss InvokeModel response: OpenAI-style body usage the key map does not read,
# so the model-agnostic count headers supply the billing counts.
_GPT_OSS_BODY = {
    "object": "chat.completion",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "Hi"}}],
    "usage": {"prompt_tokens": 20, "completion_tokens": 30, "total_tokens": 50},
}
_COUNT_HEADERS = {
    "x-amzn-bedrock-input-token-count": "20",
    "x-amzn-bedrock-output-token-count": "30",
}


def test_forward_invoke_model_merges_mixed_usage_keys():
    body = {
        "usage": {
            "input_tokens": 10,
            "output_tokens": 5,
            "cacheReadInputTokenCount": 7,
            "cacheWriteInputTokenCount": 3,
        }
    }
    _, usage = forward_invoke_model(_Client({"body": _Body(body)}), "m", {})
    assert usage["input_tokens"] == 10
    assert usage["output_tokens"] == 5
    assert usage["cache_read_input_tokens"] == 7
    assert usage["cache_write_input_tokens"] == 3


def test_forward_invoke_model_bedrock_tokens_aliases_supported():
    body = {
        "usage": {
            "inputTokens": 10,
            "outputTokens": 5,
            "cacheReadInputTokens": 7,
            "cacheWriteInputTokens": 3,
        }
    }
    _, usage = forward_invoke_model(_Client({"body": _Body(body)}), "m", {})
    assert usage["cache_read_input_tokens"] == 7
    assert usage["cache_write_input_tokens"] == 3


def test_forward_invoke_model_anthropic_zero_wins_when_key_present():
    body = {"usage": {"input_tokens": 0, "output_tokens": 0, "inputTokens": 5, "outputTokens": 6}}
    _, usage = forward_invoke_model(_Client({"body": _Body(body)}), "m", {})
    assert usage["input_tokens"] == 0
    assert usage["output_tokens"] == 0


@pytest.mark.parametrize(("tier", "expected"), [({"type": "flex"}, "flex"), (None, "standard")])
def test_forward_converse_reports_service_tier(tier, expected):
    response = {**_CONVERSE, **({"serviceTier": tier} if tier else {})}
    _, usage = forward_converse(_Client(response), "m", {})
    assert usage["service_tier"] == expected
    assert "raw_service_tier" not in usage


@pytest.mark.parametrize(("tier", "forwarded"), [("flex", True), (None, False), ("", False)])
def test_forward_invoke_model_passes_service_tier_only_when_set(tier, forwarded):
    client = _Client({"body": _Body(_CLAUDE_BODY)})
    forward_invoke_model(client, "m", {}, service_tier=tier)
    assert ("serviceTier" in client.kwargs) is forwarded
    if forwarded:
        assert client.kwargs["serviceTier"] == tier


def test_forward_invoke_model_reads_response_tier():
    client = _Client({"body": _Body(_CLAUDE_BODY), "serviceTier": "flex"})
    _, usage = forward_invoke_model(client, "m", {})
    assert usage["service_tier"] == "flex"
    assert usage["raw_service_tier"] == "flex"


def test_forward_invoke_model_without_response_tier_bills_standard():
    _, usage = forward_invoke_model(_Client({"body": _Body(_CLAUDE_BODY)}), "m", {})
    assert usage["service_tier"] == "standard"
    assert "raw_service_tier" not in usage


def test_forward_invoke_model_header_count_fallback_for_gpt_oss():
    client = _Client(
        {"body": _Body(_GPT_OSS_BODY), "ResponseMetadata": {"HTTPHeaders": _COUNT_HEADERS}}
    )
    _, usage = forward_invoke_model(client, "openai.gpt-oss-120b-1:0", {})
    assert usage["input_tokens"] == 20
    assert usage["output_tokens"] == 30


def test_forward_invoke_model_cache_count_headers_fallback():
    """Bedrock's cache-count headers fill cache counters the body doesn't report."""
    headers = {
        **_COUNT_HEADERS,
        "x-amzn-bedrock-cache-read-input-token-count": "11",
        "x-amzn-bedrock-cache-write-input-token-count": "4",
    }
    client = _Client({"body": _Body(_GPT_OSS_BODY), "ResponseMetadata": {"HTTPHeaders": headers}})
    _, usage = forward_invoke_model(client, "openai.gpt-oss-120b-1:0", {})
    assert usage["cache_read_input_tokens"] == 11
    assert usage["cache_write_input_tokens"] == 4


@pytest.mark.parametrize(
    "body_usage",
    [
        {"input_tokens": 7, "output_tokens": 8},  # Anthropic
        {"inputTokens": 7, "outputTokens": 8},  # Nova
    ],
)
def test_forward_invoke_model_body_usage_wins_over_headers(body_usage):
    client = _Client(
        {"body": _Body({"usage": body_usage}), "ResponseMetadata": {"HTTPHeaders": _COUNT_HEADERS}}
    )
    _, usage = forward_invoke_model(client, "m", {})
    assert usage["input_tokens"] == 7
    assert usage["output_tokens"] == 8


def test_forward_invoke_model_usageless_body_warns_and_returns_zero(caplog):
    with caplog.at_level("WARNING"):
        _, usage = forward_invoke_model(_Client({"body": _Body({"images": []})}), "m", {})
    assert {k: usage[k] for k in ("input_tokens", "output_tokens")} == {
        "input_tokens": 0,
        "output_tokens": 0,
    }
    warnings = [json.loads(r.message) for r in caplog.records if "usage_parse" in r.message]
    assert warnings == [{"event": "usage_parse_warning", "field": "usage", "model_id": "m"}]


@pytest.mark.parametrize(("tier", "forwarded"), [("priority", True), (None, False), ("", False)])
def test_open_invoke_stream_passes_service_tier_only_when_set(tier, forwarded):
    client = _Client({"body": iter([])})
    open_invoke_stream(client, "m", {}, service_tier=tier)
    assert ("serviceTier" in client.kwargs) is forwarded
    if forwarded:
        assert client.kwargs["serviceTier"] == tier


def test_iter_converse_stream_reads_metadata_tier():
    events = [
        {"contentBlockDelta": {"delta": {"text": "Hi"}}},
        {
            "metadata": {
                "usage": {"inputTokens": 3, "outputTokens": 4},
                "serviceTier": {"type": "flex"},
            }
        },
    ]
    usage_out: dict = {}
    list(iter_converse_stream(iter(events), usage_out))
    assert usage_out["service_tier"] == "flex"
    assert usage_out["input_tokens"] == 3


def _invoke_stream(*chunks: dict) -> list[dict]:
    return [{"chunk": {"bytes": json.dumps(c).encode()}} for c in chunks]


def test_iter_invoke_stream_gpt_oss_metrics_on_untyped_last_chunk():
    events = _invoke_stream(
        {"object": "chat.completion.chunk", "choices": [{"delta": {"content": "Hi"}}]},
        {
            "object": "chat.completion.chunk",
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "amazon-bedrock-invocationMetrics": {
                "inputTokenCount": 20,
                "outputTokenCount": 30,
                "invocationLatency": 400,
                "firstByteLatency": 100,
            },
        },
    )
    usage_out: dict = {}
    list(iter_invoke_stream(iter(events), usage_out))
    assert usage_out == {"input_tokens": 20, "output_tokens": 30}


def test_iter_invoke_stream_nova_metadata_usage_beats_zero_metrics():
    events = _invoke_stream(
        {"messageStart": {"role": "assistant"}},
        {"contentBlockDelta": {"delta": {"text": "Hi"}, "contentBlockIndex": 0}},
        {"messageStop": {"stopReason": "end_turn"}},
        {
            "metadata": {"usage": {"inputTokens": 5, "outputTokens": 10}, "metrics": {}},
            "amazon-bedrock-invocationMetrics": {"inputTokenCount": 0, "outputTokenCount": 0},
        },
    )
    usage_out: dict = {}
    list(iter_invoke_stream(iter(events), usage_out))
    assert usage_out["input_tokens"] == 5
    assert usage_out["output_tokens"] == 10


# ---------------------------------------------------------------------------
# apply_output_cap — per-protocol field names (issue #8)
# ---------------------------------------------------------------------------


def test_output_cap_responses_uses_max_output_tokens():
    """The Responses API ignores max_tokens — writing it there would silently skip the cap."""
    body = apply_output_cap({"input": "hi"}, ROUTE_RESPONSES, 64)
    assert body["max_output_tokens"] == 64
    assert "max_tokens" not in body


def test_output_cap_responses_takes_the_lower_of_user_and_cap():
    assert (
        apply_output_cap({"max_output_tokens": 10}, ROUTE_RESPONSES, 64)["max_output_tokens"] == 10
    )
    assert (
        apply_output_cap({"max_output_tokens": 999}, ROUTE_RESPONSES, 64)["max_output_tokens"] == 64
    )


def test_output_cap_invoke_still_uses_max_tokens():
    body = apply_output_cap({"messages": []}, ROUTE_INVOKE, 64)
    assert body["max_tokens"] == 64
    assert "max_output_tokens" not in body


def test_output_cap_converse_still_uses_inference_config():
    body = apply_output_cap({"messages": []}, ROUTE_CONVERSE, 64)
    assert body["inferenceConfig"]["maxTokens"] == 64


def test_output_cap_none_is_passthrough():
    original = {"input": "hi"}
    assert apply_output_cap(original, ROUTE_RESPONSES, None) is original


def test_output_cap_does_not_mutate_caller_body():
    original = {"input": "hi"}
    apply_output_cap(original, ROUTE_RESPONSES, 64)
    assert original == {"input": "hi"}
