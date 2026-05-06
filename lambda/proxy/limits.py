import json
import math
import time

from botocore.exceptions import ClientError


class LimitError(Exception):
    def __init__(self, code: str, message: str, status: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def check_rate_limit(token_id: str, token_row: dict, rate_limit_table) -> None:
    """Enforce per-second rate limit via DynamoDB conditional ADD.

    Absent limit_rps attribute = no rate limit enforced.
    limit_rps = 0 blocks all requests (explicit pre-check required because the
    attribute_not_exists guard in the condition would let one request through
    even when :limit is 0).
    """
    if "limit_rps" not in token_row:
        return
    limit_rps = int(token_row["limit_rps"])
    if limit_rps == 0:
        raise LimitError("RATE_LIMIT_EXCEEDED", "Rate limit exceeded", 429)
    now_sec = int(time.time())
    try:
        rate_limit_table.update_item(
            Key={"token_id": token_id, "window_second": now_sec},
            UpdateExpression="ADD #c :one SET #ttl = if_not_exists(#ttl, :exp)",
            ConditionExpression="attribute_not_exists(#c) OR #c < :limit",
            ExpressionAttributeNames={"#c": "count", "#ttl": "ttl"},
            ExpressionAttributeValues={
                ":one": 1,
                ":limit": limit_rps,
                ":exp": now_sec + 10,
            },
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise LimitError("RATE_LIMIT_EXCEEDED", "Rate limit exceeded", 429) from exc
        raise


def check_monthly_quota(token_row: dict, usage: dict) -> None:
    """Check monthly request count and USD budget limits.

    Reads the same usage dict for both checks (caller fetches once).
    Absent limit attribute = unlimited.  Value 0 = block all.
    """
    if "limit_monthly_requests" in token_row:
        limit = int(token_row["limit_monthly_requests"])
        current = int(usage.get("requests", 0))
        if limit == 0 or current >= limit:
            raise LimitError(
                "MONTHLY_REQUEST_QUOTA_EXCEEDED", "Monthly request quota exhausted", 429
            )
    if "limit_monthly_usd_micros" in token_row:
        limit = int(token_row["limit_monthly_usd_micros"])
        current = int(usage.get("usd_micros", 0))
        if limit == 0 or current >= limit:
            raise LimitError("MONTHLY_BUDGET_EXCEEDED", "Monthly budget exhausted", 429)


def estimate_input_tokens(body: dict, route: str) -> int:
    """Heuristic input token estimate: ceil(total_prompt_chars / 4).

    Does NOT call a tokenizer — no external deps, stays within 5 MB Lambda ZIP.
    This is a ceiling estimate; the true count from Bedrock is used for billing.
    """
    chars = _extract_chars(body, route)
    if chars == 0:
        chars = len(json.dumps(body))
    return math.ceil(chars / 4)


def _extract_chars(body: dict, route: str) -> int:
    chars = 0
    if route == "converse":
        for msg in body.get("messages", []):
            for block in msg.get("content", []):
                if isinstance(block, dict):
                    chars += len(block.get("text", ""))
        for block in body.get("system", []):
            if isinstance(block, dict):
                chars += len(block.get("text", ""))
    else:
        # InvokeModel — Anthropic native format
        for msg in body.get("messages", []):
            content = msg.get("content", "")
            if isinstance(content, str):
                chars += len(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        chars += len(block.get("text", ""))
        system = body.get("system", "")
        if isinstance(system, str):
            chars += len(system)
    return chars


def check_input_cap(estimated_tokens: int, token_row: dict) -> None:
    """Reject if heuristic estimate exceeds limit_max_input_tokens."""
    if "limit_max_input_tokens" not in token_row:
        return
    limit = int(token_row["limit_max_input_tokens"])
    if estimated_tokens > limit:
        raise LimitError(
            "INPUT_TOKEN_LIMIT_EXCEEDED",
            f"Estimated input tokens ({estimated_tokens}) exceeds cap ({limit})",
            413,
        )


def check_model_allowlist(model_id: str, token_row: dict, default_models: list[str]) -> None:
    """Enforce the per-token model allowlist.

    Priority:
      1. token_row["allowed_models"] (SS) if present.
      2. default_models (from ALLOWED_MODELS_DEFAULT env var) if non-empty.
      3. No restriction (absent attribute + empty default = all models allowed).
    """
    if "allowed_models" in token_row:
        allowed = token_row["allowed_models"]
    elif default_models:
        allowed = set(default_models)
    else:
        return
    if model_id not in allowed:
        raise LimitError("MODEL_NOT_ALLOWED", f"Model {model_id!r} is not permitted", 403)


def write_usage(
    token_id: str,
    period: str,
    in_tokens: int,
    out_tokens: int,
    cache_read_input_tokens: int,
    cache_write_input_tokens: int,
    usd_micros: int,
    usage_table,
) -> None:
    """Atomically increment all four usage counters for the current period.

    DynamoDB ADD is atomic at the item level; auto-initialises missing attributes
    to zero, so the first call of a new month creates the item.
    """
    usage_table.update_item(
        Key={"token_id": token_id, "period": period},
        UpdateExpression=(
            "ADD requests :r, input_tokens :i, output_tokens :o, "
            "cache_read_input_tokens :cri, cache_write_input_tokens :cwi, usd_micros :u"
        ),
        ExpressionAttributeValues={
            ":r": 1,
            ":i": in_tokens,
            ":o": out_tokens,
            ":cri": cache_read_input_tokens,
            ":cwi": cache_write_input_tokens,
            ":u": usd_micros,
        },
    )
