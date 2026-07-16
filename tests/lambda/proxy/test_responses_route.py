"""Tests for POST /openai/v1/responses (the bedrock-mantle route, issue #8).

Real tables (moto), real pricing catalog, real httpx client + SigV4 — only the mantle
socket is faked, so preflight → cap → forward → billing all run for real.
"""

import json

import httpx
import pytest
from conftest import _make_token

MODEL = "openai.gpt-5.6-luna"
# Catalog rates for mantle/openai.gpt-5.6-luna: $1.10/$6.60 per 1M (µUSD per 1k tokens).
LUNA_INPUT_RATE = 1100
LUNA_OUTPUT_RATE = 6600
# The Opus-tier fallback an uncatalogued model would hit — the bug this route must avoid.
FALLBACK_INPUT_RATE = 15_000

USAGE = {
    "input_tokens": 1000,
    "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
    "output_tokens": 1000,
    "output_tokens_details": {"reasoning_tokens": 800},
}


def _mantle_client(handler) -> httpx.Client:
    return httpx.Client(
        base_url="https://bedrock-mantle.us-east-1.api.aws",
        transport=httpx.MockTransport(handler),
    )


def _ok_handler(request):
    return httpx.Response(200, json={"output": [], "usage": USAGE})


def _sse_handler(request):
    completed = {"type": "response.completed", "response": {"usage": USAGE}}
    body = (
        "event: response.output_text.delta\n"
        'data: {"type": "response.output_text.delta", "delta": "hi"}\n'
        "\n"
        "event: response.completed\n"
        f"data: {json.dumps(completed)}\n"
        "\n"
    )
    return httpx.Response(200, text=body)


@pytest.fixture()
def responses_client(test_token):
    """TestClient with get_tables + get_mantle overridden. Yields a factory."""
    from app import app
    from deps import get_mantle, get_tables
    from fastapi.testclient import TestClient

    token_id, bearer_token, tables = test_token
    app.dependency_overrides[get_tables] = lambda: tables

    def _make(handler=_ok_handler):
        app.dependency_overrides[get_mantle] = lambda: _mantle_client(handler)
        return TestClient(app, raise_server_exceptions=False)

    yield _make, token_id, bearer_token, tables
    app.dependency_overrides.clear()


def _auth(bearer):
    return {"Authorization": f"Bearer {bearer}"}


def _pricing_audit(caplog) -> dict:
    for record in reversed(caplog.records):
        try:
            payload = json.loads(record.message)
        except (json.JSONDecodeError, ValueError):
            continue
        if payload.get("event") == "pricing_audit" and payload.get("status") == 200:
            return payload
    raise AssertionError("no successful pricing_audit event emitted")


# ---------------------------------------------------------------------------
# Happy path + billing
# ---------------------------------------------------------------------------


def test_responses_happy_path_bills_at_catalog_rates(responses_client, caplog):
    make, token_id, bearer, tables = responses_client
    with caplog.at_level("INFO"):
        resp = make().post(
            "/openai/v1/responses", json={"model": MODEL, "input": "hi"}, headers=_auth(bearer)
        )
    assert resp.status_code == 200

    audit = _pricing_audit(caplog)
    assert audit["fallback_applied"] is False
    assert audit["applied_rates"]["input_usd_micros_per_1k"] == LUNA_INPUT_RATE
    assert audit["applied_rates"]["output_usd_micros_per_1k"] == LUNA_OUTPUT_RATE
    # 1000 in + 1000 out at $1.10/$6.60 per 1M = 1100 + 6600 µUSD
    assert audit["usd_micros"] == LUNA_INPUT_RATE + LUNA_OUTPUT_RATE

    _, usage_table, _ = tables
    from datetime import UTC, datetime

    period = datetime.now(UTC).strftime("%Y-%m")
    row = usage_table.get_item(Key={"token_id": token_id, "period": period})["Item"]
    assert int(row["input_tokens"]) == 1000
    assert int(row["usd_micros"]) == LUNA_INPUT_RATE + LUNA_OUTPUT_RATE


