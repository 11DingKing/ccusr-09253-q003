"""服务端业务模块。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from .core.replay import Event as CoreEvent
from .core.replay import EventType
from .models import Event as EventModel
from .models import Freeze, Plan, RuleApproval, RuleAudit, RuleVersion


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def get_plan(db: Session, plan_version: str) -> Plan | None:
    return db.get(Plan, plan_version)


def upsert_plan(
    db: Session,
    *,
    plan_version: str,
    iana_timezone: str,
    required_seconds: int,
) -> Plan:
    stmt = sqlite_insert(Plan).values(
        plan_version=plan_version,
        iana_timezone=iana_timezone,
        required_seconds=required_seconds,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["plan_version"],
        set_={
            "iana_timezone": iana_timezone,
            "required_seconds": required_seconds,
        },
    )
    db.execute(stmt)
    db.commit()
    plan = db.get(Plan, plan_version)
    assert plan is not None
    return plan


def _to_core_event(row: EventModel) -> CoreEvent:
    return CoreEvent(
        event_id=row.event_id,
        plan_version=row.plan_version,
        event_type=EventType(row.event_type),
        student_id=row.student_id,
        payload=dict(row.payload),
        created_at=row.created_at,
        rule_id=row.rule_id,
    )


def insert_events(
    db: Session,
    *,
    plan_version: str,
    events: list[dict[str, Any]],
) -> tuple[list[str], list[str]]:
    """执行确定性的业务处理。"""
    accepted: list[str] = []
    duplicates: list[str] = []
    for e in events:
        stmt = sqlite_insert(EventModel).values(
            event_id=e["event_id"],
            plan_version=plan_version,
            student_id=e["student_id"],
            event_type=e["event_type"],
            payload=e["payload"],
            rule_id=e.get("rule_id"),
        )
        stmt = stmt.on_conflict_do_nothing(
            index_elements=["event_id", "plan_version"]
        ).returning(EventModel.id)
        inserted_id = db.execute(stmt).scalar_one_or_none()
        if inserted_id is not None:
            accepted.append(e["event_id"])
        else:
            duplicates.append(e["event_id"])
    db.commit()
    return accepted, duplicates


def load_events(db: Session, plan_version: str) -> list[CoreEvent]:
    stmt = select(EventModel).where(EventModel.plan_version == plan_version)
    rows = db.execute(stmt).scalars().all()
    return [_to_core_event(r) for r in rows]


def load_events_up_to(
    db: Session, plan_version: str, max_event_id: str
) -> list[CoreEvent]:
    """执行确定性的业务处理。"""
    stmt = (
        select(EventModel)
        .where(EventModel.plan_version == plan_version)
        .where(EventModel.event_id <= max_event_id)
    )
    rows = db.execute(stmt).scalars().all()
    return [_to_core_event(r) for r in rows]


def max_event_id(db: Session, plan_version: str) -> str | None:
    stmt = (
        select(EventModel.event_id)
        .where(EventModel.plan_version == plan_version)
        .order_by(EventModel.event_id.desc())
        .limit(1)
    )
    return db.execute(stmt).scalar_one_or_none()


def load_unsettled_events_with_rule(
    db: Session, plan_version: str, rule_id: str, settled_boundary: str | None
) -> list[EventModel]:
    """尚未被任何冻结覆盖、且固定到指定规则版本的事件。"""
    stmt = (
        select(EventModel)
        .where(EventModel.plan_version == plan_version)
        .where(EventModel.rule_id == rule_id)
    )
    if settled_boundary is not None:
        stmt = stmt.where(EventModel.event_id > settled_boundary)
    rows = db.execute(stmt).scalars().all()
    return list(rows)


def update_event_rule(
    db: Session, event_pk: int, *, expected_rule_id: str, new_rule_id: str | None
) -> bool:
    """条件更新事件的固定规则，避免与并发重定版互相覆盖。"""
    stmt = (
        update(EventModel)
        .where(EventModel.id == event_pk)
        .where(EventModel.rule_id == expected_rule_id)
        .values(rule_id=new_rule_id)
    )
    result = db.execute(stmt)
    return result.rowcount == 1


def get_freeze(
    db: Session, plan_version: str, freeze_id: str
) -> Freeze | None:
    return db.get(Freeze, (plan_version, freeze_id))


def insert_freeze(
    db: Session,
    *,
    plan_version: str,
    freeze_id: str,
    snapshot: dict[str, Any],
    event_cutoff_id: str | None,
) -> Freeze | None:
    """执行确定性的业务处理。"""
    stmt = sqlite_insert(Freeze).values(
        plan_version=plan_version,
        freeze_id=freeze_id,
        snapshot=snapshot,
        event_cutoff_id=event_cutoff_id,
    )
    stmt = stmt.on_conflict_do_nothing(
        index_elements=["plan_version", "freeze_id"]
    ).returning(Freeze.plan_version)
    inserted = db.execute(stmt).scalar_one_or_none()
    db.commit()
    if inserted is not None:
        return db.get(Freeze, (plan_version, freeze_id))
    return None


def max_freeze_cutoff(db: Session, plan_version: str) -> str | None:
    """该方案已被冻结覆盖的最大事件截止点（结算边界）。"""
    stmt = (
        select(Freeze.event_cutoff_id)
        .where(Freeze.plan_version == plan_version)
        .where(Freeze.event_cutoff_id.is_not(None))
        .order_by(Freeze.event_cutoff_id.desc())
        .limit(1)
    )
    return db.execute(stmt).scalar_one_or_none()


# ---------------------------------------------------------------------------
# 规则版本
# ---------------------------------------------------------------------------


def insert_rule(
    db: Session,
    *,
    plan_version: str,
    rule_id: str,
    params: dict[str, Any],
    created_by: str,
) -> RuleVersion | None:
    stmt = sqlite_insert(RuleVersion).values(
        plan_version=plan_version,
        rule_id=rule_id,
        state="draft",
        params=params,
        created_by=created_by,
    )
    stmt = stmt.on_conflict_do_nothing(
        index_elements=["plan_version", "rule_id"]
    ).returning(RuleVersion.rule_id)
    inserted = db.execute(stmt).scalar_one_or_none()
    if inserted is None:
        return None
    return get_rule(db, plan_version, rule_id)


def get_rule(db: Session, plan_version: str, rule_id: str) -> RuleVersion | None:
    return db.get(RuleVersion, (plan_version, rule_id))


def list_rules(db: Session, plan_version: str) -> list[RuleVersion]:
    stmt = (
        select(RuleVersion)
        .where(RuleVersion.plan_version == plan_version)
        .order_by(RuleVersion.rule_id)
    )
    return list(db.execute(stmt).scalars().all())


def transition_rule(
    db: Session,
    plan_version: str,
    rule_id: str,
    *,
    from_states: Iterable[str],
    to_state: str,
    sets: dict[str, Any] | None = None,
) -> bool:
    """条件状态迁移：仅当当前状态属于 from_states 时生效。

    并发发布同一规则时，SQLite 串行化写事务保证只有一个请求看到
    rowcount == 1，其余请求据此判定冲突。
    """
    values = {
        "state": to_state,
        "version": RuleVersion.version + 1,
        "updated_at": _utcnow(),
    }
    if sets:
        values.update(sets)
    stmt = (
        update(RuleVersion)
        .where(RuleVersion.plan_version == plan_version)
        .where(RuleVersion.rule_id == rule_id)
        .where(RuleVersion.state.in_(tuple(from_states)))
        .values(**values)
    )
    result = db.execute(stmt)
    return result.rowcount == 1


def published_rules(db: Session, plan_version: str) -> list[RuleVersion]:
    """已发布且未撤回的规则（scheduled/active），构成生效时间线。"""
    stmt = (
        select(RuleVersion)
        .where(RuleVersion.plan_version == plan_version)
        .where(RuleVersion.state.in_(("scheduled", "active")))
        .where(RuleVersion.effective_from.is_not(None))
    )
    return list(db.execute(stmt).scalars().all())


def due_scheduled_rules(db: Session) -> list[RuleVersion]:
    """全部方案中等待激活的定时规则；按生效时刻在 Python 侧过滤。"""
    stmt = (
        select(RuleVersion)
        .where(RuleVersion.state == "scheduled")
        .where(RuleVersion.effective_from.is_not(None))
    )
    return list(db.execute(stmt).scalars().all())


def add_approval(
    db: Session, *, plan_version: str, rule_id: str, approver_id: str
) -> bool:
    stmt = sqlite_insert(RuleApproval).values(
        plan_version=plan_version,
        rule_id=rule_id,
        approver_id=approver_id,
    )
    stmt = stmt.on_conflict_do_nothing(
        index_elements=["plan_version", "rule_id", "approver_id"]
    ).returning(RuleApproval.approver_id)
    return db.execute(stmt).scalar_one_or_none() is not None


def list_approvals(db: Session, plan_version: str, rule_id: str) -> list[RuleApproval]:
    stmt = (
        select(RuleApproval)
        .where(RuleApproval.plan_version == plan_version)
        .where(RuleApproval.rule_id == rule_id)
        .order_by(RuleApproval.approver_id)
    )
    return list(db.execute(stmt).scalars().all())


def add_rule_audit(
    db: Session,
    *,
    plan_version: str,
    rule_id: str,
    action: str,
    actor_id: str,
    reason: str = "",
    detail: dict[str, Any] | None = None,
) -> None:
    db.add(
        RuleAudit(
            plan_version=plan_version,
            rule_id=rule_id,
            action=action,
            actor_id=actor_id,
            reason=reason,
            detail=detail or {},
        )
    )


def list_rule_audits(
    db: Session, plan_version: str, rule_id: str
) -> list[RuleAudit]:
    stmt = (
        select(RuleAudit)
        .where(RuleAudit.plan_version == plan_version)
        .where(RuleAudit.rule_id == rule_id)
        .order_by(RuleAudit.id)
    )
    return list(db.execute(stmt).scalars().all())
