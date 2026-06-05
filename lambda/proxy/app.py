"""FastAPI proxy application — entry point for the LWA container Lambda.

Replaces handler.py. Served by uvicorn via the AWS Lambda Web Adapter (LWA)
with invoke_mode=RESPONSE_STREAM on a Lambda Function URL.

Environment variables (required):
  TOKENS_TABLE      — DynamoDB tokens table name
  USAGE_TABLE       — DynamoDB usage table name
  RATE_LIMIT_TABLE  — DynamoDB rate_limit table name

Environment variables (optional):
  BEDROCK_REGION          — AWS region for bedrock-runtime (default: us-east-1)
  ALLOWED_MODELS_DEFAULT  — Comma-separated default model allowlist
  PRICING_JSON            — JSON object overriding the built-in price map

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
 10. apply output cap (handler step, not preflight)
 11. forward to Bedrock
 12. post-flight usage ADD (failure → log ERROR, still return 200)

Phase B seam: streaming routes (POST /model/{model_id}/converse-stream and
POST /model/{model_id}/invoke-with-response-stream) attach here using
``run_preflight`` + ``bedrock.open_converse_stream`` / ``open_invoke_stream``
(per amendment B1) before constructing a StreamingResponse.
"""

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from auth import AuthError, parse_bearer_token_from_request, verify_secret
from bedrock import BedrockError, apply_output_cap, forward_converse, forward_invoke_model
from deps import get_bedrock, get_tables
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
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

