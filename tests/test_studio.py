"""Create module: hooks (library, generator, watchlist), idea generator, scripts, projects, exports."""

import pytest

from app.api.v1.analysis import analyze_limiter
from app.api.v1.channels import get_session_factory, search_limiter
from app.api.v1.studio import free_ai_limiter, script_limiter
from app.models import HookTemplate, Profile
from app.modules.discovery_analysis.llm import get_llm_provider


@pytest.fixture
def st_app(app, sessionmaker):
    app.dependency_overrides[get_session_factory] = lambda: sessionmaker
    for lim in (search_limiter, analyze_limiter, free_ai_limiter, script_limiter):
        lim.reset()
    return app


async def _ws(client, user, name="WS"):
    return (await client.post("/api/v1/workspaces", headers=user.headers, json={"name": name})).json()["id"]


async def _used(client, user) -> int:
    return (await client.get("/api/v1/credits", headers=user.headers)).json()["used"]


# ------------------------------------------------------------------ hooks


async def test_hook_library_is_public_and_filterable(st_app, client, db):
    db.add_all(
        [
            HookTemplate(
                text="Stop doing cardio like this", category="contrarian", niche="Fitness", views=900
            ),
            HookTemplate(
                text="Why is nobody talking about this?", category="question", niche="Business", views=50
            ),
            HookTemplate(text="Hidden draft hook", category="question", niche="Business", is_published=False),
        ]
    )
    await db.commit()
    all_ = (await client.get("/api/v1/hooks")).json()
    assert [h["text"] for h in all_] == ["Stop doing cardio like this", "Why is nobody talking about this?"]
    assert len((await client.get("/api/v1/hooks?category=question")).json()) == 1
    assert len((await client.get("/api/v1/hooks?niche=Fitness&q=cardio")).json()) == 1


async def test_admin_hook_curation_requires_admin(st_app, client, alice, db):
    body = {"text": "The one mistake killing your reach", "category": "problem"}
    assert (await client.post("/api/v1/admin/hooks", headers=alice.headers, json=body)).status_code == 403
    await client.get("/api/v1/me", headers=alice.headers)
    p = await db.get(Profile, alice.id)
    p.is_admin = True
    await db.commit()
    r = await client.post("/api/v1/admin/hooks", headers=alice.headers, json=body)
    assert r.status_code == 201
    assert (
        await client.delete(f"/api/v1/admin/hooks/{r.json()['id']}", headers=alice.headers)
    ).status_code == 204


async def test_hook_generator_is_free_and_watchlist_dedupes(st_app, client, alice, bob):
    ws = await _ws(client, alice)
    r = await client.post(
        f"/api/v1/workspaces/{ws}/hooks/generate",
        headers=alice.headers,
        json={"topic": "meal prep", "category": "question", "count": 10},
    )
    assert r.status_code == 200
    hooks = r.json()
    assert len(hooks) == 10 and all(h["category"] == "question" for h in hooks)
    assert all("meal prep" in h["text"].lower() for h in hooks)
    assert await _used(client, alice) == 0

    body = {"text": hooks[0]["text"], "category": "question", "source": "generator"}
    r1 = await client.post(f"/api/v1/workspaces/{ws}/saved-hooks", headers=alice.headers, json=body)
    r2 = await client.post(f"/api/v1/workspaces/{ws}/saved-hooks", headers=alice.headers, json=body)
    assert r1.status_code == 201 and r2.status_code == 200 and r1.json()["id"] == r2.json()["id"]
    saved = (await client.get(f"/api/v1/workspaces/{ws}/saved-hooks", headers=alice.headers)).json()
    assert len(saved) == 1

    assert (await client.get(f"/api/v1/workspaces/{ws}/saved-hooks", headers=bob.headers)).status_code == 404
    hid = saved[0]["id"]
    bob_ws = await _ws(client, bob)
    assert (
        await client.delete(f"/api/v1/workspaces/{bob_ws}/saved-hooks/{hid}", headers=bob.headers)
    ).status_code == 404
    assert (
        await client.delete(f"/api/v1/workspaces/{ws}/saved-hooks/{hid}", headers=alice.headers)
    ).status_code == 204


