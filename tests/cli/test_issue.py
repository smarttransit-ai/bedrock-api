"""Tests for the `issue` subcommand."""

import hashlib
import re


def test_issue_happy_path(run_cli, tables):
    """Issue a token with all limits; verify stdout has bearer token, DynamoDB row created."""
    stdout, stderr, code = run_cli(
        [
            "issue",
            "alice",
            "--budget",
            "10.50",
            "--rps",
            "5",
            "--monthly-requests",
            "1000",
            "--max-input-tokens",
            "4000",
            "--max-output-tokens",
            "2000",
            "--models",
            "us.anthropic.claude-sonnet-4-6",
            "--note",
            "grad student",
        ]
    )

    assert code == 0
    bearer = stdout.strip()
    assert re.fullmatch(r"bk_[0-9a-f]{32}\.[0-9a-f]{64}", bearer), f"Bad token: {bearer}"

    token_id, secret = bearer.split(".", 1)

    # Verify DynamoDB row
    tokens_table, _ = tables
    item = tokens_table.get_item(Key={"token_id": token_id})["Item"]
    assert item["owner"] == "alice"
    assert item["status"] == "active"
    assert item["limit_monthly_usd_micros"] == 10_500_000
    assert item["limit_rps"] == 5
    assert item["limit_monthly_requests"] == 1000
    assert item["limit_max_input_tokens"] == 4000
    assert item["limit_max_output_tokens"] == 2000
    assert item["allowed_models"] == {"us.anthropic.claude-sonnet-4-6"}
    assert item["note"] == "grad student"
    assert "secret_hash" in item
    assert "secret" not in item  # raw secret never stored

    # Verify hash matches what auth.py expects
    salt_hex, stored_hash = item["secret_hash"].split(":", 1)
    candidate = hashlib.sha256(bytes.fromhex(salt_hex) + bytes.fromhex(secret)).hexdigest()
    assert candidate == stored_hash

    # Verify metadata on stderr (not secret)
    assert "alice" in stderr
    assert token_id in stderr
    assert bearer not in stderr  # secret never on stderr


def test_issue_minimal(run_cli, tables):
    """Issue with only owner — gets the $200 default budget; other limits absent."""
    stdout, _, code = run_cli(["issue", "bob"])
    assert code == 0

    token_id = stdout.strip().split(".")[0]
    tokens_table, _ = tables
    item = tokens_table.get_item(Key={"token_id": token_id})["Item"]
    assert item["owner"] == "bob"
    assert item["status"] == "active"
    assert item["limit_monthly_usd_micros"] == 200_000_000  # default $200/month
    # All other limit attributes absent (= unlimited)
    for attr in [
        "limit_rps",
        "limit_monthly_requests",
        "limit_max_input_tokens",
        "limit_max_output_tokens",
        "allowed_models",
        "admin",
    ]:
        assert attr not in item, f"Unexpected attribute {attr}"


def test_issue_admin_flag(run_cli, tables):
    """--admin sets admin=True on the row; default issue omits it."""
    stdout, stderr, code = run_cli(["issue", "ops", "--admin"])
    assert code == 0
    token_id = stdout.strip().split(".")[0]
    tokens_table, _ = tables
    item = tokens_table.get_item(Key={"token_id": token_id})["Item"]
    assert item["admin"] is True
    assert "admin" in stderr


def test_issue_empty_models_not_written(run_cli, tables):
    """--models '' should not write allowed_models (never write empty SS)."""
    stdout, _, code = run_cli(["issue", "carol", "--models", ""])
    assert code == 0

    token_id = stdout.strip().split(".")[0]
    tokens_table, _ = tables
    item = tokens_table.get_item(Key={"token_id": token_id})["Item"]
    assert "allowed_models" not in item


def test_issue_models_stripped(run_cli, tables):
    """Whitespace around model IDs is stripped."""
    stdout, _, code = run_cli(["issue", "dave", "--models", " m1 , m2 "])
    assert code == 0

    token_id = stdout.strip().split(".")[0]
    tokens_table, _ = tables
    item = tokens_table.get_item(Key={"token_id": token_id})["Item"]
    assert item["allowed_models"] == {"m1", "m2"}


def test_issue_bearer_only_on_stdout(run_cli, tables):
    """Secret bearer token appears on stdout exactly once, never on stderr."""
    stdout, stderr, code = run_cli(["issue", "eve"])
    assert code == 0
    bearer = stdout.strip()
    assert bearer not in stderr
    assert stdout.count(bearer) == 1
