"""Admin panel login defined by env vars (ADMIN_EMAIL / ADMIN_PASSWORD_HASH / ADMIN_SESSION_SECRET).

- The password is never stored in plain text: PBKDF2-SHA256, 600k iterations, random salt.
- A successful login returns a short-lived HS256 token with its own audience, so it can never be
  mistaken for a Supabase token (and vice versa).
"""

import base64
import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import jwt

from app.core.config import Settings


@dataclass(frozen=True)
class AdminActor:
    """Who is acting in the admin panel. `id` is None for the env-defined admin (no Supabase account);
    audit rows then have no actor_id and show as "Admin (panel login)"."""

    id: uuid.UUID | None
    email: str | None
    source: Literal["panel", "supabase"]


ALG = "HS256"
AUDIENCE = "smart-content-admin"
ISSUER = "smart-content-backend"
ITERATIONS = 600_000


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def hash_password(password: str, *, iterations: int = ITERATIONS) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${_b64(salt)}${_b64(dk)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algo, iters, salt, digest = encoded.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), _unb64(salt), int(iters))
        return hmac.compare_digest(dk, _unb64(digest))
    except (ValueError, TypeError):
        return False


def check_login(settings: Settings, email: str, password: str) -> bool:
    if not settings.admin_login_enabled:
        return False
    email_ok = hmac.compare_digest(
        email.strip().lower().encode(), settings.admin_email.strip().lower().encode()
    )
    pw_ok = verify_password(password, settings.admin_password_hash)  # always run: same timing either way
    return email_ok and pw_ok


def issue_token(settings: Settings) -> tuple[str, datetime]:
    now = datetime.now(UTC)
    exp = now + timedelta(hours=settings.admin_session_hours)
    claims = {
        "sub": "env-admin",
        "email": settings.admin_email.strip().lower(),
        "aud": AUDIENCE,
        "iss": ISSUER,
        "iat": now,
        "exp": exp,
    }
    return jwt.encode(claims, settings.admin_session_secret, algorithm=ALG), exp


def verify_token(settings: Settings, token: str) -> dict[str, Any] | None:
    """Claims if `token` is a valid admin session for the *current* admin email, else None."""
    if not settings.admin_login_enabled or not token:
        return None
    try:
        claims = jwt.decode(
            token, settings.admin_session_secret, algorithms=[ALG], audience=AUDIENCE, issuer=ISSUER
        )
    except jwt.PyJWTError:
        return None
    # Changing ADMIN_EMAIL (or the secret) instantly signs out old sessions.
    if claims.get("email") != settings.admin_email.strip().lower():
        return None
    return claims
