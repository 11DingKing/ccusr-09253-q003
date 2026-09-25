"""服务端业务模块。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from . import rule_service, services
from .core.rules import RuleResolutionError, RuleSpec
from .db import get_db
from .schemas import (
    DiffOut,
    EventBatchIn,
    FreezeIn,
    ImportResult,
    PlanIn,
    PlanOut,
    RuleApproveIn,
    RuleDiffOut,
    RuleDraftIn,
    RuleImpactOut,
    RuleOut,
    RulePreviewIn,
    RuleRollbackIn,
    RuleWithdrawIn,
    RuleAuditOut,
    SnapshotOut,
    StudentProgressOut,
)

router = APIRouter(prefix="/api")


def _now() -> datetime:
    return datetime.now(timezone.utc)


@router.post("/plans", response_model=PlanOut, status_code=status.HTTP_201_CREATED)
def create_plan(body: PlanIn, db: Session = Depends(get_db)) -> Any:
    return services.ensure_plan(
        db,
        plan_version=body.plan_version,
        iana_timezone=body.iana_timezone,
        required_seconds=body.required_seconds,
    )


@router.get("/plans/{plan_version}", response_model=PlanOut)
def read_plan(plan_version: str, db: Session = Depends(get_db)) -> Any:
    plan = services.get_plan_plain(db, plan_version)
    if plan is None:
        raise HTTPException(status_code=404, detail="plan not found")
    return plan


@router.post(
    "/plans/{plan_version}/events",
    response_model=ImportResult,
    status_code=status.HTTP_201_CREATED,
)
def post_events(
    plan_version: str, body: EventBatchIn, db: Session = Depends(get_db)
) -> Any:
    try:
        return services.import_events(
            db,
            plan_version=plan_version,
            events=[e.model_dump() for e in body.events],
        )
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuleResolutionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/snapshot",
    response_model=SnapshotOut,
)
def get_snapshot(plan_version: str, db: Session = Depends(get_db)) -> Any:
    try:
        snap = services.current_snapshot(db, plan_version)
        return snap.to_dict()
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/students/{student_id}/progress",
    response_model=StudentProgressOut,
)
def get_progress(
    plan_version: str, student_id: str, db: Session = Depends(get_db)
) -> Any:
    try:
        result = services.student_progress(db, plan_version, student_id)
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="student not found")
    return result


@router.post(
    "/plans/{plan_version}/freezes/{freeze_id}",
    response_model=SnapshotOut,
    status_code=status.HTTP_201_CREATED,
)
def post_freeze(
    plan_version: str,
    freeze_id: str,
    body: FreezeIn,
    db: Session = Depends(get_db),
) -> Any:
    try:
        snap, _ = services.freeze_semester(
            db, plan_version=plan_version, freeze_id=freeze_id
        )
        return snap.to_dict()
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/freezes/{freeze_id}",
    response_model=SnapshotOut,
)
def get_freeze(
    plan_version: str, freeze_id: str, db: Session = Depends(get_db)
) -> Any:
    try:
        snap = services.get_frozen_snapshot(db, plan_version, freeze_id)
        return snap.to_dict()
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except services.FreezeNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/freezes/{freeze_id}/explain/{student_id}",
    response_model=StudentProgressOut,
)
def explain_freeze_student(
    plan_version: str,
    freeze_id: str,
    student_id: str,
    db: Session = Depends(get_db),
) -> Any:
    try:
        result = services.explain_frozen_student(
            db, plan_version, freeze_id, student_id
        )
    except (services.PlanNotFoundError, services.FreezeNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="student not found")
    return result


@router.get(
    "/plans/{plan_version}/freezes/{freeze_id}/diff/{other_freeze_id}",
    response_model=DiffOut,
)
def get_diff(
    plan_version: str,
    freeze_id: str,
    other_freeze_id: str,
    db: Session = Depends(get_db),
) -> Any:
    try:
        return services.diff_freezes(
            db, plan_version, freeze_id, other_freeze_id
        )
    except (services.PlanNotFoundError, services.FreezeNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# ============================================================ 规则版本


def _rule_spec(body: RuleDraftIn | RulePreviewIn) -> RuleSpec:
    return RuleSpec(
        required_seconds=body.required_seconds,
        seconds_per_lesson_unit=body.seconds_per_lesson_unit,
        daily_cap_seconds=body.daily_cap_seconds,
        internship_requires_confirmation=body.internship_requires_confirmation,
    )


@router.post(
    "/plans/{plan_version}/rules",
    response_model=RuleOut,
    status_code=status.HTTP_201_CREATED,
)
def create_rule_draft(
    plan_version: str, body: RuleDraftIn, db: Session = Depends(get_db)
) -> Any:
    try:
        return rule_service.create_draft(
            db,
            plan_version=plan_version,
            spec=_rule_spec(body),
            created_by=body.created_by,
            effective_at=body.effective_at,
            rule_version_id=body.rule_version_id,
        )
    except rule_service.RuleNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (
        rule_service.RuleValidationError,
        rule_service.RuleConflictError,
    ) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post(
    "/plans/{plan_version}/rules/{rule_version_id}/approve",
    response_model=RuleOut,
)
def approve_rule(
    plan_version: str,
    rule_version_id: str,
    body: RuleApproveIn,
    db: Session = Depends(get_db),
) -> Any:
    _require_rule_in_plan(db, plan_version, rule_version_id)
    try:
        return rule_service.approve(
            db,
            rule_version_id=rule_version_id,
            approver_id=body.approver_id,
            note=body.note,
            now=_now(),
        )
    except rule_service.RuleNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except rule_service.ApprovalError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except rule_service.RuleStateConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post(
    "/plans/{plan_version}/rules/{rule_version_id}/withdraw",
    response_model=RuleOut,
)
def withdraw_rule(
    plan_version: str,
    rule_version_id: str,
    body: RuleWithdrawIn,
    db: Session = Depends(get_db),
) -> Any:
    _require_rule_in_plan(db, plan_version, rule_version_id)
    try:
        return rule_service.withdraw(
            db,
            rule_version_id=rule_version_id,
            actor_id=body.actor_id,
            reason=body.reason,
            now=_now(),
        )
    except rule_service.RuleNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except rule_service.RuleValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except rule_service.RuleStateConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post(
    "/plans/{plan_version}/rules/{rule_version_id}/rollback",
    response_model=RuleOut,
)
def rollback_rule(
    plan_version: str,
    rule_version_id: str,
    body: RuleRollbackIn,
    db: Session = Depends(get_db),
) -> Any:
    _require_rule_in_plan(db, plan_version, rule_version_id)
    try:
        return rule_service.rollback(
            db,
            target_rule_version_id=rule_version_id,
            actor_id=body.actor_id,
            reason=body.reason,
            now=_now(),
            new_rule_version_id=body.new_rule_version_id,
        )
    except rule_service.RuleNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except rule_service.RuleValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (
        rule_service.RuleStateConflictError,
        rule_service.RuleConflictError,
    ) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/rules",
    response_model=list[RuleOut],
)
def list_rules(plan_version: str, db: Session = Depends(get_db)) -> Any:
    return rule_service.list_rules(db, plan_version)


def _require_rule_in_plan(db: Session, plan_version: str, rule_version_id: str) -> dict[str, Any]:
    try:
        result = rule_service.get_rule(db, rule_version_id)
    except rule_service.RuleNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if result["plan_version"] != plan_version:
        raise HTTPException(status_code=404, detail="rule version not found in plan")
    return result


@router.get(
    "/plans/{plan_version}/rules/{rule_version_id}",
    response_model=RuleOut,
)
def get_rule(
    plan_version: str, rule_version_id: str, db: Session = Depends(get_db)
) -> Any:
    return _require_rule_in_plan(db, plan_version, rule_version_id)


@router.get(
    "/plans/{plan_version}/rules/{rule_version_id}/audit",
    response_model=list[RuleAuditOut],
)
def get_rule_audit(
    plan_version: str, rule_version_id: str, db: Session = Depends(get_db)
) -> Any:
    _require_rule_in_plan(db, plan_version, rule_version_id)
    return rule_service.get_audit_trail(db, rule_version_id)


@router.get(
    "/plans/{plan_version}/rules/{rule_version_id}/approvals",
    response_model=list[dict[str, Any]],
)
def get_rule_approvals(
    plan_version: str, rule_version_id: str, db: Session = Depends(get_db)
) -> Any:
    _require_rule_in_plan(db, plan_version, rule_version_id)
    return rule_service.get_approvals(db, rule_version_id)


@router.get(
    "/plans/{plan_version}/rules/{left_id}/diff/{right_id}",
    response_model=RuleDiffOut,
)
def diff_rules(
    plan_version: str,
    left_id: str,
    right_id: str,
    db: Session = Depends(get_db),
) -> Any:
    try:
        return rule_service.diff_rules(db, plan_version, left_id, right_id)
    except rule_service.RuleNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/plans/{plan_version}/rules-preview",
    response_model=RuleImpactOut,
)
def preview_rule_impact(
    plan_version: str, body: RulePreviewIn, db: Session = Depends(get_db)
) -> Any:
    try:
        return rule_service.preview_impact(
            db,
            plan_version=plan_version,
            candidate_spec=_rule_spec(body),
            effective_at=body.effective_at,
        )
    except (
        services.PlanNotFoundError,
        rule_service.RuleNotFoundError,
    ) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except rule_service.RuleValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
