"""bedrock-api admin CLI.

Talks directly to DynamoDB using the operator's boto3 credentials.
Tables resolved as {prefix}-tokens and {prefix}-usage.

Stdout: bearer token (issue only).
Stderr: all other output and errors.
"""

import os
import sys
from datetime import UTC, datetime

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

from bedrock_api.formatting import format_token_list, format_token_show, format_usage
from bedrock_api.tokens import current_period, generate_token


def _die(msg: str, code: int = 1) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def token_id_arg(value: str) -> str:
    """Accept a token id, and tolerate a full bearer token without ever echoing its secret.

    A bearer is ``<token_id>.<secret>``. Passing the whole thing is an easy mistake: it is what
    lives in a caller's .env, and it starts with the same ``bk_`` prefix as the id. Every command
    here echoes ``args.token_id`` back in error and confirmation messages, so an unsplit bearer
    ends up in terminal scrollback, CI logs and shell history -- a working credential, disclosed
    by a typo.

    Splitting at the argparse boundary fixes all of those call sites at once, because the secret
    half never reaches ``args`` and so cannot reach a print. It also turns a confusing
    "token_id not found" into a hint about what the caller actually did.
    """
    token_id, sep, _secret = value.partition(".")
    if sep:
        print(
            f"note: got a full bearer token; using the id before the '.' ({token_id}). "
            "The secret half is not needed and has not been logged.",
            file=sys.stderr,
        )
    return token_id


def _get_tables(args):
    region = args.region
    dynamodb = boto3.resource("dynamodb", region_name=region)
    prefix = args.table_prefix
    tokens_table = dynamodb.Table(f"{prefix}-tokens")
    usage_table = dynamodb.Table(f"{prefix}-usage")
    return tokens_table, usage_table


def _parse_models(models_str: str) -> set[str] | None:
    """Parse comma-separated model IDs, strip whitespace.

    Returns None if string is falsy (not provided).
    Returns empty set if string is empty or whitespace-only (triggers REMOVE).
    Returns non-empty set otherwise.
    """
    if models_str is None:
        return None
    parts = {m.strip() for m in models_str.split(",") if m.strip()}
    return parts  # may be empty set — caller must handle


def _scan_all(table, **kwargs) -> list[dict]:
    """Paginate through a Scan, returning all items."""
    items = []
    response = table.scan(**kwargs)
    items.extend(response.get("Items", []))
    while "LastEvaluatedKey" in response:
        response = table.scan(ExclusiveStartKey=response["LastEvaluatedKey"], **kwargs)
        items.extend(response.get("Items", []))
    return items


def _query_all(table, **kwargs) -> list[dict]:
    """Paginate through a Query, returning all items."""
    items = []
    response = table.query(**kwargs)
    items.extend(response.get("Items", []))
    while "LastEvaluatedKey" in response:
        response = table.query(ExclusiveStartKey=response["LastEvaluatedKey"], **kwargs)
        items.extend(response.get("Items", []))
    return items


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------


def cmd_issue(args) -> None:
    tokens_table, _ = _get_tables(args)

    token_id, bearer_token, secret_hash = generate_token()
    now = datetime.now(UTC).isoformat()

    item: dict = {
        "token_id": token_id,
        "secret_hash": secret_hash,
        "owner": args.owner,
        "created_at": now,
        "status": "active",
    }

    if args.budget is not None:
        item["limit_monthly_usd_micros"] = int(round(float(args.budget) * 1_000_000))
    if args.rps is not None:
        item["limit_rps"] = args.rps
    if args.monthly_requests is not None:
        item["limit_monthly_requests"] = args.monthly_requests
    if args.max_input_tokens is not None:
        item["limit_max_input_tokens"] = args.max_input_tokens
    if args.max_output_tokens is not None:
        item["limit_max_output_tokens"] = args.max_output_tokens
    if args.note:
        item["note"] = args.note

    # --models: only write SS if non-empty; never write empty SS (DynamoDB rejects it)
    if args.models is not None:
        model_set = _parse_models(args.models)
        if model_set:
            item["allowed_models"] = model_set

    if args.admin:
        item["admin"] = True

    try:
        tokens_table.put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(token_id)",
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            _die(f"token_id collision: {token_id} already exists (should never happen)")
        raise

    # Bearer token to stdout ONLY — can be piped to a file
    print(bearer_token)

    # All metadata to stderr
    print(f"token_id:   {token_id}", file=sys.stderr)
    print(f"owner:      {args.owner}", file=sys.stderr)
    print(f"created_at: {now}", file=sys.stderr)
    if args.budget is not None:
        print(f"budget:     ${args.budget:.2f}/month", file=sys.stderr)
    if args.rps is not None:
        print(f"rps:        {args.rps}", file=sys.stderr)
    if args.monthly_requests is not None:
        print(f"monthly_requests: {args.monthly_requests}", file=sys.stderr)
    if args.max_input_tokens is not None:
        print(f"max_input_tokens: {args.max_input_tokens}", file=sys.stderr)
    if args.max_output_tokens is not None:
        print(f"max_output_tokens: {args.max_output_tokens}", file=sys.stderr)
    if args.models is not None and item.get("allowed_models"):
        print(f"models:     {', '.join(sorted(item['allowed_models']))}", file=sys.stderr)
    if args.note:
        print(f"note:       {args.note}", file=sys.stderr)
    if args.admin:
        print("admin:      true", file=sys.stderr)


