"""Collections: public + free reads, admin-only curation, export, validation, audit."""

from sqlalchemy import update

from app.models import Profile


async def _admin(client, sessionmaker, user):
    await client.get("/api/v1/me", headers=user.headers)
    async with sessionmaker() as s:
        await s.execute(update(Profile).where(Profile.id == user.id).values(is_admin=True))
        await s.commit()
    return user.headers


VIDEO = {
    "platform": "tiktok",
    "url": "https://www.tiktok.com/@creator/video/123",
    "title": "Three reasons your hook fails",
    "creator_handle": "@creator",
    "views": 2_400_000,
    "outlier_score": 48.5,
    "notes": "Opens on the payoff, then rewinds.",
}


async def _make(client, h, slug="fast-push-in", published=True, category="visual_hooks", name="Fast Push-In"):
    r = await client.post(
        "/api/v1/admin/collections",
        headers=h,
        json={
            "slug": slug,
            "name": name,
            "category": category,
            "sub": "Camera",
            "description": "A sudden zoom on the subject in the first frame.",
            "breakdown": ["Zoom lands before 0.5s", "Payoff by 70%"],
            "is_published": published,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


async def test_public_reads_need_no_login_and_hide_drafts(client, sessionmaker, alice):
    h = await _admin(client, sessionmaker, alice)
    pub = await _make(client, h)
    await _make(client, h, slug="draft-one", published=False, name="Draft")
    await client.post(f"/api/v1/admin/collections/{pub['id']}/videos", headers=h, json=VIDEO)

    r = await client.get("/api/v1/collections")  # no Authorization header
    assert r.status_code == 200
    rows = r.json()
    assert [c["slug"] for c in rows] == ["fast-push-in"]
    assert rows[0]["video_count"] == 1

    d = (await client.get("/api/v1/collections/fast-push-in")).json()
    assert d["breakdown"] == ["Zoom lands before 0.5s", "Payoff by 70%"]
    assert d["videos"][0]["creator_handle"] == "creator"  # @ stripped
    assert (await client.get("/api/v1/collections/draft-one")).status_code == 404


async def test_filters_and_sort(client, sessionmaker, alice):
    h = await _admin(client, sessionmaker, alice)
    a = await _make(client, h, slug="a-format", category="formats", name="Alpha Format")
    await _make(client, h, slug="b-hook", category="visual_hooks", name="Beta Hook")
    for i in range(2):
        await client.post(
            f"/api/v1/admin/collections/{a['id']}/videos",
            headers=h,
            json={**VIDEO, "url": f"https://www.tiktok.com/@c/video/{i}"},
        )
    assert [c["slug"] for c in (await client.get("/api/v1/collections?category=formats")).json()] == [
        "a-format"
    ]
    assert [c["slug"] for c in (await client.get("/api/v1/collections?q=beta")).json()] == ["b-hook"]
    by_count = [c["slug"] for c in (await client.get("/api/v1/collections?sort=most_videos")).json()]
    assert by_count == ["a-format", "b-hook"]
    by_name = [c["slug"] for c in (await client.get("/api/v1/collections?sort=name")).json()]
    assert by_name == ["a-format", "b-hook"]
    assert (await client.get("/api/v1/collections?category=nope")).status_code == 422


async def test_export_markdown_and_json(client, sessionmaker, alice):
    h = await _admin(client, sessionmaker, alice)
    c = await _make(client, h)
    await client.post(f"/api/v1/admin/collections/{c['id']}/videos", headers=h, json=VIDEO)
    r = await client.get("/api/v1/collections/fast-push-in/export")
    assert r.status_code == 200
    assert 'filename="fast-push-in-reference.md"' in r.headers["content-disposition"]
    assert "# Fast Push-In" in r.text and "48.5x outlier" in r.text and "2,400,000 views" in r.text
    j = (await client.get("/api/v1/collections/fast-push-in/export?format=json")).json()
    assert j["category"] == "Visual Hooks" and j["examples"][0]["views"] == 2_400_000


async def test_only_admins_can_curate(client, alice, bob):
    await client.get("/api/v1/me", headers=bob.headers)
    r = await client.post(
        "/api/v1/admin/collections",
        headers=bob.headers,
        json={"slug": "x", "name": "Nope", "category": "formats"},
    )
    assert r.status_code == 403
    assert (await client.post("/api/v1/admin/collections", json={})).status_code == 401


async def test_validation_duplicates_and_audit(client, sessionmaker, alice):
    h = await _admin(client, sessionmaker, alice)
    c = await _make(client, h)
    # slug taken
    r = await client.post(
        "/api/v1/admin/collections",
        headers=h,
        json={"slug": "fast-push-in", "name": "Again", "category": "formats"},
    )
    assert r.status_code == 409 and r.json()["error"]["code"] == "slug_taken"
    # bad slug / url not matching platform / non-https
    assert (
        await client.post(
            "/api/v1/admin/collections",
            headers=h,
            json={"slug": "Bad Slug", "name": "X", "category": "formats"},
        )
    ).status_code == 422
    vid_url = f"/api/v1/admin/collections/{c['id']}/videos"
    bad = {**VIDEO, "url": "https://www.youtube.com/shorts/abc"}
    assert (await client.post(vid_url, headers=h, json=bad)).status_code == 422
    assert (
        await client.post(vid_url, headers=h, json={**VIDEO, "url": "http://www.tiktok.com/x"})
    ).status_code == 422
    # duplicate video
    assert (await client.post(vid_url, headers=h, json=VIDEO)).status_code == 201
    r = await client.post(vid_url, headers=h, json=VIDEO)
    assert r.status_code == 409 and r.json()["error"]["code"] == "duplicate_video"
    # positions auto-increment; remove works
    d = (
        await client.post(vid_url, headers=h, json={**VIDEO, "url": "https://www.tiktok.com/@c/video/9"})
    ).json()
    assert [v["position"] for v in d["videos"]] == [0, 1]
    d = (await client.delete(f"{vid_url}/{d['videos'][0]['id']}", headers=h)).json()
    assert len(d["videos"]) == 1
    # publish toggle + delete
    r = await client.patch(f"/api/v1/admin/collections/{c['id']}", headers=h, json={"is_published": False})
    assert r.json()["is_published"] is False
    assert (await client.get("/api/v1/collections/fast-push-in")).status_code == 404
    assert (await client.delete(f"/api/v1/admin/collections/{c['id']}", headers=h)).status_code == 204

    actions = [a["action"] for a in (await client.get("/api/v1/admin/audit", headers=h)).json()]
    for expected in (
        "collection.create",
        "collection.video_add",
        "collection.video_remove",
        "collection.update",
        "collection.delete",
    ):
        assert expected in actions
