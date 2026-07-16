"""FastAPI dependency providers for the proxy Lambda.

These are thin wrappers around the AWS clients so that tests can override them via
``app.dependency_overrides`` without modifying ``app.py``. The boto3 providers are
called once per request (no module-level caching); ``get_mantle`` is the documented
exception — see its docstring.
"""

import os

import boto3
import httpx


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


def get_s3():
    """Return an S3 boto3 client (for the live pricing catalog object)."""
    return boto3.client("s3")


_MANTLE_CLIENT: httpx.Client | None = None


def get_mantle():
    """Return the shared httpx client for the bedrock-mantle OpenAI-compatible endpoint.

    boto3 cannot address bedrock-mantle: it is not a bedrock-runtime operation but an
    OpenAI-shaped REST path (/openai/v1/responses) on its own host, so it needs a plain
    HTTP client with SigV4-signed requests (see mantle.sign_headers).

    Unlike the boto3 providers above, this one IS cached across requests — deliberately.
    httpx.Client owns a connection pool and defines no __del__, so a per-request client is
    never closed and leaks its pooled sockets for the life of the execution context. A
    single long-lived client also reuses TLS connections across invocations. It is
    thread-safe, and it holds no credentials — those are resolved per call in
    mantle.sign_headers, so caching here does not pin rotated credentials.

    Tests still override this via app.dependency_overrides (which replaces the provider
    outright), so the cache does not leak state between tests.

    The read timeout is generous because it also covers streaming responses, where the
    gap between SSE frames — not the total call — is what must not time out.
    """
    global _MANTLE_CLIENT
    if _MANTLE_CLIENT is None:
        region = os.environ.get("BEDROCK_REGION", "us-east-1")
        base_url = os.environ.get("MANTLE_ENDPOINT_URL", f"https://bedrock-mantle.{region}.api.aws")
        _MANTLE_CLIENT = httpx.Client(
            base_url=base_url,
            timeout=httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0),
        )
    return _MANTLE_CLIENT
