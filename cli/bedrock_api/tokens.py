"""Token generation and hashing primitives for the bedrock-api admin CLI.

All functions in this module must agree exactly with lambda/proxy/auth.py:verify_secret().
"""

import hashlib
import secrets
from datetime import UTC, datetime


def generate_token() -> tuple[str, str, str]:
    """Return (token_id, bearer_token, secret_hash).

    token_id     = "bk_" + secrets.token_hex(16)   # 35 chars, safe to log
    bearer_token = "<token_id>.<secret>"             # 100 chars, sent in Authorization header
    secret_hash  = "<32hex-salt>:<64hex-sha256>"     # 97 chars, stored in DynamoDB

    Hash algorithm mirrors lambda/proxy/auth.py:verify_secret().
    """
    token_id = f"bk_{secrets.token_hex(16)}"  # "bk_" + 32 hex = 35 chars
    secret = secrets.token_hex(32)  # 64 hex chars (32 bytes of entropy)
    salt_hex = secrets.token_hex(16)  # 32 hex chars (16 bytes)
    digest = hashlib.sha256(bytes.fromhex(salt_hex) + bytes.fromhex(secret)).hexdigest()
    secret_hash = f"{salt_hex}:{digest}"  # 32 + ":" + 64 = 97 chars
    bearer_token = f"{token_id}.{secret}"  # 35 + "." + 64 = 100 chars
    return token_id, bearer_token, secret_hash


def current_period() -> str:
    """Return the current UTC billing period as YYYY-MM.

    Mirrors the period computation in lambda/proxy/limits.py:write_usage().
    """
    return datetime.now(UTC).strftime("%Y-%m")
