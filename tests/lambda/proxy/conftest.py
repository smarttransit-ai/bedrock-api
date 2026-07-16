"""Shared fixtures and helpers for lambda/proxy tests.

Module-level code runs before any test module is imported, ensuring env vars
and sys.path are configured before app.py reads them at import time.
"""

import hashlib
import os
import secrets
import sys

# Add lambda/proxy to sys.path so flat imports (from auth import ...) work in tests.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../../lambda/proxy"))

# Fake AWS credentials required by moto.
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SECURITY_TOKEN", "testing")
os.environ.setdefault("AWS_SESSION_TOKEN", "testing")

# Table names — must be set before app.py is imported (module-level reads).
os.environ.setdefault("TOKENS_TABLE", "test-tokens")
os.environ.setdefault("USAGE_TABLE", "test-usage")
os.environ.setdefault("RATE_LIMIT_TABLE", "test-rate-limit")
os.environ.setdefault("BEDROCK_REGION", "us-east-1")
os.environ.setdefault("PRICING_BUCKET", "test-pricing")
os.environ.setdefault("PRICING_OBJECT_KEY", "pricing/current.json")
os.environ.setdefault("LITELLM_SOURCE_URL", "https://example.invalid/litellm.json")
os.environ.setdefault("PRICING_CACHE_TTL_S", "60")

import boto3  # noqa: E402 (must come after env var setup)
import pytest
from moto import mock_aws

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TOKENS_TABLE = os.environ["TOKENS_TABLE"]
USAGE_TABLE = os.environ["USAGE_TABLE"]
RATE_LIMIT_TABLE = os.environ["RATE_LIMIT_TABLE"]
# A real catalog entry (priced, not fallback) that also contains a colon, so the
# encoded form below exercises %3A path-decoding on every request that uses it.
DEFAULT_MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
# URL-encoded form: colons in path segments must be percent-encoded (%3A).
# FastAPI/Starlette decodes %3A → ':' before it reaches the route handler.
ENCODED_MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1%3A0"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def aws_mock():
    """Wrap each test in a fresh moto AWS environment."""
    with mock_aws():
        yield


@pytest.fixture(autouse=True)
def _reset_pricing_cache():
    """Reset the pricing module's live-catalog cache before each test (no cross-test bleed).

    Deferred import (mirrors app_client) so pricing's module-level env reads happen
    after this conftest's env setup.
    """
    import pricing

    pricing.invalidate_cache()
    yield


@pytest.fixture()
def pricing_bucket():
    """Create the live-pricing S3 bucket inside the moto context; return its name."""
    bucket = os.environ["PRICING_BUCKET"]
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=bucket)
    return bucket


