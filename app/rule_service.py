"""规则版本生命周期与影响分析服务。

生命周期：

    draft --第1人审批--> pending_approval --第2人审批--> scheduled/active
    draft/pending_approval/scheduled/active --紧急撤回--> withdrawn
    active --新版本生效--> superseded（窗口截断到新版本生效时刻）
    回滚 = 取历史版本口径立即生成一个新的 active 版本（rollback_of 留痕）

所有状态跳转都在单事务内以条件 UPDATE 完成；并发发布时，"每个培养方案
至多一个 active"的部分唯一索引保证只有一方提交成功，另一方收到 409。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .core.replay import Event as CoreEvent
from .core.replay import event_occurred_at
from .core.rules import (
    EffectiveRule,
    RuleSpec,
    RuleValidationError,
    coerce_utc,
    diff_specs,
    resolve_rule_version,
    utc,
)
from .core.snapshot import Snapshot, build_snapshot, diff_snapshots
from . import rule_repository as rr
from .repository import get_plan, load_events
from .rule_repository import RuleConflictError

REQUIRED_APPROVALS = 2


class RuleStateConflictError(Exception):
    """状态机不允许该跳转（含并发竞争落败）。"""


class RuleNotFoundError(Exception):
    pass


class ApprovalError(Exception):
    pass


# ---------------------------------------------------------------- 序列化

def _iso(value: datetime | None) -> str | None:
    return coerce_utc(value).isoformat().replace("+00:00", "Z") if value is not None else None


def rule_to_dict(row, *, approval_count: int | None = None) -> dict[str, Any]:
    data = {
        "rule_version_id": row.rule_version_id,
        "plan_version": row.plan_version,
        "state": row.state,
        "spec": dict(row.spec),
        "created_by": row.created_by,
        "effective_at": _iso(row.effective_at),
        "end_at": _iso(row.end_at),
        "activated_at": _iso(row.activated_at),
        "superseded_by": row.superseded_by,
        "rollback_of": row.rollback_of,
        "revision": row.revision,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }
    if approval_count is not None:
        data["approval_count"] = approval_count
    return data


def _catalog(rows) -> list[dict[str, Any]]:
    """快照内嵌的规则目录：历史快照据此自解释每条规则来源。"""
    return [
        {
            "rule_version_id": r.rule_version_id,
            "state": r.state,
            "spec": dict(r.spec),
            "created_by": r.created_by,
            "effective_at": _iso(r.effective_at),
            "end_at": _iso(r.end_at),
            "activated_at": _iso(r.activated_at),
            "superseded_by": r.superseded_by,
            "rollback_of": r.rollback_of,
        }
        for r in rows
    ]


# ---------------------------------------------------------------- 草拟

def create_draft(
    db: Session,
    *,
    plan_version: str,
    spec: RuleSpec,
    created_by: str,
    effective_at: datetime,
    rule_version_id: str | None = None,
    rollback_of: str | None = None,
) -> dict[str, Any]:
    plan = get_plan(db, plan_version)
    if plan is None:
        raise RuleNotFoundError(f"plan version '{plan_version}' is not registered")
    spec.validate()
    instant = utc(effective_at)
    author = created_by.strip()
    if not author:
        raise RuleValidationError("created_by 不能为空")
    if rollback_of is not None and rr.get_rule_version(db, rollback_of) is None:
        raise RuleNotFoundError(f"回滚目标版本 {rollback_of} 不存在")

    rule_version_id = (rule_version_id or f"RV-{uuid4().hex[:12]}").strip()
    row = rr.insert_rule_version(
        db,
        rule_version_id=rule_version_id,
        plan_version=plan_version,
        spec=spec,
        created_by=author,
        effective_at=instant,
        rollback_of=rollback_of,
    )
    rr.add_audit(
        db,
        rule_version_id=rule_version_id,
        plan_version=plan_version,
        action="created",
        actor_id=author,
        from_state=None,
        to_state="draft",
        reason="草稿创建" + (f"，回滚自 {rollback_of}" if rollback_of else ""),
        detail={"spec": spec.to_dict(), "effective_at": _iso(instant)},
    )
    return rule_to_dict(row, approval_count=0)


# ---------------------------------------------------------------- 审批

def approve(
    db: Session,
    *,
    rule_version_id: str,
    approver_id: str,
    note: str,
    now: datetime,
) -> dict[str, Any]:
    """双人审核：作者不能审批；两名不同审核人各审批一次后方可生效。"""
    instant = utc(now)
    row = rr.get_rule_version(db, rule_version_id)
    if row is None:
        raise RuleNotFoundError(f"规则版本 {rule_version_id} 不存在")
    approver = approver_id.strip()
    if not approver:
        raise ApprovalError("approver_id 不能为空")
    if approver == row.created_by:
        raise ApprovalError("起草人不能作为审核人（职责分离）")
    if row.state not in ("draft", "pending_approval"):
        raise RuleStateConflictError(
            f"状态为 {row.state} 的版本不能继续审批"
        )

    if not rr.add_approval(
        db,
        rule_version_id=rule_version_id,
        approver_id=approver,
        note=note.strip(),
        now=instant,
        commit=False,
    ):
        db.rollback()
        raise ApprovalError("同一审核人不能重复审批")

    count = rr.count_approvals(db, rule_version_id)

    if count < REQUIRED_APPROVALS:
        # 第一名审核人后进入待决状态；审批记录与状态变更同一事务提交。
        if row.state == "draft":
            updated = rr.conditional_update_state(
                db,
                rule_version_id=rule_version_id,
                expected_states=("draft",),
                to_state="pending_approval",
            )
            if not updated:
                db.rollback()
                raise RuleStateConflictError("审批并发冲突，请重试")
        rr.add_audit(
            db,
            rule_version_id=rule_version_id,
            plan_version=row.plan_version,
            action="approved",
            actor_id=approver,
            from_state=row.state,
            to_state="pending_approval",
            reason=note.strip(),
            detail={"approval_count": count},
            commit=False,
        )
        db.commit()
        db.refresh(row)
        return rule_to_dict(row, approval_count=count)

    # 第二名审核人：审批留痕与生效在同一事务内完成，失败可整体重试。
    rr.add_audit(
        db,
        rule_version_id=rule_version_id,
        plan_version=row.plan_version,
        action="approved",
        actor_id=approver,
        from_state=row.state,
        to_state=row.state,
        reason=note.strip(),
        detail={"approval_count": count},
        commit=False,
    )
    return _activate(db, row, instant, actor_id=approver)


def _activate(
    db: Session,
    row,
    now: datetime,
    *,
    actor_id: str,
) -> dict[str, Any]:
    """在单事务内激活版本：截断旧 active，再把本版本切到 active。

    定时版本按其声明的 effective_at 截断旧版本；即时生效（审批时生效时间
    已过）按 max(effective_at, now) 截断，保证窗口不回退。激活前读取
    "当前 active 版本"作为乐观条件，并发发布落败方收到冲突而不是静默产生
    零长度窗口。
    """
    effective = coerce_utc(row.effective_at)
    boundary = effective if effective > now else now
    target_state = "scheduled" if effective > now else "active"

    # 同一生效时刻不允许两个 scheduled 版本，杜绝窗口分叉。
    if target_state == "scheduled":
        others = rr.list_rule_versions(db, row.plan_version)
        clash = next(
            (
                r
                for r in others
                if r.rule_version_id != row.rule_version_id
                and r.state == "scheduled"
                and coerce_utc(r.effective_at) == effective
            ),
            None,
        )
        if clash is not None:
            db.rollback()
            raise RuleStateConflictError(
                f"生效时间与待生效版本 {clash.rule_version_id} 冲突"
            )

    try:
        if target_state == "active":
            expected = rr.active_version_id(db, row.plan_version)
            if expected is not None:
                incumbent = rr.get_rule_version(db, expected)
                incumbent_effective = coerce_utc(incumbent.effective_at)
                # 同刻竞争（当前版本生效时刻不早于本版本）：拒绝歧义，让调用方抉择。
                if incumbent_effective >= boundary:
                    db.rollback()
                    raise RuleStateConflictError(
                        "并发发布冲突：与当前生效版本的生效时刻相同或更早"
                    )
                changed = rr.supersede_active(
                    db,
                    plan_version=row.plan_version,
                    end_at=boundary,
                    successor_id=row.rule_version_id,
                    expected_active_id=expected,
                )
                if not changed:
                    db.rollback()
                    raise RuleStateConflictError(
                        "并发发布冲突：当前生效版本已被其他操作取代"
                    )
        updated = rr.conditional_update_state(
            db,
            rule_version_id=row.rule_version_id,
            expected_states=("draft", "pending_approval", "scheduled"),
            to_state=target_state,
            # 立即生效时以实际激活时刻作为窗口起点，避免与旧版本历史窗口重叠。
            effective_at=boundary if target_state == "active" else None,
            activated_at=now if target_state == "active" else None,
        )
        if not updated:
            db.rollback()
            raise RuleStateConflictError("规则状态已被其他操作改变")
        rr.add_audit(
            db,
            rule_version_id=row.rule_version_id,
            plan_version=row.plan_version,
            action="scheduled" if target_state == "scheduled" else "activated",
            actor_id=actor_id,
            from_state=row.state,
            to_state=target_state,
            reason="双人审核通过",
            detail={"effective_at": _iso(row.effective_at)},
            commit=False,
        )
        db.commit()
    except (RuleConflictError, IntegrityError) as exc:
        db.rollback()
        raise RuleStateConflictError(
            "并发发布冲突：同一生效时刻或当前生效版本已被占用"
        ) from exc
    except Exception:
        db.rollback()
        raise

    db.refresh(row)
    return rule_to_dict(
        row, approval_count=rr.count_approvals(db, row.rule_version_id)
    )


def activate_due_scheduled(db: Session, now: datetime) -> list[str]:
    """定时生效（启动恢复与守护轮询共用）。返回本次激活的版本 id。"""
    activated: list[str] = []
    for row in rr.list_scheduled_due(db, utc(now)):
        try:
            effective = coerce_utc(row.effective_at)
            expected = rr.active_version_id(db, row.plan_version)
            if expected is not None:
                active_row = rr.get_rule_version(db, expected)
                active_effective = coerce_utc(active_row.effective_at)
                if active_effective is not None and active_effective >= effective:
                    # 生效时刻之后已发布更新的口径（如紧急回滚）：定时版本作废，
                    # 压缩为空窗口，绝不回溯覆盖。
                    updated = rr.conditional_update_state(
                        db,
                        rule_version_id=row.rule_version_id,
                        expected_states=("scheduled",),
                        to_state="withdrawn",
                        end_at=effective,
                    )
                    if updated:
                        rr.add_audit(
                            db,
                            rule_version_id=row.rule_version_id,
                            plan_version=row.plan_version,
                            action="auto_withdrawn",
                            actor_id="scheduler",
                            from_state="scheduled",
                            to_state="withdrawn",
                            reason="激活时发现生效时刻之后已有更新口径，定时版本作废",
                            detail={
                                "active_rule_version_id": expected,
                                "effective_at": _iso(effective),
                            },
                            commit=False,
                        )
                        db.commit()
                    else:
                        db.rollback()
                    continue
                changed = rr.supersede_active(
                    db,
                    plan_version=row.plan_version,
                    end_at=effective,
                    successor_id=row.rule_version_id,
                    expected_active_id=expected,
                )
                if not changed:
                    # 并发发布已改变 active 版本；下个周期重新评估。
                    db.rollback()
                    continue
            updated = rr.conditional_update_state(
                db,
                rule_version_id=row.rule_version_id,
                expected_states=("scheduled",),
                to_state="active",
                activated_at=utc(now),
            )
            if updated:
                rr.add_audit(
                    db,
                    rule_version_id=row.rule_version_id,
                    plan_version=row.plan_version,
                    action="activated",
                    actor_id="scheduler",
                    from_state="scheduled",
                    to_state="active",
                    reason="到达定时生效时间",
                    detail={"effective_at": _iso(row.effective_at)},
                    commit=False,
                )
                db.commit()
                activated.append(row.rule_version_id)
            else:
                db.rollback()
        except Exception:
            db.rollback()
    return activated


# ---------------------------------------------------------------- 撤回

def withdraw(
    db: Session,
    *,
    rule_version_id: str,
    actor_id: str,
    reason: str,
    now: datetime,
) -> dict[str, Any]:
    """紧急撤回：只截断适用窗口上界，不重写任何历史绑定。"""
    instant = utc(now)
    row = rr.get_rule_version(db, rule_version_id)
    if row is None:
        raise RuleNotFoundError(f"规则版本 {rule_version_id} 不存在")
    actor = actor_id.strip()
    note = reason.strip()
    if not actor or not note:
        raise RuleValidationError("撤回必须记录操作人和原因")
    if row.state not in ("draft", "pending_approval", "scheduled", "active"):
        raise RuleStateConflictError(
            f"状态为 {row.state} 的历史版本不能撤回"
        )

    if row.state == "active":
        # 只影响撤回时刻之后尚未结算的区间。
        end_at = instant
        expected = ("active",)
    else:
        # 尚未生效：窗口压缩为空区间，永远不会解析到事件。
        end_at = coerce_utc(row.effective_at)
        expected = ("draft", "pending_approval", "scheduled")

    updated = rr.conditional_update_state(
        db,
        rule_version_id=rule_version_id,
        expected_states=expected,
        to_state="withdrawn",
        end_at=end_at,
    )
    if not updated:
        db.rollback()
        raise RuleStateConflictError("撤回并发冲突，请重试")
    rr.add_audit(
        db,
        rule_version_id=rule_version_id,
        plan_version=row.plan_version,
        action="withdrawn",
        actor_id=actor,
        from_state=row.state,
        to_state="withdrawn",
        reason=note,
        detail={"end_at": _iso(end_at)},
        commit=False,
    )
    db.commit()
    db.refresh(row)
    return rule_to_dict(
        row, approval_count=rr.count_approvals(db, rule_version_id)
    )


# ---------------------------------------------------------------- 回滚

def rollback(
    db: Session,
    *,
    target_rule_version_id: str,
    actor_id: str,
    reason: str,
    now: datetime,
    new_rule_version_id: str | None = None,
) -> dict[str, Any]:
    """回滚 = 紧急撤回当前口径（截断窗口）+ 以目标版本口径立即发布新版本。

    属于与紧急撤回同级的应急操作：直接生效并完整留痕，不再走双人审核
    （审核留痕可在事后审计中补充）。
    """
    instant = utc(now)
    target = rr.get_rule_version(db, target_rule_version_id)
    if target is None:
        raise RuleNotFoundError(f"规则版本 {target_rule_version_id} 不存在")
    actor = actor_id.strip()
    note = reason.strip()
    if not actor or not note:
        raise RuleValidationError("回滚必须记录操作人和原因")

    spec = RuleSpec.from_dict(target.spec)
    new_id = (new_rule_version_id or f"RV-{uuid4().hex[:12]}").strip()
    row = rr.insert_rule_version(
        db,
        rule_version_id=new_id,
        plan_version=target.plan_version,
        spec=spec,
        created_by=actor,
        effective_at=instant,
        rollback_of=target_rule_version_id,
    )
    try:
        expected = rr.active_version_id(db, target.plan_version)
        if expected is not None:
            incumbent = rr.get_rule_version(db, expected)
            if coerce_utc(incumbent.effective_at) >= instant:
                db.rollback()
                raise RuleStateConflictError(
                    "并发发布冲突：当前生效版本与回滚同时刻生效"
                )
            changed = rr.supersede_active(
                db,
                plan_version=target.plan_version,
                end_at=instant,
                successor_id=new_id,
                expected_active_id=expected,
            )
            if not changed:
                db.rollback()
                raise RuleStateConflictError(
                    "并发发布冲突：当前生效版本已被其他操作取代"
                )
        updated = rr.conditional_update_state(
            db,
            rule_version_id=new_id,
            expected_states=("draft",),
            to_state="active",
            activated_at=instant,
        )
        if not updated:
            db.rollback()
            raise RuleStateConflictError("回滚并发冲突，请重试")
        rr.add_audit(
            db,
            rule_version_id=new_id,
            plan_version=target.plan_version,
            action="rollback",
            actor_id=actor,
            from_state=None,
            to_state="active",
            reason=f"回滚到 {target_rule_version_id}：{note}",
            detail={
                "rollback_of": target_rule_version_id,
                "spec": spec.to_dict(),
            },
            commit=False,
        )
        db.commit()
    except (RuleConflictError, IntegrityError) as exc:
        db.rollback()
        raise RuleStateConflictError("并发发布冲突，回滚失败") from exc
    except Exception:
        db.rollback()
        raise

    db.refresh(row)
    return rule_to_dict(row, approval_count=0)


# ---------------------------------------------------------------- 查询/差异

def get_rule(db: Session, rule_version_id: str) -> dict[str, Any]:
    row = rr.get_rule_version(db, rule_version_id)
    if row is None:
        raise RuleNotFoundError(f"规则版本 {rule_version_id} 不存在")
    return rule_to_dict(
        row, approval_count=rr.count_approvals(db, rule_version_id)
    )


def list_rules(db: Session, plan_version: str) -> list[dict[str, Any]]:
    rows = rr.list_rule_versions(db, plan_version)
    return [
        rule_to_dict(r, approval_count=rr.count_approvals(db, r.rule_version_id))
        for r in rows
    ]


def get_audit_trail(db: Session, rule_version_id: str) -> list[dict[str, Any]]:
    row = rr.get_rule_version(db, rule_version_id)
    if row is None:
        raise RuleNotFoundError(f"规则版本 {rule_version_id} 不存在")
    return [
        {
            "id": a.id,
            "rule_version_id": a.rule_version_id,
            "action": a.action,
            "actor_id": a.actor_id,
            "from_state": a.from_state,
            "to_state": a.to_state,
            "reason": a.reason,
            "detail": a.detail,
            "created_at": _iso(a.created_at),
        }
        for a in rr.list_audit_logs(db, rule_version_id)
    ]


def get_approvals(db: Session, rule_version_id: str) -> list[dict[str, Any]]:
    if rr.get_rule_version(db, rule_version_id) is None:
        raise RuleNotFoundError(f"规则版本 {rule_version_id} 不存在")
    return [
        {
            "approver_id": a.approver_id,
            "note": a.note,
            "approved_at": _iso(a.approved_at),
        }
        for a in rr.list_approvals(db, rule_version_id)
    ]


def diff_rules(
    db: Session, plan_version: str, left_id: str, right_id: str
) -> dict[str, Any]:
    left = rr.get_rule_version(db, left_id)
    right = rr.get_rule_version(db, right_id)
    if left is None or right is None:
        raise RuleNotFoundError("规则版本不存在")
    if left.plan_version != plan_version or right.plan_version != plan_version:
        raise RuleNotFoundError("规则版本不属于该培养方案")
    changes = diff_specs(
        RuleSpec.from_dict(left.spec), RuleSpec.from_dict(right.spec)
    )
    return {
        "plan_version": plan_version,
        "left_rule_version_id": left_id,
        "right_rule_version_id": right_id,
        "changes": changes,
        "fields_changed": sorted(changes),
        "identical": not changes,
        "left": rule_to_dict(left),
        "right": rule_to_dict(right),
    }


# ------------------------------------------------------ 事件导入时的绑定

def bind_events(
    db: Session, plan_version: str, events: list[dict[str, Any]], now: datetime
) -> tuple[dict[str, str | None], dict[str, datetime]]:
    """按各事件业务发生时间解析当时有效的规则版本（导入时一次性固化）。"""
    rows = rr.list_rule_versions(db, plan_version)
    rules: list[EffectiveRule] = rr.effective_rules(rows)
    bindings: dict[str, str | None] = {}
    occurred: dict[str, datetime] = {}
    fallback = utc(now)
    for e in events:
        core = CoreEvent(
            event_id=e["event_id"],
            plan_version=plan_version,
            event_type=e["event_type"],
            student_id=e["student_id"],
            payload=e["payload"],
            created_at=fallback,
        )
        moment = event_occurred_at(core)
        occurred[e["event_id"]] = moment
        bindings[e["event_id"]] = resolve_rule_version(moment, rules)
    return bindings, occurred


# ------------------------------------------------------ 快照（含规则目录）

def _plan_snapshot_inputs(
    db: Session, plan
) -> tuple[list[CoreEvent], dict[str, RuleSpec], list[dict[str, Any]]]:
    events = load_events(db, plan.plan_version)
    rows = rr.list_rule_versions(db, plan.plan_version)
    specs = rr.rule_specs_map(rows)
    return events, specs, _catalog(rows)


def _effective_required_seconds(db: Session, plan) -> int:
    """达标线按当前 active 版本口径；无 active 版本时沿用培养方案注册值。

    冻结快照把该值一并固化，因此历史快照解释的是冻结当时的达标要求。
    """
    active = rr.get_active_version(db, plan.plan_version)
    if active is None:
        return plan.required_seconds
    return int(active.spec.get("required_seconds", plan.required_seconds))


def current_rule_snapshot(db: Session, plan_version: str) -> Snapshot:
    from .services import PlanNotFoundError

    plan = get_plan(db, plan_version)
    if plan is None:
        raise PlanNotFoundError(f"plan version '{plan_version}' is not registered")
    events, specs, catalog = _plan_snapshot_inputs(db, plan)
    return build_snapshot(
        events,
        plan_version=plan_version,
        timezone_name=plan.iana_timezone,
        required_seconds=_effective_required_seconds(db, plan),
        rule_specs=specs,
        rule_catalog=catalog,
    )


def freeze_rule_snapshot(
    db: Session, plan_version: str, freeze_id: str
) -> tuple[Snapshot, bool]:
    """与冻结体系对接：快照内嵌规则目录，冻结后永不改变。"""
    from .repository import get_freeze, insert_freeze, max_event_id
    from .services import PlanNotFoundError

    plan = get_plan(db, plan_version)
    if plan is None:
        raise PlanNotFoundError(f"plan version '{plan_version}' is not registered")
    existing = get_freeze(db, plan_version, freeze_id)
    if existing is not None:
        return Snapshot.from_dict(existing.snapshot), False

    cutoff = max_event_id(db, plan_version)
    events, specs, catalog = _plan_snapshot_inputs(db, plan)
    snap = build_snapshot(
        events,
        plan_version=plan_version,
        timezone_name=plan.iana_timezone,
        required_seconds=_effective_required_seconds(db, plan),
        freeze_id=freeze_id,
        event_cutoff_id=cutoff,
        rule_specs=specs,
        rule_catalog=catalog,
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


# ------------------------------------------------------ 影响预览

def preview_impact(
    db: Session,
    *,
    plan_version: str,
    candidate_spec: RuleSpec,
    effective_at: datetime,
) -> dict[str, Any]:
    """假设候选口径自 effective_at 起生效，重放并与当前快照对比。

    预览不写库：effective_at 之前的事件保留真实绑定，之后的事件改按候选
    口径重算（无论其实际绑定版本），用于发布前评估影响面。
    """
    from .services import PlanNotFoundError

    plan = get_plan(db, plan_version)
    if plan is None:
        raise PlanNotFoundError(f"plan version '{plan_version}' is not registered")
    candidate_spec.validate()
    boundary = utc(effective_at)

    events = load_events(db, plan_version)
    rows = rr.list_rule_versions(db, plan_version)
    specs = rr.rule_specs_map(rows)
    catalog = _catalog(rows)

    candidate = build_snapshot(
        events,
        plan_version=plan_version,
        timezone_name=plan.iana_timezone,
        required_seconds=candidate_spec.required_seconds,
        rule_specs=specs,
        rule_catalog=catalog,
        override=(candidate_spec, boundary),
    )
    current = build_snapshot(
        events,
        plan_version=plan_version,
        timezone_name=plan.iana_timezone,
        required_seconds=_effective_required_seconds(db, plan),
        rule_specs=specs,
        rule_catalog=catalog,
    )
    diff = diff_snapshots(current, candidate)
    return {
        "plan_version": plan_version,
        "candidate_spec": candidate_spec.to_dict(),
        "effective_at": _iso(boundary),
        "students_affected": diff["students_affected"],
        "student_changes": diff["student_changes"],
        "preview_snapshot": candidate.to_dict(),
    }
