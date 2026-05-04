"""Shared fixtures for bedrock_api CLI tests.

Fake AWS credentials are set at module level so boto3 never attempts a real
AWS call during import or fixture setup. Same pattern as
tests/lambda/proxy/conftest.py.
"""

import os
import sys

# Fake credentials — must be set before boto3 is imported.
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SECURITY_TOKEN", "testing")
os.environ.setdefault("AWS_SESSION_TOKEN", "testing")

import boto3  # noqa: E402
import pytest
from moto import mock_aws

# CLI is installed editable via pyproject.toml so bedrock_api imports work
# without any sys.path manipulation.

TABLE_PREFIX = "test"
TOKENS_TABLE = f"{TABLE_PREFIX}-tokens"
USAGE_TABLE = f"{TABLE_PREFIX}-usage"


# ---------------------------------------------------------------------------
# Core fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def aws_mock():
    """Wrap each test in a fresh moto AWS environment."""
    with mock_aws():
        yield


@pytest.fixture()
def tables():
    """Create tokens and usage DynamoDB tables; return (tokens_table, usage_table)."""
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

    return tokens_table, usage_table


@pytest.fixture()
def tokens_table(tables):
    return tables[0]


@pytest.fixture()
def usage_table(tables):
    return tables[1]


# ---------------------------------------------------------------------------
# run_cli helper — fixture factory so capsys is available
# ---------------------------------------------------------------------------


@pytest.fixture()
def run_cli(capsys):
    """Return a callable run_cli(args_list) → (stdout, stderr, exit_code).

    Patches sys.argv, calls bedrock_api.cli:main(), captures output.
    Always uses --table-prefix test and --region us-east-1.
    """

    def _run(args: list[str]) -> tuple[str, str, int]:
        from bedrock_api.cli import main

        argv = ["bedrock-api", "--table-prefix", TABLE_PREFIX, "--region", "us-east-1"] + args
        old_argv = sys.argv
        sys.argv = argv
        exit_code = 0
        try:
            main()
        except SystemExit as exc:
            exit_code = exc.code if isinstance(exc.code, int) else 1
        finally:
            sys.argv = old_argv
        captured = capsys.readouterr()
        return captured.out, captured.err, exit_code

    return _run


# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------


def make_active_token(
    tokens_table, token_id: str | None = None, owner: str = "alice"
) -> tuple[str, str]:
    """Insert an active token row; return (token_id, bearer_token)."""
    import hashlib
    import secrets as _secrets

    if token_id is None:
        token_id = f"bk_{_secrets.token_hex(16)}"
    secret = _secrets.token_hex(32)
    salt_hex = _secrets.token_hex(16)
    digest = hashlib.sha256(bytes.fromhex(salt_hex) + bytes.fromhex(secret)).hexdigest()
    secret_hash = f"{salt_hex}:{digest}"

    tokens_table.put_item(
        Item={
            "token_id": token_id,
            "secret_hash": secret_hash,
            "owner": owner,
            "created_at": "2026-01-01T00:00:00+00:00",
            "status": "active",
        }
    )
    return token_id, f"{token_id}.{secret}"