def test_reasoning_tokens_logged_but_not_billed(responses_client, caplog):
    """DD2: 800 reasoning tokens are inside the 1000 output tokens, not added to them."""
    make, _, bearer, _ = responses_client
    with caplog.at_level("INFO"):
        make().post(
            "/openai/v1/responses", json={"model": MODEL, "input": "hi"}, headers=_auth(bearer)
        )
    audit = _pricing_audit(caplog)
    assert audit["reasoning_tokens"] == 800
    assert audit["output_tokens"] == 1000  # not 1800
    assert audit["component_micros"]["output_usd_micros"] == LUNA_OUTPUT_RATE


def test_streaming_bills_from_stream_tail_at_catalog_rates(responses_client, caplog):
    """Regression: streaming billed via pf.model_id would silently hit the Opus fallback."""
    make, token_id, bearer, tables = responses_client
    with caplog.at_level("INFO"):
        resp = make(_sse_handler).post(
            "/openai/v1/responses",
            json={"model": MODEL, "input": "hi", "stream": True},
            headers=_auth(bearer),
        )
        assert resp.status_code == 200
        body = resp.text

    # event: lines must survive the relay — OpenAI SDK clients dispatch on them.
    assert "event: response.output_text.delta" in body
    assert "event: response.completed" in body

    audit = _pricing_audit(caplog)
    assert audit["fallback_applied"] is False
    assert audit["applied_rates"]["input_usd_micros_per_1k"] == LUNA_INPUT_RATE
    assert audit["applied_rates"]["input_usd_micros_per_1k"] != FALLBACK_INPUT_RATE
    assert audit["reasoning_tokens"] == 800

    _, usage_table, _ = tables
    from datetime import UTC, datetime

    period = datetime.now(UTC).strftime("%Y-%m")
    row = usage_table.get_item(Key={"token_id": token_id, "period": period})["Item"]
    assert int(row["usd_micros"]) == LUNA_INPUT_RATE + LUNA_OUTPUT_RATE


# ---------------------------------------------------------------------------
# Model resolution from body
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("body", [{"input": "hi"}, {"model": "", "input": "hi"}, {"model": 7}])
def test_missing_or_invalid_model_is_400(responses_client, body):
    make, _, bearer, _ = responses_client
    resp = make().post("/openai/v1/responses", json=body, headers=_auth(bearer))
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "BAD_REQUEST"


def test_model_id_from_body_is_logged(responses_client, caplog):
    make, _, bearer, _ = responses_client
    with caplog.at_level("INFO"):
        make().post(
            "/openai/v1/responses", json={"model": MODEL, "input": "hi"}, headers=_auth(bearer)
        )
    assert _pricing_audit(caplog)["model_id"] == MODEL


# ---------------------------------------------------------------------------
# Auth / limits parity with the Bedrock routes
# ---------------------------------------------------------------------------


def test_no_token_is_401(responses_client):
    make, _, _, _ = responses_client
    assert (
        make().post("/openai/v1/responses", json={"model": MODEL, "input": "hi"}).status_code == 401
    )


def test_malformed_json_from_unauthenticated_caller_is_401_not_400(responses_client):
    """Auth must precede body parsing — the body-carried model must not reorder the checks."""
    make, _, _, _ = responses_client
    resp = make().post(
        "/openai/v1/responses",
        content=b"{not json",
        headers={"Authorization": "Bearer bk_bogus.deadbeef", "content-type": "application/json"},
    )
    assert resp.status_code == 401


