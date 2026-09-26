"""服务端业务模块。"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .core.replay import Event as CoreEvent
from .core.replay import EventType, display_rule_id
from .core.rules import (
    BASELINE_RULE,
    BASELINE_RULE_ID,
    RuleError,
    RuleInterval,
    RuleSpec,
    diff_params,
    pick_applicable,
    spec_from_params,
    spec_to_params,
    ensure_utc,
)
from .core.snapshot import Snapshot, build_snapshot, diff_snapshots, explain_student
from .models import RuleVersion
from .repository import (
    add_approval,
    add_rule_audit,
    due_scheduled_rules,
    get_freeze,
    get_plan,
    get_rule,
    insert_events,
    insert_freeze,
    insert_rule,
    list_approvals,
    list_rule_audits,
    list_rules,
    load_events,
    load_events_up_to,
    load_unsettled_events_with_rule,
    max_event_id,
    max_freeze_cutoff,
    published_rules,
    transition_rule,
    update_event_rule,
    upsert_plan,
)


class PlanNotFoundError(Exception):
    pass


class FreezeConflictError(Exception):
    pass


class FreezeNotFoundError(Exception):
    pass


class RuleNotFoundError(Exception):
    pass


class RuleStateError(Exception):
    """生命周期状态不允许该操作。"""


class RuleConflictError(Exception):
    """并发或唯一性冲突。"""


class RuleValidationError(Exception):
    """请求参数不合法。"""


class EventValidationError(Exception):
    """事件负载不合法。"""


def _now(now: datetime | None) -> datetime:
    moment = now if now is not None else datetime.now(timezone.utc)
    if moment.tzinfo is None:
        raise RuleValidationError("时间必须包含时区")
    return moment.astimezone(timezone.utc)


def _iso(moment: datetime | None) -> str | None:
    if moment is None:
        return None
    return ensure_utc(moment).isoformat().replace("+00:00", "Z")


def get_plan_plain(db: Session, plan_version: str) -> dict[str, Any] | None:
    plan = get_plan(db, plan_version)
    if plan is None:
        return None
    return {
        "plan_version": plan.plan_version,
        "iana_timezone": plan.iana_timezone,
        "required_seconds": plan.required_seconds,
    }


def ensure_plan(
    db: Session,
    *,
    plan_version: str,
    iana_timezone: str,
    required_seconds: int,
) -> dict[str, Any]:
    plan = upsert_plan(
        db,
        plan_version=plan_version,
        iana_timezone=iana_timezone,
        required_seconds=required_seconds,
    )
    return {
        "plan_version": plan.plan_version,
        "iana_timezone": plan.iana_timezone,
        "required_seconds": plan.required_seconds,
    }


def _require_plan(db: Session, plan_version: str):
    plan = get_plan(db, plan_version)
    if plan is None:
        raise PlanNotFoundError(f"plan version '{plan_version}' is not registered")
    return plan


def _require_rule(db: Session, plan_version: str, rule_id: str) -> RuleVersion:
    row = get_rule(db, plan_version, rule_id)
    if row is None:
        raise RuleNotFoundError(
            f"rule '{rule_id}' for plan '{plan_version}' does not exist"
        )
    return row


def _validate_params(params: dict[str, Any]) -> RuleSpec:
    try:
        return spec_from_params(params)
    except RuleError as exc:
        raise RuleValidationError(str(exc)) from exc


# ---------------------------------------------------------------------------
# 事件导入：导入时按事件发生时刻固定适用规则版本
# ---------------------------------------------------------------------------


def _event_occurrence(event: dict[str, Any], fallback: datetime) -> datetime:
    """事件的业务发生时刻；签到取 check_in_at，其余取导入时刻。"""
    if event.get("event_type") == EventType.CHECKIN.value:
        raw = (event.get("payload") or {}).get("check_in_at")
        try:
            moment = datetime.fromisoformat(str(raw))
        except (TypeError, ValueError) as exc:
            raise EventValidationError(
                f"事件 {event.get('event_id')} 的 check_in_at 无法解析"
            ) from exc
        if moment.tzinfo is None:
            raise EventValidationError("check_in_at 必须包含时区")
        return moment.astimezone(timezone.utc)
    return fallback


def _stored_occurrence(event_type: str, payload: dict[str, Any], created_at: datetime) -> datetime:
    if event_type == EventType.CHECKIN.value:
        raw = (payload or {}).get("check_in_at")
        if raw:
            try:
                moment = datetime.fromisoformat(str(raw))
            except (TypeError, ValueError):
                moment = None
            if moment is not None and moment.tzinfo is not None:
                return moment.astimezone(timezone.utc)
    return ensure_utc(created_at)


def _timeline(db: Session, plan_version: str) -> list[RuleInterval]:
    return [
        RuleInterval(rule_id=row.rule_id, effective_from=ensure_utc(row.effective_from))
        for row in published_rules(db, plan_version)
    ]


def import_events(
    db: Session,
    *,
    plan_version: str,
    events: list[dict[str, Any]],
    now: datetime | None = None,
) -> dict[str, Any]:
    _require_plan(db, plan_version)
    instant = _now(now)
    # 惰性激活到期的定时规则，保证导入解析看到最新时间线（重启后同样成立）。
    recover_scheduled_rules(db, now=instant)
    timeline = _timeline(db, plan_version)

    rows: list[dict[str, Any]] = []
    pinned: dict[str, str] = {}
    for event in events:
        occurrence = _event_occurrence(event, instant)
        rule_id = pick_applicable(timeline, occurrence)
        rows.append({**event, "rule_id": rule_id})
        pinned[event["event_id"]] = display_rule_id(rule_id)

    accepted, duplicates = insert_events(db, plan_version=plan_version, events=rows)
    accepted_set = set(accepted)
    return {
        "accepted": len(accepted),
        "duplicates": duplicates,
        "rejected": [],
        "pinned": {eid: rid for eid, rid in pinned.items() if eid in accepted_set},
    }


# ---------------------------------------------------------------------------
# 快照与冻结
# ---------------------------------------------------------------------------


def _rule_maps(
    db: Session, plan_version: str
) -> tuple[dict[str, RuleSpec], dict[str, dict[str, Any]]]:
    specs: dict[str, RuleSpec] = {}
    meta: dict[str, dict[str, Any]] = {}
    for row in list_rules(db, plan_version):
        specs[row.rule_id] = spec_from_params(row.params)
        meta[row.rule_id] = {
            "state": row.state,
            "effective_from": _iso(row.effective_from),
            "withdrawn_at": _iso(row.withdrawn_at),
        }
    return specs, meta


def current_snapshot(db: Session, plan_version: str) -> Snapshot:
    plan = _require_plan(db, plan_version)
    specs, meta = _rule_maps(db, plan_version)
    events = load_events(db, plan_version)
    return build_snapshot(
        events,
        plan_version=plan_version,
        timezone_name=plan.iana_timezone,
        required_seconds=plan.required_seconds,
        rules=specs,
        rule_meta=meta,
    )


def student_progress(
    db: Session, plan_version: str, student_id: str
) -> dict[str, Any] | None:
    snap = current_snapshot(db, plan_version)
    return explain_student(snap, student_id)


def freeze_semester(
    db: Session, *, plan_version: str, freeze_id: str
) -> tuple[Snapshot, bool]:
    """执行确定性的业务处理。"""
    plan = _require_plan(db, plan_version)
    existing = get_freeze(db, plan_version, freeze_id)
    if existing is not None:
        return Snapshot.from_dict(existing.snapshot), False

    specs, meta = _rule_maps(db, plan_version)
    cutoff = max_event_id(db, plan_version)
    events = load_events(db, plan_version)
    snap = build_snapshot(
        events,
        plan_version=plan_version,
        timezone_name=plan.iana_timezone,
        required_seconds=plan.required_seconds,
        freeze_id=freeze_id,
        event_cutoff_id=cutoff,
        rules=specs,
        rule_meta=meta,
    )
    row = insert_freeze(
        db,
        plan_version=plan_version,
        freeze_id=freeze_id,
        snapshot=snap.to_dict(),
        event_cutoff_id=cutoff,
    )
    if row is None:
        existing = get_freeze(db, plan_version, freeze_id)
        assert existing is not None
        return Snapshot.from_dict(existing.snapshot), False
    return snap, True


def get_frozen_snapshot(
    db: Session, plan_version: str, freeze_id: str
) -> Snapshot:
    _require_plan(db, plan_version)
    row = get_freeze(db, plan_version, freeze_id)
    if row is None:
        raise FreezeNotFoundError(
            f"freeze '{freeze_id}' for plan '{plan_version}' does not exist"
        )
    return Snapshot.from_dict(row.snapshot)


def explain_frozen_student(
    db: Session, plan_version: str, freeze_id: str, student_id: str
) -> dict[str, Any] | None:
    snap = get_frozen_snapshot(db, plan_version, freeze_id)
    return explain_student(snap, student_id)


def diff_freezes(
    db: Session, plan_version: str, old_freeze_id: str, new_freeze_id: str
) -> dict[str, Any]:
    old = get_frozen_snapshot(db, plan_version, old_freeze_id)
    new = get_frozen_snapshot(db, plan_version, new_freeze_id)
    return diff_snapshots(old, new)


# ---------------------------------------------------------------------------
# 规则版本生命周期：草拟 → 双人审核 → 定时生效 → （紧急撤回）
# ---------------------------------------------------------------------------


def _rule_out(db: Session, row: RuleVersion) -> dict[str, Any]:
    approvals = list_approvals(db, row.plan_version, row.rule_id)
    return {
        "plan_version": row.plan_version,
        "rule_id": row.rule_id,
        "state": row.state,
        "params": spec_to_params(spec_from_params(row.params)),
        "effective_from": _iso(row.effective_from),
        "withdrawn_at": _iso(row.withdrawn_at),
        "created_by": row.created_by,
        "version": row.version,
        "approvals": [a.approver_id for a in approvals],
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def _baseline_out(plan_version: str) -> dict[str, Any]:
    return {
        "plan_version": plan_version,
        "rule_id": BASELINE_RULE_ID,
        "state": "baseline",
        "params": spec_to_params(BASELINE_RULE),
        "effective_from": None,
        "withdrawn_at": None,
        "created_by": "system",
        "version": 0,
        "approvals": [],
        "created_at": None,
        "updated_at": None,
    }


def create_rule(
    db: Session,
    *,
    plan_version: str,
    rule_id: str,
    params: dict[str, Any],
    actor_id: str,
    reason: str = "",
) -> dict[str, Any]:
    _require_plan(db, plan_version)
    rule_id = rule_id.strip()
    actor = actor_id.strip()
    if not rule_id or not actor:
        raise RuleValidationError("规则标识与操作人不能为空")
    if rule_id == BASELINE_RULE_ID:
        raise RuleValidationError(f"'{BASELINE_RULE_ID}' 为保留标识")
    spec = _validate_params(params)
    row = insert_rule(
        db,
        plan_version=plan_version,
        rule_id=rule_id,
        params=spec_to_params(spec),
        created_by=actor,
    )
    if row is None:
        raise RuleConflictError(f"rule '{rule_id}' already exists")
    add_rule_audit(
        db,
        plan_version=plan_version,
        rule_id=rule_id,
        action="create",
        actor_id=actor,
        reason=reason,
        detail={"params": spec_to_params(spec)},
    )
    db.commit()
    return _rule_out(db, get_rule(db, plan_version, rule_id))


def list_rule_versions(db: Session, plan_version: str) -> dict[str, Any]:
    _require_plan(db, plan_version)
    return {"rules": [_rule_out(db, row) for row in list_rules(db, plan_version)]}


def get_rule_detail(
    db: Session, plan_version: str, rule_id: str
) -> dict[str, Any]:
    _require_plan(db, plan_version)
    if rule_id == BASELINE_RULE_ID:
        out = _baseline_out(plan_version)
        out["audit"] = []
        return out
    row = _require_rule(db, plan_version, rule_id)
    out = _rule_out(db, row)
    out["audit"] = [
        {
            "action": entry.action,
            "actor_id": entry.actor_id,
            "reason": entry.reason,
            "detail": dict(entry.detail),
            "created_at": _iso(entry.created_at),
        }
        for entry in list_rule_audits(db, plan_version, rule_id)
    ]
    return out


def patch_rule(
    db: Session,
    *,
    plan_version: str,
    rule_id: str,
    params: dict[str, Any],
    actor_id: str,
    reason: str,
) -> dict[str, Any]:
    _require_plan(db, plan_version)
    _require_rule(db, plan_version, rule_id)
    actor = actor_id.strip()
    if not actor or not reason.strip():
        raise RuleValidationError("修改规则必须记录操作人和原因")
    spec = _validate_params(params)
    ok = transition_rule(
        db,
        plan_version,
        rule_id,
        from_states=("draft",),
        to_state="draft",
        sets={"params": spec_to_params(spec)},
    )
    if not ok:
        db.rollback()
        raise RuleStateError("仅草稿状态的规则可修改参数")
    add_rule_audit(
        db,
        plan_version=plan_version,
        rule_id=rule_id,
        action="patch",
        actor_id=actor,
        reason=reason,
        detail={"params": spec_to_params(spec)},
    )
    db.commit()
    return _rule_out(db, get_rule(db, plan_version, rule_id))


def submit_rule(
    db: Session, *, plan_version: str, rule_id: str, actor_id: str
) -> dict[str, Any]:
    _require_plan(db, plan_version)
    _require_rule(db, plan_version, rule_id)
    actor = actor_id.strip()
    if not actor:
        raise RuleValidationError("操作人不能为空")
    ok = transition_rule(
        db, plan_version, rule_id, from_states=("draft",), to_state="in_review"
    )
    if not ok:
        current = get_rule(db, plan_version, rule_id)
        db.rollback()
        if current.state == "in_review":
            return _rule_out(db, current)
        raise RuleStateError("仅草稿状态的规则可提交审核")
    add_rule_audit(
        db,
        plan_version=plan_version,
        rule_id=rule_id,
        action="submit",
        actor_id=actor,
    )
    db.commit()
    return _rule_out(db, get_rule(db, plan_version, rule_id))


def approve_rule(
    db: Session,
    *,
    plan_version: str,
    rule_id: str,
    approver_id: str,
    reason: str = "",
) -> dict[str, Any]:
    _require_plan(db, plan_version)
    row = _require_rule(db, plan_version, rule_id)
    approver = approver_id.strip()
    if not approver:
        raise RuleValidationError("审核人不能为空")
    if row.state == "approved":
        return _rule_out(db, row)
    if row.state != "in_review":
        raise RuleStateError("仅审核中的规则可审批")
    if approver == row.created_by:
        raise RuleStateError("拟稿人不能审核自己创建的规则")

    added = add_approval(
        db, plan_version=plan_version, rule_id=rule_id, approver_id=approver
    )
    if added:
        add_rule_audit(
            db,
            plan_version=plan_version,
            rule_id=rule_id,
            action="approve",
            actor_id=approver,
            reason=reason,
        )
    # 双人审核：集齐两名不同审核人后自动进入已审批状态。
    if len(list_approvals(db, plan_version, rule_id)) >= 2:
        transition_rule(
            db,
            plan_version,
            rule_id,
            from_states=("in_review",),
            to_state="approved",
        )
    db.commit()
    return _rule_out(db, get_rule(db, plan_version, rule_id))


def schedule_rule(
    db: Session,
    *,
    plan_version: str,
    rule_id: str,
    effective_from: datetime,
    actor_id: str,
    reason: str = "",
    now: datetime | None = None,
) -> tuple[dict[str, Any], bool]:
    """发布（定时生效）。返回 (规则视图, 本次调用是否完成了状态迁移)。

    并发或重试发布同一规则到同一生效时刻时，只有一个调用迁移状态，
    其余调用幂等地返回当前状态（False）；生效时刻冲突则抛 RuleConflictError。
    """
    _require_plan(db, plan_version)
    _require_rule(db, plan_version, rule_id)
    actor = actor_id.strip()
    if not actor:
        raise RuleValidationError("操作人不能为空")
    if effective_from.tzinfo is None:
        raise RuleValidationError("生效时间必须包含时区")
    instant = _now(now)
    effective_utc = effective_from.astimezone(timezone.utc)
    if effective_utc < instant:
        raise RuleValidationError("生效时间不能早于当前时间，规则不允许追溯生效")

    try:
        ok = transition_rule(
            db,
            plan_version,
            rule_id,
            from_states=("approved",),
            to_state="scheduled",
            sets={"effective_from": effective_utc},
        )
        if not ok:
            db.rollback()
        else:
            add_rule_audit(
                db,
                plan_version=plan_version,
                rule_id=rule_id,
                action="schedule",
                actor_id=actor,
                reason=reason,
                detail={"effective_from": _iso(effective_utc)},
            )
            db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise RuleConflictError("该生效时刻已被其他已发布规则占用") from exc

    if not ok:
        current = get_rule(db, plan_version, rule_id)
        if (
            current.state in ("scheduled", "active")
            and current.effective_from is not None
            and ensure_utc(current.effective_from) == effective_utc
        ):
            return _rule_out(db, current), False
        raise RuleStateError("仅已审批状态的规则可发布生效")
    return _rule_out(db, get_rule(db, plan_version, rule_id)), True


def _repin_unsettled(
    db: Session, plan_version: str, withdrawn_rule_id: str
) -> list[dict[str, Any]]:
    """撤回后把未结算事件重新固定到回退规则。

    结算边界（最近一次冻结的截止事件）之前的事件保持原固定版本，
    历史冻结快照因此不受影响；边界之后的事件按撤回后的时间线重新解析。
    """
    boundary = max_freeze_cutoff(db, plan_version)
    timeline = _timeline(db, plan_version)
    repinned: list[dict[str, Any]] = []
    while True:
        rows = load_unsettled_events_with_rule(
            db, plan_version, withdrawn_rule_id, boundary
        )
        if not rows:
            return repinned
        updated = 0
        for row in rows:
            occurrence = _stored_occurrence(row.event_type, row.payload, row.created_at)
            new_rule_id = pick_applicable(timeline, occurrence)
            if update_event_rule(
                db, row.id, expected_rule_id=withdrawn_rule_id, new_rule_id=new_rule_id
            ):
                updated += 1
                repinned.append(
                    {
                        "event_id": row.event_id,
                        "from_rule_id": withdrawn_rule_id,
                        "to_rule_id": display_rule_id(new_rule_id),
                    }
                )
        if updated == 0:
            return repinned


def withdraw_rule(
    db: Session,
    *,
    plan_version: str,
    rule_id: str,
    actor_id: str,
    reason: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    _require_plan(db, plan_version)
    _require_rule(db, plan_version, rule_id)
    actor = actor_id.strip()
    if not actor or not reason.strip():
        raise RuleValidationError("紧急撤回必须记录操作人和原因")
    instant = _now(now)

    ok = transition_rule(
        db,
        plan_version,
        rule_id,
        from_states=("scheduled", "active"),
        to_state="withdrawn",
        sets={"withdrawn_at": instant},
    )
    if not ok:
        db.rollback()
        current = get_rule(db, plan_version, rule_id)
        if current.state == "withdrawn":
            return {
                "plan_version": plan_version,
                "rule_id": rule_id,
                "state": current.state,
                "withdrawn_at": _iso(current.withdrawn_at),
                "repinned": 0,
                "repinned_events": [],
            }
        raise RuleStateError("仅已发布（scheduled/active）的规则可撤回")

    repinned = _repin_unsettled(db, plan_version, rule_id)
    add_rule_audit(
        db,
        plan_version=plan_version,
        rule_id=rule_id,
        action="withdraw",
        actor_id=actor,
        reason=reason,
        detail={"repinned": len(repinned)},
    )
    db.commit()
    current = get_rule(db, plan_version, rule_id)
    return {
        "plan_version": plan_version,
        "rule_id": rule_id,
        "state": current.state,
        "withdrawn_at": _iso(current.withdrawn_at),
        "repinned": len(repinned),
        "repinned_events": repinned,
    }


def recover_scheduled_rules(db: Session, *, now: datetime | None = None) -> int:
    """把已到生效时刻的定时规则物化为 active。

    激活本是由 effective_from 派生的事实，这里将其落库，使进程重启后
    无需任何外部调度器即可恢复时间线推进。
    """
    instant = _now(now)
    promoted = 0
    for row in due_scheduled_rules(db):
        if ensure_utc(row.effective_from) <= instant:
            ok = transition_rule(
                db,
                row.plan_version,
                row.rule_id,
                from_states=("scheduled",),
                to_state="active",
            )
            if ok:
                add_rule_audit(
                    db,
                    plan_version=row.plan_version,
                    rule_id=row.rule_id,
                    action="activate",
                    actor_id="system",
                    detail={"effective_from": _iso(row.effective_from)},
                )
                promoted += 1
    db.commit()
    return promoted


# ---------------------------------------------------------------------------
# 规则差异、影响预览、回滚、适用查询
# ---------------------------------------------------------------------------


def _spec_for(db: Session, plan_version: str, rule_id: str) -> RuleSpec:
    if rule_id == BASELINE_RULE_ID:
        return BASELINE_RULE
    row = _require_rule(db, plan_version, rule_id)
    return spec_from_params(row.params)


def diff_rules(
    db: Session, plan_version: str, left_rule_id: str, right_rule_id: str
) -> dict[str, Any]:
    _require_plan(db, plan_version)
    left = _spec_for(db, plan_version, left_rule_id)
    right = _spec_for(db, plan_version, right_rule_id)
    return {
        "plan_version": plan_version,
        "left_rule_id": left_rule_id,
        "right_rule_id": right_rule_id,
        "changes": diff_params(left, right),
    }


def preview_rule(
    db: Session,
    *,
    plan_version: str,
    rule_id: str,
    effective_from: datetime | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """影响预览：假设该规则自 effective_from 起生效，重算未固定事件并对比。"""
    plan = _require_plan(db, plan_version)
    candidate_id: str | None
    if rule_id == BASELINE_RULE_ID:
        candidate_id = None
    else:
        _require_rule(db, plan_version, rule_id)
        candidate_id = rule_id
    boundary = (
        ensure_utc(effective_from) if effective_from is not None else _now(now)
    )

    specs, meta = _rule_maps(db, plan_version)
    events = load_events(db, plan_version)
    current = build_snapshot(
        events,
        plan_version=plan_version,
        timezone_name=plan.iana_timezone,
        required_seconds=plan.required_seconds,
        rules=specs,
        rule_meta=meta,
    )

    shifted: list[CoreEvent] = []
    repinned = 0
    for event in events:
        occurrence = _stored_occurrence(
            event.event_type.value, event.payload, event.created_at
        )
        if occurrence >= boundary and event.rule_id != candidate_id:
            shifted.append(replace(event, rule_id=candidate_id))
            repinned += 1
        else:
            shifted.append(event)
    projected = build_snapshot(
        shifted,
        plan_version=plan_version,
        timezone_name=plan.iana_timezone,
        required_seconds=plan.required_seconds,
        rules=specs,
        rule_meta=meta,
    )
    diff = diff_snapshots(current, projected)
    return {
        "plan_version": plan_version,
        "rule_id": display_rule_id(candidate_id),
        "effective_from": _iso(boundary),
        "events_repinned": repinned,
        "students_affected": diff["students_affected"],
        "student_changes": diff["student_changes"],
    }


def rollback_rule(
    db: Session,
    *,
    plan_version: str,
    rule_id: str,
    new_rule_id: str,
    actor_id: str,
    reason: str = "",
) -> dict[str, Any]:
    """回滚：以历史版本（或基线）的口径创建新草稿，走正常审核发布流程。"""
    _require_plan(db, plan_version)
    if rule_id == BASELINE_RULE_ID:
        params = spec_to_params(BASELINE_RULE)
    else:
        source = _require_rule(db, plan_version, rule_id)
        params = dict(source.params)
    new_rule_id = new_rule_id.strip()
    actor = actor_id.strip()
    if not new_rule_id or not actor:
        raise RuleValidationError("规则标识与操作人不能为空")
    if new_rule_id == BASELINE_RULE_ID:
        raise RuleValidationError(f"'{BASELINE_RULE_ID}' 为保留标识")
    row = insert_rule(
        db,
        plan_version=plan_version,
        rule_id=new_rule_id,
        params=params,
        created_by=actor,
    )
    if row is None:
        raise RuleConflictError(f"rule '{new_rule_id}' already exists")
    add_rule_audit(
        db,
        plan_version=plan_version,
        rule_id=new_rule_id,
        action="rollback",
        actor_id=actor,
        reason=reason,
        detail={"source_rule_id": rule_id},
    )
    db.commit()
    return _rule_out(db, get_rule(db, plan_version, new_rule_id))


def applicable_rule(
    db: Session, plan_version: str, at: datetime
) -> dict[str, Any]:
    _require_plan(db, plan_version)
    if at.tzinfo is None:
        raise RuleValidationError("查询时间必须包含时区")
    rule_id = pick_applicable(_timeline(db, plan_version), at)
    spec = (
        BASELINE_RULE
        if rule_id is None
        else spec_from_params(get_rule(db, plan_version, rule_id).params)
    )
    return {
        "plan_version": plan_version,
        "at": _iso(at),
        "rule_id": display_rule_id(rule_id),
        "params": spec_to_params(spec),
    }
