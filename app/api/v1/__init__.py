from fastapi import APIRouter

from app.api.v1 import admin, billing, credits, me, plans, waitlist, workspaces

router = APIRouter(prefix="/api/v1")
router.include_router(billing.router)
router.include_router(me.router)
router.include_router(plans.router)
router.include_router(workspaces.router)
router.include_router(credits.router)
router.include_router(waitlist.router)
router.include_router(admin.router)