def test_allowlist_blocks_body_carried_model(responses_client, tables):
    """A body-carried model is gated exactly like a path-carried one."""
    make, _, _, _ = responses_client  # fixture already overrides get_tables with `tables`
    tokens_table, _, _ = tables
    tid, bearer, shash = _make_token()
    tokens_table.put_item(
        Item={
            "token_id": tid,
            "secret_hash": shash,
            "owner": "restricted",
            "status": "active",
            "allowed_models": {"anthropic.claude-sonnet-4-6"},
        }
    )
    resp = make().post(
        "/openai/v1/responses", json={"model": MODEL, "input": "hi"}, headers=_auth(bearer)
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "MODEL_NOT_ALLOWED"


def test_output_cap_applies_max_output_tokens(responses_client, tables):
    """Responses uses max_output_tokens; setting max_tokens would leave the cap unenforced."""
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"usage": USAGE})

    make, _, _, _ = responses_client  # fixture already overrides get_tables with `tables`
    tokens_table, _, _ = tables
    tid, bearer, shash = _make_token()
    tokens_table.put_item(
        Item={
            "token_id": tid,
            "secret_hash": shash,
            "owner": "capped",
            "status": "active",
            "limit_max_output_tokens": 64,
        }
    )
    make(handler).post(
        "/openai/v1/responses",
        json={"model": MODEL, "input": "hi", "max_output_tokens": 4096},
        headers=_auth(bearer),
    )
    assert seen["body"]["max_output_tokens"] == 64
    assert "max_tokens" not in seen["body"]


def test_upstream_error_is_mapped(responses_client):
    def handler(request):
        return httpx.Response(
            400, json={"error": {"type": "validation_error", "message": "unsupported API"}}
        )

    make, _, bearer, _ = responses_client
    resp = make(handler).post(
        "/openai/v1/responses", json={"model": MODEL, "input": "hi"}, headers=_auth(bearer)
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "BAD_REQUEST"


def test_billing_failure_still_returns_the_upstream_response(responses_client, caplog, monkeypatch):
    """The upstream already ran and consumed tokens — a compute_cost failure must not 500."""
    import app

    def boom(**_):
        raise RuntimeError("pricing catalog corrupt")

    monkeypatch.setattr(app, "compute_cost", boom)
    make, _, bearer, _ = responses_client
    with caplog.at_level("INFO"):
        resp = make().post(
            "/openai/v1/responses", json={"model": MODEL, "input": "hi"}, headers=_auth(bearer)
        )
    assert resp.status_code == 200  # caller gets their answer despite the billing failure
    assert resp.json()["usage"]["output_tokens"] == 1000
    assert any(
        r.message.startswith("{") and '"billing_failed"' in r.message for r in caplog.records
    )


def test_midstream_error_emits_terminal_frame_and_skips_billing(responses_client, caplog):
    """A 200 stream that dies mid-flight: caller gets a terminal error frame; no partial bill.

    Usage never arrives (the completed event is lost), so _post_flight_write must skip the
    write rather than bill a partial/zero-token request.
    """

    def handler(request):
        # A first frame, then a truncated line with no terminal response.completed event.
        return httpx.Response(
            200, text='event: response.output_text.delta\ndata: {"delta": "hi"}\n\n'
        )

    make, token_id, bearer, tables = responses_client
    with caplog.at_level("INFO"):
        resp = make(handler).post(
            "/openai/v1/responses",
            json={"model": MODEL, "input": "hi", "stream": True},
            headers=_auth(bearer),
        )
        assert resp.status_code == 200
        assert "event: response.output_text.delta" in resp.text

    # No terminal usage event → no usage row written (not a zero-token bill).
    _, usage_table, _ = tables
    from datetime import UTC, datetime

    period = datetime.now(UTC).strftime("%Y-%m")
    assert "Item" not in usage_table.get_item(Key={"token_id": token_id, "period": period})
    # And no successful pricing_audit was emitted.
    audits = [
        json.loads(r.message)
        for r in caplog.records
        if r.message.startswith("{") and '"pricing_audit"' in r.message
    ]
    assert all(a.get("status") != 200 for a in audits)
