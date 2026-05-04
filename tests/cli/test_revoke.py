"""Tests for the `revoke` subcommand."""

from tests.cli.conftest import make_active_token


def test_revoke_active_token(run_cli, tables):
    """Revoking an active token updates status and sets revoked_at."""
    tokens_table, _ = tables
    token_id, _ = make_active_token(tokens_table)

    _, stderr, code = run_cli(["revoke", token_id])
    assert code == 0
    assert "revoked" in stderr

    item = tokens_table.get_item(Key={"token_id": token_id})["Item"]
    assert item["status"] == "revoked"
    assert "revoked_at" in item


def test_revoke_already_revoked_is_idempotent(run_cli, tables):
    """Revoking an already-revoked token exits 0 (idempotent)."""
    tokens_table, _ = tables
    token_id, _ = make_active_token(tokens_table)
    # First revoke
    run_cli(["revoke", token_id])
    # Second revoke — should be idempotent
    _, stderr, code = run_cli(["revoke", token_id])
    assert code == 0
    assert "already revoked" in stderr


def test_revoke_nonexistent_exits_1(run_cli, tables):
    """Revoking a non-existent token exits 1 with an error."""
    _, _, code = run_cli(["revoke", "bk_" + "0" * 32])
    assert code == 1
