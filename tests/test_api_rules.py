"""规则版本生命周期 API：草拟、双人审核、定时生效、紧急撤回与回滚。"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

from app import services
from tests.conftest import SHANGHAI_PLAN, TestSessionLocal

UTC = timezone.utc
PV = SHANGHAI_PLAN["plan_version"]


def _create_plan(client):
    resp = client.post("/api/plans", json=SHANGHAI_PLAN)
    assert resp.status_code == 201, resp.text


def _checkin(eid, student, start, end, activity_type="regular"):
    return {
        "event_id": eid,
        "event_type": "checkin",
        "student_id": student,
        "payload": {
            "activity_id": "A1",
            "activity_type": activity_type,
            "check_in_at": start,
            "check_out_at": end,
        },
    }


def _import(client, events):
    resp = client.post(f"/api/plans/{PV}/events", json={"events": events})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _make_rule(client, rule_id, *, effective_in=timedelta(hours=1), params=None, effective_at=None):
    """创建并完成双人审核、定时发布的完整生命周期，返回发布后的规则。"""
    body = {
        "rule_id": rule_id,
        "params": params or {"seconds_per_lesson": 3000},
        "actor_id": "drafter",
    }
    resp = client.post(f"/api/plans/{PV}/rules", json=body)
    assert resp.status_code == 201, resp.text
    resp = client.post(
        f"/api/plans/{PV}/rules/{rule_id}/submit", json={"actor_id": "drafter"}
    )
    assert resp.status_code == 200, resp.text
    for approver in ("reviewer-a", "reviewer-b"):
        resp = client.post(
            f"/api/plans/{PV}/rules/{rule_id}/approve",
            json={"approver_id": approver},
        )
        assert resp.status_code == 200, resp.text
    effective_from = effective_at or (datetime.now(UTC) + effective_in)
    resp = client.post(
        f"/api/plans/{PV}/rules/{rule_id}/schedule",
        json={"actor_id": "registrar", "effective_from": _iso(effective_from)},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# 生命周期：草拟 → 双人审核 → 定时生效
# ---------------------------------------------------------------------------


def test_full_lifecycle_with_dual_approval(client):
    _create_plan(client)
    resp = client.post(
        f"/api/plans/{PV}/rules",
        json={
            "rule_id": "R-50min",
            "params": {"seconds_per_lesson": 3000, "daily_cap_seconds": 14400},
            "actor_id": "drafter",
            "reason": "新学期口径",
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["state"] == "draft"

    # 草稿期可修改参数，之后锁定
    resp = client.patch(
        f"/api/plans/{PV}/rules/R-50min",
        json={
            "params": {"seconds_per_lesson": 3000},
            "actor_id": "drafter",
            "reason": "去掉每日上限",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["params"]["daily_cap_seconds"] is None

    resp = client.post(
        f"/api/plans/{PV}/rules/R-50min/submit", json={"actor_id": "drafter"}
    )
    assert resp.json()["state"] == "in_review"

    # 提交后参数锁定
    resp = client.patch(
        f"/api/plans/{PV}/rules/R-50min",
        json={
            "params": {"seconds_per_lesson": 2700},
            "actor_id": "drafter",
            "reason": "试图篡改",
        },
    )
    assert resp.status_code == 409

    # 拟稿人不能自审
    resp = client.post(
        f"/api/plans/{PV}/rules/R-50min/approve", json={"approver_id": "drafter"}
    )
    assert resp.status_code == 409

    # 第一名审核人：仍在审核中；重复审核幂等
    resp = client.post(
        f"/api/plans/{PV}/rules/R-50min/approve", json={"approver_id": "reviewer-a"}
    )
    assert resp.json()["state"] == "in_review"
    resp = client.post(
        f"/api/plans/{PV}/rules/R-50min/approve", json={"approver_id": "reviewer-a"}
    )
    body = resp.json()
    assert body["state"] == "in_review"
    assert body["approvals"] == ["reviewer-a"]

    # 第二名不同的审核人：双人审核完成
    resp = client.post(
        f"/api/plans/{PV}/rules/R-50min/approve", json={"approver_id": "reviewer-b"}
    )
    body = resp.json()
    assert body["state"] == "approved"
    assert sorted(body["approvals"]) == ["reviewer-a", "reviewer-b"]

    # 审计轨迹完整记录生命周期
    detail = client.get(f"/api/plans/{PV}/rules/R-50min").json()
    actions = [entry["action"] for entry in detail["audit"]]
    assert actions == ["create", "patch", "submit", "approve", "approve"]


def test_schedule_requires_approved_state_and_future_effective(client):
    _create_plan(client)
    client.post(
        f"/api/plans/{PV}/rules",
        json={"rule_id": "R-1", "params": {}, "actor_id": "drafter"},
    )
    # 草稿不能直接发布
    resp = client.post(
        f"/api/plans/{PV}/rules/R-1/schedule",
        json={
            "actor_id": "registrar",
            "effective_from": _iso(datetime.now(UTC) + timedelta(hours=1)),
        },
    )
    assert resp.status_code == 409

    client.post(f"/api/plans/{PV}/rules/R-1/submit", json={"actor_id": "drafter"})
    client.post(f"/api/plans/{PV}/rules/R-1/approve", json={"approver_id": "a"})
    client.post(f"/api/plans/{PV}/rules/R-1/approve", json={"approver_id": "b"})

    # 不允许追溯生效
    resp = client.post(
        f"/api/plans/{PV}/rules/R-1/schedule",
        json={
            "actor_id": "registrar",
            "effective_from": _iso(datetime.now(UTC) - timedelta(hours=1)),
        },
    )
    assert resp.status_code == 400

    # 定时生效后，适用版本查询按绝对瞬间解析
    effective = datetime.now(UTC) + timedelta(hours=1)
    resp = client.post(
        f"/api/plans/{PV}/rules/R-1/schedule",
        json={"actor_id": "registrar", "effective_from": _iso(effective)},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["state"] == "scheduled"

    before = client.get(
        f"/api/plans/{PV}/rules/applicable",
        params={"at": _iso(effective - timedelta(seconds=1))},
    ).json()
    assert before["rule_id"] == "baseline"
    after = client.get(
        f"/api/plans/{PV}/rules/applicable", params={"at": _iso(effective)}
    ).json()
    assert after["rule_id"] == "R-1"
    assert after["params"]["seconds_per_lesson"] == 2700


def test_import_pins_rule_by_occurrence_across_timezones(client):
    """跨时区生效：生效瞬间与事件发生时刻都按绝对时间比较。"""
    _create_plan(client)
    effective = datetime.now(UTC) + timedelta(minutes=5)
    _make_rule(
        client,
        "R-50min",
        effective_in=timedelta(minutes=5),
        params={"seconds_per_lesson": 3000},
    )

    before_occ = (effective - timedelta(hours=2)).astimezone(
        timezone(timedelta(hours=8))
    )
    after_occ = (effective + timedelta(hours=2)).astimezone(
        timezone(timedelta(hours=-5))
    )
    result = _import(
        client,
        [
            _checkin(
                "E-01",
                "S1",
                before_occ.isoformat(),
                (before_occ + timedelta(minutes=90)).isoformat(),
            ),
            _checkin(
                "E-02",
                "S1",
                after_occ.isoformat(),
                (after_occ + timedelta(minutes=90)).isoformat(),
            ),
        ],
    )
    assert result["pinned"] == {"E-01": "baseline", "E-02": "R-50min"}

    progress = client.get(f"/api/plans/{PV}/students/S1/progress").json()
    # 基线 5400//2700 = 2 学时；新版 5400//3000 = 1 学时
    assert progress["lesson_units"] == 3
    segments = {seg["rule_id"]: seg for seg in progress["rule_segments"]}
    assert segments["baseline"]["lesson_units"] == 2
    assert segments["R-50min"]["lesson_units"] == 1
    checkin_rules = {c["event_id"]: c["rule_id"] for c in progress["checkins"]}
    assert checkin_rules == {"E-01": "baseline", "E-02": "R-50min"}


# ---------------------------------------------------------------------------
# 紧急撤回：只影响尚未结算的区间
# ---------------------------------------------------------------------------


def test_withdraw_repins_only_unsettled_events(client):
    _create_plan(client)
    now = datetime.now(UTC)
    _make_rule(client, "R-50min", effective_in=timedelta(minutes=5))
    effective = now + timedelta(minutes=5)

    # E-01 发生在规则生效后，固定到 R-50min
    occ1 = effective + timedelta(minutes=30)
    _import(
        client,
        [_checkin("E-01", "S1", _iso(occ1), _iso(occ1 + timedelta(minutes=90)))],
    )
    # 冻结覆盖 E-01：该区间已结算
    freeze = client.post(f"/api/plans/{PV}/freezes/F-01", json={})
    assert freeze.status_code == 201
    assert freeze.json()["event_cutoff_id"] == "E-01"

    # 冻结之后到达的事件 E-02/E-03 也固定到 R-50min（未结算）
    occ2 = effective + timedelta(minutes=150)
    occ3 = effective + timedelta(minutes=300)
    _import(
        client,
        [
            _checkin("E-02", "S1", _iso(occ2), _iso(occ2 + timedelta(minutes=90))),
            _checkin("E-03", "S1", _iso(occ3), _iso(occ3 + timedelta(minutes=90))),
        ],
    )

    # 紧急撤回：只有未结算的 E-02/E-03 被重新固定到基线
    resp = client.post(
        f"/api/plans/{PV}/rules/R-50min/withdraw",
        json={"actor_id": "registrar", "reason": "口径发布错误，紧急撤回"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["state"] == "withdrawn"
    assert body["repinned"] == 2
    assert {e["event_id"] for e in body["repinned_events"]} == {"E-02", "E-03"}
    assert all(e["to_rule_id"] == "baseline" for e in body["repinned_events"])

    # 历史冻结快照保持原样，仍能解释每条记录的规则来源
    frozen = client.get(f"/api/plans/{PV}/freezes/F-01").json()
    assert frozen["students"][0]["lesson_units"] == 5400 // 3000
    assert frozen["students"][0]["checkins"][0]["rule_id"] == "R-50min"
    assert frozen["rules"]["R-50min"]["seconds_per_lesson"] == 3000
    assert frozen["rules"]["R-50min"]["state"] == "scheduled"

    # 实时快照：E-01 仍按 R-50min（已结算），E-02/E-03 回到基线
    live = client.get(f"/api/plans/{PV}/snapshot").json()
    student = live["students"][0]
    checkin_rules = {c["event_id"]: c["rule_id"] for c in student["checkins"]}
    assert checkin_rules == {
        "E-01": "R-50min",
        "E-02": "baseline",
        "E-03": "baseline",
    }
    assert student["lesson_units"] == 5400 // 3000 + 2 * (5400 // 2700)
    # 撤回版本仍保留在快照的规则来源里（state=withdrawn）
    assert live["rules"]["R-50min"]["state"] == "withdrawn"

    # 撤回是幂等的
    resp = client.post(
        f"/api/plans/{PV}/rules/R-50min/withdraw",
        json={"actor_id": "registrar", "reason": "重复撤回"},
    )
    assert resp.status_code == 200
    assert resp.json()["repinned"] == 0

    # 撤回后新导入的事件直接落到回退版本
    occ4 = effective + timedelta(minutes=450)
    result = _import(
        client,
        [_checkin("E-04", "S1", _iso(occ4), _iso(occ4 + timedelta(minutes=90)))],
    )
    assert result["pinned"] == {"E-04": "baseline"}


def test_withdraw_requires_published_state_and_reason(client):
    _create_plan(client)
    client.post(
        f"/api/plans/{PV}/rules",
        json={"rule_id": "R-1", "params": {}, "actor_id": "drafter"},
    )
    resp = client.post(
        f"/api/plans/{PV}/rules/R-1/withdraw",
        json={"actor_id": "registrar", "reason": "草稿不能撤回"},
    )
    assert resp.status_code == 409
    resp = client.post(
        f"/api/plans/{PV}/rules/R-1/withdraw",
        json={"actor_id": "registrar", "reason": ""},
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 规则差异、影响预览、回滚
# ---------------------------------------------------------------------------


def test_rule_diff_and_preview_do_not_mutate_state(client):
    _create_plan(client)
    now = datetime.now(UTC)
    occ = now - timedelta(hours=2)
    _import(
        client,
        [_checkin("E-01", "S1", _iso(occ), _iso(occ + timedelta(minutes=90)))],
    )
    client.post(
        f"/api/plans/{PV}/rules",
        json={
            "rule_id": "R-50min",
            "params": {"seconds_per_lesson": 3000, "daily_cap_seconds": 3600},
            "actor_id": "drafter",
        },
    )

    # 参数级差异（左侧版本 → 右侧基线）
    diff = client.get(f"/api/plans/{PV}/rules/R-50min/diff/baseline").json()
    assert diff["changes"]["seconds_per_lesson"] == {"before": 3000, "after": 2700}
    assert diff["changes"]["daily_cap_seconds"] == {"before": 3600, "after": None}

    # 影响预览：假设一小时前生效，学员学时变化可见
    preview = client.post(
        f"/api/plans/{PV}/rules/R-50min/preview",
        json={"effective_from": _iso(now - timedelta(hours=3))},
    ).json()
    assert preview["events_repinned"] == 1
    assert preview["students_affected"] == 1
    change = preview["student_changes"][0]
    assert change["student_id"] == "S1"
    assert change["fields"]["lesson_units"]["before"] == 2
    assert change["fields"]["lesson_units"]["after"] == 1

    # 预览是只读的：实时快照与事件固定版本不变
    progress = client.get(f"/api/plans/{PV}/students/S1/progress").json()
    assert progress["lesson_units"] == 2
    assert progress["checkins"][0]["rule_id"] == "baseline"


def test_rollback_creates_draft_from_historical_version(client):
    _create_plan(client)
    _make_rule(client, "R-50min", params={"seconds_per_lesson": 3000})

    # 回滚到基线口径：生成一个携带基线参数的新草稿
    resp = client.post(
        f"/api/plans/{PV}/rules/baseline/rollback",
        json={"new_rule_id": "R-restore", "actor_id": "registrar"},
    )
    assert resp.status_code == 201, resp.text
    draft = resp.json()
    assert draft["state"] == "draft"
    assert draft["params"]["seconds_per_lesson"] == 2700

    # 回滚到某个历史版本
    resp = client.post(
        f"/api/plans/{PV}/rules/R-50min/rollback",
        json={"new_rule_id": "R-copy", "actor_id": "registrar"},
    )
    assert resp.status_code == 201
    assert resp.json()["params"]["seconds_per_lesson"] == 3000

    detail = client.get(f"/api/plans/{PV}/rules/R-restore").json()
    assert detail["audit"][0]["action"] == "rollback"
    assert detail["audit"][0]["detail"]["source_rule_id"] == "baseline"


# ---------------------------------------------------------------------------
# 并发发布与生效时刻唯一性
# ---------------------------------------------------------------------------


def test_concurrent_publish_only_one_wins(client):
    _create_plan(client)
    client.post(
        f"/api/plans/{PV}/rules",
        json={"rule_id": "R-race", "params": {}, "actor_id": "drafter"},
    )
    client.post(f"/api/plans/{PV}/rules/R-race/submit", json={"actor_id": "drafter"})
    client.post(f"/api/plans/{PV}/rules/R-race/approve", json={"approver_id": "a"})
    client.post(f"/api/plans/{PV}/rules/R-race/approve", json={"approver_id": "b"})

    effective = datetime.now(UTC) + timedelta(hours=1)
    outcomes: list[str] = []
    lock = threading.Lock()

    def _publish():
        session = TestSessionLocal()
        try:
            _, transitioned = services.schedule_rule(
                session,
                plan_version=PV,
                rule_id="R-race",
                effective_from=effective,
                actor_id="registrar",
            )
            with lock:
                outcomes.append("ok" if transitioned else "idempotent")
        except (services.RuleStateError, services.RuleConflictError):
            with lock:
                outcomes.append("conflict")
        finally:
            session.close()

    threads = [threading.Thread(target=_publish) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 并发发布同一规则到同一生效时刻：只有一个调用完成状态迁移，
    # 其余调用幂等地看到已发布状态，时间线不被破坏。
    assert outcomes.count("ok") == 1
    assert outcomes.count("conflict") == 0
    detail = client.get(f"/api/plans/{PV}/rules/R-race").json()
    assert detail["state"] == "scheduled"
    assert detail["version"] == 4  # create=1, submit=2, approved=3, scheduled=4


def test_effective_slot_is_unique_across_rules(client):
    _create_plan(client)
    effective = datetime.now(UTC) + timedelta(hours=1)
    _make_rule(client, "R-1", effective_at=effective)
    # 第二条规则审批完成后试图占用同一生效瞬间
    client.post(
        f"/api/plans/{PV}/rules",
        json={"rule_id": "R-2", "params": {}, "actor_id": "drafter"},
    )
    client.post(f"/api/plans/{PV}/rules/R-2/submit", json={"actor_id": "drafter"})
    client.post(f"/api/plans/{PV}/rules/R-2/approve", json={"approver_id": "a"})
    client.post(f"/api/plans/{PV}/rules/R-2/approve", json={"approver_id": "b"})
    resp = client.post(
        f"/api/plans/{PV}/rules/R-2/schedule",
        json={"actor_id": "registrar", "effective_from": _iso(effective)},
    )
    assert resp.status_code == 409
    # 错开一秒即可发布
    resp = client.post(
        f"/api/plans/{PV}/rules/R-2/schedule",
        json={
            "actor_id": "registrar",
            "effective_from": _iso(effective + timedelta(seconds=1)),
        },
    )
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# 重启恢复与迟到事件、版本分叉
# ---------------------------------------------------------------------------


def test_restart_recovery_promotes_due_rules(client):
    """定时规则在进程重启后由恢复扫描物化为 active。"""
    _create_plan(client)
    t0 = datetime.now(UTC)
    session = TestSessionLocal()
    try:
        services.create_rule(
            session, plan_version=PV, rule_id="R-sched", params={}, actor_id="drafter"
        )
        services.submit_rule(session, plan_version=PV, rule_id="R-sched", actor_id="drafter")
        services.approve_rule(session, plan_version=PV, rule_id="R-sched", approver_id="a")
        services.approve_rule(session, plan_version=PV, rule_id="R-sched", approver_id="b")
        services.schedule_rule(
            session,
            plan_version=PV,
            rule_id="R-sched",
            effective_from=t0 + timedelta(minutes=1),
            actor_id="registrar",
            now=t0,
        )
        # 模拟重启：全新会话执行恢复扫描
        recovered = TestSessionLocal()
        try:
            promoted = services.recover_scheduled_rules(
                recovered, now=t0 + timedelta(minutes=2)
            )
            assert promoted == 1
        finally:
            recovered.close()
        detail = services.get_rule_detail(session, PV, "R-sched")
        assert detail["state"] == "active"
        assert detail["audit"][-1]["action"] == "activate"
    finally:
        session.close()


def test_late_events_keep_historical_rule_and_freeze_stays_immutable(client):
    """迟到事件按发生时刻固定版本；冻结快照不被后续导入改变。"""
    _create_plan(client)
    now = datetime.now(UTC)
    _make_rule(client, "R-50min", effective_in=timedelta(minutes=5))
    effective = now + timedelta(minutes=5)

    # 生效前的历史签到 + 生效后的签到
    old_occ = effective - timedelta(days=2)
    new_occ = effective + timedelta(hours=1)
    _import(
        client,
        [
            _checkin("E-01", "S1", _iso(old_occ), _iso(old_occ + timedelta(minutes=90))),
            _checkin("E-02", "S1", _iso(new_occ), _iso(new_occ + timedelta(minutes=90))),
        ],
    )
    freeze = client.post(f"/api/plans/{PV}/freezes/F-01", json={}).json()
    assert freeze["students"][0]["lesson_units"] == 5400 // 2700 + 5400 // 3000

    # 冻结后又有迟到事件到达，发生时刻在旧口径区间
    late_occ = effective - timedelta(days=1)
    result = _import(
        client,
        [
            _checkin(
                "E-03", "S1", _iso(late_occ), _iso(late_occ + timedelta(minutes=90))
            )
        ],
    )
    assert result["pinned"] == {"E-03": "baseline"}

    # 冻结快照不变；实时快照把迟到事件计在基线口径下
    frozen = client.get(f"/api/plans/{PV}/freezes/F-01").json()
    assert frozen["event_cutoff_id"] == "E-02"
    assert len(frozen["students"][0]["checkins"]) == 2
    live = client.get(f"/api/plans/{PV}/snapshot").json()
    student = live["students"][0]
    assert student["lesson_units"] == 2 * (5400 // 2700) + 5400 // 3000
    segments = {seg["rule_id"] for seg in student["rule_segments"]}
    assert segments == {"baseline", "R-50min"}


def test_version_fork_and_withdrawal_heals_timeline(client):
    """版本分叉：R2、R3 先后生效；撤回 R2 后其区间回退到基线，R3 不受影响。"""
    _create_plan(client)
    now = datetime.now(UTC)
    _make_rule(client, "R-2", effective_in=timedelta(minutes=5))
    _make_rule(
        client,
        "R-3",
        effective_in=timedelta(hours=2),
        params={"seconds_per_lesson": 3600},
    )

    occ_base = now - timedelta(hours=1)
    occ_r2 = now + timedelta(minutes=30)
    occ_r3 = now + timedelta(hours=3)
    result = _import(
        client,
        [
            _checkin("E-01", "S1", _iso(occ_base), _iso(occ_base + timedelta(minutes=90))),
            _checkin("E-02", "S1", _iso(occ_r2), _iso(occ_r2 + timedelta(minutes=90))),
            _checkin("E-03", "S1", _iso(occ_r3), _iso(occ_r3 + timedelta(minutes=90))),
        ],
    )
    assert result["pinned"] == {
        "E-01": "baseline",
        "E-02": "R-2",
        "E-03": "R-3",
    }

    resp = client.post(
        f"/api/plans/{PV}/rules/R-2/withdraw",
        json={"actor_id": "registrar", "reason": "分叉版本发布有误"},
    )
    assert resp.status_code == 200
    assert [e["event_id"] for e in resp.json()["repinned_events"]] == ["E-02"]

    live = client.get(f"/api/plans/{PV}/snapshot").json()
    student = live["students"][0]
    checkin_rules = {c["event_id"]: c["rule_id"] for c in student["checkins"]}
    assert checkin_rules == {"E-01": "baseline", "E-02": "baseline", "E-03": "R-3"}
    # 基线两段 5400//2700*2，R-3 一段 5400//3600
    assert student["lesson_units"] == 2 * 2 + 1
    # 被撤回版本不再参与后续解析
    applicable = client.get(
        f"/api/plans/{PV}/rules/applicable",
        params={"at": _iso(now + timedelta(minutes=40))},
    ).json()
    assert applicable["rule_id"] == "baseline"


def test_rule_endpoints_validate_plan_and_rule_existence(client):
    _create_plan(client)
    assert client.get("/api/plans/NOPE/rules").status_code == 404
    assert client.get(f"/api/plans/{PV}/rules/NOPE").status_code == 404
    resp = client.post(
        f"/api/plans/{PV}/rules",
        json={"rule_id": "baseline", "params": {}, "actor_id": "x"},
    )
    assert resp.status_code == 400
    # 基线作为内置版本可查询
    baseline = client.get(f"/api/plans/{PV}/rules/baseline").json()
    assert baseline["state"] == "baseline"
    assert baseline["params"]["seconds_per_lesson"] == 2700
