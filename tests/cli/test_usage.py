"""Tests for the `usage` subcommand."""

from decimal import Decimal

from tests.cli.conftest import make_active_token


def test_usage_with_data(run_cli, tables):
    """usage outputs correct counters and USD formatted to 4 decimal places."""
    tokens_table, usage_table = tables
    token_id, _ = make_active_token(tokens_table)

    usage_table.put_item(
        Item={
            "token_id": token_id,
            "period": "2026-01",
            "requests": Decimal("100"),
            "input_tokens": Decimal("5000"),
            "output_tokens": Decimal("2000"),
            "usd_micros": Decimal("15000"),
        }
    )

    _, stderr, code = run_cli(["usage", token_id, "--period", "2026-01"])
    assert code == 0
    assert "100" in stderr
    assert "5000" in stderr
    assert "2000" in stderr
    assert "0.0150" in stderr  # 15000 / 1_000_000


def test_usage_no_data_for_period(run_cli, tables):
    """usage with no row for the period outputs zeros, exits 0."""
    tokens_table, _ = tables
    token_id, _ = make_active_token(tokens_table)

    _, stderr, code = run_cli(["usage", token_id, "--period", "2026-01"])
    assert code == 0
    assert "0" in stderr
    assert "0.0000" in stderr


def test_usage_nonexistent_token_exits_0(run_cli, tables):
    """usage for a token that never existed exits 0 (queries usage table, not tokens).

    The usage subcommand queries the usage table directly by (token_id, period).
    If neither the token nor the usage item exists, it correctly returns zeros
    rather than an error, because the operator may query historical periods after
    a token has been cleaned up.
    """
    _, _, code = run_cli(["usage", "bk_" + "0" * 32])
    assert code == 0


def test_usage_default_period(run_cli, tables):
    """usage without --period uses the current UTC month."""
    from bedrock_api.tokens import current_period

    tokens_table, usage_table = tables
    token_id, _ = make_active_token(tokens_table)

    period = current_period()
    usage_table.put_item(
        Item={
            "token_id": token_id,
            "period": period,
            "requests": Decimal("7"),
            "input_tokens": Decimal("100"),
            "output_tokens": Decimal("50"),
            "usd_micros": Decimal("100"),
        }
    )

    _, stderr, code = run_cli(["usage", token_id])
    assert code == 0
    assert "7" in stderr
