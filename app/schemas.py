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
    pinned: dict[str, str] = {}


class DailyTotal(BaseModel):
    academic_day: str
    seconds: int


class CheckinExplanation(BaseModel):
    event_id: str
    activity_id: str
    activity_type: str
    status: str
    counts: bool
    rule_id: str | None = None
    check_in_at_utc: str
    check_out_at_utc: str
    raw_seconds: int
    academic_days: list[dict[str, Any]]


class AdjustmentOut(BaseModel):
    event_id: str
    seconds: int
    reason: str
    rule_id: str | None = None


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
    rule_segments: list[dict[str, Any]] = []
    checkins: list[CheckinExplanation]
    adjustments: list[AdjustmentOut]


class SnapshotOut(BaseModel):
    plan_version: str
    freeze_id: str | None
    timezone: str
    required_seconds: int
    generated_at: str
    event_cutoff_id: str | None
    rules: dict[str, Any] = {}
    students: list[dict[str, Any]]


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


# ---------------------------------------------------------------------------
# 规则版本
# ---------------------------------------------------------------------------


class RuleParams(BaseModel):
    seconds_per_lesson: int = Field(45 * 60, gt=0, le=86400)
    confirmation_required_types: list[str] = ["internship"]
    daily_cap_seconds: int | None = Field(None, gt=0, le=7 * 86400)


class RuleCreateIn(BaseModel):
    rule_id: str = Field(..., min_length=1, max_length=128)
    params: RuleParams = RuleParams()
    actor_id: str = Field(..., min_length=1, max_length=128)
    reason: str = ""


class RulePatchIn(BaseModel):
    params: RuleParams
    actor_id: str = Field(..., min_length=1, max_length=128)
    reason: str = Field(..., min_length=1, max_length=512)


class RuleSubmitIn(BaseModel):
    actor_id: str = Field(..., min_length=1, max_length=128)


class RuleApproveIn(BaseModel):
    approver_id: str = Field(..., min_length=1, max_length=128)
    reason: str = ""


class RuleScheduleIn(BaseModel):
    actor_id: str = Field(..., min_length=1, max_length=128)
    effective_from: datetime
    reason: str = ""

    @field_validator("effective_from")
    @classmethod
    def _ensure_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("effective_from must be timezone-aware (RFC 3339)")
        return v


class RuleWithdrawIn(BaseModel):
    actor_id: str = Field(..., min_length=1, max_length=128)
    reason: str = Field(..., min_length=1, max_length=512)


class RulePreviewIn(BaseModel):
    effective_from: datetime | None = None

    @field_validator("effective_from")
    @classmethod
    def _ensure_aware(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.tzinfo is None:
            raise ValueError("effective_from must be timezone-aware (RFC 3339)")
        return v


class RuleRollbackIn(BaseModel):
    new_rule_id: str = Field(..., min_length=1, max_length=128)
    actor_id: str = Field(..., min_length=1, max_length=128)
    reason: str = ""


class RuleOut(BaseModel):
    plan_version: str
    rule_id: str
    state: str
    params: dict[str, Any]
    effective_from: str | None
    withdrawn_at: str | None
    created_by: str
    version: int
    approvals: list[str]
    created_at: str | None
    updated_at: str | None


class RuleDetailOut(RuleOut):
    audit: list[dict[str, Any]]


class RuleListOut(BaseModel):
    rules: list[RuleOut]


class ApplicableOut(BaseModel):
    plan_version: str
    at: str
    rule_id: str
    params: dict[str, Any]


class RuleDiffOut(BaseModel):
    plan_version: str
    left_rule_id: str
    right_rule_id: str
    changes: dict[str, Any]


class PreviewOut(BaseModel):
    plan_version: str
    rule_id: str
    effective_from: str | None
    events_repinned: int
    students_affected: int
    student_changes: list[dict[str, Any]]


class WithdrawOut(BaseModel):
    plan_version: str
    rule_id: str
    state: str
    withdrawn_at: str | None
    repinned: int
    repinned_events: list[dict[str, Any]]
