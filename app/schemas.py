"""服务端业务模块。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class PlanIn(BaseModel):
    plan_version: str = Field(..., min_length=1, max_length=128)
    iana_timezone: str = Field(..., min_length=1, max_length=64)
    required_seconds: int = Field(0, ge=0)


class PlanOut(BaseModel):
    plan_version: str
    iana_timezone: str
    required_seconds: int


class CheckinPayload(BaseModel):
    activity_id: str = ""
    activity_type: str = "regular"
    check_in_at: datetime
    check_out_at: datetime

    @model_validator(mode="after")
    def _check_order(self) -> "CheckinPayload":
        if self.check_out_at <= self.check_in_at:
            raise ValueError("check_out_at must be after check_in_at")
        return self

    @field_validator("check_in_at", "check_out_at")
    @classmethod
    def _ensure_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware (RFC 3339)")
        return v


class MentorConfirmPayload(BaseModel):
    checkin_event_id: str


class LeaveCorrectionPayload(BaseModel):
    adjustment_seconds: int
    reason: str = ""


class EventIn(BaseModel):
    event_id: str = Field(..., min_length=1, max_length=128)
    event_type: Literal["checkin", "mentor_confirm", "leave_correction"]
    student_id: str = Field(..., min_length=1, max_length=128)
    payload: dict[str, Any]


class EventBatchIn(BaseModel):
    events: list[EventIn]


class EventOut(BaseModel):
    event_id: str
    plan_version: str
    event_type: str
    student_id: str
    payload: dict[str, Any]
    created_at: datetime

    model_config = {"from_attributes": True}


class ImportResult(BaseModel):
    accepted: int
    duplicates: list[str]
    rejected: list[dict[str, Any]]
    # event_id -> 导入时固化的规则版本 id（null 表示基线口径）
    bindings: dict[str, str | None] = {}


class DailyTotal(BaseModel):
    academic_day: str
    seconds: int


class CheckinExplanation(BaseModel):
    event_id: str
    activity_id: str
    activity_type: str
    status: str
    counts: bool
    check_in_at_utc: str
    check_out_at_utc: str
    raw_seconds: int
    academic_days: list[dict[str, Any]]


class AdjustmentOut(BaseModel):
    event_id: str
    seconds: int
    reason: str


class StudentProgressOut(BaseModel):
    student_id: str
    confirmed_seconds: int
    pending_seconds: int
    adjustment_seconds: int
    total_seconds: int
    lesson_units: int
    pending_lesson_units: int
    meets_requirement: bool
    daily: list[DailyTotal]
    checkins: list[CheckinExplanation]
    adjustments: list[AdjustmentOut]


class SnapshotOut(BaseModel):
    plan_version: str
    freeze_id: str | None
    timezone: str
    required_seconds: int
    generated_at: str
    event_cutoff_id: str | None
    students: list[dict[str, Any]]
    rule_catalog: list[dict[str, Any]] = []


class FreezeIn(BaseModel):
    pass


class DiffOut(BaseModel):
    plan_version: str
    old_freeze_id: str | None
    new_freeze_id: str | None
    old_generated_at: str
    new_generated_at: str
    old_event_cutoff_id: str | None
    new_event_cutoff_id: str | None
    student_changes: list[dict[str, Any]]
    students_affected: int


# ---------------------------------------------------------- 规则版本

class RuleSpecIn(BaseModel):
    required_seconds: int = Field(0, ge=0)
    seconds_per_lesson_unit: int = Field(45 * 60, gt=0)
    daily_cap_seconds: int | None = Field(None, gt=0)
    internship_requires_confirmation: bool = True


class RuleDraftIn(RuleSpecIn):
    effective_at: datetime
    created_by: str = Field(..., min_length=1, max_length=128)
    rule_version_id: str | None = Field(None, min_length=1, max_length=128)

    @field_validator("effective_at")
    @classmethod
    def _ensure_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("effective_at 必须携带时区信息（RFC 3339）")
        return v


class RuleApproveIn(BaseModel):
    approver_id: str = Field(..., min_length=1, max_length=128)
    note: str = ""


class RuleWithdrawIn(BaseModel):
    actor_id: str = Field(..., min_length=1, max_length=128)
    reason: str = Field(..., min_length=1)


class RuleRollbackIn(BaseModel):
    actor_id: str = Field(..., min_length=1, max_length=128)
    reason: str = Field(..., min_length=1)
    new_rule_version_id: str | None = Field(None, min_length=1, max_length=128)


class RulePreviewIn(RuleSpecIn):
    effective_at: datetime

    @field_validator("effective_at")
    @classmethod
    def _ensure_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("effective_at 必须携带时区信息（RFC 3339）")
        return v


class RuleOut(BaseModel):
    rule_version_id: str
    plan_version: str
    state: str
    spec: dict[str, Any]
    created_by: str
    effective_at: str
    end_at: str | None
    activated_at: str | None
    superseded_by: str | None
    rollback_of: str | None
    revision: int
    created_at: str
    updated_at: str
    approval_count: int | None = None


class RuleDiffOut(BaseModel):
    plan_version: str
    left_rule_version_id: str
    right_rule_version_id: str
    changes: dict[str, dict[str, Any]]
    fields_changed: list[str]
    identical: bool
    left: dict[str, Any]
    right: dict[str, Any]


class RuleImpactOut(BaseModel):
    plan_version: str
    candidate_spec: dict[str, Any]
    effective_at: str
    students_affected: int
    student_changes: list[dict[str, Any]]
    preview_snapshot: dict[str, Any]


class RuleAuditOut(BaseModel):
    id: int
    rule_version_id: str
    action: str
    actor_id: str
    from_state: str | None
    to_state: str | None
    reason: str
    detail: dict[str, Any] | None
    created_at: str
