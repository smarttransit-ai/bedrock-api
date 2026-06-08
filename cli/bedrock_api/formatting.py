"""Table rendering for bedrock-api CLI output.

All display functions write to sys.stderr by default so that only the
secret bearer token reaches stdout on `issue`.
"""

import sys
from decimal import Decimal


def _truncate(s: str, n: int) -> str:
    """Truncate s to exactly n visible characters, appending … if truncated."""
    if len(s) > n:
        return s[: n - 1] + "…"
    return s.ljust(n)


def _int(val) -> int:
    """Convert DynamoDB Decimal or str to int safely."""
    return int(Decimal(str(val)))


def _usd(micros) -> str:
    return f"{_int(micros) / 1_000_000:.4f}"


def format_token_list(items: list[dict], file=None) -> None:
    """Print tokens as a wide fixed-width table."""
    if file is None:
        file = sys.stderr
    col_defs = [
        ("TOKEN_ID", 17),
        ("OWNER", 16),
        ("STATUS", 8),
        ("ADMIN", 5),
        ("CREATED_AT", 20),
        ("REQUESTS", 10),
        ("USD", 10),
    ]
    header = "  ".join(h.ljust(w) for h, w in col_defs)
    sep = "  ".join("-" * w for _, w in col_defs)
    print(header, file=file)
    print(sep, file=file)
    for item in items:
        token_id = _truncate(item.get("token_id", ""), 17)
        owner = _truncate(item.get("owner", ""), 16)
        status = _truncate(item.get("status", ""), 8)
        admin = "yes" if item.get("admin") is True else ""
        created = _truncate(item.get("created_at", "")[:19], 20)  # trim sub-seconds
        requests = str(_int(item["requests"])) if "requests" in item else "0"
        usd = _usd(item["usd_micros"]) if "usd_micros" in item else "0.0000"
        row = "  ".join(
            [
                token_id,
                owner,
                status,
                admin.ljust(5),
                created,
                requests.ljust(10),
                usd.ljust(10),
            ]
        )
        print(row, file=file)


def format_token_show(token_row: dict, usage_row: dict | None, file=None) -> None:
    """Print all token metadata (never secret_hash) and current-period usage."""
    if file is None:
        file = sys.stderr
    # Never output the secret hash — drop it explicitly
    safe = {k: v for k, v in token_row.items() if k != "secret_hash"}

    print("=== Token ===", file=file)
    for key in [
        "token_id",
        "owner",
        "status",
        "admin",
        "created_at",
        "revoked_at",
        "note",
        "limit_rps",
        "limit_monthly_requests",
        "limit_monthly_usd_micros",
        "limit_max_input_tokens",
        "limit_max_output_tokens",
        "allowed_models",
    ]:
        if key in safe:
            val = safe[key]
            if key == "limit_monthly_usd_micros":
                print(f"  {key}: {_usd(val)} USD/month ({_int(val)} µUSD)", file=file)
            elif isinstance(val, set):
                print(f"  {key}: {', '.join(sorted(val))}", file=file)
            else:
                print(f"  {key}: {val}", file=file)

    print("=== Usage (current period) ===", file=file)
    if usage_row:
        print(f"  requests:      {_int(usage_row.get('requests', 0))}", file=file)
        print(f"  input_tokens:  {_int(usage_row.get('input_tokens', 0))}", file=file)
        print(f"  output_tokens: {_int(usage_row.get('output_tokens', 0))}", file=file)
        print(f"  usd:           {_usd(usage_row.get('usd_micros', 0))}", file=file)
    else:
        print("  (no usage this period)", file=file)


def format_usage(token_id: str, period: str, usage_row: dict | None, file=None) -> None:
    """Print usage counters for a token+period."""
    if file is None:
        file = sys.stderr
    print(f"token_id: {token_id}", file=file)
    print(f"period:   {period}", file=file)
    if usage_row:
        print(f"requests:      {_int(usage_row.get('requests', 0))}", file=file)
        print(f"input_tokens:  {_int(usage_row.get('input_tokens', 0))}", file=file)
        print(f"output_tokens: {_int(usage_row.get('output_tokens', 0))}", file=file)
        print(f"usd:           {_usd(usage_row.get('usd_micros', 0))}", file=file)
    else:
        print("requests:      0", file=file)
        print("input_tokens:  0", file=file)
        print("output_tokens: 0", file=file)
        print("usd:           0.0000", file=file)
