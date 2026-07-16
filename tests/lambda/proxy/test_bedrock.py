from bedrock import apply_output_cap, forward_invoke_model, normalize_usage
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


def test_forward_invoke_model_merges_mixed_usage_keys():
    class _Body:
        def read(self):
            return (
                b'{"usage":{"input_tokens":10,"output_tokens":5,'
                b'"cacheReadInputTokenCount":7,"cacheWriteInputTokenCount":3}}'
            )

    class _Client:
        def invoke_model(self, **kwargs):
            return {"body": _Body()}

    _, usage = forward_invoke_model(_Client(), "m", {})
    assert usage["input_tokens"] == 10
    assert usage["output_tokens"] == 5
    assert usage["cache_read_input_tokens"] == 7
    assert usage["cache_write_input_tokens"] == 3


def test_forward_invoke_model_bedrock_tokens_aliases_supported():
    class _Body:
        def read(self):
            return (
                b'{"usage":{"inputTokens":10,"outputTokens":5,'
                b'"cacheReadInputTokens":7,"cacheWriteInputTokens":3}}'
            )

    class _Client:
        def invoke_model(self, **kwargs):
            return {"body": _Body()}

    _, usage = forward_invoke_model(_Client(), "m", {})
    assert usage["cache_read_input_tokens"] == 7
    assert usage["cache_write_input_tokens"] == 3


def test_forward_invoke_model_anthropic_zero_wins_when_key_present():
    class _Body:
        def read(self):
            return (
                b'{"usage":{"input_tokens":0,"output_tokens":0,"inputTokens":5,"outputTokens":6}}'
            )

    class _Client:
        def invoke_model(self, **kwargs):
            return {"body": _Body()}

    _, usage = forward_invoke_model(_Client(), "m", {})
    assert usage["input_tokens"] == 0
    assert usage["output_tokens"] == 0


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
