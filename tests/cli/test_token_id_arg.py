"""Tests for bearer-token normalisation on the token_id positional.

A bearer is ``<token_id>.<secret>``. Passing the whole thing is an easy mistake — it is the
value that sits in a caller's .env and it shares the ``bk_`` prefix with the id. Every
subcommand echoes its token_id back in error and confirmation messages, so an unsplit bearer
would land in scrollback, CI logs and shell history as a working credential.
"""

from bedrock_api.cli import token_id_arg
from tests.cli.conftest import make_active_token

SECRET = "4b97db5aa91cf4577458408189d34bfd182d1289cfc6ebaccd27807ccec9a221"


def test_plain_token_id_passes_through():
    assert token_id_arg("bk_9640fae28ddacd7f7bd8c3b6f651beef") == "bk_9640fae28ddacd7f7bd8c3b6f651beef"


def test_full_bearer_is_split_to_the_id(capsys):
    assert token_id_arg(f"bk_abc123.{SECRET}") == "bk_abc123"
    err = capsys.readouterr().err
    assert SECRET not in err, "the secret half must never be echoed"
    assert "bk_abc123" in err


def test_unknown_bearer_error_does_not_leak_the_secret(run_cli, tables):
    """The regression this guards: `set-limit <full-bearer>` on an unknown id printed the
    whole credential back in 'token_id not found: ...'."""
    stdout, stderr, code = run_cli(["set-limit", f"bk_doesnotexist.{SECRET}", "--budget", "50"])

    assert code != 0
    assert SECRET not in stdout
    assert SECRET not in stderr
    assert "bk_doesnotexist" in stderr


def test_commands_accept_a_bearer_and_act_on_the_id(run_cli, tables):
    """Splitting is not merely cosmetic — the command must still find the real token."""
    tokens_table, _ = tables
    token_id, _ = make_active_token(tokens_table, owner="alice")

    stdout, stderr, code = run_cli(["set-limit", f"{token_id}.{SECRET}", "--budget", "50"])

    assert code == 0, stderr
    assert SECRET not in stdout and SECRET not in stderr
    item = tokens_table.get_item(Key={"token_id": token_id})["Item"]
    assert item["limit_monthly_usd_micros"] == 50_000_000