@pytest.fixture()
def tables():
    """Create the three DynamoDB tables and return (tokens, usage, rate_limit)."""
    dynamodb = boto3.resource("dynamodb", region_name="us-east-1")

    tokens_table = dynamodb.create_table(
        TableName=TOKENS_TABLE,
        KeySchema=[{"AttributeName": "token_id", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "token_id", "AttributeType": "S"},
            {"AttributeName": "owner", "AttributeType": "S"},
            {"AttributeName": "created_at", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "owner-index",
                "KeySchema": [
                    {"AttributeName": "owner", "KeyType": "HASH"},
                    {"AttributeName": "created_at", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
        BillingMode="PAY_PER_REQUEST",
    )

    usage_table = dynamodb.create_table(
        TableName=USAGE_TABLE,
        KeySchema=[
            {"AttributeName": "token_id", "KeyType": "HASH"},
            {"AttributeName": "period", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "token_id", "AttributeType": "S"},
            {"AttributeName": "period", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )

    rate_limit_table = dynamodb.create_table(
        TableName=RATE_LIMIT_TABLE,
        KeySchema=[
            {"AttributeName": "token_id", "KeyType": "HASH"},
            {"AttributeName": "window_second", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "token_id", "AttributeType": "S"},
            {"AttributeName": "window_second", "AttributeType": "N"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )

    return tokens_table, usage_table, rate_limit_table


@pytest.fixture()
def test_token(tables):
    """Create an active token row and return (token_id, bearer_token, tables)."""
    tokens_table, usage_table, rate_limit_table = tables
    token_id, bearer_token, secret_hash = _make_token()
    tokens_table.put_item(
        Item={
            "token_id": token_id,
            "secret_hash": secret_hash,
            "owner": "test-owner",
            "created_at": "2026-01-01T00:00:00Z",
            "status": "active",
        }
    )
    return token_id, bearer_token, tables


@pytest.fixture()
def bedrock_stub():
    """Return a (client, Stubber) pair for bedrock-runtime."""
    from botocore.stub import Stubber

    client = boto3.client("bedrock-runtime", region_name="us-east-1")
    stubber = Stubber(client)
    return client, stubber


@pytest.fixture()
def app_client(test_token, bedrock_stub):
    """TestClient with dependency_overrides for get_tables and get_bedrock.

    Imports get_tables/get_bedrock from deps (not app) to avoid dependency-override
    identity fragility (amendment R4).

    Yields (http_client, token_id, bearer_token, tables, stubber).
    """
    from app import app
    from deps import get_bedrock, get_tables
    from fastapi.testclient import TestClient

    token_id, bearer_token, tables = test_token
    client_bedrock, stubber = bedrock_stub
    app.dependency_overrides[get_tables] = lambda: tables
    app.dependency_overrides[get_bedrock] = lambda: client_bedrock
    with TestClient(app, raise_server_exceptions=False) as http_client:
        yield http_client, token_id, bearer_token, tables, stubber
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_token() -> tuple[str, str, str]:
    """Generate (token_id, bearer_token, secret_hash) for test fixtures."""
    token_id = f"bk_{secrets.token_bytes(16).hex()}"
    secret = secrets.token_bytes(32).hex()
    salt = os.urandom(16)
    digest = hashlib.sha256(bytes.fromhex(salt.hex()) + bytes.fromhex(secret)).hexdigest()
    secret_hash = f"{salt.hex()}:{digest}"
    bearer_token = f"{token_id}.{secret}"
    return token_id, bearer_token, secret_hash


def converse_response(
    text: str = "Hello!",
    in_tokens: int = 10,
    out_tokens: int = 5,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> dict:
    """Build a minimal Bedrock Converse API service response for the Stubber."""
    return {
        "output": {
            "message": {
                "role": "assistant",
                "content": [{"text": text}],
            }
        },
        "stopReason": "end_turn",
        "usage": {
            "inputTokens": in_tokens,
            "outputTokens": out_tokens,
            "cacheReadInputTokens": cache_read_tokens,
            "cacheWriteInputTokens": cache_write_tokens,
            "totalTokens": in_tokens + out_tokens,
        },
        "metrics": {"latencyMs": 50},
    }


# ---------------------------------------------------------------------------
# litellm fixture helpers
# ---------------------------------------------------------------------------


def litellm_raw_from_catalog(catalog: dict, factor: float = 1.0) -> dict:
    """Build a raw litellm-shaped map that round-trips *catalog* through build_catalog.

    Inverts the catalog's key namespacing: a ``mantle/<id>`` entry must be emitted as
    ``bedrock_mantle/<id>`` with litellm_provider ``bedrock_mantle``, everything else as
    ``bedrock/<id>`` with ``bedrock_converse``. Prefixing mantle keys with ``bedrock/``
    instead would leave a slash in the stripped key, so filter_bedrock would drop them and
    the rebuilt catalog would silently lose the whole mantle family.

    ``factor`` scales every rate (used to exercise the drift band).
    """
    from pricing_catalog import MANTLE_NAMESPACE

    raw = {}
    for model_id, modes in catalog.items():
        od = modes["on_demand"]
        if model_id.startswith(MANTLE_NAMESPACE):
            key = f"bedrock_mantle/{model_id.removeprefix(MANTLE_NAMESPACE)}"
            provider = "bedrock_mantle"
        else:
            key = f"bedrock/{model_id}"
            provider = "bedrock_converse"
        raw[key] = {
            "litellm_provider": provider,
            "input_cost_per_token": od["input_usd_micros_per_1k"] / 1e9 * factor,
            "output_cost_per_token": od["output_usd_micros_per_1k"] / 1e9 * factor,
        }
    return raw
