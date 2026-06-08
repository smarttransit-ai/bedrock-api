"""FastAPI proxy application — entry point for the LWA container Lambda.

Served by uvicorn via the AWS Lambda Web Adapter (LWA) with
invoke_mode=RESPONSE_STREAM, behind an API Gateway REST API (REGIONAL) whose
AWS_PROXY integration runs in response-streaming (STREAM) mode.

Environment variables (required):
  TOKENS_TABLE      — DynamoDB tokens table name
  USAGE_TABLE       — DynamoDB usage table name
  RATE_LIMIT_TABLE  — DynamoDB rate_limit table name

Environment variables (optional):
  BEDROCK_REGION          — AWS region for bedrock-runtime (default: us-east-1)
  ALLOWED_MODELS_DEFAULT  — Comma-separated default model allowlist
  PRICING_BUCKET          — S3 bucket holding the live pricing catalog
  LITELLM_SOURCE_URL      — upstream litellm price map URL (refresh source)
  PRICING_CACHE_TTL_S     — live-catalog cache TTL in seconds (default: 60)

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
 10. apply output cap (route step, not preflight)
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
from bedrock import (
    BedrockError,
    _sse_json_default,
    apply_output_cap,
    forward_converse,
    forward_invoke_model,
    iter_converse_stream,
    iter_invoke_stream,
    open_converse_stream,
    open_invoke_stream,
)
from deps import get_bedrock, get_s3, get_tables
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from limits import (
    LimitError,
    check_input_cap,
    check_model_allowlist,
    check_monthly_quota,
    check_rate_limit,
    estimate_input_tokens,
    write_usage,
)
from pricing import compute_cost, invalidate_cache
from pricing_refresh import refresh_pricing
from pricing_store import load_live_meta

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
    """Result of run_preflight — raw parsed body (output cap applied by the route)."""

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
) -> PreflightResult:
    """Execute pre-flight steps 1–9 and return a PreflightResult.

    Raises AuthError, LimitError, or BedrockError on rejection.
    Output cap (apply_output_cap) is NOT applied here — it is a separate
    route step (amendment B5) so each route applies it explicitly.
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


def require_admin(request: Request, tokens_table) -> str:
    """Authenticate an admin bearer token (token row ``admin == True``); return token_id.

    Mirrors the auth steps of run_preflight. Raises AuthError — 401 for an
    invalid/revoked token, 403 when the (valid) token is not an admin. Gates
    admin-only routes.
    """
    token_id, secret = parse_bearer_token_from_request(request)
    token_row = tokens_table.get_item(Key={"token_id": token_id}).get("Item")
    if not token_row or token_row.get("status") != "active":
        raise AuthError("INVALID_TOKEN", "Invalid or revoked token")
    if not verify_secret(secret, token_row["secret_hash"]):
        raise AuthError("INVALID_TOKEN", "Invalid token secret")
    if token_row.get("admin") is not True:
        raise AuthError("FORBIDDEN", "admin token required", 403)
    return token_id


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/health")
async def health():
    """LWA readiness check — no auth."""
    return {"status": "ok"}


async def _run_nonstreaming(
    model_id: str,
    request: Request,
    tables,
    bedrock_client,
    route: str,
    forward_fn,
):
    """Shared body for the non-streaming converse/invoke routes.

    The two routes differ only by route name and the Bedrock forward function;
    the pre-flight, output-cap, billing, and logging flow is identical.
    """
    start_ms = time.monotonic() * 1000
    log_ctx: dict = {}

    try:
        pf = await run_preflight(request, model_id, route, tables)
        log_ctx = pf.log_ctx
        start_ms = pf.start_ms

        # B5: Apply output cap explicitly as a separate step (not in preflight).
        max_out = (
            int(pf.token_row["limit_max_output_tokens"])
            if "limit_max_output_tokens" in pf.token_row
            else None
        )
        body = apply_output_cap(pf.body, route, max_out)

        bedrock_resp, usage = forward_fn(bedrock_client, model_id, body)

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


@app.post("/model/{model_id}/converse")
async def converse(
    model_id: str,
    request: Request,
    tables=Depends(get_tables),
    bedrock_client=Depends(get_bedrock),
):
    """POST /model/{model_id}/converse — non-streaming Converse API proxy."""
    return await _run_nonstreaming(
        model_id, request, tables, bedrock_client, "converse", forward_converse
    )


@app.post("/model/{model_id}/invoke")
async def invoke(
    model_id: str,
    request: Request,
    tables=Depends(get_tables),
    bedrock_client=Depends(get_bedrock),
):
    """POST /model/{model_id}/invoke — non-streaming InvokeModel API proxy."""
    return await _run_nonstreaming(
        model_id, request, tables, bedrock_client, "invoke", forward_invoke_model
    )


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


