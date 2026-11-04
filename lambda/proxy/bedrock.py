import base64
import json
import logging
from collections.abc import Generator

from botocore.exceptions import ClientError, ParamValidationError
from pricing_catalog import normalize_service_tier
from routes import ROUTE_CONVERSE, ROUTE_RESPONSES

logger = logging.getLogger(__name__)

# boto3 sends the InvokeModel serviceTier parameter as this header; the proxy reads it from
# invoke requests and echoes Bedrock's reported tier back under it.
TIER_HEADER = "X-Amzn-Bedrock-Service-Tier"

# Canonical usage counters — the billing dimensions every usage extractor reports.
TOKEN_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_write_input_tokens",
)

# Bedrock-native usage names (Converse, converse-stream metadata, Nova invoke streams).
_BEDROCK_KEY_MAP = {
    "input_tokens": "inputTokens",
    "output_tokens": "outputTokens",
    "cache_read_input_tokens": ["cacheReadInputTokenCount", "cacheReadInputTokens"],
    "cache_write_input_tokens": ["cacheWriteInputTokenCount", "cacheWriteInputTokens"],
}

# Model-agnostic count headers on every non-streaming InvokeModel response.
_INPUT_COUNT_HEADER = "x-amzn-bedrock-input-token-count"
_OUTPUT_COUNT_HEADER = "x-amzn-bedrock-output-token-count"
_CACHE_READ_COUNT_HEADER = "x-amzn-bedrock-cache-read-input-token-count"
_CACHE_WRITE_COUNT_HEADER = "x-amzn-bedrock-cache-write-input-token-count"
_COUNT_HEADERS = (
    _INPUT_COUNT_HEADER,
    _OUTPUT_COUNT_HEADER,
    _CACHE_READ_COUNT_HEADER,
    _CACHE_WRITE_COUNT_HEADER,
)

# InvokeModel usage, first present wins: Anthropic body keys, then Bedrock-style body keys
# (wrapped providers such as Nova), then the count headers (e.g. gpt-oss, whose body
# reports {prompt_tokens, completion_tokens}).
_INVOKE_KEY_MAP = {
    "input_tokens": ["input_tokens", "inputTokens", _INPUT_COUNT_HEADER],
    "output_tokens": ["output_tokens", "outputTokens", _OUTPUT_COUNT_HEADER],
    "cache_read_input_tokens": [
        "cache_read_input_tokens",
        "cacheReadInputTokenCount",
        "cacheReadInputTokens",
        _CACHE_READ_COUNT_HEADER,
    ],
    "cache_write_input_tokens": [
        "cache_creation_input_tokens",
        "cacheWriteInputTokenCount",
        "cacheWriteInputTokens",
        _CACHE_WRITE_COUNT_HEADER,
    ],
}


def _tier_kwargs(service_tier: str | None) -> dict:
    """boto3 kwargs forwarding the client's requested tier, only when one was sent."""
    return {"serviceTier": service_tier} if service_tier else {}


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
    usage_payload: object, key_map: dict[str, str | list[str]]
) -> dict[str, int | str | bool]:
    """Read TOKEN_KEYS from *usage_payload* via *key_map*, defaulting each to 0.

    A payload that is not a dict, or holds no input-token alias, also sets ``usage_missing``
    (and nothing otherwise). It is not a token key: app._bill logs it once, with the model,
    as ``usage_parse_warning``. iter_invoke_stream's Claude path parses counters directly and
    sets the flag itself when ``message_start`` carries no input count.
    """
    if not isinstance(usage_payload, dict):
        usage_payload = {}
    usage = {k: _parse_base10_int(_extract_value(usage_payload, key_map[k]), k) for k in TOKEN_KEYS}
    if not _has_key(usage_payload, key_map["input_tokens"]):
        usage["usage_missing"] = True
    return usage


def forward_converse(client, model_id: str, body: dict) -> tuple[dict, dict[str, int | str | bool]]:
    """Call Bedrock Converse API and return (response_body, normalized_usage).

    modelId is a path parameter for the SDK; strip it from the body kwargs if
    present to avoid a duplicate-parameter error.
    ResponseMetadata is stripped before returning to the client. The usage dict
    carries the reported ``serviceTier`` as ``service_tier`` (normalized).
    """
    kwargs = {k: v for k, v in body.items() if k != "modelId"}
    try:
        response = client.converse(modelId=model_id, **kwargs)
    except ParamValidationError as exc:
        raise BedrockError("BAD_REQUEST", f"Invalid request body: {exc}", 400) from exc
    except ClientError as exc:
        _raise_bedrock_error(exc)
    response.pop("ResponseMetadata", None)
    usage = normalize_usage(response.get("usage"), _BEDROCK_KEY_MAP)
    usage["service_tier"] = normalize_service_tier(response.get("serviceTier"))
    return response, usage


