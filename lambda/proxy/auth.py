import hashlib
import hmac


class AuthError(Exception):
    def __init__(self, code: str, message: str, status: int = 401) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def parse_bearer_token(event: dict) -> tuple[str, str]:
    """Extract (token_id, secret) from the Authorization header.

    API Gateway HTTP API v2 sends headers in lowercase.
    Token format: bk_<32hex>.<64hex>
    """
    headers = event.get("headers") or {}
    auth = headers.get("authorization") or headers.get("Authorization") or ""
    if not auth.startswith("Bearer "):
        raise AuthError("INVALID_TOKEN", "Missing or malformed Authorization header")
    token = auth[7:].strip()
    if "." not in token:
        raise AuthError("INVALID_TOKEN", "Invalid token format")
    token_id, secret = token.split(".", 1)
    if not token_id.startswith("bk_"):
        raise AuthError("INVALID_TOKEN", "Invalid token format")
    return token_id, secret


def parse_bearer_token_from_request(request) -> tuple[str, str]:
    """Adapter for FastAPI Request → calls parse_bearer_token with a minimal event dict."""
    return parse_bearer_token({"headers": dict(request.headers)})


def verify_secret(secret: str, secret_hash: str) -> bool:
    """Constant-time SHA-256 verification against the stored salt:hash value.

    secret_hash format: <32hex-salt>:<64hex-sha256>
    Uses hmac.compare_digest to prevent timing attacks.
    """
    parts = secret_hash.split(":", 1)
    if len(parts) != 2:
        return False
    salt_hex, stored_hash = parts
    try:
        candidate = hashlib.sha256(bytes.fromhex(salt_hex) + bytes.fromhex(secret)).hexdigest()
    except ValueError:
        return False
    return hmac.compare_digest(candidate, stored_hash)
