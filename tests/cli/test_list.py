"""Tests for the `list` subcommand."""

from datetime import UTC

from tests.cli.conftest import make_active_token


def _revoke(tokens_table, token_id):
    from datetime import datetime

    tokens_table.update_item(
        Key={"token_id": token_id},
        UpdateExpression="SET #s = :revoked, revoked_at = :now",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":revoked": "revoked",
            ":now": datetime.now(UTC).isoformat(),
        },
    )


def test_list_default_shows_active_only(run_cli, tables):
    """Default list (--status active) returns only active tokens."""
    tokens_table, _ = tables
    active_id, _ = make_active_token(tokens_table, owner="alice")
    revoked_id, _ = make_active_token(tokens_table, owner="bob")
    _revoke(tokens_table, revoked_id)

    _, stderr, code = run_cli(["list"])
    assert code == 0
    assert active_id[:16] in stderr
    assert revoked_id[:16] not in stderr


def test_list_status_revoked(run_cli, tables):
    """--status revoked returns only revoked tokens."""
    tokens_table, _ = tables
    active_id, _ = make_active_token(tokens_table, owner="alice")
    revoked_id, _ = make_active_token(tokens_table, owner="bob")
    _revoke(tokens_table, revoked_id)

    _, stderr, code = run_cli(["list", "--status", "revoked"])
    assert code == 0
    assert revoked_id[:16] in stderr
    assert active_id[:16] not in stderr


def test_list_status_all(run_cli, tables):
    """--status all returns both active and revoked tokens."""
    tokens_table, _ = tables
    active_id, _ = make_active_token(tokens_table, owner="alice")
    revoked_id, _ = make_active_token(tokens_table, owner="bob")
    _revoke(tokens_table, revoked_id)

    _, stderr, code = run_cli(["list", "--status", "all"])
    assert code == 0
    assert active_id[:16] in stderr
    assert revoked_id[:16] in stderr


def test_list_owner_filter(run_cli, tables):
    """--owner filters to only that owner's tokens via GSI."""
    tokens_table, _ = tables
    alice_id, _ = make_active_token(tokens_table, owner="alice")
    bob_id, _ = make_active_token(tokens_table, owner="bob")

    _, stderr, code = run_cli(["list", "--owner", "alice"])
    assert code == 0
    assert alice_id[:16] in stderr
    assert bob_id[:16] not in stderr


def test_list_shows_usage_data(run_cli, tables):
    """list output includes current-period usage (requests and USD)."""
    from decimal import Decimal

    from bedrock_api.tokens import current_period

    tokens_table, usage_table = tables
    token_id, _ = make_active_token(tokens_table, owner="alice")

    period = current_period()
    usage_table.put_item(
        Item={
            "token_id": token_id,
            "period": period,
            "requests": Decimal("17"),
            "usd_micros": Decimal("5000"),
        }
    )

    _, stderr, code = run_cli(["list"])
    assert code == 0
    assert "17" in stderr
    assert "0.0050" in stderr  # 5000 / 1_000_000


def test_list_empty_table(run_cli, tables):
    """List on empty table exits 0 with no token rows."""
    _, stderr, code = run_cli(["list"])
    assert code == 0
    # Header should still print
    assert "TOKEN_ID" in stderr
