# Calling the bedrock-api proxy

The proxy accepts standard [AWS Bedrock Converse](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_Converse.html)
and InvokeModel request bodies. Authentication is a bearer token in the
`Authorization` header — no SigV4 signing required.

**Streaming is supported** via the `converse-stream` / `invoke-with-response-stream`
routes (Server-Sent Events) — see [Streaming](#streaming).

---

## Endpoint format

```
POST {API_URL}/model/{url-encoded-model-id}/converse
POST {API_URL}/model/{url-encoded-model-id}/invoke
POST {API_URL}/model/{url-encoded-model-id}/converse-stream                # SSE
POST {API_URL}/model/{url-encoded-model-id}/invoke-with-response-stream    # SSE
POST {API_URL}/openai/v1/responses                                         # OpenAI models
```

`{API_URL}` includes the API Gateway stage, e.g.
`https://<id>.execute-api.us-east-1.amazonaws.com/v1`.

The last route reaches a different family of models (OpenAI, Gemma, Grok) over the
OpenAI **Responses API**; it takes the model in the request body rather than the path.
See "OpenAI models" below.

Model IDs contain colons (`:`), which must be percent-encoded as `%3A`:

| Model | Path segment |
|---|---|
| `us.anthropic.claude-sonnet-4-6` | `us.anthropic.claude-sonnet-4-6` |
| `us.anthropic.claude-haiku-4-5-20251001-v1:0` | `us.anthropic.claude-haiku-4-5-20251001-v1%3A0` |

---

## curl

```bash
API_URL="https://abc123.execute-api.us-east-1.amazonaws.com/v1"
TOKEN="bk_<32hex>.<64hex>"
MODEL="us.anthropic.claude-sonnet-4-6"

curl -s -X POST "${API_URL}/model/${MODEL}/converse" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{
    "messages": [
      {"role": "user", "content": [{"text": "Hello!"}]}
    ]
  }' | jq .
```

---

## Python — httpx

```python
from urllib.parse import quote
import httpx

API_URL = "https://abc123.execute-api.us-east-1.amazonaws.com/v1"
TOKEN = "bk_<32hex>.<64hex>"
MODEL_ID = "us.anthropic.claude-sonnet-4-6"

url = f"{API_URL}/model/{quote(MODEL_ID, safe='')}/converse"

response = httpx.post(
    url,
    headers={"Authorization": f"Bearer {TOKEN}"},
    json={"messages": [{"role": "user", "content": [{"text": "Hello!"}]}]},
    timeout=30.0,
)
response.raise_for_status()
print(response.json()["output"]["message"]["content"][0]["text"])
```

---

## Python — boto3

Use `botocore.UNSIGNED` to suppress SigV4 signing, then inject the bearer
token via a `before-send` event hook:

```python
import boto3
from botocore import UNSIGNED
from botocore.config import Config

API_URL = "https://abc123.execute-api.us-east-1.amazonaws.com/v1"
TOKEN = "bk_<32hex>.<64hex>"

client = boto3.client(
    "bedrock-runtime",
    region_name="us-east-1",
    endpoint_url=API_URL,
    config=Config(signature_version=UNSIGNED),
)


def _add_bearer(request, **kwargs):
    request.headers["Authorization"] = f"Bearer {TOKEN}"


# Register once; applies to all bedrock-runtime calls on this client
client.meta.events.register("before-send.bedrock-runtime.*", _add_bearer)

response = client.converse(
    modelId="us.anthropic.claude-sonnet-4-6",
    messages=[{"role": "user", "content": [{"text": "Hello!"}]}],
)
print(response["output"]["message"]["content"][0]["text"])
```

`UNSIGNED` removes all AWS credential headers. The `before-send` hook then
adds `Authorization: Bearer <token>` before the HTTP request is dispatched.
boto3 constructs the URL path (`/model/{modelId}/converse`) automatically, so
no manual URL encoding is needed when using the SDK. Set `endpoint_url` to the
full stage URL including `/v1`.

---

## Streaming

The `converse-stream` and `invoke-with-response-stream` routes return
**Server-Sent Events** (`Content-Type: text/event-stream`). Each event is a
`data: {json}\n\n` frame relaying the raw Bedrock stream event verbatim (no
transform); the final `metadata` event carries the token usage.

Errors are handled by position: a failure *before* the stream opens (auth,
throttling, bad params) returns a normal HTTP 4xx/5xx JSON error; a failure
*mid-stream* arrives as a terminal `data: {"error": {...}}` frame on an
already-200 response.

```bash
curl -sN -X POST "${API_URL}/model/${MODEL}/converse-stream" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":[{"text":"Hello!"}]}]}'
```

```
data: {"messageStart": {"role": "assistant"}}
data: {"contentBlockDelta": {"delta": {"text": "Hello"}, "contentBlockIndex": 0}}
data: {"contentBlockStop": {"contentBlockIndex": 0}}
data: {"messageStop": {"stopReason": "end_turn"}}
data: {"metadata": {"usage": {"inputTokens": 10, "outputTokens": 4, "totalTokens": 14}}}
```

`invoke-with-response-stream` relays the InvokeModel chunk payloads the same way
(each chunk's bytes decoded to JSON, bytes-valued fields base64-encoded for
transport).

---

## OpenAI models (Responses API)

Some models — `openai.gpt-5.6-luna`, `openai.gpt-5.6-sol`, `openai.gpt-5.6-terra`,
`openai.gpt-oss-120b`, `google.gemma-4-31b`, `xai.grok-4.3` and friends — are **not**
reachable through `converse` / `invoke`; they are served on the OpenAI **Responses API**
and rejected as invalid model IDs by the Bedrock routes. Use `/openai/v1/responses`:

```bash
curl -s -X POST "${API_URL}/openai/v1/responses" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"model":"openai.gpt-5.6-luna","input":"Hello!"}'
```

The model goes in the **body**, not the path — so this route needs no percent-encoding.
The request and response bodies are passed through unmodified, which means you can point
the official OpenAI SDK straight at the proxy:

```python
from openai import OpenAI

client = OpenAI(base_url=f"{API_URL}/openai/v1", api_key=TOKEN)
resp = client.responses.create(model="openai.gpt-5.6-luna", input="Hello!")
print(resp.output_text)
```

Your bearer token goes in `api_key`; the proxy signs the upstream AWS request itself.

Add `"stream": true` to receive Server-Sent Events. Unlike the Bedrock streaming routes,
these frames carry the Responses API's own `event:` type lines alongside each `data:`
line, relayed verbatim, so SDK streaming (`client.responses.stream(...)`) works normally.

Your per-token limits, budget, and model allowlist apply exactly as they do on the
Bedrock routes. The allowlist matches the model ID as written in the body.

**Token accounting.** Usage is reported as `input_tokens` / `output_tokens`, with a
breakdown in `output_tokens_details.reasoning_tokens`. Reasoning tokens are **already
counted inside `output_tokens`** — they are billed at the normal output rate and are not
charged twice. `GET /usage` reports them within your output-token total.

---

## Response shape

The proxy returns the Bedrock response body unmodified (with `ResponseMetadata`
stripped). For the Converse API:

```json
{
  "output": {
    "message": {
      "role": "assistant",
      "content": [{"text": "Hello!"}]
    }
  },
  "stopReason": "end_turn",
  "usage": {
    "inputTokens": 10,
    "outputTokens": 4,
    "totalTokens": 14
  }
}
```

On error the proxy returns:

```json
{"error": {"code": "INVALID_TOKEN", "message": "Invalid or revoked token"}}
```

| HTTP | Code | Meaning |
|---|---|---|
| 401 | `INVALID_TOKEN` | Missing, revoked, or invalid token |
| 403 | `MODEL_NOT_ALLOWED` | Model not in this token's allowlist |
| 413 | `INPUT_TOKEN_LIMIT_EXCEEDED` | Request exceeds the token's input-token cap |
| 429 | `RATE_LIMIT_EXCEEDED` | Per-second rate limit exceeded |
| 429 | `MONTHLY_REQUEST_QUOTA_EXCEEDED` | Monthly request quota exhausted |
| 429 | `MONTHLY_BUDGET_EXCEEDED` | Monthly USD budget exhausted |
| 429 | `BEDROCK_THROTTLED` | Bedrock is throttling requests |
| 502 | `BEDROCK_ERROR` | Bedrock returned an error |
