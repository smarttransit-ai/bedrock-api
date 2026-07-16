"""bedrock-mantle (OpenAI Responses API) transport.

A second provider path alongside ``bedrock.py``. bedrock-mantle serves OpenAI-compatible
models (openai.gpt-5.6-luna, xai.grok-4.3, google.gemma-4-*) that Converse/InvokeModel
cannot reach at all — the model IDs are rejected as invalid by bedrock-runtime.

Kept separate from bedrock.py deliberately: that module is boto3/EventStream-shaped, this
one is httpx/SSE-shaped. Mixing two transports in one module would obscure both. What IS
shared lives upstream in app.py — auth, limits, output cap, billing, logging — so this
module only owns "talk to mantle and report usage".

Auth is SigV4 with service name ``bedrock`` (not a Bedrock API key).
"""

import json
import logging
import os
from collections.abc import Generator

import botocore.session
import httpx
from bedrock import BedrockError, normalize_usage
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.exceptions import NoCredentialsError

logger = logging.getLogger(__name__)

RESPONSES_PATH = "/openai/v1/responses"

# OpenAI error `type` → (our error code, HTTP status).
# invalid_api_key/permission_denied_error mean OUR SigV4 signing or IAM is broken, never
# that the caller's bearer token is bad — surfacing 401 would wrongly tell a client to
# re-issue a perfectly good token, so it maps to 502 like any upstream failure.
_ERROR_STATUS = {
    "validation_error": ("BAD_REQUEST", 400),
    "invalid_request_error": ("BAD_REQUEST", 400),
    "not_found_error": ("MODEL_NOT_FOUND", 404),
    "rate_limit_error": ("THROTTLED", 429),
}
_DEFAULT_ERROR = ("BEDROCK_ERROR", 502)


def sign_headers(client, body: bytes, path: str = RESPONSES_PATH) -> dict[str, str]:
    """Return SigV4-signed headers for a POST of *body* to *path*.

    The botocore Session is built per call, never cached at module level: Lambda rotates
    execution-role credentials by rewriting AWS_SESSION_TOKEN in the environment, and a
    cached Session keeps returning the credentials it first resolved — which start failing
    with 403s after the first rotation. Session construction is cheap next to the network
    call it precedes.
    """
    session = botocore.session.get_session()
    credentials = session.get_credentials()
    if credentials is None:
        raise BedrockError("BEDROCK_ERROR", "No AWS credentials available for mantle", 502)
    # Must match the region in the endpoint host (deps.get_mantle derives it the same way).
    # botocore's own config resolves AWS_REGION, which is the Lambda's deploy region and can
    # differ from BEDROCK_REGION — signing a different region than the host 403s.
    region = os.environ.get("BEDROCK_REGION", "us-east-1")
    url = f"{client.base_url}{path}"
    request = AWSRequest(
        method="POST", url=url, data=body, headers={"content-type": "application/json"}
    )
    try:
        SigV4Auth(credentials, "bedrock", region).add_auth(request)
    except NoCredentialsError as exc:
        raise BedrockError("BEDROCK_ERROR", "No AWS credentials available for mantle", 502) from exc
    return dict(request.headers)


def _raise_mantle_error(status: int, payload: dict | None) -> None:
    """Map an OpenAI-shaped error envelope onto BedrockError. Always raises."""
    error = (payload or {}).get("error") or {}
    err_type = error.get("type", "")
    message = error.get("message") or f"bedrock-mantle returned HTTP {status}"
    code, mapped_status = _ERROR_STATUS.get(err_type, _DEFAULT_ERROR)
    raise BedrockError(code, message, mapped_status)


def _flatten_usage(usage: dict | None) -> dict:
    """Flatten the Responses usage shape so normalize_usage (flat reader) can read it.

    Responses nests cache counters one level down:
        {"input_tokens": 12,
         "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
         "output_tokens": 5,
         "output_tokens_details": {"reasoning_tokens": 0}}

    Flattening here (rather than teaching normalize_usage about nested paths) keeps the
    Converse key-map contract untouched — one call site absorbs the shape difference.
    """
    if not isinstance(usage, dict):
        return {}
    flat = dict(usage)
    for detail_key in ("input_tokens_details", "output_tokens_details"):
        details = usage.get(detail_key)
        if isinstance(details, dict):
            flat.update(details)
    return flat


