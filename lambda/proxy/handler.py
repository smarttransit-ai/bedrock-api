"""Bedrock proxy Lambda — entry point.

API Gateway HTTP API v2 (payloadFormatVersion=2.0).

Environment variables (required):
  TOKENS_TABLE      — DynamoDB tokens table name
  USAGE_TABLE       — DynamoDB usage table name
  RATE_LIMIT_TABLE  — DynamoDB rate_limit table name

Environment variables (optional):
  BEDROCK_REGION          — AWS region for bedrock-runtime (default: us-east-1)
  ALLOWED_MODELS_DEFAULT  — Comma-separated default model allowlist; applied when
                            a token has no allowed_models attribute.
                            Empty string (default) = no system-level restriction.
  PRICING_JSON            — JSON object overriding the built-in price map.

Pre-flight order (cheap rejects first):
  1. parse bearer token from Authorization header         → 401
  2. DynamoDB GetItem on tokens table
  3. revoked / missing check                              → 401
  4. secret verification (SHA-256 + hmac.compare_digest) → 401
  5. per-second rate limit (conditional ADD)              → 429
  6. monthly request quota   ┐ single usage GetItem       → 429
  7. monthly USD budget      ┘                            → 429
  8. input token cap heuristic (ceil(chars/4))            → 413
  9. model allowlist check                                → 403
 10. forward to Bedrock (Converse primary, InvokeModel fallback)
 11. post-flight usage ADD (failure → log ERROR, still return 200)
"""

import base64
import json
import logging
import os
import time
from datetime import UTC, datetime

import boto3
from auth import AuthError, parse_bearer_token, verify_secret
from bedrock import (
    BedrockError,
    apply_output_cap,
    forward_converse,
    forward_invoke_model,
    parse_route,
)
from limits import (
    LimitError,
    check_input_cap,
    check_model_allowlist,
    check_monthly_quota,
    check_rate_limit,
    estimate_input_tokens,
    write_usage,
)
from pricing import compute_cost

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Read required table names at import time (cached across warm Lambda invocations).
TOKENS_TABLE = os.environ["TOKENS_TABLE"]
USAGE_TABLE = os.environ["USAGE_TABLE"]
RATE_LIMIT_TABLE = os.environ["RATE_LIMIT_TABLE"]


def _bedrock_region() -> str:
    return os.environ.get("BEDROCK_REGION", "us-east-1")


def _allowed_models_default() -> list[str]:
    raw = os.environ.get("ALLOWED_MODELS_DEFAULT", "")
    return [m.strip() for m in raw.split(",") if m.strip()]


def handler(
    event: dict,
    context,
    *,
    _bedrock_client=None,
    _tables: tuple | None = None,
) -> dict:
    """Lambda handler.

    _bedrock_client and _tables are injected only in tests.
    In production, boto3 clients are created here (one per Lambda instance,
    reused across warm invocations via the module-level closure).
    """
    start_ms = time.monotonic() * 1000

    if _tables is not None:
        tokens_table, usage_table, rate_limit_table = _tables
    else:
        dynamodb = boto3.resource("dynamodb")
        tokens_table = dynamodb.Table(TOKENS_TABLE)
        usage_table = dynamodb.Table(USAGE_TABLE)
        rate_limit_table = dynamodb.Table(RATE_LIMIT_TABLE)

    if _bedrock_client is None:
        _bedrock_client = boto3.client("bedrock-runtime", region_name=_bedrock_region())

    log_ctx: dict = {}

    try:
        # --- Step 1-4: Authentication ---
        token_id, secret = parse_bearer_token(event)
        log_ctx["token_id"] = token_id

        token_row = tokens_table.get_item(Key={"token_id": token_id}).get("Item")
        if not token_row or token_row.get("status") != "active":
            raise AuthError("INVALID_TOKEN", "Invalid or revoked token")
        if not verify_secret(secret, token_row["secret_hash"]):
            raise AuthError("INVALID_TOKEN", "Invalid token secret")

        log_ctx["owner"] = token_row.get("owner", "unknown")

        # --- Step 5: Rate limit ---
        check_rate_limit(token_id, token_row, rate_limit_table)

        # --- Steps 6-7: Monthly quota + budget (single usage read) ---
        period = datetime.now(UTC).strftime("%Y-%m")
        usage = usage_table.get_item(Key={"token_id": token_id, "period": period}).get("Item", {})
        check_monthly_quota(token_row, usage)

        # --- Parse request body and route ---
        body_str = event.get("body") or "{}"
        if event.get("isBase64Encoded"):
            body_str = base64.b64decode(body_str).decode("utf-8")
        try:
            body = json.loads(body_str)
        except (json.JSONDecodeError, ValueError) as exc:
            raise BedrockError("BAD_REQUEST", "Invalid JSON in request body", 400) from exc

        model_id, route = parse_route(event)
        log_ctx["model_id"] = model_id

        # --- Step 8: Input token cap (heuristic) ---
        check_input_cap(estimate_input_tokens(body, route), token_row)

        # --- Step 9: Model allowlist ---
        check_model_allowlist(model_id, token_row, _allowed_models_default())

        # --- Step 10: Forward to Bedrock ---
        max_out = (
            int(token_row["limit_max_output_tokens"])
            if "limit_max_output_tokens" in token_row
            else None
        )
        body = apply_output_cap(body, route, max_out)

        if route == "converse":
            bedrock_resp, in_tok, out_tok = forward_converse(_bedrock_client, model_id, body)
        else:
            bedrock_resp, in_tok, out_tok = forward_invoke_model(_bedrock_client, model_id, body)

        log_ctx.update(input_tokens=in_tok, output_tokens=out_tok)

        # --- Compute cost (integer USD-micros, no floats) ---
        usd_micros = compute_cost(model_id, in_tok, out_tok)
        log_ctx["usd_micros"] = usd_micros

        # --- Step 11: Post-flight usage write ---
        # If this fails, log at ERROR and still return the response.
        # We accept rare under-counting over returning 5xx to a client that already
        # received a valid Bedrock response.
        try:
            write_usage(token_id, period, in_tok, out_tok, usd_micros, usage_table)
        except Exception as exc:
            logger.error(
                json.dumps(
                    {
                        **log_ctx,
                        "event": "usage_write_failed",
                        "error": str(exc),
                    }
                )
            )

        latency_ms = int((time.monotonic() * 1000) - start_ms)
        logger.info(
            json.dumps(
                {
                    **log_ctx,
                    "event": "request_complete",
                    "status": 200,
                    "latency_ms": latency_ms,
                }
            )
        )
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(bedrock_resp),
        }

    except (AuthError, LimitError, BedrockError) as exc:
        latency_ms = int((time.monotonic() * 1000) - start_ms)
        logger.info(
            json.dumps(
                {
                    **log_ctx,
                    "event": "request_rejected",
                    "error_code": exc.code,
                    "status": exc.status,
                    "latency_ms": latency_ms,
                }
            )
        )
        return _error_response(exc.status, exc.code, exc.message)

    except Exception:
        latency_ms = int((time.monotonic() * 1000) - start_ms)
        logger.exception(
            json.dumps({**log_ctx, "event": "unhandled_error", "latency_ms": latency_ms})
        )
        return _error_response(500, "INTERNAL_ERROR", "Internal server error")


def _error_response(status: int, code: str, message: str) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"error": {"code": code, "message": message}}),
    }
