"""Tests for the `show` subcommand."""

from decimal import Decimal

from bedrock_api.tokens import current_period

from tests.cli.conftest import make_active_token


def test_show_token_with_usage(run_cli, tables):
    """Show outputs metadata and current-period usage; never outputs secret_hash."""
    tokens_table, usage_table = tables
    token_id, _ = make_active_token(tokens_table, owner="alice")

    # Insert usage data for the current period (cmd_show queries current_period())
    period = current_period()
    usage_table.put_item(
        Item={
            "token_id": token_id,
            "period": period,
            "requests": Decimal("42"),
            "input_tokens": Decimal("1000"),
            "output_tokens": Decimal("500"),
            "usd_micros": Decimal("3000"),
        }
    )

    _, stderr, code = run_cli(["show", token_id])
    assert code == 0
    assert token_id in stderr
    assert "alice" in stderr
    assert "active" in stderr
    # Verify usage figures appear
    assert "42" in stderr
    assert "1000" in stderr
    assert "500" in stderr
    assert "0.0030" in stderr  # 3000 / 1_000_000
    # secret_hash must never appear
    assert "secret_hash" not in stderr


def test_show_token_no_usage(run_cli, tables):
    """Show with no usage row still outputs token metadata with zero usage."""
    tokens_table, _ = tables
    token_id, _ = make_active_token(tokens_table, owner="bob")

    _, stderr, code = run_cli(["show", token_id])
    assert code == 0
    assert token_id in stderr
    assert "no usage this period" in stderr


def test_show_nonexistent_exits_1(run_cli, tables):
    """Show for a non-existent token exits 1."""
    _, _, code = run_cli(["show", "bk_" + "0" * 32])
    assert code == 1
