from fastapi import APIRouter

from app.api.v1 import (
    admin,
    analysis,
    billing,
    channels,
    collections,
    credits,
    me,
    plans,
    social,
    studio,
    thumbnails,
    waitlist,
    workspaces,
)

router = APIRouter(prefix="/api/v1")
router.include_router(channels.router)
router.include_router(analysis.router)
router.include_router(studio.router)
router.include_router(studio.admin_router)
router.include_router(collections.router)
router.include_router(collections.admin_router)
router.include_router(billing.router)
router.include_router(social.router)
router.include_router(me.router)
router.include_router(plans.router)
router.include_router(workspaces.router)
router.include_router(credits.router)
router.include_router(waitlist.router)
router.include_router(admin.router)
router.include_router(thumbnails.router)