async def test_free_ai_rate_limit(st_app, client, alice, monkeypatch):
    monkeypatch.setattr(free_ai_limiter, "limit", 2)
    ws = await _ws(client, alice)
    for expected in (200, 200, 429):
        r = await client.post(
            f"/api/v1/workspaces/{ws}/hooks/generate",
            headers=alice.headers,
            json={"topic": "golf", "count": 5},
        )
        assert r.status_code == expected


# ------------------------------------------------------------------ ideas


async def test_idea_generator_saves_generated_ideas_free(st_app, client, alice):
    ws = await _ws(client, alice)
    r = await client.post(
        f"/api/v1/workspaces/{ws}/ideas/generate",
        headers=alice.headers,
        json={"topic": "home workouts", "count": 3},
    )
    assert r.status_code == 201 and len(r.json()) == 3 and r.json()[0]["source"] == "generated"
    assert len((await client.get(f"/api/v1/workspaces/{ws}/ideas", headers=alice.headers)).json()) == 3
    assert await _used(client, alice) == 0


# ------------------------------------------------------------------ scripts


async def test_script_costs_one_credit_and_is_idempotent(st_app, client, alice):
    ws = await _ws(client, alice)
    h = {**alice.headers, "Idempotency-Key": "script-req-0001"}
    body = {
        "title": "Grow on TikTok",
        "topic": "How I grew to 10K followers",
        "hook": "I did this for 30 days",
    }
    r = await client.post(f"/api/v1/workspaces/{ws}/scripts", headers=h, json=body)
    assert r.status_code == 201
    s = r.json()
    assert s["blocks"]["hook"] == "I did this for 30 days" and all(s["blocks"].values())
    assert s["version_count"] == 1 and s["versions"][0]["note"] == "generated"
    again = await client.post(f"/api/v1/workspaces/{ws}/scripts", headers=h, json=body)
    assert again.status_code == 200 and again.json()["id"] == s["id"]
    assert await _used(client, alice) == 1


async def test_failed_script_generation_charges_nothing(st_app, client, alice):
    ws = await _ws(client, alice)
    r = await client.post(
        f"/api/v1/workspaces/{ws}/scripts",
        headers=alice.headers,
        json={"title": "x", "topic": "failscript please"},
    )
    assert r.status_code == 502 and r.json()["error"]["code"] == "provider_error"
    assert await _used(client, alice) == 0
    assert (await client.get(f"/api/v1/workspaces/{ws}/scripts", headers=alice.headers)).json() == []


async def test_script_edit_versions_expand_restore_export(st_app, client, alice):
    ws = await _ws(client, alice)
    s = (
        await client.post(
            f"/api/v1/workspaces/{ws}/scripts", headers=alice.headers, json={"title": "T", "topic": "golf"}
        )
    ).json()
    sid = s["id"]
    first_version = s["versions"][0]["id"]
    blocks = {**s["blocks"], "cta": "Comment GOLF for the drill"}
    r = await client.patch(
        f"/api/v1/workspaces/{ws}/scripts/{sid}", headers=alice.headers, json={"blocks": blocks}
    )
    assert r.json()["blocks"]["cta"] == "Comment GOLF for the drill" and r.json()["version_count"] == 2
    # No-op save doesn't add a version.
    r = await client.patch(
        f"/api/v1/workspaces/{ws}/scripts/{sid}", headers=alice.headers, json={"blocks": blocks}
    )
    assert r.json()["version_count"] == 2

    r = await client.post(f"/api/v1/workspaces/{ws}/scripts/{sid}/expand", headers=alice.headers)
    assert (
        r.status_code == 200
        and r.json()["expanded"] is True
        and len(r.json()["blocks"]["main"]) > len(blocks["main"])
    )
    assert await _used(client, alice) == 1  # expand is free

    r = await client.post(
        f"/api/v1/workspaces/{ws}/scripts/{sid}/versions/{first_version}/restore", headers=alice.headers
    )
    assert r.json()["blocks"]["cta"] == s["blocks"]["cta"] and r.json()["versions"][0]["note"] == "restore"

    md = await client.get(
        f"/api/v1/workspaces/{ws}/scripts/{sid}/export?format=markdown", headers=alice.headers
    )
    assert (
        md.status_code == 200
        and md.text.startswith("# T")
        and "attachment" in md.headers["content-disposition"]
    )
    js = await client.get(f"/api/v1/workspaces/{ws}/scripts/{sid}/export?format=json", headers=alice.headers)
    assert js.json()["title"] == "T"
    exports = (await client.get(f"/api/v1/workspaces/{ws}/exports", headers=alice.headers)).json()
    assert [e["format"] for e in exports] == ["Json", "Markdown"]

    lst = (await client.get(f"/api/v1/workspaces/{ws}/scripts", headers=alice.headers)).json()
    assert lst[0]["id"] == sid and lst[0]["version_count"] == 4
    assert (
        await client.delete(f"/api/v1/workspaces/{ws}/scripts/{sid}", headers=alice.headers)
    ).status_code == 204


