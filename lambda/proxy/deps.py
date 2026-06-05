"""FastAPI dependency providers for the proxy Lambda.

These are thin wrappers around boto3 so that tests can override them via
``app.dependency_overrides`` without modifying ``app.py``. Each function is
called once per request (no module-level caching) so test overrides take effect.
"""

import os

import boto3


def get_tables():
    """Return (tokens_table, usage_table, rate_limit_table) DynamoDB Table objects."""
    dynamodb = boto3.resource("dynamodb")
    tokens_table = dynamodb.Table(os.environ["TOKENS_TABLE"])
    usage_table = dynamodb.Table(os.environ["USAGE_TABLE"])
    rate_limit_table = dynamodb.Table(os.environ["RATE_LIMIT_TABLE"])
    return tokens_table, usage_table, rate_limit_table


def get_bedrock():
    """Return a bedrock-runtime boto3 client."""
    region = os.environ.get("BEDROCK_REGION", "us-east-1")
    return boto3.client("bedrock-runtime", region_name=region)
