"""Supabase access-token verification.

The backend never trusts the browser: every protected request carries
`Authorization: Bearer <supabase access token>`, which we verify against the
project's JWKS (asymmetric signing keys), checking signature, expiry, audience
and issuer.
"""

from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

import jwt
from jwt import PyJWKClient

from app.core.config import Settings
from app.core.errors import Unauthorized

ALLOWED_ALGS = ["ES256", "RS256", "EdDSA"]


@dataclass(frozen=True)
class AuthUser:
    id: UUID
    email: str | None
    claims: dict[str, Any]


class KeyResolver(Protocol):
    def key_for(self, token: str) -> Any: ...


class JwksKeyResolver:
    """Fetches and caches the Supabase JWKS; resolves the signing key by `kid`."""

    def __init__(self, jwks_url: str):
        self._client = PyJWKClient(jwks_url, cache_keys=True, lifespan=600)

    def key_for(self, token: str) -> Any:
        return self._client.get_signing_key_from_jwt(token).key


class TokenVerifier:
    def __init__(self, resolver: KeyResolver, *, audience: str, issuer: str | None):
        self.resolver = resolver
        self.audience = audience
        self.issuer = issuer

    def verify(self, token: str) -> AuthUser:
        try:
            key = self.resolver.key_for(token)
            claims = jwt.decode(
                token,
                key=key,
                algorithms=ALLOWED_ALGS,
                audience=self.audience,
                issuer=self.issuer or None,
                options={"require": ["exp", "sub"]},
            )
        except jwt.ExpiredSignatureError as e:
            raise Unauthorized("Session expired. Please sign in again.", code="token_expired") from e
        except (jwt.PyJWTError, ValueError) as e:
            raise Unauthorized("Invalid access token.") from e
        try:
            user_id = UUID(str(claims["sub"]))
        except ValueError as e:
            raise Unauthorized("Invalid token subject.") from e
        return AuthUser(id=user_id, email=claims.get("email"), claims=claims)


def build_verifier(settings: Settings) -> TokenVerifier:
    if not settings.supabase_url:
        raise Unauthorized(
            "Authentication is not configured (SUPABASE_URL missing).", code="auth_not_configured"
        )
    return TokenVerifier(
        JwksKeyResolver(settings.jwks_url),
        audience=settings.supabase_jwt_audience,
        issuer=settings.jwt_issuer,
    )