@app.post("/admin/pricing/refresh")
async def admin_pricing_refresh(
    request: Request,
    tables=Depends(get_tables),
    s3=Depends(get_s3),
):
    """POST /admin/pricing/refresh — re-pull pricing from litellm, validate, make it live.

    Admin only. All-or-nothing: refresh_pricing only writes S3 if the rebuilt catalog
    passes validation, so a failure leaves current pricing untouched.
    """
    tokens_table, _, _ = tables
    try:
        admin_token_id = require_admin(request, tokens_table)
    except AuthError as exc:
        # Direct error response — not a billed request, so no pricing_audit log.
        return _error_json(exc.status, exc.code, exc.message)
    try:
        summary = refresh_pricing(s3)
    except Exception as exc:
        # Detail (URL, boto/validation message) goes to the log, not the wire.
        logger.exception(json.dumps({"event": "pricing_refresh_failed", "error": str(exc)}))
        return _error_json(502, "REFRESH_FAILED", "pricing refresh failed")
    invalidate_cache()
    logger.info(
        json.dumps(
            {
                "event": "pricing_refresh",
                "token_id": admin_token_id,
                "entry_count": summary["entry_count"],
            }
        )
    )
    return JSONResponse(summary)


@app.get("/admin/pricing")
async def admin_pricing_status(
    request: Request,
    tables=Depends(get_tables),
    s3=Depends(get_s3),
):
    """GET /admin/pricing — provenance of the currently-live pricing (admin only).

    Returns the live catalog's meta (fetched_at, source, entry_count) when an S3
    object exists, or `source: "default"` when none does (the baked-in
    DEFAULT_PRICING is in use). Does not mutate anything.
    """
    tokens_table, _, _ = tables
    try:
        require_admin(request, tokens_table)
    except AuthError as exc:
        return _error_json(exc.status, exc.code, exc.message)
    try:
        meta = load_live_meta(s3)
    except Exception as exc:
        logger.exception(json.dumps({"event": "pricing_read_failed", "error": str(exc)}))
        return _error_json(502, "PRICING_READ_FAILED", "could not read live pricing")
    if meta is None:
        return JSONResponse({"source": "default", "meta": None})
    return JSONResponse({"source": "live", "meta": meta})


# ---------------------------------------------------------------------------
# Streaming helpers
# ---------------------------------------------------------------------------


def _post_flight_write(usage_out: dict, pf: PreflightResult, log_ctx: dict) -> None:
    """Write usage + emit billing logs after a streaming response completes.

    Skips the write when usage_out is empty (client disconnect before any
    usage events were received).  Never raises — errors are logged only.

    Observability parity with non-streaming routes (R5):
      - compute_cost failure → logs ``billing_failed`` and returns early
      - pricing_audit always emitted after a successful compute
      - write_usage failure → logs ``usage_write_failed`` (does not suppress request_complete)
      - request_complete always emitted when usage was written
    """
    # O1: guard at top before reading token vars
    if not usage_out:
        # No usage data at all — skip billing write (e.g. client disconnect)
        return

    in_tok = usage_out.get("input_tokens", 0)
    out_tok = usage_out.get("output_tokens", 0)
    cache_read_tok = usage_out.get("cache_read_input_tokens", 0)
    cache_write_tok = usage_out.get("cache_write_input_tokens", 0)

    latency_ms = int((time.monotonic() * 1000) - pf.start_ms)
    log_ctx.update(
        input_tokens=in_tok,
        output_tokens=out_tok,
        cache_read_input_tokens=cache_read_tok,
        cache_write_input_tokens=cache_write_tok,
    )

    # (a) compute_cost in its own try/except for distinct observability
    try:
        (
            usd_micros,
            component_micros,
            fallback_applied,
            fallback_dimensions,
            applied_rates,
        ) = compute_cost(
            model_id=pf.model_id,
            pricing_mode=pf.pricing_mode,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cache_read_input_tokens=cache_read_tok,
            cache_write_input_tokens=cache_write_tok,
        )
    except Exception as exc:
        logger.error(json.dumps({**log_ctx, "event": "billing_failed", "error": str(exc)}))
        return

    # (b) pricing_audit unconditional after successful compute
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

    # (c) write_usage in its own try/except — failure logs usage_write_failed but
    #     does not suppress request_complete
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

    # (d) request_complete always emitted when usage_out was populated
    logger.info(
        json.dumps(
            {**log_ctx, "event": "request_complete", "status": 200, "latency_ms": latency_ms}
        )
    )


