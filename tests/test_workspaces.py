async def test_create_and_list_workspaces(client, alice):
    r = await client.post("/api/v1/workspaces", headers=alice.headers, json={"name": "Fitness"})
    assert r.status_code == 201
    ws = r.json()
    assert ws["role"] == "owner" and ws["is_active"] is True
    r = await client.get("/api/v1/workspaces", headers=alice.headers)
    assert [w["name"] for w in r.json()] == ["Fitness"]


async def test_workspace_limit_enforced_by_plan(client, alice):
    for i in range(3):  # Pro = 3 (FR-WS-04)
        assert (
            await client.post("/api/v1/workspaces", headers=alice.headers, json={"name": f"W{i}"})
        ).status_code == 201
    r = await client.post("/api/v1/workspaces", headers=alice.headers, json={"name": "W4"})
    assert r.status_code == 403
    err = r.json()["error"]
    assert err["code"] == "plan_limit"
    assert err["details"] == {"limit": 3, "plan": "pro", "resource": "workspaces"}


async def test_deactivating_frees_a_slot_and_reactivating_is_limited(client, alice):
    ids = [
        (await client.post("/api/v1/workspaces", headers=alice.headers, json={"name": f"W{i}"})).json()["id"]
        for i in range(3)
    ]
    r = await client.patch(f"/api/v1/workspaces/{ids[0]}", headers=alice.headers, json={"is_active": False})
    assert r.json()["is_active"] is False
    assert (
        await client.post("/api/v1/workspaces", headers=alice.headers, json={"name": "W4"})
    ).status_code == 201
    r = await client.patch(f"/api/v1/workspaces/{ids[0]}", headers=alice.headers, json={"is_active": True})
    assert r.status_code == 403 and r.json()["error"]["code"] == "plan_limit"


async def test_tenant_isolation(client, alice, bob):
    ws_id = (await client.post("/api/v1/workspaces", headers=alice.headers, json={"name": "Private"})).json()[
        "id"
    ]
    # Bob can't read, rename or list Alice's workspace — and gets 404, not 403 (no existence leak).
    assert (await client.get(f"/api/v1/workspaces/{ws_id}", headers=bob.headers)).status_code == 404
    assert (
        await client.patch(f"/api/v1/workspaces/{ws_id}", headers=bob.headers, json={"name": "Hacked"})
    ).status_code == 404
    assert (await client.get("/api/v1/workspaces", headers=bob.headers)).json() == []
    assert (await client.get(f"/api/v1/workspaces/{ws_id}", headers=alice.headers)).json()[
        "name"
    ] == "Private"


async def test_rename_validation(client, alice):
    ws_id = (await client.post("/api/v1/workspaces", headers=alice.headers, json={"name": "A"})).json()["id"]
    assert (
        await client.patch(f"/api/v1/workspaces/{ws_id}", headers=alice.headers, json={"name": ""})
    ).status_code == 422
    r = await client.patch(f"/api/v1/workspaces/{ws_id}", headers=alice.headers, json={"name": "Renamed"})
    assert r.json()["name"] == "Renamed"