async def test_version_history_is_capped(st_app, client, alice):
    ws = await _ws(client, alice)
    s = (
        await client.post(
            f"/api/v1/workspaces/{ws}/scripts", headers=alice.headers, json={"title": "T", "topic": "x"}
        )
    ).json()
    for i in range(25):
        await client.patch(
            f"/api/v1/workspaces/{ws}/scripts/{s['id']}", headers=alice.headers, json={"title": f"T{i}"}
        )
    full = (await client.get(f"/api/v1/workspaces/{ws}/scripts/{s['id']}", headers=alice.headers)).json()
    assert full["version_count"] == 20 and full["versions"][0]["title"] == "T24"


async def test_script_from_analyzed_video_uses_context(st_app, client, alice):
    ws = await _ws(client, alice)
    await client.post(
        f"/api/v1/workspaces/{ws}/channels",
        headers=alice.headers,
        json={"url": "https://www.tiktok.com/@growthgrace"},
    )
    vid = (await client.get(f"/api/v1/workspaces/{ws}/videos", headers=alice.headers)).json()["items"][0][
        "id"
    ]
    await client.post(f"/api/v1/workspaces/{ws}/videos/{vid}/analysis", headers=alice.headers)
    idea = (await client.get(f"/api/v1/workspaces/{ws}/ideas", headers=alice.headers)).json()[0]
    r = await client.post(
        f"/api/v1/workspaces/{ws}/scripts",
        headers=alice.headers,
        json={"title": idea["title"], "topic": idea["title"], "source_idea_id": idea["id"]},
    )
    assert r.status_code == 201 and "Inspired by" in r.json()["blocks"]["story"]
    assert r.json()["source_idea_id"] == idea["id"]
    assert await _used(client, alice) == 2  # analysis + script


async def test_scripts_are_workspace_private(st_app, client, alice, bob):
    ws = await _ws(client, alice)
    sid = (
        await client.post(
            f"/api/v1/workspaces/{ws}/scripts", headers=alice.headers, json={"title": "T", "topic": "x"}
        )
    ).json()["id"]
    bob_ws = await _ws(client, bob)
    for path in (f"/api/v1/workspaces/{ws}/scripts/{sid}", f"/api/v1/workspaces/{bob_ws}/scripts/{sid}"):
        assert (await client.get(path, headers=bob.headers)).status_code == 404


async def test_out_of_credits_blocks_script(st_app, client, alice, db):
    from app.models import CreditLedger

    ws = await _ws(client, alice)
    me = (await client.get("/api/v1/me", headers=alice.headers)).json()
    db.add(
        CreditLedger(
            user_id=alice.id,
            kind="adjustment",
            amount=-me["credits"]["remaining"],
            action="t",
            idempotency_key="drain",
            period_key=me["credits"]["period_start"],
        )
    )
    await db.commit()
    r = await client.post(
        f"/api/v1/workspaces/{ws}/scripts", headers=alice.headers, json={"title": "T", "topic": "x"}
    )
    assert r.status_code == 402


# ------------------------------------------------------------------ projects + exports


