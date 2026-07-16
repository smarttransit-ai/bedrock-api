"""Input-token estimation per protocol (limits.estimate_input_tokens)."""

import math

from limits import estimate_input_tokens
from routes import ROUTE_CONVERSE, ROUTE_INVOKE, ROUTE_RESPONSES


def test_responses_string_input():
    body = {"input": "x" * 400}
    assert estimate_input_tokens(body, ROUTE_RESPONSES) == 100


def test_responses_instructions_are_counted():
    body = {"input": "x" * 400, "instructions": "y" * 400}
    assert estimate_input_tokens(body, ROUTE_RESPONSES) == 200


def test_responses_structured_input_items():
    body = {
        "input": [
            {"role": "user", "content": [{"type": "input_text", "text": "x" * 200}]},
            {"role": "user", "content": "y" * 200},
        ]
    }
    assert estimate_input_tokens(body, ROUTE_RESPONSES) == 100


def test_responses_estimate_beats_the_json_envelope_fallback():
    """Without a responses branch this would fall back to len(json.dumps(body)).

    The envelope is much larger than the prompt, so the fallback would over-estimate and
    could trip check_input_cap on a request that is actually within the cap.
    """
    body = {"input": "hi", "model": "openai.gpt-5.6-luna", "max_output_tokens": 4096}
    envelope_estimate = math.ceil(len(__import__("json").dumps(body)) / 4)
    assert estimate_input_tokens(body, ROUTE_RESPONSES) < envelope_estimate


def test_responses_empty_input_falls_back_to_envelope():
    """A body with no readable text still yields a non-zero estimate (shared fallback)."""
    assert estimate_input_tokens({"model": "openai.gpt-5.6-luna"}, ROUTE_RESPONSES) > 0


def test_responses_tolerates_malformed_shapes():
    for payload in ({"input": 7}, {"input": [None, 3]}, {"input": [{"content": 9}]}):
        assert estimate_input_tokens(payload, ROUTE_RESPONSES) > 0  # envelope fallback


def test_converse_and_invoke_estimates_unchanged():
    converse = {"messages": [{"content": [{"text": "x" * 400}]}]}
    assert estimate_input_tokens(converse, ROUTE_CONVERSE) == 100
    invoke = {"messages": [{"content": "x" * 400}]}
    assert estimate_input_tokens(invoke, ROUTE_INVOKE) == 100
