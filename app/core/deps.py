"""Request dependencies: DB session, authenticated profile, admin guard."""

from functools import lru_cache

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

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
def get_auth_user(request: Request, verifier: TokenVerifier = Depends(get_verifier)) -> AuthUser:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise Unauthorized("Missing bearer token.")
    return verifier.verify(token)


async def current_profile(
    user: AuthUser = Depends(get_auth_user), db: AsyncSession = Depends(get_db)
) -> Profile:
    return await get_or_create_profile(db, user)


async def admin_profile(profile: Profile = Depends(current_profile)) -> Profile:
    if not profile.is_admin:
        raise Forbidden("Admin access required.")
    return profile