# Ensure INFO-level logs emit under uvicorn / Lambda Web Adapter (which does
# not configure the root logger; without this, the root logger stays WARNING
# and pricing_audit / request_complete / request_rejected events are dropped).
# basicConfig installs a StreamHandler when no handlers exist (the common case
# under uvicorn/LWA). setLevel ensures the level is applied even when a handler
# was already attached (e.g. in test environments where pytest pre-configures
# logging before the app module is imported).
logging.basicConfig(level=logging.INFO)
logging.getLogger().setLevel(logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()


# ---------------------------------------------------------------------------
# PreflightResult dataclass
# ---------------------------------------------------------------------------


@dataclass
class PreflightResult:
    """Result of run_preflight — raw parsed body (output cap applied by handler)."""

    token_id: str
    token_row: dict
    period: str
    body: dict  # raw parsed request body; output cap is applied by the route handler (B5)
    model_id: str
    pricing_mode: str
    log_ctx: dict
    start_ms: float
    usage_table: object  # DynamoDB Table reference for post-flight write


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _allowed_models_default() -> list[str]:
    raw = os.environ.get("ALLOWED_MODELS_DEFAULT", "")
    return [m.strip() for m in raw.split(",") if m.strip()]


def _resolve_pricing_mode(token_row: dict) -> str:
    mode = token_row.get("pricing_mode", "on_demand")
    if mode in ("on_demand", "batch"):
        return mode
    raise BedrockError("INTERNAL_ERROR", f"Invalid pricing_mode {mode!r}", 500)


def _error_json(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


def _handle_known_error(exc, log_ctx: dict, start_ms: float) -> JSONResponse:
    latency_ms = int((time.monotonic() * 1000) - start_ms)
    logger.info(
        json.dumps(
            {
                **log_ctx,
                "event": "pricing_audit",
                "fallback_applied": False,
                "fallback_dimensions": [],
                "pricing_mode": log_ctx.get("pricing_mode", "on_demand"),
                "component_micros": {},
                "usd_micros": log_ctx.get("usd_micros"),
                "token_counters": {
                    "input_tokens": log_ctx.get("input_tokens", 0),
                    "output_tokens": log_ctx.get("output_tokens", 0),
                    "cache_read_input_tokens": log_ctx.get("cache_read_input_tokens", 0),
                    "cache_write_input_tokens": log_ctx.get("cache_write_input_tokens", 0),
                },
                "status": exc.status,
            }
        )
    )
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
    return _error_json(exc.status, exc.code, exc.message)


# ---------------------------------------------------------------------------
# Exception handler for unhandled errors
# ---------------------------------------------------------------------------


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception(json.dumps({"event": "unhandled_error"}))
    return _error_json(500, "INTERNAL_ERROR", "Internal server error")


# ---------------------------------------------------------------------------
# Pre-flight pipeline (steps 1–9)
# ---------------------------------------------------------------------------


async def run_preflight(
    request: Request,
    model_id: str,
    route: str,
    tables,
    bedrock_client,
) -> PreflightResult:
    """Execute pre-flight steps 1–9 and return a PreflightResult.

    Raises AuthError, LimitError, or BedrockError on rejection.
    Output cap (apply_output_cap) is NOT applied here — it is a separate
    handler step (amendment B5) so each route applies it explicitly.
    """
    start_ms = time.monotonic() * 1000
    tokens_table, usage_table, rate_limit_table = tables
    log_ctx: dict = {}

    # Step 1–4: Authentication
    token_id, secret = parse_bearer_token_from_request(request)
    log_ctx["token_id"] = token_id

    token_row = tokens_table.get_item(Key={"token_id": token_id}).get("Item")
    if not token_row or token_row.get("status") != "active":
        raise AuthError("INVALID_TOKEN", "Invalid or revoked token")
    if not verify_secret(secret, token_row["secret_hash"]):
        raise AuthError("INVALID_TOKEN", "Invalid token secret")

    log_ctx["owner"] = token_row.get("owner", "unknown")

    # Step 5: Rate limit
    check_rate_limit(token_id, token_row, rate_limit_table)

    # Steps 6–7: Monthly quota + budget (single usage read)
    period = datetime.now(UTC).strftime("%Y-%m")
    usage = usage_table.get_item(Key={"token_id": token_id, "period": period}).get("Item", {})
    check_monthly_quota(token_row, usage)

    # Step 7b: Parse request body
    # Starlette raises json.JSONDecodeError (a subclass of ValueError) for
    # malformed JSON; catching both matches the old handler parity and lets
    # genuine read errors (IOError, etc.) surface as 500.
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise BedrockError("BAD_REQUEST", "Invalid JSON in request body", 400) from exc

    log_ctx["model_id"] = model_id

    # Step 8: Input token cap (heuristic)
    check_input_cap(estimate_input_tokens(body, route), token_row)

    # Step 9: Model allowlist
    check_model_allowlist(model_id, token_row, _allowed_models_default())

    raw_mode = token_row.get("pricing_mode", "on_demand")
    if raw_mode not in ("on_demand", "batch"):
        logger.warning(
            json.dumps({**log_ctx, "event": "pricing_mode_invalid", "pricing_mode": raw_mode})
        )
    pricing_mode = _resolve_pricing_mode(token_row)
    log_ctx["pricing_mode"] = pricing_mode

    return PreflightResult(
        token_id=token_id,
        token_row=token_row,
        period=period,
        body=body,
        model_id=model_id,
        pricing_mode=pricing_mode,
        log_ctx=log_ctx,
        start_ms=start_ms,
        usage_table=usage_table,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/health")
async def health():
    """LWA readiness check — no auth."""
    return {"status": "ok"}


@app.post("/model/{model_id}/converse")
async def converse(
    model_id: str,
    request: Request,
    tables=Depends(get_tables),
    bedrock_client=Depends(get_bedrock),
):
    """POST /model/{model_id}/converse — non-streaming Converse API proxy."""
    start_ms = time.monotonic() * 1000
    log_ctx: dict = {}

    try:
        pf = await run_preflight(request, model_id, "converse", tables, bedrock_client)
        log_ctx = pf.log_ctx
        start_ms = pf.start_ms

        # B5: Apply output cap explicitly as a separate step (not in preflight).
        max_out = (
            int(pf.token_row["limit_max_output_tokens"])
            if "limit_max_output_tokens" in pf.token_row
            else None
        )
        body = apply_output_cap(pf.body, "converse", max_out)

        bedrock_resp, usage = forward_converse(bedrock_client, model_id, body)

        in_tok = usage["input_tokens"]
        out_tok = usage["output_tokens"]
        cache_read_tok = usage["cache_read_input_tokens"]
        cache_write_tok = usage["cache_write_input_tokens"]
        log_ctx.update(
            input_tokens=in_tok,
            output_tokens=out_tok,
            cache_read_input_tokens=cache_read_tok,
            cache_write_input_tokens=cache_write_tok,
        )

        (
            usd_micros,
            component_micros,
            fallback_applied,
            fallback_dimensions,
            applied_rates,
        ) = compute_cost(
            model_id=model_id,
            pricing_mode=pf.pricing_mode,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cache_read_input_tokens=cache_read_tok,
            cache_write_input_tokens=cache_write_tok,
        )
        log_ctx["usd_micros"] = usd_micros
        logger.info(
            json.dumps(
                {
                    **log_ctx,
                    "event": "pricing_audit",
                    "component_micros": component_micros,
                    "applied_rates": applied_rates,
                    "fallback_applied": fallback_applied,
                    "fallback_dimensions": fallback_dimensions,
                    "status": 200,
                }
            )
        )

        try:
            write_usage(
                pf.token_id,
                pf.period,
                in_tok,
                out_tok,
                cache_read_tok,
                cache_write_tok,
                usd_micros,
                pf.usage_table,
            )
        except Exception as exc:
            logger.error(json.dumps({**log_ctx, "event": "usage_write_failed", "error": str(exc)}))

        latency_ms = int((time.monotonic() * 1000) - start_ms)
        logger.info(
            json.dumps(
                {**log_ctx, "event": "request_complete", "status": 200, "latency_ms": latency_ms}
            )
        )
        return JSONResponse(bedrock_resp)

    except (AuthError, LimitError, BedrockError) as exc:
        return _handle_known_error(exc, log_ctx, start_ms)
    except Exception:
        latency_ms = int((time.monotonic() * 1000) - start_ms)
        logger.exception(
            json.dumps({**log_ctx, "event": "unhandled_error", "latency_ms": latency_ms})
        )
        return _error_json(500, "INTERNAL_ERROR", "Internal server error")


@app.post("/model/{model_id}/invoke")
async def invoke(
    model_id: str,
    request: Request,
    tables=Depends(get_tables),
    bedrock_client=Depends(get_bedrock),
):
    """POST /model/{model_id}/invoke — non-streaming InvokeModel API proxy."""
    start_ms = time.monotonic() * 1000
    log_ctx: dict = {}

    try:
        pf = await run_preflight(request, model_id, "invoke", tables, bedrock_client)
        log_ctx = pf.log_ctx
        start_ms = pf.start_ms

        # B5: Apply output cap explicitly as a separate step (not in preflight).
        max_out = (
            int(pf.token_row["limit_max_output_tokens"])
            if "limit_max_output_tokens" in pf.token_row
            else None
        )
        body = apply_output_cap(pf.body, "invoke", max_out)

        bedrock_resp, usage = forward_invoke_model(bedrock_client, model_id, body)

        in_tok = usage["input_tokens"]
        out_tok = usage["output_tokens"]
        cache_read_tok = usage["cache_read_input_tokens"]
        cache_write_tok = usage["cache_write_input_tokens"]
        log_ctx.update(
            input_tokens=in_tok,
            output_tokens=out_tok,
            cache_read_input_tokens=cache_read_tok,
            cache_write_input_tokens=cache_write_tok,
        )

        (
            usd_micros,
            component_micros,
            fallback_applied,
            fallback_dimensions,
            applied_rates,
        ) = compute_cost(
            model_id=model_id,
            pricing_mode=pf.pricing_mode,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cache_read_input_tokens=cache_read_tok,
            cache_write_input_tokens=cache_write_tok,
        )
        log_ctx["usd_micros"] = usd_micros
        logger.info(
            json.dumps(
                {
                    **log_ctx,
                    "event": "pricing_audit",
                    "component_micros": component_micros,
                    "applied_rates": applied_rates,
                    "fallback_applied": fallback_applied,
                    "fallback_dimensions": fallback_dimensions,
                    "status": 200,
                }
            )
        )

        try:
            write_usage(
                pf.token_id,
                pf.period,
                in_tok,
                out_tok,
                cache_read_tok,
                cache_write_tok,
                usd_micros,
                pf.usage_table,
            )
        except Exception as exc:
            logger.error(json.dumps({**log_ctx, "event": "usage_write_failed", "error": str(exc)}))

        latency_ms = int((time.monotonic() * 1000) - start_ms)
        logger.info(
            json.dumps(
                {**log_ctx, "event": "request_complete", "status": 200, "latency_ms": latency_ms}
            )
        )
        return JSONResponse(bedrock_resp)

    except (AuthError, LimitError, BedrockError) as exc:
        return _handle_known_error(exc, log_ctx, start_ms)
    except Exception:
        latency_ms = int((time.monotonic() * 1000) - start_ms)
        logger.exception(
            json.dumps({**log_ctx, "event": "unhandled_error", "latency_ms": latency_ms})
        )
        return _error_json(500, "INTERNAL_ERROR", "Internal server error")


@app.get("/usage")
async def usage_endpoint(
    request: Request,
    tables=Depends(get_tables),
):
    """GET /usage — self-service usage and limits (auth only, no rate-limit/quota check)."""
    start_ms = time.monotonic() * 1000
    log_ctx: dict = {}

    try:
        tokens_table, usage_table, _ = tables

        # Steps 1–4: Authentication only
        token_id, secret = parse_bearer_token_from_request(request)
        log_ctx["token_id"] = token_id

        token_row = tokens_table.get_item(Key={"token_id": token_id}).get("Item")
        if not token_row or token_row.get("status") != "active":
            raise AuthError("INVALID_TOKEN", "Invalid or revoked token")
        if not verify_secret(secret, token_row["secret_hash"]):
            raise AuthError("INVALID_TOKEN", "Invalid token secret")

        log_ctx["owner"] = token_row.get("owner", "unknown")

        period = datetime.now(UTC).strftime("%Y-%m")
        item = usage_table.get_item(Key={"token_id": token_id, "period": period}).get("Item", {})

        usd_micros_used = int(item.get("usd_micros", 0))

        usage_data = {
            "requests": int(item.get("requests", 0)),
            "input_tokens": int(item.get("input_tokens", 0)),
            "output_tokens": int(item.get("output_tokens", 0)),
            "cache_read_input_tokens": int(item.get("cache_read_input_tokens", 0)),
            "cache_write_input_tokens": int(item.get("cache_write_input_tokens", 0)),
            "usd": round(usd_micros_used / 1_000_000, 6),
        }

        def _limit(attr: str):
            val = token_row.get(attr)
            return int(val) if val is not None else None

        monthly_usd_micros_limit = _limit("limit_monthly_usd_micros")

        limits_data = {
            "monthly_requests": _limit("limit_monthly_requests"),
            "monthly_usd": round(monthly_usd_micros_limit / 1_000_000, 6)
            if monthly_usd_micros_limit is not None
            else None,
            "max_input_tokens": _limit("limit_max_input_tokens"),
            "max_output_tokens": _limit("limit_max_output_tokens"),
            "rps": _limit("limit_rps"),
        }

        remaining = {}
        if limits_data["monthly_requests"] is not None:
            remaining["requests"] = max(0, limits_data["monthly_requests"] - usage_data["requests"])
        if monthly_usd_micros_limit is not None:
            remaining["usd"] = round(
                max(0, monthly_usd_micros_limit - usd_micros_used) / 1_000_000, 6
            )

        body: dict = {"period": period, "usage": usage_data, "limits": limits_data}
        if remaining:
            body["remaining"] = remaining

        latency_ms = int((time.monotonic() * 1000) - start_ms)
        logger.info(
            json.dumps(
                {**log_ctx, "event": "request_complete", "status": 200, "latency_ms": latency_ms}
            )
        )
        return JSONResponse(body)

    except (AuthError, LimitError, BedrockError) as exc:
        return _handle_known_error(exc, log_ctx, start_ms)
    except Exception:
        latency_ms = int((time.monotonic() * 1000) - start_ms)
        logger.exception(
            json.dumps({**log_ctx, "event": "unhandled_error", "latency_ms": latency_ms})
        )
        return _error_json(500, "INTERNAL_ERROR", "Internal server error")