def cmd_revoke(args) -> None:
    tokens_table, _ = _get_tables(args)

    response = tokens_table.get_item(Key={"token_id": args.token_id})
    item = response.get("Item")
    if not item:
        _die(f"token_id not found: {args.token_id}")

    if item.get("status") == "revoked":
        print(f"already revoked: {args.token_id}", file=sys.stderr)
        return

    now = datetime.now(UTC).isoformat()
    try:
        tokens_table.update_item(
            Key={"token_id": args.token_id},
            UpdateExpression="SET #status = :revoked, revoked_at = :now",
            ConditionExpression="attribute_exists(token_id)",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":revoked": "revoked", ":now": now},
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            _die(f"token_id not found: {args.token_id}")
        raise

    print(f"revoked: {args.token_id} at {now}", file=sys.stderr)


def _enrich_with_usage(items: list[dict], usage_table, period: str) -> list[dict]:
    """Fetch current-period usage for each token and merge into item dicts.

    Uses batch_get_item in chunks of 100 to minimize round-trips.
    """
    if not items:
        return items

    # Build request keys in chunks of 100 (DynamoDB batch limit)
    table_name = usage_table.name
    keys = [{"token_id": item["token_id"], "period": period} for item in items]
    usage_map: dict[str, dict] = {}

    for i in range(0, len(keys), 100):
        chunk = keys[i : i + 100]
        response = usage_table.meta.client.batch_get_item(
            RequestItems={table_name: {"Keys": chunk}}
        )
        for row in response.get("Responses", {}).get(table_name, []):
            usage_map[row["token_id"]] = row
        # Handle UnprocessedKeys (retry automatically — rare in practice)
        unprocessed = response.get("UnprocessedKeys", {})
        while unprocessed:
            retry = usage_table.meta.client.batch_get_item(RequestItems=unprocessed)
            for row in retry.get("Responses", {}).get(table_name, []):
                usage_map[row["token_id"]] = row
            unprocessed = retry.get("UnprocessedKeys", {})

    for item in items:
        usage = usage_map.get(item["token_id"], {})
        item["requests"] = usage.get("requests", 0)
        item["usd_micros"] = usage.get("usd_micros", 0)
    return items


def cmd_list(args) -> None:
    tokens_table, usage_table = _get_tables(args)
    status_filter = args.status  # "active", "revoked", or "all"

    if args.owner:
        # Query via owner-index GSI
        query_kwargs: dict = {
            "IndexName": "owner-index",
            "KeyConditionExpression": Key("owner").eq(args.owner),
        }
        if status_filter != "all":
            query_kwargs["FilterExpression"] = Attr("status").eq(status_filter)
        items = _query_all(tokens_table, **query_kwargs)
    else:
        # Full table scan with optional status filter
        scan_kwargs: dict = {}
        if status_filter != "all":
            scan_kwargs["FilterExpression"] = Attr("status").eq(status_filter)
        items = _scan_all(tokens_table, **scan_kwargs)

    _enrich_with_usage(items, usage_table, current_period())
    format_token_list(items)


def cmd_show(args) -> None:
    tokens_table, usage_table = _get_tables(args)

    response = tokens_table.get_item(Key={"token_id": args.token_id})
    token_row = response.get("Item")
    if not token_row:
        _die(f"token_id not found: {args.token_id}")

    period = current_period()
    usage_response = usage_table.get_item(Key={"token_id": args.token_id, "period": period})
    usage_row = usage_response.get("Item")

    format_token_show(token_row, usage_row)


def cmd_set_limit(args) -> None:
    tokens_table, _ = _get_tables(args)

    set_parts = []
    remove_parts = []
    expr_values: dict = {}

    if args.budget is not None:
        set_parts.append("limit_monthly_usd_micros = :budget")
        expr_values[":budget"] = int(round(float(args.budget) * 1_000_000))

    if args.rps is not None:
        set_parts.append("limit_rps = :rps")
        expr_values[":rps"] = args.rps

    if args.monthly_requests is not None:
        set_parts.append("limit_monthly_requests = :mr")
        expr_values[":mr"] = args.monthly_requests

    if args.max_input_tokens is not None:
        set_parts.append("limit_max_input_tokens = :mit")
        expr_values[":mit"] = args.max_input_tokens

    if args.max_output_tokens is not None:
        set_parts.append("limit_max_output_tokens = :mot")
        expr_values[":mot"] = args.max_output_tokens

    if args.models is not None:
        model_set = _parse_models(args.models)
        if model_set:
            # SET the string set
            set_parts.append("allowed_models = :models")
            expr_values[":models"] = model_set
        else:
            # Empty string → REMOVE the attribute (never write empty SS)
            remove_parts.append("allowed_models")

    if not set_parts and not remove_parts:
        _die(
            "at least one limit flag is required (--budget, --rps, --monthly-requests, "
            "--max-input-tokens, --max-output-tokens, --models)"
        )

    # Build combined UpdateExpression
    update_expr = ""
    if set_parts:
        update_expr += "SET " + ", ".join(set_parts)
    if remove_parts:
        update_expr += (" " if update_expr else "") + "REMOVE " + ", ".join(remove_parts)

    update_kwargs: dict = {
        "Key": {"token_id": args.token_id},
        "UpdateExpression": update_expr,
        "ConditionExpression": "attribute_exists(token_id)",
    }
    if expr_values:
        update_kwargs["ExpressionAttributeValues"] = expr_values

    try:
        tokens_table.update_item(**update_kwargs)
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            _die(f"token_id not found: {args.token_id}")
        raise

    print(f"updated limits for: {args.token_id}", file=sys.stderr)


