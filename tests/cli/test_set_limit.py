"""Tests for the `set-limit` subcommand."""

from tests.cli.conftest import make_active_token


def test_set_limit_rps(run_cli, tables):
    """--rps updates limit_rps in DynamoDB."""
    tokens_table, _ = tables
    token_id, _ = make_active_token(tokens_table)

    _, _, code = run_cli(["set-limit", token_id, "--rps", "10"])
    assert code == 0

    item = tokens_table.get_item(Key={"token_id": token_id})["Item"]
    assert int(item["limit_rps"]) == 10


def test_set_limit_budget(run_cli, tables):
    """--budget stores integer µUSD correctly."""
    tokens_table, _ = tables
    token_id, _ = make_active_token(tokens_table)

    _, _, code = run_cli(["set-limit", token_id, "--budget", "25.50"])
    assert code == 0

    item = tokens_table.get_item(Key={"token_id": token_id})["Item"]
    assert int(item["limit_monthly_usd_micros"]) == 25_500_000


def test_set_limit_models(run_cli, tables):
    """--models updates allowed_models string set."""
    tokens_table, _ = tables
    token_id, _ = make_active_token(tokens_table)

    _, _, code = run_cli(["set-limit", token_id, "--models", "m1,m2"])
    assert code == 0

    item = tokens_table.get_item(Key={"token_id": token_id})["Item"]
    assert item["allowed_models"] == {"m1", "m2"}


def test_set_limit_models_empty_removes_attribute(run_cli, tables):
    """--models '' removes the allowed_models attribute (never write empty SS)."""
    tokens_table, _ = tables
    token_id, _ = make_active_token(tokens_table)
    # First set models, then clear
    run_cli(["set-limit", token_id, "--models", "m1"])
    _, _, code = run_cli(["set-limit", token_id, "--models", ""])
    assert code == 0

    item = tokens_table.get_item(Key={"token_id": token_id})["Item"]
    assert "allowed_models" not in item


def test_set_limit_multiple_flags(run_cli, tables):
    """Multiple flags in one invocation → single UpdateItem."""
    tokens_table, _ = tables
    token_id, _ = make_active_token(tokens_table)

    _, _, code = run_cli(
        [
            "set-limit",
            token_id,
            "--rps",
            "20",
            "--monthly-requests",
            "5000",
            "--max-input-tokens",
            "8000",
        ]
    )
    assert code == 0

    item = tokens_table.get_item(Key={"token_id": token_id})["Item"]
    assert int(item["limit_rps"]) == 20
    assert int(item["limit_monthly_requests"]) == 5000
    assert int(item["limit_max_input_tokens"]) == 8000


def test_set_limit_max_output_tokens(run_cli, tables):
    """--max-output-tokens updates limit_max_output_tokens."""
    tokens_table, _ = tables
    token_id, _ = make_active_token(tokens_table)

    _, _, code = run_cli(["set-limit", token_id, "--max-output-tokens", "2000"])
    assert code == 0

    item = tokens_table.get_item(Key={"token_id": token_id})["Item"]
    assert int(item["limit_max_output_tokens"]) == 2000


def test_set_limit_no_flags_exits_1(run_cli, tables):
    """set-limit with no flags exits 1 with helpful message."""
    tokens_table, _ = tables
    token_id, _ = make_active_token(tokens_table)

    _, stderr, code = run_cli(["set-limit", token_id])
    assert code == 1
    assert "at least one limit flag" in stderr


def test_set_limit_nonexistent_exits_1(run_cli, tables):
    """set-limit on unknown token exits 1."""
    _, _, code = run_cli(["set-limit", "bk_" + "0" * 32, "--rps", "5"])
    assert code == 1
