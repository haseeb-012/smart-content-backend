"""Request dependencies: DB session, authenticated profile, admin guard."""

from dataclasses import dataclass
from functools import lru_cache

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import admin_auth
from app.core.admin_auth import AdminActor
from app.core.config import get_settings
from app.core.errors import Forbidden, Unauthorized
from app.core.security import AuthUser, TokenVerifier, build_verifier
from app.db.session import get_db
from app.models import Profile
from app.modules.identity.service import get_or_create_profile


@lru_cache
def _verifier() -> TokenVerifier:
    return build_verifier(get_settings())


def get_verifier() -> TokenVerifier:
    """Overridden in tests with a verifier backed by a local test key."""
    return _verifier()


# Deliberately a sync `def`: FastAPI runs it in a worker thread, so the (occasional, cached)
# blocking JWKS fetch inside PyJWKClient never stalls the event loop. Don't make it `async`.
def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise Unauthorized("Missing bearer token.")
    return token


def get_auth_user(request: Request, verifier: TokenVerifier = Depends(get_verifier)) -> AuthUser:
    return verifier.verify(_bearer(request))


async def current_profile(
    user: AuthUser = Depends(get_auth_user), db: AsyncSession = Depends(get_db)
) -> Profile:
    return await get_or_create_profile(db, user)


def _admin_check(request: Request, verifier: TokenVerifier = Depends(get_verifier)) -> "_AdminCheck":
    token = _bearer(request)
    if claims := admin_auth.verify_token(get_settings(), token):
        return _AdminCheck(actor=AdminActor(id=None, email=claims.get("email"), source="panel"))
    return _AdminCheck(user=verifier.verify(token))


@dataclass(frozen=True)
class _AdminCheck:
    actor: AdminActor | None = None
    user: AuthUser | None = None


async def admin_actor(
    check: _AdminCheck = Depends(_admin_check), db: AsyncSession = Depends(get_db)
) -> AdminActor:
    """Admin guard for every /admin route: the panel login, or a Supabase user with is_admin."""
    if check.actor:
        return check.actor
    assert check.user is not None  # noqa: S101
    profile = await get_or_create_profile(db, check.user)
    if not profile.is_admin:
        raise Forbidden("Admin access required.")
    return AdminActor(id=profile.id, email=profile.email, source="supabase")