# ---------------------------------------------------------------------------
# Streaming helper (R6: module-level so tests can drive it directly)
# ---------------------------------------------------------------------------


def _sse_stream(event_iter, usage_out: dict, pf: PreflightResult, log_ctx: dict):
    """Generate SSE frames from an event iterator; run _post_flight_write in finally.

    Factored out of the route _stream() closures (R6) so test code can drive
    the generator directly — partially iterate, call gen.close(), and assert
    _post_flight_write behaviour without going through HTTP.

    Used by both converse-stream and invoke-with-response-stream routes.  The
    caller is responsible for building the correct event_iter (iter_converse_stream
    or iter_invoke_stream result) before passing it here.

    BB2: json.dumps uses _sse_json_default so bytes-valued Bedrock events
    (e.g. reasoningContent.redactedContent) are base64-encoded rather than
    crashing with TypeError and degrading the stream.
    R4: generic except uses logger.exception so mid-stream production errors
    carry a traceback (parity with non-streaming routes).
    """
    try:
        for event in event_iter:
            yield f"data: {json.dumps(event, default=_sse_json_default)}\n\n"
    except BedrockError as exc:
        # Mid-stream Bedrock error after a 200 + headers were already sent. Log a
        # terminal event for observability parity with the non-streaming reject
        # path: _post_flight_write skips when no usage arrived before the error,
        # so without this the request would vanish from the logs.
        logger.warning(
            json.dumps(
                {**log_ctx, "event": "stream_error", "error_code": exc.code, "status": exc.status}
            )
        )
        yield (f"data: {json.dumps({'error': {'code': exc.code, 'message': exc.message}})}\n\n")
    except Exception as exc:
        logger.exception(json.dumps({**log_ctx, "event": "stream_error", "error": str(exc)}))
        yield (
            "data: "
            + json.dumps({"error": {"code": "STREAM_ERROR", "message": "Stream error"}})
            + "\n\n"
        )
    finally:
        _post_flight_write(usage_out, pf, log_ctx)


# ---------------------------------------------------------------------------
# Streaming routes (Phase B)
# ---------------------------------------------------------------------------


async def _run_streaming(
    model_id: str,
    request: Request,
    tables,
    bedrock_client,
    route: str,
    open_fn,
    iter_fn,
    stream_key: str,
):
    """Shared body for the streaming converse/invoke routes.

    The two routes differ only by route name, the Bedrock stream-open and
    per-event iterator functions, and the response key holding the event stream.
    """
    start_ms = time.monotonic() * 1000
    log_ctx: dict = {}

    try:
        pf = await run_preflight(request, model_id, route, tables)
        log_ctx = pf.log_ctx
        start_ms = pf.start_ms

        # B5: Apply output cap
        max_out = (
            int(pf.token_row["limit_max_output_tokens"])
            if "limit_max_output_tokens" in pf.token_row
            else None
        )
        body = apply_output_cap(pf.body, route, max_out)

        # B1: Eager stream open — SDK call BEFORE StreamingResponse so call-time
        # errors (throttling, param validation) return real 4xx/5xx.
        response = open_fn(bedrock_client, model_id, body)

    except (AuthError, LimitError, BedrockError) as exc:
        return _handle_known_error(exc, log_ctx, start_ms)
    except Exception:
        latency_ms = int((time.monotonic() * 1000) - start_ms)
        logger.exception(
            json.dumps({**log_ctx, "event": "unhandled_error", "latency_ms": latency_ms})
        )
        return _error_json(500, "INTERNAL_ERROR", "Internal server error")

    # SSE generator — runs after headers are sent; errors yield terminal frame
    usage_out: dict = {}

    return StreamingResponse(
        _sse_stream(iter_fn(response[stream_key], usage_out), usage_out, pf, log_ctx),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/model/{model_id}/converse-stream")
async def converse_stream(
    model_id: str,
    request: Request,
    tables=Depends(get_tables),
    bedrock_client=Depends(get_bedrock),
):
    """POST /model/{model_id}/converse-stream — streaming Converse API proxy."""
    return await _run_streaming(
        model_id,
        request,
        tables,
        bedrock_client,
        "converse",
        open_converse_stream,
        iter_converse_stream,
        "stream",
    )


@app.post("/model/{model_id}/invoke-with-response-stream")
async def invoke_with_response_stream(
    model_id: str,
    request: Request,
    tables=Depends(get_tables),
    bedrock_client=Depends(get_bedrock),
):
    """POST /model/{model_id}/invoke-with-response-stream — streaming InvokeModel proxy."""
    return await _run_streaming(
        model_id,
        request,
        tables,
        bedrock_client,
        "invoke",
        open_invoke_stream,
        iter_invoke_stream,
        "body",
    )
