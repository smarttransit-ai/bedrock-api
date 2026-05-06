import json
import logging
from urllib.parse import unquote

from botocore.exceptions import ClientError, ParamValidationError

logger = logging.getLogger(__name__)


class BedrockError(Exception):
    def __init__(self, code: str, message: str, status: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def parse_route(event: dict) -> tuple[str, str]:
    """Extract (model_id, route) from the APIGW rawPath.

    Expected path: /model/{url-encoded-model-id}/converse
                or /model/{url-encoded-model-id}/invoke

    Model IDs contain colons (e.g. us.anthropic.claude-sonnet-4-6)
    which are percent-encoded as %3A by clients. urllib.parse.unquote decodes them.

    Converse API is the primary path for all supported models.
    InvokeModel (/invoke) is used for models that do not support Converse
    (e.g. Stable Diffusion, Titan Embeddings). Claude models support Converse.
    Streaming (/invoke-with-response-stream) is not supported in v1.
    """
    raw_path = event.get("rawPath", "")
    parts = raw_path.strip("/").split("/")
    if len(parts) != 3 or parts[0] != "model":
        raise BedrockError("BAD_REQUEST", f"Invalid path: {raw_path!r}", 400)
    model_id = unquote(parts[1])
    route = parts[2]
    if route not in ("converse", "invoke"):
        raise BedrockError(
            "BAD_REQUEST",
            f"Unsupported route {route!r}. Use 'converse' or 'invoke'.",
            400,
        )
    return model_id, route


def apply_output_cap(body: dict, route: str, max_output_tokens: int | None) -> dict:
    """Inject output token cap into the request body before forwarding to Bedrock.

    Takes the minimum of the user-specified value and the cap so we never
    exceed the per-token limit while still honouring lower user requests.
    Returns a shallow copy of body; does not mutate the caller's dict.
    """
    if max_output_tokens is None:
        return body
    body = dict(body)
    if route == "converse":
        inf_cfg = dict(body.get("inferenceConfig") or {})
        existing = inf_cfg.get("maxTokens")
        inf_cfg["maxTokens"] = min(existing, max_output_tokens) if existing else max_output_tokens
        body["inferenceConfig"] = inf_cfg
    else:
        existing = body.get("max_tokens")
        body["max_tokens"] = min(existing, max_output_tokens) if existing else max_output_tokens
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
    code = exc.response["Error"]["Code"]
    if code in ("ThrottlingException", "TooManyRequestsException"):
        raise BedrockError("BEDROCK_THROTTLED", "Bedrock rate limit exceeded", 429) from exc
    raise BedrockError(
        "BEDROCK_ERROR", f"Bedrock error: {exc.response['Error']['Message']}", 502
    ) from exc
