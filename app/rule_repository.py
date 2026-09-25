"""规则版本的持久化操作。

所有状态变更都使用条件 UPDATE（乐观锁），配合 ``rule_versions`` 上的
"每个培养方案至多一个 active"部分唯一索引，保证并发发布与定时激活在
SQLite 下也只有一个事务胜出。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .core.rules import EffectiveRule, RuleSpec, coerce_utc
from .models import RuleApproval, RuleAuditLog, RuleVersion

WINDOWED_STATES = ("active", "superseded", "withdrawn")


class RuleConflictError(Exception):
    """并发状态竞争：规则版本已被其他事务修改。"""


def get_rule_version(db: Session, rule_version_id: str) -> RuleVersion | None:
    return db.get(RuleVersion, rule_version_id)


def list_rule_versions(
    db: Session, plan_version: str
) -> list[RuleVersion]:
    stmt = (
        select(RuleVersion)
        .where(RuleVersion.plan_version == plan_version)
        .order_by(RuleVersion.effective_at, RuleVersion.rule_version_id)
    )
    return list(db.execute(stmt).scalars().all())


def insert_rule_version(
    db: Session,
    *,
    rule_version_id: str,
    plan_version: str,
    spec: RuleSpec,
    created_by: str,
    effective_at: datetime,
    rollback_of: str | None = None,
) -> RuleVersion:
    row = RuleVersion(
        rule_version_id=rule_version_id,
        plan_version=plan_version,
        state="draft",
        spec=spec.to_dict(),
        created_by=created_by,
        effective_at=effective_at,
        end_at=None,
        rollback_of=rollback_of,
        revision=1,
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError as exc:  # 主键冲突等
        db.rollback()
        raise RuleConflictError(f"规则版本 {rule_version_id} 已存在") from exc
    db.refresh(row)
    return row


def add_approval(
    db: Session,
    *,
    rule_version_id: str,
    approver_id: str,
    note: str,
    now: datetime,
    commit: bool = True,
) -> bool:
    """插入审批记录；同一审核人重复审批返回 False（唯一约束兜底）。

    ``commit=False`` 时只 flush，与调用方的状态变更共用同一事务。
    """
    db.add(
        RuleApproval(
            rule_version_id=rule_version_id,
            approver_id=approver_id,
            note=note,
            approved_at=now,
        )
    )
    try:
        db.flush()
    except IntegrityError:
        if commit:
            db.rollback()
        return False
    if commit:
        db.commit()
    return True


def list_approvals(db: Session, rule_version_id: str) -> list[RuleApproval]:
    stmt = (
        select(RuleApproval)
        .where(RuleApproval.rule_version_id == rule_version_id)
        .order_by(RuleApproval.approved_at, RuleApproval.id)
    )
    return list(db.execute(stmt).scalars().all())


def count_approvals(db: Session, rule_version_id: str) -> int:
    rows = list_approvals(db, rule_version_id)
    return len({r.approver_id for r in rows})


def add_audit(
    db: Session,
    *,
    rule_version_id: str,
    plan_version: str,
    action: str,
    actor_id: str,
    from_state: str | None,
    to_state: str | None,
    reason: str,
    detail: dict[str, Any] | None = None,
    commit: bool = True,
) -> RuleAuditLog:
    row = RuleAuditLog(
        rule_version_id=rule_version_id,
        plan_version=plan_version,
        action=action,
        actor_id=actor_id,
        from_state=from_state,
        to_state=to_state,
        reason=reason,
        detail=detail,
    )
    db.add(row)
    if commit:
        db.commit()
        db.refresh(row)
    return row


def list_audit_logs(
    db: Session, rule_version_id: str
) -> list[RuleAuditLog]:
    stmt = (
        select(RuleAuditLog)
        .where(RuleAuditLog.rule_version_id == rule_version_id)
        .order_by(RuleAuditLog.id)
    )
    return list(db.execute(stmt).scalars().all())


def conditional_update_state(
    db: Session,
    *,
    rule_version_id: str,
    expected_states: Iterable[str],
    to_state: str | None = None,
    end_at: datetime | None = None,
    effective_at: datetime | None = None,
    superseded_by: str | None = None,
    activated_at: datetime | None = None,
) -> int:
    """带状态前置条件的更新，返回受影响行数（0 表示竞争失败）。"""
    values: dict[str, Any] = {"revision": RuleVersion.revision + 1}
    if to_state is not None:
        values["state"] = to_state
    if end_at is not None:
        values["end_at"] = end_at
    if effective_at is not None:
        values["effective_at"] = effective_at
    if superseded_by is not None:
        values["superseded_by"] = superseded_by
    if activated_at is not None:
        values["activated_at"] = activated_at

    stmt = (
        update(RuleVersion)
        .where(RuleVersion.rule_version_id == rule_version_id)
        .where(RuleVersion.state.in_(tuple(expected_states)))
        .values(**values)
    )
    result = db.execute(stmt)
    return result.rowcount


def supersede_active(
    db: Session,
    *,
    plan_version: str,
    end_at: datetime,
    successor_id: str,
    expected_active_id: str | None = ...,
) -> int:
    """把培养方案当前 active 版本截断到 ``end_at``，返回受影响行数。

    传入 ``expected_active_id`` 时只截断该版本（乐观并发条件）；该 id 已经
    不是 active（被并发事务抢先取代）时返回 0，调用方据此放弃激活。
    """
    stmt = (
        update(RuleVersion)
        .where(RuleVersion.plan_version == plan_version)
        .where(RuleVersion.state == "active")
        .where(RuleVersion.rule_version_id != successor_id)
        .values(
            state="superseded",
            end_at=end_at,
            superseded_by=successor_id,
            revision=RuleVersion.revision + 1,
        )
    )
    if expected_active_id is not ...:
        if expected_active_id is None:
            # 调用方认为此刻没有 active 版本：不截断任何行（由后续检查兜底）。
            return 0
        stmt = stmt.where(RuleVersion.rule_version_id == expected_active_id)
    return db.execute(stmt).rowcount


def active_version_id(db: Session, plan_version: str) -> str | None:
    stmt = select(RuleVersion.rule_version_id).where(
        RuleVersion.plan_version == plan_version,
        RuleVersion.state == "active",
    )
    return db.execute(stmt).scalar_one_or_none()


def withdraw_scheduled_versions(
    db: Session, *, plan_version: str, now: datetime
) -> list[str]:
    """撤回培养方案所有尚未生效的 scheduled 版本，返回其 id。"""
    stmt = select(RuleVersion).where(
        RuleVersion.plan_version == plan_version,
        RuleVersion.state == "scheduled",
    )
    rows = list(db.execute(stmt).scalars().all())
    ids = [r.rule_version_id for r in rows]
    for row in rows:
        row.state = "withdrawn"
        # 空窗口 [effective_at, effective_at)，永远解析不到事件。
        row.end_at = row.effective_at
        row.revision += 1
    return ids


def list_scheduled_due(db: Session, now: datetime) -> list[RuleVersion]:
    # 在 Python 侧比较，规避 SQLite 对 aware/naive DATETIME 的字符串绑定差异。
    stmt = (
        select(RuleVersion)
        .where(RuleVersion.state == "scheduled")
        .order_by(RuleVersion.effective_at, RuleVersion.rule_version_id)
    )
    rows = list(db.execute(stmt).scalars().all())
    moment = coerce_utc(now)
    return [r for r in rows if coerce_utc(r.effective_at) <= moment]


def get_active_version(
    db: Session, plan_version: str
) -> RuleVersion | None:
    stmt = select(RuleVersion).where(
        RuleVersion.plan_version == plan_version,
        RuleVersion.state == "active",
    )
    return db.execute(stmt).scalar_one_or_none()


def effective_rules(rows: Iterable[RuleVersion]) -> list[EffectiveRule]:
    """把持久化行转换为重放/解析使用的带窗口规则。"""
    result: list[EffectiveRule] = []
    for row in rows:
        if row.state not in WINDOWED_STATES:
            continue
        # 撤回的定时版本（空窗口）不可能包含任何时刻。
        if row.state == "withdrawn" and row.end_at == row.effective_at:
            continue
        result.append(
            EffectiveRule(
                rule_version_id=row.rule_version_id,
                spec=RuleSpec.from_dict(row.spec),
                effective_at=coerce_utc(row.effective_at),
                end_at=coerce_utc(row.end_at),
                state=row.state,
            )
        )
    return result


def rule_specs_map(rows: Iterable[RuleVersion]) -> dict[str, RuleSpec]:
    """事件可能绑定任何已进入过窗口的版本（含被取代/撤回），全部提供口径。"""
    return {
        row.rule_version_id: RuleSpec.from_dict(row.spec)
        for row in rows
    }
