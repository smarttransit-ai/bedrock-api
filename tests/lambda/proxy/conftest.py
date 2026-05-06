"""Shared fixtures and helpers for lambda/proxy tests.

Module-level code runs before any test module is imported, ensuring env vars
and sys.path are configured before handler.py reads them at import time.
"""

import hashlib
import json
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

# Table names — must be set before handler.py is imported (module-level reads).
os.environ.setdefault("TOKENS_TABLE", "test-tokens")
os.environ.setdefault("USAGE_TABLE", "test-usage")
os.environ.setdefault("RATE_LIMIT_TABLE", "test-rate-limit")
os.environ.setdefault("BEDROCK_REGION", "us-east-1")

import boto3  # noqa: E402 (must come after env var setup)
import pytest
from moto import mock_aws

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TOKENS_TABLE = os.environ["TOKENS_TABLE"]
USAGE_TABLE = os.environ["USAGE_TABLE"]
RATE_LIMIT_TABLE = os.environ["RATE_LIMIT_TABLE"]
DEFAULT_MODEL = "us.anthropic.claude-sonnet-4-6"
ENCODED_MODEL = "us.anthropic.claude-sonnet-4-6"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def aws_mock():
    """Wrap each test in a fresh moto AWS environment."""
    with mock_aws():
        yield


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


def make_event(
    path: str,
    body: dict,
    bearer_token: str,
    method: str = "POST",
) -> dict:
    """Build a minimal API Gateway HTTP API v2 event."""
    return {
        "version": "2.0",
        "routeKey": f"{method} {path}",
        "rawPath": path,
        "rawQueryString": "",
        "headers": {
            "authorization": f"Bearer {bearer_token}",
            "content-type": "application/json",
        },
        "requestContext": {"http": {"method": method, "path": path, "protocol": "HTTP/1.1"}},
        "body": json.dumps(body),
        "isBase64Encoded": False,
    }


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
