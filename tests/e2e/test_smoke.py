"""End-to-end smoke test for the bedrock-api gateway.

Skipped unless E2E=1 is set in the environment.

Required environment variables:
  BEDROCK_API_URL     — deployed API Gateway URL (no trailing slash)
  BEDROCK_API_REGION  — AWS region of the deployed stack

Optional environment variables:
  BEDROCK_API_TABLE_PREFIX — DynamoDB table name prefix (default: bedrock-api)
  BEDROCK_API_MODEL_ID     — Bedrock model to use (default: Claude Sonnet 4.6)

AWS credentials must be configured (e.g. AWS_PROFILE, instance profile) so
that the CLI can write to DynamoDB and this test can read from it.
"""

import os
import subprocess
from datetime import UTC, datetime
from urllib.parse import quote

import boto3
import httpx
import pytest

pytestmark = pytest.mark.skipif(not os.getenv("E2E"), reason="E2E=1 not set")

# ---------------------------------------------------------------------------
# Configuration from environment
# ---------------------------------------------------------------------------

API_URL = os.getenv("BEDROCK_API_URL", "")
REGION = os.getenv("BEDROCK_API_REGION", "us-east-1")
TABLE_PREFIX = os.getenv("BEDROCK_API_TABLE_PREFIX", "bedrock-api")
MODEL_ID = os.getenv(
    "BEDROCK_API_MODEL_ID",
    "us.anthropic.claude-sonnet-4-6",
)

# Minimal Converse request body
_PAYLOAD = {
    "messages": [
        {
            "role": "user",
            "content": [{"type": "text", "text": "Say hello in one word."}],
        }
    ]
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _cli(*args: str) -> subprocess.CompletedProcess:
    """Run the bedrock-api CLI with shared region + table-prefix flags."""
    cmd = [
        "bedrock-api",
        "--region",
        REGION,
        "--table-prefix",
        TABLE_PREFIX,
        *args,
    ]
    return subprocess.run(cmd, capture_output=True, text=True)


@pytest.fixture()
def issued_token():
    """Issue an e2e-smoke token; revoke it at teardown (idempotent)."""
    result = _cli("issue", "e2e-smoke")
    assert result.returncode == 0, f"bedrock-api issue failed:\n{result.stderr}"

    bearer = result.stdout.strip()
    # Token format: bk_<32hex>.<64hex>
    token_id = bearer.split(".")[0]

    yield token_id, bearer

    # Teardown — revoke regardless of test outcome (idempotent on double-revoke)
    _cli("revoke", token_id)


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


def test_smoke(issued_token):
    token_id, bearer = issued_token

    converse_url = f"{API_URL}/model/{quote(MODEL_ID, safe='')}/converse"
    headers = {"Authorization": f"Bearer {bearer}"}

    # (b) Call the API with the issued token — expect 200
    resp = httpx.post(converse_url, json=_PAYLOAD, headers=headers, timeout=30.0)
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"

    # (c) Verify usage counters were written
    period = datetime.now(UTC).strftime("%Y-%m")
    usage_table = boto3.resource("dynamodb", region_name=REGION).Table(f"{TABLE_PREFIX}-usage")
    usage = usage_table.get_item(Key={"token_id": token_id, "period": period}).get("Item", {})
    assert int(usage.get("requests", 0)) >= 1, "Expected requests >= 1 in usage table"
    assert int(usage.get("output_tokens", 0)) > 0, "Expected output_tokens > 0 in usage table"

    # (d) Revoke the token
    revoke = _cli("revoke", token_id)
    assert revoke.returncode == 0, f"bedrock-api revoke failed:\n{revoke.stderr}"

    # (e) Same request should now be rejected with 401
    resp2 = httpx.post(converse_url, json=_PAYLOAD, headers=headers, timeout=30.0)
    assert resp2.status_code == 401, (
        f"Expected 401 after revoke, got {resp2.status_code}: {resp2.text}"
    )


def test_converse_stream_smoke(issued_token):
    """POST /model/{model_id}/converse-stream — chunks arrive + usage row incremented."""
    token_id, bearer = issued_token

    stream_url = f"{API_URL}/model/{quote(MODEL_ID, safe='')}/converse-stream"
    headers = {"Authorization": f"Bearer {bearer}"}

    with httpx.stream("POST", stream_url, json=_PAYLOAD, headers=headers, timeout=30.0) as resp:
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
        assert resp.headers.get("content-type", "").startswith("text/event-stream"), (
            f"Expected text/event-stream, got {resp.headers.get('content-type')}"
        )
        raw_chunks = list(resp.iter_text())

    assert any("data:" in chunk for chunk in raw_chunks), "Expected at least one SSE data frame"

    period = datetime.now(UTC).strftime("%Y-%m")
    usage_table = boto3.resource("dynamodb", region_name=REGION).Table(f"{TABLE_PREFIX}-usage")
    usage = usage_table.get_item(Key={"token_id": token_id, "period": period}).get("Item", {})
    assert int(usage.get("requests", 0)) >= 1, "Expected requests >= 1 in usage table"
    assert int(usage.get("output_tokens", 0)) > 0, "Expected output_tokens > 0 in usage table"


def test_invoke_stream_smoke(issued_token):
    """POST /model/{model_id}/invoke-with-response-stream — chunks arrive + usage row written."""
    token_id, bearer = issued_token

    # Anthropic native format for InvokeModel
    invoke_payload = {
        "messages": [{"role": "user", "content": "Say hello in one word."}],
        "max_tokens": 20,
        "anthropic_version": "bedrock-2023-05-31",
    }

    stream_url = f"{API_URL}/model/{quote(MODEL_ID, safe='')}/invoke-with-response-stream"
    headers = {"Authorization": f"Bearer {bearer}"}

    with httpx.stream(
        "POST", stream_url, json=invoke_payload, headers=headers, timeout=30.0
    ) as resp:
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
        assert resp.headers.get("content-type", "").startswith("text/event-stream"), (
            f"Expected text/event-stream, got {resp.headers.get('content-type')}"
        )
        raw_chunks = list(resp.iter_text())

    assert any("data:" in chunk for chunk in raw_chunks), "Expected at least one SSE data frame"

    period = datetime.now(UTC).strftime("%Y-%m")
    usage_table = boto3.resource("dynamodb", region_name=REGION).Table(f"{TABLE_PREFIX}-usage")
    usage = usage_table.get_item(Key={"token_id": token_id, "period": period}).get("Item", {})
    assert int(usage.get("requests", 0)) >= 1, "Expected requests >= 1 in usage table"
    assert int(usage.get("output_tokens", 0)) > 0, "Expected output_tokens > 0 in usage table"
