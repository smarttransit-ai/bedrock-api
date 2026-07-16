import base64
import json
import logging
from collections.abc import Generator

from botocore.exceptions import ClientError, ParamValidationError
from routes import ROUTE_CONVERSE, ROUTE_RESPONSES

logger = logging.getLogger(__name__)


def _sse_json_default(o):
    """JSON serializer for types not handled by the stdlib encoder.

    Bytes and bytearray are base64-encoded to an ASCII string for SSE/JSON
    transport (unavoidable — document the representation to callers).
    All other types raise TypeError so genuinely non-serializable objects
    still surface as errors rather than being silently mangled.
    """
    if isinstance(o, (bytes, bytearray)):
        return base64.b64encode(o).decode("ascii")
    raise TypeError(f"Object of type {type(o).__name__} is not JSON serializable")


class BedrockError(Exception):
    def __init__(self, code: str, message: str, status: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def apply_output_cap(body: dict, route: str, max_output_tokens: int | None) -> dict:
    """Inject output token cap into the request body before forwarding upstream.

    Takes the minimum of the user-specified value and the cap so we never
    exceed the per-token limit while still honouring lower user requests.
    Returns a shallow copy of body; does not mutate the caller's dict.

    Each protocol names the field differently: Converse nests maxTokens under
    inferenceConfig, InvokeModel uses max_tokens, and the Responses API uses
    max_output_tokens (it ignores max_tokens outright, so getting this branch wrong
    silently disables the cap rather than erroring).
    """
    if max_output_tokens is None:
        return body
    body = dict(body)
    if route == ROUTE_CONVERSE:
        inf_cfg = dict(body.get("inferenceConfig") or {})
        existing = inf_cfg.get("maxTokens")
        inf_cfg["maxTokens"] = min(existing, max_output_tokens) if existing else max_output_tokens
        body["inferenceConfig"] = inf_cfg
        return body
    field = "max_output_tokens" if route == ROUTE_RESPONSES else "max_tokens"
    existing = body.get(field)
    body[field] = min(existing, max_output_tokens) if existing else max_output_tokens
    return body


def _parse_base10_int(value, field_name: str) -> int:
    if isinstance(value, bool):
        logger.warning(
            json.dumps({"event": "usage_parse_warning", "field": field_name, "raw_value": value})
        )
        return 0
    if isinstance(value, int):
        return max(0, min(value, 9_223_372_036_854_775_807))
    if isinstance(value, str):
        if value.isdigit() or (value.startswith("-") and value[1:].isdigit()):
            parsed = int(value, 10)
            return max(0, min(parsed, 9_223_372_036_854_775_807))
    logger.warning(
        json.dumps({"event": "usage_parse_warning", "field": field_name, "raw_value": value})
    )
    return 0


def _extract_value(payload: dict, key_or_keys: str | list[str]):
    if isinstance(key_or_keys, str):
        return payload.get(key_or_keys, 0)
    for key in key_or_keys:
        if key in payload:
            return payload.get(key, 0)
    return 0


def _has_key(payload: dict, key_or_keys: str | list[str]) -> bool:
    if isinstance(key_or_keys, str):
        return key_or_keys in payload
    return any(key in payload for key in key_or_keys)


def normalize_usage(
    usage_payload: dict | None, key_map: dict[str, str | list[str]]
) -> dict[str, int]:
    if not isinstance(usage_payload, dict):
        logger.warning(
            json.dumps(
                {"event": "usage_parse_warning", "field": "usage", "raw_value": usage_payload}
            )
        )
        usage_payload = {}
    return {
        "input_tokens": _parse_base10_int(
            _extract_value(usage_payload, key_map["input_tokens"]), "input_tokens"
        ),
        "output_tokens": _parse_base10_int(
            _extract_value(usage_payload, key_map["output_tokens"]), "output_tokens"
        ),
        "cache_read_input_tokens": _parse_base10_int(
            _extract_value(usage_payload, key_map["cache_read_input_tokens"]),
            "cache_read_input_tokens",
        ),
        "cache_write_input_tokens": _parse_base10_int(
            _extract_value(usage_payload, key_map["cache_write_input_tokens"]),
            "cache_write_input_tokens",
        ),
    }


def forward_converse(client, model_id: str, body: dict) -> tuple[dict, dict[str, int]]:
    """Call Bedrock Converse API and return (response_body, normalized_usage).

    modelId is a path parameter for the SDK; strip it from the body kwargs if
    present to avoid a duplicate-parameter error.
    ResponseMetadata is stripped before returning to the client.
    """
    kwargs = {k: v for k, v in body.items() if k != "modelId"}
    try:
        response = client.converse(modelId=model_id, **kwargs)
    except ParamValidationError as exc:
        raise BedrockError("BAD_REQUEST", f"Invalid request body: {exc}", 400) from exc
    except ClientError as exc:
        _raise_bedrock_error(exc)
    response.pop("ResponseMetadata", None)
    usage = normalize_usage(
        response.get("usage"),
        {
            "input_tokens": "inputTokens",
            "output_tokens": "outputTokens",
            "cache_read_input_tokens": ["cacheReadInputTokenCount", "cacheReadInputTokens"],
            "cache_write_input_tokens": [
                "cacheWriteInputTokenCount",
                "cacheWriteInputTokens",
            ],
        },
    )
    return response, usage


def forward_invoke_model(client, model_id: str, body: dict) -> tuple[dict, dict[str, int]]:
    """Call Bedrock InvokeModel API and return (response_body, normalized_usage).

    Token usage is extracted using the Anthropic response format
    (response_body["usage"]["input_tokens"] / ["output_tokens"]).
    Other model families may not populate usage; we default to 0 in that case.
    """
    try:
        response = client.invoke_model(
            modelId=model_id,
            body=json.dumps(body),
            contentType="application/json",
            accept="application/json",
        )
    except ParamValidationError as exc:
        raise BedrockError("BAD_REQUEST", f"Invalid request body: {exc}", 400) from exc
    except ClientError as exc:
        _raise_bedrock_error(exc)
    response_body = json.loads(response["body"].read())
    usage = response_body.get("usage")
    # Deterministic precedence:
    # 1) Anthropic-native keys
    # 2) Bedrock-style keys (if returned by wrapped providers)
    anthropic = normalize_usage(
        usage,
        {
            "input_tokens": "input_tokens",
            "output_tokens": "output_tokens",
            "cache_read_input_tokens": "cache_read_input_tokens",
            "cache_write_input_tokens": "cache_creation_input_tokens",
        },
    )
    bedrock_style = normalize_usage(
        usage,
        {
            "input_tokens": "inputTokens",
            "output_tokens": "outputTokens",
            "cache_read_input_tokens": ["cacheReadInputTokenCount", "cacheReadInputTokens"],
            "cache_write_input_tokens": ["cacheWriteInputTokenCount", "cacheWriteInputTokens"],
        },
    )
    usage_dict = usage if isinstance(usage, dict) else {}
    merged = {
        "input_tokens": anthropic["input_tokens"]
        if _has_key(usage_dict, "input_tokens")
        else bedrock_style["input_tokens"],
        "output_tokens": anthropic["output_tokens"]
        if _has_key(usage_dict, "output_tokens")
        else bedrock_style["output_tokens"],
        "cache_read_input_tokens": anthropic["cache_read_input_tokens"]
        if _has_key(usage_dict, "cache_read_input_tokens")
        else bedrock_style["cache_read_input_tokens"],
        "cache_write_input_tokens": anthropic["cache_write_input_tokens"]
        if _has_key(usage_dict, "cache_creation_input_tokens")
        else bedrock_style["cache_write_input_tokens"],
    }
    return response_body, merged


def _raise_bedrock_error(exc: ClientError) -> None:
    # validationException asymmetry (intentional): pre-stream ClientError → 502 here,
    # while a mid-stream validationException member → 400 in _EVENTSTREAM_ERROR_MEMBERS.
    # Do NOT change this to 400 — it would break the existing non-streaming contract.
    code = exc.response["Error"]["Code"]
    if code in ("ThrottlingException", "TooManyRequestsException"):
        raise BedrockError("BEDROCK_THROTTLED", "Bedrock rate limit exceeded", 429) from exc
    raise BedrockError(
        "BEDROCK_ERROR", f"Bedrock error: {exc.response['Error']['Message']}", 502
    ) from exc


# ---------------------------------------------------------------------------
# EventStream error-member mapper (amendment B6)
# ---------------------------------------------------------------------------

# Maps Bedrock EventStream error member names → (BedrockError.code, http_status)
# These members appear as keys in the event dict returned by the EventStream iterator.
#
# validationException asymmetry (intentional): mid-stream member → 400 (BAD_REQUEST)
# whereas a pre-stream Bedrock ValidationException ClientError → 502 via
# _raise_bedrock_error.  The mid-stream path is a cleaner 400 because the request
# was already accepted and the validation failure is model-side; the pre-stream path
# keeps the existing contract.  Do NOT change _raise_bedrock_error to match.
_EVENTSTREAM_ERROR_MEMBERS: dict[str, tuple[str, int]] = {
    "throttlingException": ("BEDROCK_THROTTLED", 429),
    "modelStreamErrorException": ("BEDROCK_STREAM_ERROR", 502),
    # intentional asymmetry — see comment above
    "validationException": ("BAD_REQUEST", 400),
    "internalServerException": ("BEDROCK_ERROR", 502),
    # R2: aligned to BEDROCK_ERROR/502 for consistency with the rest of the Bedrock surface
    "serviceUnavailableException": ("BEDROCK_ERROR", 502),
    # BB1: mid-stream timeout → 504 Gateway Timeout
    "modelTimeoutException": ("BEDROCK_ERROR", 504),
}


def _check_eventstream_error(event: dict) -> None:
    """Raise BedrockError if *event* is an EventStream error member (amendment B6).

    Bedrock wraps mid-stream errors as top-level dict keys whose value is
    the error detail dict (e.g. {"throttlingException": {"message": "..."}}).
    Called on every event before it is yielded to the caller.
    """
    for member, (code, status) in _EVENTSTREAM_ERROR_MEMBERS.items():
        if member in event:
            detail = event[member]
            msg = detail.get("message", member) if isinstance(detail, dict) else member
            raise BedrockError(code, msg, status)


# ---------------------------------------------------------------------------
# Streaming — Converse API (amendment B1: open/iter split)
# ---------------------------------------------------------------------------


def open_converse_stream(client, model_id: str, body: dict):
    """Open a Converse streaming session and return the raw boto3 response.

    Performs the SDK call eagerly so that call-time errors (e.g. throttling,
    invalid params) surface as BedrockError BEFORE a StreamingResponse is
    constructed (amendment B1).  Caller iterates response["stream"].
    """
    kwargs = {k: v for k, v in body.items() if k != "modelId"}
    try:
        return client.converse_stream(modelId=model_id, **kwargs)
    except ParamValidationError as exc:
        raise BedrockError("BAD_REQUEST", f"Invalid request body: {exc}", 400) from exc
    except ClientError as exc:
        _raise_bedrock_error(exc)


def iter_converse_stream(event_stream, usage_out: dict) -> Generator[dict, None, None]:
    """Iterate a Converse EventStream, populating *usage_out* from the metadata event.

    Converse key map:
      inputTokens → input_tokens
      outputTokens → output_tokens
      cacheReadInputTokenCount / cacheReadInputTokens → cache_read_input_tokens
      cacheWriteInputTokenCount / cacheWriteInputTokens → cache_write_input_tokens

    Yields each event dict.  Raises BedrockError on mid-stream error members (B6).
    """
    for event in event_stream:
        _check_eventstream_error(event)
        if "metadata" in event:
            usage_payload = event["metadata"].get("usage", {})
            parsed = normalize_usage(
                usage_payload,
                {
                    "input_tokens": "inputTokens",
                    "output_tokens": "outputTokens",
                    "cache_read_input_tokens": [
                        "cacheReadInputTokenCount",
                        "cacheReadInputTokens",
                    ],
                    "cache_write_input_tokens": [
                        "cacheWriteInputTokenCount",
                        "cacheWriteInputTokens",
                    ],
                },
            )
            usage_out.update(parsed)
        yield event


# ---------------------------------------------------------------------------
# Streaming — InvokeModel with response stream (amendment B1: open/iter split)
# ---------------------------------------------------------------------------


def open_invoke_stream(client, model_id: str, body: dict):
    """Open an InvokeModelWithResponseStream session and return the raw response.

    Performs the SDK call eagerly so call-time errors return real 4xx/5xx
    before StreamingResponse is constructed (amendment B1).
    """
    try:
        return client.invoke_model_with_response_stream(
            modelId=model_id,
            body=json.dumps(body),
            contentType="application/json",
            accept="application/json",
        )
    except ParamValidationError as exc:
        raise BedrockError("BAD_REQUEST", f"Invalid request body: {exc}", 400) from exc
    except ClientError as exc:
        _raise_bedrock_error(exc)


def iter_invoke_stream(event_stream, usage_out: dict) -> Generator[dict, None, None]:
    """Iterate an InvokeModelWithResponseStream body, populating *usage_out*.

    Cache token extraction (amendment B2):
      - message_start.message.usage supplies input_tokens,
        cache_read_input_tokens (cache_read_input_tokens key) and
        cache_creation_input_tokens → cache_write_input_tokens.
      - message_delta.usage supplies output_tokens.
      - amazon-bedrock-invocationMetrics in message_stop is a FALLBACK only —
        fills in non-zero values only when the corresponding counter is still 0.
        Cache counters are never overwritten from invocationMetrics.

    Raises BedrockError on mid-stream error members (B6).
    Yields each decoded chunk dict.
    """
    for event in event_stream:
        _check_eventstream_error(event)
        chunk = event.get("chunk")
        if chunk is None:
            yield event
            continue
        raw = chunk.get("bytes", b"")
        if not raw:
            continue
        decoded = json.loads(raw)

        # Extract usage counters progressively
        chunk_type = decoded.get("type")
        if chunk_type == "message_start":
            msg_usage = decoded.get("message", {}).get("usage", {})
            usage_out["input_tokens"] = _parse_base10_int(
                msg_usage.get("input_tokens", 0), "input_tokens"
            )
            usage_out["cache_read_input_tokens"] = _parse_base10_int(
                msg_usage.get("cache_read_input_tokens", 0), "cache_read_input_tokens"
            )
            usage_out["cache_write_input_tokens"] = _parse_base10_int(
                msg_usage.get("cache_creation_input_tokens", 0), "cache_write_input_tokens"
            )
        elif chunk_type == "message_delta":
            delta_usage = decoded.get("usage", {})
            usage_out["output_tokens"] = _parse_base10_int(
                delta_usage.get("output_tokens", 0), "output_tokens"
            )
        elif chunk_type == "message_stop":
            metrics = decoded.get("amazon-bedrock-invocationMetrics", {})
            if metrics:
                # Fallback: fill in only if not yet populated from primary events.
                # Key-presence check (not falsiness) so a legitimate 0 from message_start
                # or message_delta is never overwritten by invocationMetrics.
                if "input_tokens" not in usage_out:
                    usage_out["input_tokens"] = _parse_base10_int(
                        metrics.get("inputTokenCount", 0), "input_tokens"
                    )
                if "output_tokens" not in usage_out:
                    usage_out["output_tokens"] = _parse_base10_int(
                        metrics.get("outputTokenCount", 0), "output_tokens"
                    )
                # Cache counters are never sourced from invocationMetrics

        yield decoded
