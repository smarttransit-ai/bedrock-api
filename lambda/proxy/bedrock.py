import json
from urllib.parse import unquote

from botocore.exceptions import ClientError


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


def forward_converse(client, model_id: str, body: dict) -> tuple[dict, int, int]:
    """Call Bedrock Converse API and return (response_body, input_tokens, output_tokens).

    modelId is a path parameter for the SDK; strip it from the body kwargs if
    present to avoid a duplicate-parameter error.
    ResponseMetadata is stripped before returning to the client.
    """
    kwargs = {k: v for k, v in body.items() if k != "modelId"}
    try:
        response = client.converse(modelId=model_id, **kwargs)
    except ClientError as exc:
        _raise_bedrock_error(exc)
    response.pop("ResponseMetadata", None)
    usage = response.get("usage") or {}
    return response, int(usage.get("inputTokens", 0)), int(usage.get("outputTokens", 0))


def forward_invoke_model(client, model_id: str, body: dict) -> tuple[dict, int, int]:
    """Call Bedrock InvokeModel API and return (response_body, input_tokens, output_tokens).

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
    except ClientError as exc:
        _raise_bedrock_error(exc)
    response_body = json.loads(response["body"].read())
    usage = response_body.get("usage") or {}
    in_tokens = int(usage.get("input_tokens", 0))
    out_tokens = int(usage.get("output_tokens", 0))
    return response_body, in_tokens, out_tokens


def _raise_bedrock_error(exc: ClientError) -> None:
    code = exc.response["Error"]["Code"]
    if code in ("ThrottlingException", "TooManyRequestsException"):
        raise BedrockError("BEDROCK_THROTTLED", "Bedrock rate limit exceeded", 429) from exc
    raise BedrockError(
        "BEDROCK_ERROR", f"Bedrock error: {exc.response['Error']['Message']}", 502
    ) from exc
