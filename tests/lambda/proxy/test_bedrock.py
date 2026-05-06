from bedrock import forward_invoke_model, normalize_usage


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
