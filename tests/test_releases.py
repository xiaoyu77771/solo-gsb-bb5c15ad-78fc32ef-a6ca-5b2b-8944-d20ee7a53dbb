"""多开关发布批次 API 测试（POST /api/v1/releases）。"""


def _make_flag(client, flag_key, *, weights=(50, 50), rule_region=None, description=""):
    """创建 red/blue 两变体开关；rule_region 提供时加 region eq -> red 规则。"""
    rules = []
    if rule_region is not None:
        rules.append(
            {
                "priority": 1,
                "attribute": "region",
                "operator": "eq",
                "values": [rule_region],
                "variant_key": "red",
            }
        )
    payload = {
        "flag_key": flag_key,
        "description": description,
        "variants": [
            {"key": "red", "weight": weights[0]},
            {"key": "blue", "weight": weights[1]},
        ],
        "rules": rules,
    }
    resp = client.post("/api/v1/flags", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _candidate(flag_key, expected_version, *, weights=(0, 100), description="批次候选"):
    """blue 全量候选：无规则，red 权重 0 / blue 权重 100。"""
    return {
        "flag_key": flag_key,
        "expected_version": expected_version,
        "description": description,
        "variants": [
            {"key": "red", "weight": weights[0]},
            {"key": "blue", "weight": weights[1]},
        ],
        "rules": [],
    }


# ---------------------------------------------------------------------------
# 创建批次
# ---------------------------------------------------------------------------


def test_create_release_builds_pending_batch_without_touching_config(client):
    _make_flag(client, "flag_a")
    before = client.get("/api/v1/flags/flag_a").json()
    versions_before = client.get("/api/v1/flags/flag_a/versions").json()

    resp = client.post(
        "/api/v1/releases",
        json={
            "flags": [_candidate("flag_a", 1)],
            "users": [{"user_id": "u3", "context": {}}],
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "pending"
    assert body["total_flags"] == 1
    assert body["total_users"] == 1
    assert body["committed_at"] is None
    item = body["items"][0]
    assert item["flag_key"] == "flag_a"
    assert item["expected_version"] == 1
    assert item["current_version_at_create"] == 1
    assert item["committed_version"] is None
    assert item["total_users"] == 1
    # u3 在 flag_a red/blue 50/50 下桶值为 4 -> red；候选 blue 全量 -> blue
    assert item["changed_users"] == 1
    assert item["changes"][0]["user_id"] == "u3"
    assert item["changes"][0]["current"]["variant_key"] == "red"
    assert item["changes"][0]["candidate"]["variant_key"] == "blue"
    assert item["migration_counts"] == {"red": {"blue": 1}}

    # 当前配置与版本历史均未被改动
    after = client.get("/api/v1/flags/flag_a").json()
    assert after == before
    assert client.get("/api/v1/flags/flag_a/versions").json() == versions_before

    # GET 返回同一批次状态
    got = client.get(f"/api/v1/releases/{body['release_id']}").json()
    assert got == body


def test_create_release_multi_flag_previews_each(client):
    _make_flag(client, "flag_a")
    _make_flag(client, "flag_b")
    resp = client.post(
        "/api/v1/releases",
        json={
            "flags": [_candidate("flag_a", 1), _candidate("flag_b", 1)],
            "users": [
                {"user_id": "u2", "context": {}},   # 两开关桶值均 <50 -> red
                {"user_id": "u3", "context": {}},   # flag_a red / flag_b blue
            ],
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["total_flags"] == 2
    assert [i["flag_key"] for i in body["items"]] == ["flag_a", "flag_b"]
    # flag_a: u2/u3 都 red->blue；flag_b: 仅 u2 red->blue
    expected = {"flag_a": ["u2", "u3"], "flag_b": ["u2"]}
    for item in body["items"]:
        assert item["total_users"] == 2
        changed = expected[item["flag_key"]]
        assert item["changed_users"] == len(changed)
        assert [c["user_id"] for c in item["changes"]] == changed
        assert item["migration_counts"] == {"red": {"blue": len(changed)}}


def test_create_release_unknown_flag_404_and_no_batch(client):
    _make_flag(client, "flag_a")
    resp = client.post(
        "/api/v1/releases",
        json={
            "flags": [_candidate("flag_a", 1), _candidate("ghost", 1)],
            "users": [{"user_id": "u1", "context": {}}],
        },
    )
    assert resp.status_code == 404
    assert "ghost" in resp.json()["detail"]
    # 任何配置都未发生变化
    assert client.get("/api/v1/flags/flag_a").json()["version"] == 1


def test_create_release_stale_expected_version_409(client):
    _make_flag(client, "flag_a")
    # 推进到 v2
    client.post("/api/v1/flags", json=_candidate("flag_a", 1))
    assert client.get("/api/v1/flags/flag_a").json()["version"] == 2

    resp = client.post(
        "/api/v1/releases",
        json={
            "flags": [_candidate("flag_a", 1)],
            "users": [{"user_id": "u1", "context": {}}],
        },
    )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "flag_a" in detail and "2" in detail and "1" in detail
    # 失败不创建任何批次
    assert client.get("/api/v1/flags/flag_a").json()["version"] == 2


def test_create_release_reuses_candidate_validation(client):
    _make_flag(client, "flag_a")
    base = {
        "flags": [_candidate("flag_a", 1)],
        "users": [{"user_id": "u1", "context": {}}],
    }

    # 权重和 != 100
    bad = {"flag_key": "flag_a", "expected_version": 1,
           "variants": [{"key": "red", "weight": 30}, {"key": "blue", "weight": 30}],
           "rules": []}
    payload = {"flags": [bad], "users": base["users"]}
    assert client.post("/api/v1/releases", json=payload).status_code == 422

    # 规则引用不存在的变体
    bad = {
        "flag_key": "flag_a",
        "expected_version": 1,
        "variants": [{"key": "red", "weight": 50}, {"key": "blue", "weight": 50}],
        "rules": [
            {"priority": 1, "attribute": "x", "operator": "eq", "values": [1], "variant_key": "ghost"}
        ],
    }
    assert client.post(
        "/api/v1/releases", json={"flags": [bad], "users": base["users"]}
    ).status_code == 422


def test_create_release_validates_batch_shape(client):
    _make_flag(client, "flag_a")

    # 空开关列表
    assert client.post(
        "/api/v1/releases",
        json={"flags": [], "users": [{"user_id": "u1", "context": {}}]},
    ).status_code == 422

    # 批次内 flag_key 重复
    assert client.post(
        "/api/v1/releases",
        json={
            "flags": [_candidate("flag_a", 1), _candidate("flag_a", 1)],
            "users": [{"user_id": "u1", "context": {}}],
        },
    ).status_code == 422

    # 空用户 / 重复用户
    assert client.post(
        "/api/v1/releases",
        json={"flags": [_candidate("flag_a", 1)], "users": []},
    ).status_code == 422
    assert client.post(
        "/api/v1/releases",
        json={
            "flags": [_candidate("flag_a", 1)],
            "users": [
                {"user_id": "u1", "context": {}},
                {"user_id": "u1", "context": {"region": "cn"}},
            ],
        },
    ).status_code == 422

    # expected_version 必须 >= 1
    bad = dict(_candidate("flag_a", 0))
    assert client.post(
        "/api/v1/releases",
        json={"flags": [bad], "users": [{"user_id": "u1", "context": {}}]},
    ).status_code == 422


def test_create_release_activation_window_candidate(client):
    # 候选带“尚未开始”的生效窗口：u3 当前 red(桶)，候选窗口外 -> 兜底 blue
    _make_flag(client, "flag_a")
    candidate = _candidate("flag_a", 1, weights=(50, 50))
    candidate["activation_window"] = {
        "active_from": "2030-01-01T00:00:00Z",
        "inactive_variant_key": "blue",
    }
    resp = client.post(
        "/api/v1/releases",
        json={
            "flags": [candidate],
            "users": [{"user_id": "u3", "context": {}}],
        },
    )
    assert resp.status_code == 201, resp.text
    change = resp.json()["items"][0]["changes"][0]
    assert change["current"]["variant_key"] == "red"
    assert change["current"]["decision_path"] == "bucket"
    assert change["candidate"]["variant_key"] == "blue"
    assert change["candidate"]["decision_path"] == "inactive"


# ---------------------------------------------------------------------------
# 幂等创建
# ---------------------------------------------------------------------------


def _release_payload(key=None):
    payload = {
        "flags": [_candidate("flag_a", 1)],
        "users": [{"user_id": "u1", "context": {}}],
    }
    if key is not None:
        payload["idempotency_key"] = key
    return payload


def test_create_release_idempotent_key_replays_first_result(client):
    _make_flag(client, "flag_a")
    first = client.post("/api/v1/releases", json=_release_payload("batch-1"))
    assert first.status_code == 201

    second = client.post("/api/v1/releases", json=_release_payload("batch-1"))
    assert second.status_code == 200
    assert second.json() == first.json()

    # 批次仍只有一个
    got = client.get(f"/api/v1/releases/{first.json()['release_id']}").json()
    assert got["status"] == "pending"


def test_create_release_same_key_different_body_409(client):
    _make_flag(client, "flag_a")
    assert client.post(
        "/api/v1/releases", json=_release_payload("batch-1")
    ).status_code == 201

    other = _release_payload("batch-1")
    other["users"] = [{"user_id": "u99", "context": {}}]
    resp = client.post("/api/v1/releases", json=other)
    assert resp.status_code == 409
    assert "batch-1" in resp.json()["detail"]


def test_create_release_without_key_makes_separate_batches(client):
    _make_flag(client, "flag_a")
    first = client.post("/api/v1/releases", json=_release_payload())
    second = client.post("/api/v1/releases", json=_release_payload())
    assert first.status_code == second.status_code == 201
    assert first.json()["release_id"] != second.json()["release_id"]


# ---------------------------------------------------------------------------
# 提交批次
# ---------------------------------------------------------------------------


def test_commit_release_appends_versions_for_all_flags_atomically(client):
    _make_flag(client, "flag_a")
    _make_flag(client, "flag_b")
    created = client.post(
        "/api/v1/releases",
        json={
            "flags": [_candidate("flag_a", 1), _candidate("flag_b", 1)],
            "users": [{"user_id": "u2", "context": {}}],
        },
    ).json()
    release_id = created["release_id"]

    resp = client.post(f"/api/v1/releases/{release_id}/commit")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "committed"
    assert body["committed_at"] is not None
    assert [i["committed_version"] for i in body["items"]] == [2, 2]
    # 预览摘要在提交后依然可查
    assert body["items"][0]["changes"][0]["candidate"]["variant_key"] == "blue"

    # 各开关当前配置已是候选内容（blue 全量），版本为 2
    for key in ("flag_a", "flag_b"):
        current = client.get(f"/api/v1/flags/{key}").json()
        assert current["version"] == 2
        assert {v["key"]: v["weight"] for v in current["variants"]} == {
            "red": 0,
            "blue": 100,
        }
        versions = [v["version"] for v in client.get(f"/api/v1/flags/{key}/versions").json()]
        assert versions == [1, 2]

    # 缺省判定立即按新版本执行：u2 在两开关上都不再落入 red
    decision = client.post(
        "/api/v1/flags/flag_a/evaluate",
        json={"user_id": "u2", "context": {}},
    ).json()
    assert decision["config_version"] == 2
    assert decision["variant_key"] == "blue"

    # GET 与 commit 返回一致
    got = client.get(f"/api/v1/releases/{release_id}").json()
    assert got == body


def test_commit_release_version_conflict_rolls_back_entire_batch(client):
    _make_flag(client, "flag_a")
    _make_flag(client, "flag_b")
    created = client.post(
        "/api/v1/releases",
        json={
            "flags": [_candidate("flag_a", 1), _candidate("flag_b", 1)],
            "users": [{"user_id": "u1", "context": {}}],
        },
    ).json()

    # 批次创建后、提交前，flag_a 被其他发布推进到 v2
    client.post("/api/v1/flags", json=_candidate("flag_a", 1))
    assert client.get("/api/v1/flags/flag_a").json()["version"] == 2
    versions_a_before = client.get("/api/v1/flags/flag_a/versions").json()
    versions_b_before = client.get("/api/v1/flags/flag_b/versions").json()

    resp = client.post(f"/api/v1/releases/{created['release_id']}/commit")
    assert resp.status_code == 409
    assert "flag_a" in resp.json()["detail"]

    # 零部分写入：flag_a 仍是 v2（未追加 v3），flag_b 仍是 v1（未追加 v2）
    assert client.get("/api/v1/flags/flag_a").json()["version"] == 2
    assert client.get("/api/v1/flags/flag_b").json()["version"] == 1
    assert client.get("/api/v1/flags/flag_a/versions").json() == versions_a_before
    assert client.get("/api/v1/flags/flag_b/versions").json() == versions_b_before

    # 批次保持 pending，所有 committed_version 仍为 null
    got = client.get(f"/api/v1/releases/{created['release_id']}").json()
    assert got["status"] == "pending"
    assert got["committed_at"] is None
    assert [i["committed_version"] for i in got["items"]] == [None, None]


def test_commit_release_conflict_when_flag_deleted(client):
    _make_flag(client, "flag_a")
    created = client.post(
        "/api/v1/releases",
        json={
            "flags": [_candidate("flag_a", 1)],
            "users": [{"user_id": "u1", "context": {}}],
        },
    ).json()

    assert client.delete("/api/v1/flags/flag_a").status_code == 204
    resp = client.post(f"/api/v1/releases/{created['release_id']}/commit")
    assert resp.status_code == 409
    assert "flag_a" in resp.json()["detail"]


def test_commit_release_is_idempotent_after_success(client):
    _make_flag(client, "flag_a")
    created = client.post(
        "/api/v1/releases",
        json={
            "flags": [_candidate("flag_a", 1)],
            "users": [{"user_id": "u1", "context": {}}],
        },
    ).json()

    first = client.post(f"/api/v1/releases/{created['release_id']}/commit")
    assert first.status_code == 200
    assert first.json()["status"] == "committed"
    assert first.json()["items"][0]["committed_version"] == 2

    # 再次提交：幂等返回首次结果，版本不继续推进
    second = client.post(f"/api/v1/releases/{created['release_id']}/commit")
    assert second.status_code == 200
    assert second.json() == first.json()
    assert client.get("/api/v1/flags/flag_a").json()["version"] == 2
    assert [v["version"] for v in client.get("/api/v1/flags/flag_a/versions").json()] == [1, 2]


def test_commit_release_unknown_404(client):
    resp = client.post("/api/v1/releases/nope/commit")
    assert resp.status_code == 404


def test_get_release_unknown_404(client):
    assert client.get("/api/v1/releases/nope").status_code == 404


def test_release_then_other_commit_both_succeed_sequentially(client):
    # 两个批次顺序提交，版本连续递增
    _make_flag(client, "flag_a")

    b1 = client.post(
        "/api/v1/releases",
        json={
            "flags": [_candidate("flag_a", 1)],
            "users": [{"user_id": "u1", "context": {}}],
        },
    ).json()
    committed1 = client.post(f"/api/v1/releases/{b1['release_id']}/commit").json()
    assert committed1["items"][0]["committed_version"] == 2

    # 第二个批次基于 v2 创建
    b2 = client.post(
        "/api/v1/releases",
        json={
            "flags": [_candidate("flag_a", 2, weights=(50, 50))],
            "users": [{"user_id": "u1", "context": {}}],
        },
    ).json()
    assert b2["status"] == "pending"
    committed2 = client.post(f"/api/v1/releases/{b2['release_id']}/commit").json()
    assert committed2["items"][0]["committed_version"] == 3
    assert [v["version"] for v in client.get("/api/v1/flags/flag_a/versions").json()] == [1, 2, 3]


def test_second_batch_created_against_old_version_gets_409_on_commit(client):
    _make_flag(client, "flag_a")
    # b1 与 b2 都基于 v1 创建
    b1 = client.post(
        "/api/v1/releases",
        json={
            "flags": [_candidate("flag_a", 1)],
            "users": [{"user_id": "u1", "context": {}}],
        },
    ).json()
    b2 = client.post(
        "/api/v1/releases",
        json={
            "flags": [_candidate("flag_a", 1, weights=(50, 50))],
            "users": [{"user_id": "u1", "context": {}}],
        },
    ).json()

    assert client.post(f"/api/v1/releases/{b1['release_id']}/commit").status_code == 200
    # b2 创建时锁定的 v1 已过期
    resp = client.post(f"/api/v1/releases/{b2['release_id']}/commit")
    assert resp.status_code == 409
    assert client.get("/api/v1/flags/flag_a").json()["version"] == 2


def test_commit_does_not_write_decision_snapshots(client):
    _make_flag(client, "flag_a")
    snapshots_before = client.get("/healthz").json()["snapshots"]
    created = client.post(
        "/api/v1/releases",
        json={
            "flags": [_candidate("flag_a", 1)],
            "users": [{"user_id": "u1", "context": {}}],
        },
    ).json()
    client.post(f"/api/v1/releases/{created['release_id']}/commit")
    assert client.get("/healthz").json()["snapshots"] == snapshots_before
