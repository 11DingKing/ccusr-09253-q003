"""规则版本生命周期、定时生效、撤回、回滚与并发的集成测试。"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest

from app import rule_service
from app.core.rules import RuleSpec
from app.scheduler import scheduler
from tests.conftest import SHANGHAI_PLAN, TestSessionLocal

UTC = timezone.utc
SH = timezone(timedelta(hours=8))


@pytest.fixture
def plan(client):
    resp = client.post("/api/plans", json=SHANGHAI_PLAN)
    assert resp.status_code == 201, resp.text
    return SHANGHAI_PLAN["plan_version"]


def _spec(**over):
    base = {
        "required_seconds": 3600,
        "seconds_per_lesson_unit": 2700,
    }
    base.update(over)
    return base


def _checkin(eid, start, end, student="S1", activity_type="regular"):
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


def _create_draft(db, plan, rid, effective_at, **spec):
    return rule_service.create_draft(
        db,
        plan_version=plan,
        spec=RuleSpec(**spec),
        created_by="author",
        effective_at=effective_at,
        rule_version_id=rid,
    )


def _approve_until_active(db, rid, now, approvers=("rev1", "rev2")):
    rule_service.approve(db, rule_version_id=rid, approver_id=approvers[0], note="ok", now=now)
    return rule_service.approve(db, rule_version_id=rid, approver_id=approvers[1], note="ok", now=now)


# ---------------------------------------------------------- 双人审核

def test_draft_requires_two_distinct_non_author_approvers(client, plan):
    eff = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    r = client.post(f"/api/plans/{plan}/rules", json={
        **_spec(), "rule_version_id": "RV-1", "created_by": "author", "effective_at": eff,
    })
    assert r.status_code == 201 and r.json()["state"] == "draft"

    # 起草人不能审批。
    r = client.post(f"/api/plans/{plan}/rules/RV-1/approve",
                    json={"approver_id": "author", "note": "x"})
    assert r.status_code == 403

    # 第一人审批 -> pending_approval。
    r = client.post(f"/api/plans/{plan}/rules/RV-1/approve",
                    json={"approver_id": "rev1", "note": "ok"})
    assert r.status_code == 200
    assert r.json()["state"] == "pending_approval"
    assert r.json()["approval_count"] == 1

    # 同一人不能重复审批。
    r = client.post(f"/api/plans/{plan}/rules/RV-1/approve",
                    json={"approver_id": "rev1", "note": "again"})
    assert r.status_code == 403

    # 生效时间在未来：第二人审批后进入 scheduled。
    r = client.post(f"/api/plans/{plan}/rules/RV-1/approve",
                    json={"approver_id": "rev2", "note": "ok"})
    assert r.status_code == 200
    assert r.json()["state"] == "scheduled"
    assert r.json()["approval_count"] == 2


def test_second_approval_in_the_past_activates_immediately(client, plan):
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    client.post(f"/api/plans/{plan}/rules", json={
        **_spec(), "rule_version_id": "RV-1", "created_by": "author", "effective_at": past,
    })
    client.post(f"/api/plans/{plan}/rules/RV-1/approve", json={"approver_id": "rev1", "note": ""})
    r = client.post(f"/api/plans/{plan}/rules/RV-1/approve", json={"approver_id": "rev2", "note": ""})
    assert r.json()["state"] == "active"
    # 立即生效以实际激活时刻为窗口起点（不早于当前时刻）。
    assert datetime.fromisoformat(r.json()["effective_at"]) >= datetime.now(UTC) - timedelta(seconds=5)


# ---------------------------------------------------------- 导入固化绑定

def test_events_pin_rule_at_import_and_upgrade_does_not_recompute(client, plan, db):
    # 历史签到（规则发布之前）：基线口径，2 小时。
    client.post(f"/api/plans/{plan}/events", json={"events": [
        _checkin("E-01", "2024-03-01T08:00:00+08:00", "2024-03-01T10:00:00+08:00"),
    ]})

    now = datetime(2024, 4, 1, tzinfo=UTC)
    _create_draft(db, plan, "RV-1", now, seconds_per_lesson_unit=2400, daily_cap_seconds=3600)
    _approve_until_active(db, "RV-1", now)

    # 规则生效后的 3 小时签到：封顶到 1 小时。
    client.post(f"/api/plans/{plan}/events", json={"events": [
        _checkin("E-02", "2024-04-02T08:00:00+08:00", "2024-04-02T11:00:00+08:00"),
    ]})
    snap = client.get(f"/api/plans/{plan}/snapshot").json()
    s1 = snap["students"][0]
    assert s1["seconds_by_rule"]["__baseline__"] == 7200
    assert s1["seconds_by_rule"]["RV-1"] == 3600
    assert s1["confirmed_seconds"] == 3 * 3600
    assert s1["lesson_units"] == 7200 // 2700 + 3600 // 2400
    checkins = {c["event_id"]: c for c in s1["checkins"]}
    assert checkins["E-01"]["rule_version_id"] is None
    assert checkins["E-02"]["rule_version_id"] == "RV-1"
    assert checkins["E-02"]["rule_spec"]["daily_cap_seconds"] == 3600
    assert {r["rule_version_id"] for r in snap["rule_catalog"]} >= {"RV-1"}

    # 再发一个新版本：E-01/E-02 的绑定与贡献不变（不整体重算）。
    later = datetime(2024, 5, 1, tzinfo=UTC)
    _create_draft(db, plan, "RV-2", later, seconds_per_lesson_unit=1800)
    _approve_until_active(db, "RV-2", later)
    s1b = client.get(f"/api/plans/{plan}/snapshot").json()["students"][0]
    assert s1b["seconds_by_rule"]["__baseline__"] == 7200
    assert s1b["seconds_by_rule"]["RV-1"] == 3600


# ---------------------------------------------------------- 定时生效/跨时区

def test_scheduled_activates_at_effective_instant_and_closes_old_window(client, plan, db):
    t0 = datetime(2024, 3, 1, tzinfo=UTC)
    _create_draft(db, plan, "RV-1", t0)
    _approve_until_active(db, "RV-1", t0)

    # 新版本：北京时间 5 月 1 日 08:00 == UTC 5 月 1 日 00:00。
    eff_sh = datetime(2024, 5, 1, 8, 0, tzinfo=SH)
    _create_draft(db, plan, "RV-2", eff_sh, seconds_per_lesson_unit=2400)
    rule_service.approve(db, rule_version_id="RV-2", approver_id="r1", note="", now=datetime(2024, 4, 1, tzinfo=UTC))
    rule_service.approve(db, rule_version_id="RV-2", approver_id="r2", note="", now=datetime(2024, 4, 1, tzinfo=UTC))
    rv2 = rule_service.get_rule(db, "RV-2")
    assert rv2["state"] == "scheduled"

    # 生效前一刻：仍是 RV-1。
    assert rule_service.bind_events(db, plan, [
        _checkin("X", "2024-05-01T07:59:00+08:00", "2024-05-01T08:30:00+08:00"),
    ], datetime(2024, 4, 2, tzinfo=UTC))[0]["X"] == "RV-1"

    # 重启恢复：停机错过生效时刻，扫描补激活。
    activated = rule_service.activate_due_scheduled(db, datetime(2024, 5, 2, tzinfo=UTC))
    assert activated == ["RV-2"]
    rv1_after = rule_service.get_rule(db, "RV-1")
    assert rv1_after["state"] == "superseded"
    assert rv1_after["end_at"] == "2024-05-01T00:00:00Z"
    # 边界时刻（左闭右开）起解析到 RV-2。
    assert rule_service.bind_events(db, plan, [
        _checkin("X", "2024-05-01T08:00:00+08:00", "2024-05-01T08:30:00+08:00"),
    ], datetime(2024, 5, 2, tzinfo=UTC))[0]["X"] == "RV-2"


def test_restart_recovery_via_scheduler_run_once(client, plan, db):
    eff = datetime(2024, 5, 1, tzinfo=UTC)
    _create_draft(db, plan, "RV-1", eff)
    _approve_until_active(db, "RV-1", datetime(2024, 4, 1, tzinfo=UTC))
    # RV-1 在 4 月审批时因生效时间未到而处于 scheduled；此刻早已到期。
    assert rule_service.get_rule(db, "RV-1")["state"] == "scheduled"
    activated = scheduler.run_once()
    assert "RV-1" in activated
    assert rule_service.get_rule(db, "RV-1")["state"] == "active"


def test_stale_scheduled_is_auto_withdrawn_when_newer_rule_already_live(client, plan, db):
    # RV-1 计划 5 月 1 日生效但停机错过；5 月 3 日紧急回滚产生 RV-2。
    t_past = datetime(2024, 4, 1, tzinfo=UTC)
    _create_draft(db, plan, "RV-1", datetime(2024, 5, 1, tzinfo=UTC))
    _approve_until_active(db, "RV-1", t_past)  # scheduled
    r2 = rule_service.rollback(
        db, target_rule_version_id="RV-1", actor_id="ops",
        reason="紧急换口径", now=datetime(2024, 5, 3, tzinfo=UTC),
        new_rule_version_id="RV-2",
    )
    assert r2["state"] == "active"

    # 5 月 4 日重启扫描：RV-1 不得回溯覆盖 RV-2，应自动作废。
    activated = rule_service.activate_due_scheduled(db, datetime(2024, 5, 4, tzinfo=UTC))
    assert activated == []
    assert rule_service.get_rule(db, "RV-1")["state"] == "withdrawn"
    actions = [a["action"] for a in rule_service.get_audit_trail(db, "RV-1")]
    assert "auto_withdrawn" in actions


# ---------------------------------------------------------- 撤回与迟到事件

def test_withdrawal_only_affects_unsettled_future_window(client, plan, db):
    t0 = datetime(2024, 4, 1, tzinfo=UTC)
    _create_draft(db, plan, "RV-1", t0, daily_cap_seconds=3600)
    _approve_until_active(db, "RV-1", t0)

    # 窗口内签到 + 冻结。
    client.post(f"/api/plans/{plan}/events", json={"events": [
        _checkin("E-01", "2024-04-02T08:00:00+08:00", "2024-04-02T11:00:00+08:00"),
    ]})
    frozen = client.post(f"/api/plans/{plan}/freezes/F-1", json={}).json()
    assert frozen["students"][0]["seconds_by_rule"]["RV-1"] == 3600

    # 5 月 1 日紧急撤回。
    rule_service.withdraw(db, rule_version_id="RV-1", actor_id="ops",
                          reason="口径有误", now=datetime(2024, 5, 1, tzinfo=UTC))
    rv1 = rule_service.get_rule(db, "RV-1")
    assert rv1["state"] == "withdrawn"
    assert rv1["end_at"] == "2024-05-01T00:00:00Z"

    # 迟到事件（重启后补导，业务时间在撤回前窗口内）仍绑定 RV-1。
    bindings, _ = rule_service.bind_events(db, plan, [
        _checkin("E-02", "2024-04-20T08:00:00+08:00", "2024-04-20T10:00:00+08:00"),
        _checkin("E-03", "2024-05-02T08:00:00+08:00", "2024-05-02T10:00:00+08:00"),
    ], datetime(2024, 5, 10, tzinfo=UTC))
    assert bindings["E-02"] == "RV-1"
    assert bindings["E-03"] is None  # 撤回后无后继规则 -> 基线

    # 历史冻结快照原样保留，仍可解释规则来源。
    frozen_again = client.get(f"/api/plans/{plan}/freezes/F-1").json()
    assert frozen_again["students"][0]["seconds_by_rule"]["RV-1"] == 3600
    catalog = {r["rule_version_id"]: r for r in frozen_again["rule_catalog"]}
    assert catalog["RV-1"]["spec"]["daily_cap_seconds"] == 3600


def test_withdraw_scheduled_before_effective_never_resolves(client, plan, db):
    eff = datetime(2024, 6, 1, tzinfo=UTC)
    _create_draft(db, plan, "RV-1", eff)
    _approve_until_active(db, "RV-1", datetime(2024, 5, 1, tzinfo=UTC))
    rule_service.withdraw(db, rule_version_id="RV-1", actor_id="ops",
                          reason="提前废止", now=datetime(2024, 5, 2, tzinfo=UTC))
    bindings, _ = rule_service.bind_events(db, plan, [
        _checkin("E-01", "2024-06-02T08:00:00+08:00", "2024-06-02T10:00:00+08:00"),
    ], datetime(2024, 5, 3, tzinfo=UTC))
    assert bindings["E-01"] is None


# ---------------------------------------------------------- 回滚

def test_rollback_publishes_new_active_version_and_closes_current(client, plan, db):
    t1 = datetime(2024, 3, 1, tzinfo=UTC)
    _create_draft(db, plan, "RV-1", t1, seconds_per_lesson_unit=2400)
    _approve_until_active(db, "RV-1", t1)
    t2 = datetime(2024, 4, 1, tzinfo=UTC)
    _create_draft(db, plan, "RV-2", t2, seconds_per_lesson_unit=1800)
    _approve_until_active(db, "RV-2", t2)

    # 回滚到 RV-1 的口径。
    rb = rule_service.rollback(
        db, target_rule_version_id="RV-1", actor_id="ops",
        reason="RV-2 计算有误", now=datetime(2024, 4, 15, tzinfo=UTC),
        new_rule_version_id="RV-RB",
    )
    assert rb["state"] == "active"
    assert rb["rollback_of"] == "RV-1"
    assert rb["spec"]["seconds_per_lesson_unit"] == 2400
    rv2 = rule_service.get_rule(db, "RV-2")
    assert rv2["state"] == "superseded"
    assert rv2["superseded_by"] == "RV-RB"
    # 审计链完整。
    actions = [a["action"] for a in rule_service.get_audit_trail(db, "RV-RB")]
    assert actions == ["rollback"]


# ---------------------------------------------------------- 差异与预览

def test_rule_diff_api(client, plan, db):
    _create_draft(db, plan, "RV-1", datetime(2024, 3, 1, tzinfo=UTC), seconds_per_lesson_unit=2700)
    _create_draft(db, plan, "RV-2", datetime(2024, 4, 1, tzinfo=UTC), seconds_per_lesson_unit=2400, daily_cap_seconds=3600)
    d = client.get(f"/api/plans/{plan}/rules/RV-1/diff/RV-2").json()
    assert d["identical"] is False
    assert set(d["fields_changed"]) == {"seconds_per_lesson_unit", "daily_cap_seconds"}


def test_impact_preview_recomputes_only_future_events(client, plan, db):
    client.post(f"/api/plans/{plan}/events", json={"events": [
        _checkin("E-01", "2024-03-01T08:00:00+08:00", "2024-03-01T11:00:00+08:00"),
        _checkin("E-02", "2024-04-01T08:00:00+08:00", "2024-04-01T11:00:00+08:00"),
    ]})
    r = client.post(f"/api/plans/{plan}/rules-preview", json={
        "effective_at": "2024-03-15T00:00:00+00:00",
        "daily_cap_seconds": 3600,
    })
    assert r.status_code == 200, r.text
    impact = r.json()
    assert impact["students_affected"] == 1
    fields = impact["student_changes"][0]["fields"]
    # E-02 的 3 小时被候选口径封顶到 1 小时；E-01 不受影响。
    assert fields["confirmed_seconds"]["after"] == 4 * 3600


# ---------------------------------------------------------- 非签到事件与异常输入

def test_mentor_confirm_binds_by_occurred_at_and_naive_is_rejected(client, plan, db):
    now = datetime(2024, 4, 1, tzinfo=UTC)
    _create_draft(db, plan, "RV-1", now)
    _approve_until_active(db, "RV-1", now)

    payload = {"events": [{
        "event_id": "C-01", "event_type": "leave_correction", "student_id": "S1",
        "payload": {"adjustment_seconds": 600, "reason": "x",
                    "occurred_at": "2024-04-02T08:00:00+08:00"},
    }]}
    r = client.post(f"/api/plans/{plan}/events", json=payload)
    assert r.status_code == 201, r.text
    assert r.json()["bindings"]["C-01"] == "RV-1"

    # 缺少时区信息的 occurred_at 拒绝导入（400），不写入任何绑定。
    bad = {"events": [{
        "event_id": "C-02", "event_type": "leave_correction", "student_id": "S1",
        "payload": {"adjustment_seconds": 600, "occurred_at": "2024-04-02T08:00:00"},
    }]}
    r = client.post(f"/api/plans/{plan}/events", json=bad)
    assert r.status_code == 400, r.text


# ---------------------------------------------------------- 并发发布

def test_concurrent_activation_only_one_wins(client, plan):
    errors: list[Exception] = []

    def _attempt(rid, approvers):
        session = TestSessionLocal()
        try:
            rule_service.create_draft(
                session, plan_version=plan,
                spec=RuleSpec(seconds_per_lesson_unit=2400),
                created_by=f"author-{rid}",
                effective_at=datetime(2024, 1, 1, tzinfo=UTC),
                rule_version_id=rid,
            )
            rule_service.approve(session, rule_version_id=rid, approver_id=approvers[0],
                                 note="", now=datetime(2024, 1, 1, tzinfo=UTC))
            rule_service.approve(session, rule_version_id=rid, approver_id=approvers[1],
                                 note="", now=datetime(2024, 1, 1, tzinfo=UTC))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            session.close()

    threads = [
        threading.Thread(target=_attempt, args=("RV-A", ("a1", "a2"))),
        threading.Thread(target=_attempt, args=("RV-B", ("b1", "b2"))),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 恰好一方成功，另一方收到冲突异常。
    assert len(errors) == 1
    assert isinstance(errors[0], rule_service.RuleStateConflictError)
    listing = client.get(f"/api/plans/{plan}/rules").json()
    active = [r for r in listing if r["state"] == "active"]
    assert len(active) == 1


def test_two_scheduled_versions_at_same_instant_conflict(client, plan, db):
    eff = datetime(2024, 6, 1, tzinfo=UTC)
    _create_draft(db, plan, "RV-1", eff)
    _approve_until_active(db, "RV-1", datetime(2024, 5, 1, tzinfo=UTC))
    assert rule_service.get_rule(db, "RV-1")["state"] == "scheduled"
    _create_draft(db, plan, "RV-2", eff, seconds_per_lesson_unit=1800)
    rule_service.approve(db, rule_version_id="RV-2", approver_id="x1", note="", now=datetime(2024, 5, 1, tzinfo=UTC))
    with pytest.raises(rule_service.RuleStateConflictError):
        rule_service.approve(db, rule_version_id="RV-2", approver_id="x2", note="", now=datetime(2024, 5, 1, tzinfo=UTC))