async def test_projects_group_and_move_items(st_app, client, alice, bob):
    ws = await _ws(client, alice)
    sid = (
        await client.post(
            f"/api/v1/workspaces/{ws}/scripts", headers=alice.headers, json={"title": "S1", "topic": "x"}
        )
    ).json()["id"]
    idea = (
        await client.post(
            f"/api/v1/workspaces/{ws}/ideas/generate",
            headers=alice.headers,
            json={"topic": "golf", "count": 1},
        )
    ).json()[0]
    p1 = (
        await client.post(f"/api/v1/workspaces/{ws}/projects", headers=alice.headers, json={"name": "Launch"})
    ).json()
    p2 = (
        await client.post(
            f"/api/v1/workspaces/{ws}/projects", headers=alice.headers, json={"name": "Client B"}
        )
    ).json()

    r = await client.post(
        f"/api/v1/workspaces/{ws}/projects/{p1['id']}/items",
        headers=alice.headers,
        json={"kind": "script", "ref_id": sid},
    )
    assert [(i["kind"], i["title"]) for i in r.json()["items"]] == [("script", "S1")]
    await client.post(
        f"/api/v1/workspaces/{ws}/projects/{p1['id']}/items",
        headers=alice.headers,
        json={"kind": "idea", "ref_id": idea["id"]},
    )
    # Moving the script to project 2 removes it from project 1.
    await client.post(
        f"/api/v1/workspaces/{ws}/projects/{p2['id']}/items",
        headers=alice.headers,
        json={"kind": "script", "ref_id": sid},
    )
    projects = {
        p["name"]: p
        for p in (await client.get(f"/api/v1/workspaces/{ws}/projects", headers=alice.headers)).json()
    }
    assert [i["kind"] for i in projects["Launch"]["items"]] == ["idea"]
    assert [i["kind"] for i in projects["Client B"]["items"]] == ["script"]

    # Someone else's item can't be added; unknown video → 404.
    bob_ws = await _ws(client, bob)
    bob_sid = (
        await client.post(
            f"/api/v1/workspaces/{bob_ws}/scripts", headers=bob.headers, json={"title": "B", "topic": "x"}
        )
    ).json()["id"]
    for item in ({"kind": "script", "ref_id": bob_sid}, {"kind": "video", "ref_id": bob_sid}):
        r = await client.post(
            f"/api/v1/workspaces/{ws}/projects/{p1['id']}/items", headers=alice.headers, json=item
        )
        assert r.status_code == 404
    assert (
        await client.get(f"/api/v1/workspaces/{ws}/projects/{p1['id']}", headers=bob.headers)
    ).status_code == 404

    r = await client.delete(
        f"/api/v1/workspaces/{ws}/projects/{p1['id']}/items/idea/{idea['id']}", headers=alice.headers
    )
    assert r.json()["items"] == []
    r = await client.patch(
        f"/api/v1/workspaces/{ws}/projects/{p2['id']}", headers=alice.headers, json={"name": "Client B (Q4)"}
    )
    assert r.json()["name"] == "Client B (Q4)"
    # Deleting a project keeps its contents in the workspace.
    assert (
        await client.delete(f"/api/v1/workspaces/{ws}/projects/{p2['id']}", headers=alice.headers)
    ).status_code == 204
    assert (
        await client.get(f"/api/v1/workspaces/{ws}/scripts/{sid}", headers=alice.headers)
    ).status_code == 200


async def test_deleted_items_disappear_from_projects(st_app, client, alice):
    ws = await _ws(client, alice)
    sid = (
        await client.post(
            f"/api/v1/workspaces/{ws}/scripts", headers=alice.headers, json={"title": "S", "topic": "x"}
        )
    ).json()["id"]
    pid = (
        await client.post(f"/api/v1/workspaces/{ws}/projects", headers=alice.headers, json={"name": "P"})
    ).json()["id"]
    await client.post(
        f"/api/v1/workspaces/{ws}/projects/{pid}/items",
        headers=alice.headers,
        json={"kind": "script", "ref_id": sid},
    )
    await client.delete(f"/api/v1/workspaces/{ws}/scripts/{sid}", headers=alice.headers)
    assert (await client.get(f"/api/v1/workspaces/{ws}/projects/{pid}", headers=alice.headers)).json()[
        "items"
    ] == []


async def test_export_log(st_app, client, alice, bob):
    ws = await _ws(client, alice)
    r = await client.post(
        f"/api/v1/workspaces/{ws}/exports",
        headers=alice.headers,
        json={"kind": "Videos", "title": "Feed", "format": "CSV"},
    )
    assert r.status_code == 201
    assert len((await client.get(f"/api/v1/workspaces/{ws}/exports", headers=alice.headers)).json()) == 1
    assert (await client.get(f"/api/v1/workspaces/{ws}/exports", headers=bob.headers)).status_code == 404


def test_llm_override_hook_exists():
    assert callable(get_llm_provider)
