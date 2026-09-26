"""服务端业务模块。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from . import services
from .db import get_db
from .schemas import (
    ApplicableOut,
    DiffOut,
    EventBatchIn,
    FreezeIn,
    ImportResult,
    PlanIn,
    PlanOut,
    PreviewOut,
    RuleApproveIn,
    RuleCreateIn,
    RuleDetailOut,
    RuleDiffOut,
    RuleListOut,
    RuleOut,
    RulePatchIn,
    RulePreviewIn,
    RuleRollbackIn,
    RuleScheduleIn,
    RuleSubmitIn,
    RuleWithdrawIn,
    SnapshotOut,
    StudentProgressOut,
    WithdrawOut,
)

router = APIRouter(prefix="/api")


RULE_EXCEPTIONS = (
    services.RuleNotFoundError,
    services.PlanNotFoundError,
    services.RuleStateError,
    services.RuleConflictError,
    services.RuleValidationError,
)


def _rule_errors(exc: Exception) -> HTTPException:
    if isinstance(exc, (services.RuleNotFoundError, services.PlanNotFoundError)):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, (services.RuleStateError, services.RuleConflictError)):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


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
    except services.EventValidationError as exc:
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


# ---------------------------------------------------------------------------
# 规则版本：草拟 → 双人审核 → 定时生效 → 紧急撤回
# ---------------------------------------------------------------------------


@router.post(
    "/plans/{plan_version}/rules",
    response_model=RuleOut,
    status_code=status.HTTP_201_CREATED,
)
def create_rule(
    plan_version: str, body: RuleCreateIn, db: Session = Depends(get_db)
) -> Any:
    try:
        return services.create_rule(
            db,
            plan_version=plan_version,
            rule_id=body.rule_id,
            params=body.params.model_dump(),
            actor_id=body.actor_id,
            reason=body.reason,
        )
    except RULE_EXCEPTIONS as exc:
        raise _rule_errors(exc) from exc


@router.get("/plans/{plan_version}/rules", response_model=RuleListOut)
def list_rules(plan_version: str, db: Session = Depends(get_db)) -> Any:
    try:
        return services.list_rule_versions(db, plan_version)
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/plans/{plan_version}/rules/applicable", response_model=ApplicableOut)
def get_applicable(
    plan_version: str,
    at: datetime = Query(..., description="RFC 3339 时间"),
    db: Session = Depends(get_db),
) -> Any:
    try:
        return services.applicable_rule(db, plan_version, at)
    except RULE_EXCEPTIONS as exc:
        raise _rule_errors(exc) from exc


@router.get("/plans/{plan_version}/rules/{rule_id}", response_model=RuleDetailOut)
def get_rule(
    plan_version: str, rule_id: str, db: Session = Depends(get_db)
) -> Any:
    try:
        return services.get_rule_detail(db, plan_version, rule_id)
    except RULE_EXCEPTIONS as exc:
        raise _rule_errors(exc) from exc


@router.patch("/plans/{plan_version}/rules/{rule_id}", response_model=RuleOut)
def patch_rule(
    plan_version: str,
    rule_id: str,
    body: RulePatchIn,
    db: Session = Depends(get_db),
) -> Any:
    try:
        return services.patch_rule(
            db,
            plan_version=plan_version,
            rule_id=rule_id,
            params=body.params.model_dump(),
            actor_id=body.actor_id,
            reason=body.reason,
        )
    except RULE_EXCEPTIONS as exc:
        raise _rule_errors(exc) from exc


@router.post("/plans/{plan_version}/rules/{rule_id}/submit", response_model=RuleOut)
def submit_rule(
    plan_version: str,
    rule_id: str,
    body: RuleSubmitIn,
    db: Session = Depends(get_db),
) -> Any:
    try:
        return services.submit_rule(
            db,
            plan_version=plan_version,
            rule_id=rule_id,
            actor_id=body.actor_id,
        )
    except RULE_EXCEPTIONS as exc:
        raise _rule_errors(exc) from exc


@router.post("/plans/{plan_version}/rules/{rule_id}/approve", response_model=RuleOut)
def approve_rule(
    plan_version: str,
    rule_id: str,
    body: RuleApproveIn,
    db: Session = Depends(get_db),
) -> Any:
    try:
        return services.approve_rule(
            db,
            plan_version=plan_version,
            rule_id=rule_id,
            approver_id=body.approver_id,
            reason=body.reason,
        )
    except RULE_EXCEPTIONS as exc:
        raise _rule_errors(exc) from exc


@router.post("/plans/{plan_version}/rules/{rule_id}/schedule", response_model=RuleOut)
def schedule_rule(
    plan_version: str,
    rule_id: str,
    body: RuleScheduleIn,
    db: Session = Depends(get_db),
) -> Any:
    try:
        out, _ = services.schedule_rule(
            db,
            plan_version=plan_version,
            rule_id=rule_id,
            effective_from=body.effective_from,
            actor_id=body.actor_id,
            reason=body.reason,
        )
        return out
    except RULE_EXCEPTIONS as exc:
        raise _rule_errors(exc) from exc


@router.post(
    "/plans/{plan_version}/rules/{rule_id}/withdraw", response_model=WithdrawOut
)
def withdraw_rule(
    plan_version: str,
    rule_id: str,
    body: RuleWithdrawIn,
    db: Session = Depends(get_db),
) -> Any:
    try:
        return services.withdraw_rule(
            db,
            plan_version=plan_version,
            rule_id=rule_id,
            actor_id=body.actor_id,
            reason=body.reason,
        )
    except RULE_EXCEPTIONS as exc:
        raise _rule_errors(exc) from exc


@router.post(
    "/plans/{plan_version}/rules/{rule_id}/preview", response_model=PreviewOut
)
def preview_rule(
    plan_version: str,
    rule_id: str,
    body: RulePreviewIn,
    db: Session = Depends(get_db),
) -> Any:
    try:
        return services.preview_rule(
            db,
            plan_version=plan_version,
            rule_id=rule_id,
            effective_from=body.effective_from,
        )
    except RULE_EXCEPTIONS as exc:
        raise _rule_errors(exc) from exc


@router.post(
    "/plans/{plan_version}/rules/{rule_id}/rollback",
    response_model=RuleOut,
    status_code=status.HTTP_201_CREATED,
)
def rollback_rule(
    plan_version: str,
    rule_id: str,
    body: RuleRollbackIn,
    db: Session = Depends(get_db),
) -> Any:
    try:
        return services.rollback_rule(
            db,
            plan_version=plan_version,
            rule_id=rule_id,
            new_rule_id=body.new_rule_id,
            actor_id=body.actor_id,
            reason=body.reason,
        )
    except RULE_EXCEPTIONS as exc:
        raise _rule_errors(exc) from exc


@router.get(
    "/plans/{plan_version}/rules/{rule_id}/diff/{other_rule_id}",
    response_model=RuleDiffOut,
)
def diff_rules(
    plan_version: str,
    rule_id: str,
    other_rule_id: str,
    db: Session = Depends(get_db),
) -> Any:
    try:
        return services.diff_rules(db, plan_version, rule_id, other_rule_id)
    except RULE_EXCEPTIONS as exc:
        raise _rule_errors(exc) from exc