# Responses usage → our canonical names. reasoning_tokens is deliberately absent: it is a
# SUBSET of output_tokens (a breakdown, not an addend), so output_tokens already bills it.
# Adding it as a component would double-charge the reasoning portion. It is logged, not billed.
USAGE_KEY_MAP = {
    "input_tokens": "input_tokens",
    "output_tokens": "output_tokens",
    "cache_read_input_tokens": "cached_tokens",
    "cache_write_input_tokens": "cache_write_tokens",
}


def reasoning_tokens_of(usage: dict | None) -> int:
    """Reasoning-token count for logging only (never a billing dimension — see USAGE_KEY_MAP)."""
    details = (usage or {}).get("output_tokens_details")
    value = details.get("reasoning_tokens") if isinstance(details, dict) else 0
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def forward_responses(client, model_id: str, body: dict) -> tuple[dict, dict[str, int]]:
    """POST /openai/v1/responses (non-streaming); return (response_body, normalized_usage).

    Mirrors bedrock.forward_converse's contract so the shared route body is unchanged.
    """
    payload = json.dumps({**body, "model": model_id, "stream": False}).encode()
    try:
        resp = client.post(RESPONSES_PATH, content=payload, headers=sign_headers(client, payload))
    except httpx.HTTPError as exc:
        raise BedrockError("BEDROCK_ERROR", f"bedrock-mantle request failed: {exc}", 502) from exc

    parsed = _safe_json(resp.text)
    if resp.status_code != 200:
        _raise_mantle_error(resp.status_code, parsed)
    if parsed is None:
        raise BedrockError("BEDROCK_ERROR", "bedrock-mantle returned non-JSON body", 502)

    usage = normalize_usage(_flatten_usage(parsed.get("usage")), USAGE_KEY_MAP)
    return parsed, usage


def open_responses_stream(client, model_id: str, body: dict):
    """Open a streaming Responses call and return the live httpx.Response.

    Eager open (amendment B1 parity): the request is sent and the status checked here, so
    auth/validation failures surface as a real 4xx/5xx BEFORE StreamingResponse is built.
    The caller MUST drive iter_responses_sse, which closes the response.
    """
    payload = json.dumps({**body, "model": model_id, "stream": True}).encode()
    request = client.build_request(
        "POST", RESPONSES_PATH, content=payload, headers=sign_headers(client, payload)
    )
    try:
        resp = client.send(request, stream=True)
    except httpx.HTTPError as exc:
        raise BedrockError("BEDROCK_ERROR", f"bedrock-mantle request failed: {exc}", 502) from exc

    if resp.status_code != 200:
        try:
            resp.read()
            _raise_mantle_error(resp.status_code, _safe_json(resp.text))
        finally:
            resp.close()
    return resp


def iter_responses_sse(resp, usage_out: dict) -> Generator[str, None, None]:
    """Relay raw SSE lines verbatim, harvesting usage from the terminal event.

    Yields the upstream's own lines rather than re-serializing parsed events. The Responses
    SSE stream pairs an ``event: <type>`` line with each ``data:`` line, and OpenAI clients
    dispatch on that type — reformatting through a data-only emitter would strip the event
    types and break compliant SDKs. Verbatim relay is also cheaper: no parse/reserialize.

    Usage arrives on the terminal ``response.completed`` event (``data.response.usage``).
    Closing the response here is mandatory: unlike boto3's GC-managed EventStream, an httpx
    streaming response holds its connection until explicitly closed.
    """
    try:
        for line in resp.iter_lines():
            yield f"{line}\n"
            if not line.startswith("data:"):
                continue
            payload = _safe_json(line[len("data:") :].strip())
            if not payload or payload.get("type") != "response.completed":
                continue
            usage = (payload.get("response") or {}).get("usage")
            if usage:
                usage_out.update(normalize_usage(_flatten_usage(usage), USAGE_KEY_MAP))
                usage_out["reasoning_tokens"] = reasoning_tokens_of(usage)
    finally:
        resp.close()


def _safe_json(text: str) -> dict | None:
    """Parse JSON, returning None rather than raising (callers map that to a 502/skip)."""
    try:
        parsed = json.loads(text)
    except ValueError:  # JSONDecodeError subclasses ValueError
        return None
    return parsed if isinstance(parsed, dict) else None