def forward_invoke_model(
    client, model_id: str, body: dict, service_tier: str | None = None
) -> tuple[dict, dict[str, int | str | bool]]:
    """Call Bedrock InvokeModel API and return (response_body, normalized_usage).

    Usage is read model-agnostically via _INVOKE_KEY_MAP from the body's ``usage`` merged
    over the response's token-count headers. ``service_tier`` (the client's tier header) is
    forwarded only when set. The usage dict carries the tier Bedrock reports, normalized,
    plus ``raw_service_tier`` (Bedrock's own value, for the echo header) when one is present.
    """
    try:
        response = client.invoke_model(
            modelId=model_id,
            body=json.dumps(body),
            contentType="application/json",
            accept="application/json",
            **_tier_kwargs(service_tier),
        )
    except ParamValidationError as exc:
        raise BedrockError("BAD_REQUEST", f"Invalid request body: {exc}", 400) from exc
    except ClientError as exc:
        _raise_bedrock_error(exc)
    response_body = json.loads(response["body"].read())
    headers = (response.get("ResponseMetadata") or {}).get("HTTPHeaders", {})
    header_counts = {h: headers[h] for h in _COUNT_HEADERS if h in headers}
    body_usage = response_body.get("usage")
    merged = {**header_counts, **(body_usage if isinstance(body_usage, dict) else {})}
    usage = normalize_usage(merged, _INVOKE_KEY_MAP)
    usage["service_tier"] = normalize_service_tier(response.get("serviceTier"))
    if isinstance(response.get("serviceTier"), str):
        usage["raw_service_tier"] = response["serviceTier"]
    return response_body, usage


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

    The metadata event also reports the service tier the call ran on (``serviceTier``).

    Yields each event dict.  Raises BedrockError on mid-stream error members (B6).
    """
    for event in event_stream:
        _check_eventstream_error(event)
        if "metadata" in event:
            metadata = event["metadata"]
            usage_out.update(normalize_usage(metadata.get("usage", {}), _BEDROCK_KEY_MAP))
            usage_out["service_tier"] = normalize_service_tier(metadata.get("serviceTier"))
        yield event


# ---------------------------------------------------------------------------
# Streaming — InvokeModel with response stream (amendment B1: open/iter split)
# ---------------------------------------------------------------------------


def open_invoke_stream(client, model_id: str, body: dict, service_tier: str | None = None):
    """Open an InvokeModelWithResponseStream session and return the raw response.

    Performs the SDK call eagerly so call-time errors return real 4xx/5xx
    before StreamingResponse is constructed (amendment B1). ``service_tier`` is
    forwarded only when set; the response's ``serviceTier`` is the tier billed.
    """
    try:
        return client.invoke_model_with_response_stream(
            modelId=model_id,
            body=json.dumps(body),
            contentType="application/json",
            accept="application/json",
            **_tier_kwargs(service_tier),
        )
    except ParamValidationError as exc:
        raise BedrockError("BAD_REQUEST", f"Invalid request body: {exc}", 400) from exc
    except ClientError as exc:
        _raise_bedrock_error(exc)


def iter_invoke_stream(event_stream, usage_out: dict) -> Generator[dict, None, None]:
    """Iterate an InvokeModelWithResponseStream body, populating *usage_out*.

    Usage extraction (cache tokens: amendment B2):
      - message_start.message.usage supplies input_tokens,
        cache_read_input_tokens (cache_read_input_tokens key) and
        cache_creation_input_tokens → cache_write_input_tokens.
      - message_delta.usage supplies output_tokens.
      - A ``metadata.usage`` chunk (Nova) supplies Converse-style counts.
      - amazon-bedrock-invocationMetrics, on ANY chunk (Claude's message_stop, gpt-oss's
        untyped last chunk, Nova's metadata chunk), is a FALLBACK only — it fills a
        counter only when it is still absent. Nova's metrics read 0/0 in the same chunk
        as its metadata, so the metadata counts win. Cache counters are filled from it
        the same way, when the metrics carry them.

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
            if "input_tokens" not in msg_usage:
                usage_out["usage_missing"] = True
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
        elif isinstance(metadata := decoded.get("metadata"), dict) and "usage" in metadata:
            usage_out.update(normalize_usage(metadata["usage"], _BEDROCK_KEY_MAP))

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
            for key, metric in (
                ("cache_read_input_tokens", "cacheReadInputTokenCount"),
                ("cache_write_input_tokens", "cacheWriteInputTokenCount"),
            ):
                if key not in usage_out and metric in metrics:
                    usage_out[key] = _parse_base10_int(metrics[metric], key)

        yield decoded