def cmd_usage(args) -> None:
    _, usage_table = _get_tables(args)

    period = args.period if args.period else current_period()
    response = usage_table.get_item(Key={"token_id": args.token_id, "period": period})
    usage_row = response.get("Item")

    format_usage(args.token_id, period, usage_row)


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def build_parser():
    import argparse

    default_region = (
        os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
    )

    parser = argparse.ArgumentParser(
        prog="bedrock-api",
        description="Admin CLI for bedrock-api token and usage management",
    )
    parser.add_argument("--region", default=default_region, help="AWS region (default: us-east-1)")
    parser.add_argument(
        "--table-prefix",
        default="bedrock-api",
        help="DynamoDB table name prefix (default: bedrock-api)",
    )

    sub = parser.add_subparsers(dest="subcommand", required=True)

    # issue
    p_issue = sub.add_parser("issue", help="Issue a new API token")
    p_issue.add_argument("owner", help="Owner label (e.g. 'alice')")
    p_issue.add_argument(
        "--budget",
        type=float,
        default=200.0,
        help="Monthly USD budget (default: 200.0). Pass 0 to block all calls; "
        "truly unlimited requires manual DynamoDB edit.",
    )
    p_issue.add_argument("--rps", type=int, help="Requests per second cap")
    p_issue.add_argument(
        "--monthly-requests", type=int, dest="monthly_requests", help="Monthly request quota"
    )
    p_issue.add_argument(
        "--max-input-tokens", type=int, dest="max_input_tokens", help="Per-request input token cap"
    )
    p_issue.add_argument(
        "--max-output-tokens",
        type=int,
        dest="max_output_tokens",
        help="Per-request output token cap",
    )
    p_issue.add_argument("--models", help="Comma-separated allowed model IDs")
    p_issue.add_argument("--note", help="Free-text label")
    p_issue.add_argument(
        "--admin",
        action="store_true",
        help="Grant admin rights (e.g. POST /admin/pricing/refresh)",
    )

    # revoke
    p_revoke = sub.add_parser("revoke", help="Revoke a token")
    p_revoke.add_argument(
        "token_id",
        type=token_id_arg,
        help="Token ID (bk_...); a full bearer token is accepted and split",
    )

    # list
    p_list = sub.add_parser("list", help="List tokens")
    p_list.add_argument(
        "--status",
        choices=["active", "revoked", "all"],
        default="active",
        help="Filter by status (default: active)",
    )
    p_list.add_argument("--owner", help="Filter by owner (uses owner-index GSI)")

    # show
    p_show = sub.add_parser("show", help="Show token details and current usage")
    p_show.add_argument(
        "token_id",
        type=token_id_arg,
        help="Token ID (bk_...); a full bearer token is accepted and split",
    )

    # set-limit
    p_set = sub.add_parser("set-limit", help="Update token limits")
    p_set.add_argument(
        "token_id",
        type=token_id_arg,
        help="Token ID (bk_...); a full bearer token is accepted and split",
    )
    p_set.add_argument("--budget", type=float, help="Monthly USD budget")
    p_set.add_argument("--rps", type=int, help="Requests per second cap")
    p_set.add_argument(
        "--monthly-requests", type=int, dest="monthly_requests", help="Monthly request quota"
    )
    p_set.add_argument(
        "--max-input-tokens", type=int, dest="max_input_tokens", help="Per-request input token cap"
    )
    p_set.add_argument(
        "--max-output-tokens",
        type=int,
        dest="max_output_tokens",
        help="Per-request output token cap",
    )
    p_set.add_argument(
        "--models",
        help="Comma-separated model IDs; pass '' (empty string) to remove restriction",
    )

    # usage
    p_usage = sub.add_parser("usage", help="Show usage counters for a token")
    p_usage.add_argument(
        "token_id",
        type=token_id_arg,
        help="Token ID (bk_...); a full bearer token is accepted and split",
    )
    p_usage.add_argument("--period", help="Billing period YYYY-MM (default: current month)")

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    dispatch = {
        "issue": cmd_issue,
        "revoke": cmd_revoke,
        "list": cmd_list,
        "show": cmd_show,
        "set-limit": cmd_set_limit,
        "usage": cmd_usage,
    }
    dispatch[args.subcommand](args)
