"""服务端业务模块。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Iterable, Mapping

from .clock import (
    academic_day,
    elapsed_seconds,
    merge_intervals,
    split_by_academic_day,
    to_utc,
    union_seconds,
)
from .rules import BASELINE_RULE, BASELINE_RULE_ID, RuleSpec


class EventType(StrEnum):
    CHECKIN = "checkin"
    MENTOR_CONFIRM = "mentor_confirm"
    LEAVE_CORRECTION = "leave_correction"


class CheckinStatus(StrEnum):
    CONFIRMED = "CONFIRMED"
    PENDING = "PENDING"


INTERNSHIP_TYPE = "internship"


def display_rule_id(rule_id: str | None) -> str:
    """事件上未固定版本（NULL）时对外展示为基线口径。"""
    return rule_id if rule_id else BASELINE_RULE_ID


@dataclass(frozen=True)
class Event:
    """封装领域状态与业务约束。"""

    event_id: str
    plan_version: str
    event_type: EventType
    student_id: str
    payload: dict[str, Any]
    created_at: datetime
    rule_id: str | None = None


@dataclass
class CheckinRecord:
    event_id: str
    student_id: str
    activity_id: str
    activity_type: str
    start_utc: datetime
    end_utc: datetime
    status: CheckinStatus
    rule_id: str | None = None

    @property
    def seconds(self) -> int:
        return elapsed_seconds(self.start_utc, self.end_utc)

    @property
    def counts(self) -> bool:
        return self.status == CheckinStatus.CONFIRMED


@dataclass
class Adjustment:
    event_id: str
    student_id: str
    seconds: int
    reason: str
    rule_id: str | None = None


@dataclass
class DayTotal:
    academic_day: str
    seconds: int


@dataclass
class RuleSegment:
    """学员在单个规则版本下的分段结算结果。"""

    rule_id: str | None
    confirmed_seconds: int
    pending_seconds: int
    adjustment_seconds: int
    total_seconds: int
    lesson_units: int
    pending_lesson_units: int
    daily: list[DayTotal] = field(default_factory=list)


@dataclass
class StudentProgress:
    student_id: str
    confirmed_seconds: int
    pending_seconds: int
    adjustment_seconds: int
    total_seconds: int
    lesson_units: int
    pending_lesson_units: int
    meets_requirement: bool
    daily: list[DayTotal] = field(default_factory=list)
    checkins: list[CheckinRecord] = field(default_factory=list)
    adjustments: list[Adjustment] = field(default_factory=list)
    segments: list[RuleSegment] = field(default_factory=list)


@dataclass
class ReplayState:
    plan_version: str
    timezone: str
    required_seconds: int
    students: dict[str, StudentProgress]


def _parse_checkin(event: Event, spec: RuleSpec) -> CheckinRecord:
    start = to_utc(datetime.fromisoformat(event.payload["check_in_at"]))
    end = to_utc(datetime.fromisoformat(event.payload["check_out_at"]))
    activity_type = event.payload.get("activity_type", "regular")
    requires_confirmation = activity_type in spec.confirmation_required_types
    status = (
        CheckinStatus.PENDING if requires_confirmation else CheckinStatus.CONFIRMED
    )
    return CheckinRecord(
        event_id=event.event_id,
        student_id=event.student_id,
        activity_id=event.payload.get("activity_id", ""),
        activity_type=activity_type,
        start_utc=start,
        end_utc=end,
        status=status,
        rule_id=event.rule_id,
    )


def _recognized_daily(
    intervals: list[tuple[datetime, datetime]],
    tz_name: str,
    daily_cap_seconds: int | None,
) -> dict[str, int]:
    """合并区间内按教学日拆分后的认可秒数（可选每日上限）。"""
    day_totals: dict[str, int] = {}
    for start, end in merge_intervals(intervals):
        for day, seg_start, seg_end in split_by_academic_day(start, end, tz_name):
            key = day.isoformat()
            day_totals[key] = day_totals.get(key, 0) + elapsed_seconds(
                seg_start, seg_end
            )
    if daily_cap_seconds is not None:
        day_totals = {
            day: min(seconds, daily_cap_seconds)
            for day, seconds in day_totals.items()
        }
    return day_totals


def replay(
    events: Iterable[Event],
    *,
    plan_version: str,
    timezone_name: str,
    required_seconds: int,
    up_to_event_id: str | None = None,
    rules: Mapping[str, RuleSpec] | None = None,
) -> ReplayState:
    """执行确定性的业务处理。

    每条事件携带导入时固定的 rule_id；重放按事件所属规则版本分组结算，
    不同版本各自合并区间、套用每日上限与学时换算后再汇总，因此规则
    升级不会整体重算历史事件。
    """
    rule_specs = rules or {}
    sorted_events = sorted(
        (e for e in events if e.plan_version == plan_version),
        key=lambda e: e.event_id,
    )
    if up_to_event_id is not None:
        sorted_events = [e for e in sorted_events if e.event_id <= up_to_event_id]

    checkins_by_student: dict[str, list[CheckinRecord]] = {}
    checkin_index: dict[str, CheckinRecord] = {}
    adjustments_by_student: dict[str, list[Adjustment]] = {}

    for event in sorted_events:
        spec = rule_specs.get(event.rule_id) or BASELINE_RULE
        if event.event_type == EventType.CHECKIN:
            record = _parse_checkin(event, spec)
            checkins_by_student.setdefault(event.student_id, []).append(record)
            checkin_index[event.event_id] = record
        elif event.event_type == EventType.MENTOR_CONFIRM:
            target_id = event.payload.get("checkin_event_id")
            target = checkin_index.get(target_id)
            if target is not None and target.student_id == event.student_id:
                target.status = CheckinStatus.CONFIRMED
        elif event.event_type == EventType.LEAVE_CORRECTION:
            seconds = int(event.payload.get("adjustment_seconds", 0))
            adjustments_by_student.setdefault(event.student_id, []).append(
                Adjustment(
                    event_id=event.event_id,
                    student_id=event.student_id,
                    seconds=seconds,
                    reason=str(event.payload.get("reason", "")),
                    rule_id=event.rule_id,
                )
            )

    all_students = set(checkins_by_student) | set(adjustments_by_student)
    students: dict[str, StudentProgress] = {}
    for student_id in all_students:
        records = checkins_by_student.get(student_id, [])
        adjustments = adjustments_by_student.get(student_id, [])

        confirmed_by_rule: dict[str | None, list[tuple[datetime, datetime]]] = {}
        pending_by_rule: dict[str | None, list[tuple[datetime, datetime]]] = {}
        for record in records:
            groups = confirmed_by_rule if record.counts else pending_by_rule
            groups.setdefault(record.rule_id, []).append(
                (record.start_utc, record.end_utc)
            )
        adjustments_by_rule: dict[str | None, list[Adjustment]] = {}
        for adjustment in adjustments:
            adjustments_by_rule.setdefault(adjustment.rule_id, []).append(adjustment)

        rule_ids = (
            set(confirmed_by_rule) | set(pending_by_rule) | set(adjustments_by_rule)
        )
        segments: list[RuleSegment] = []
        day_totals: dict[str, int] = {}
        for rule_id in sorted(rule_ids, key=lambda rid: rid or ""):
            spec = rule_specs.get(rule_id) or BASELINE_RULE
            confirmed_days = _recognized_daily(
                confirmed_by_rule.get(rule_id, []),
                timezone_name,
                spec.daily_cap_seconds,
            )
            confirmed_seconds = sum(confirmed_days.values())
            pending_seconds = union_seconds(pending_by_rule.get(rule_id, []))
            adjustment_seconds = sum(
                a.seconds for a in adjustments_by_rule.get(rule_id, [])
            )
            total_seconds = confirmed_seconds + adjustment_seconds
            if total_seconds < 0:
                total_seconds = 0
            segments.append(
                RuleSegment(
                    rule_id=rule_id,
                    confirmed_seconds=confirmed_seconds,
                    pending_seconds=pending_seconds,
                    adjustment_seconds=adjustment_seconds,
                    total_seconds=total_seconds,
                    lesson_units=total_seconds // spec.seconds_per_lesson,
                    pending_lesson_units=pending_seconds // spec.seconds_per_lesson,
                    daily=[
                        DayTotal(academic_day=day, seconds=secs)
                        for day, secs in sorted(confirmed_days.items())
                    ],
                )
            )
            for day, secs in confirmed_days.items():
                day_totals[day] = day_totals.get(day, 0) + secs

        confirmed_seconds = sum(seg.confirmed_seconds for seg in segments)
        pending_seconds = sum(seg.pending_seconds for seg in segments)
        adjustment_seconds = sum(seg.adjustment_seconds for seg in segments)
        total_seconds = sum(seg.total_seconds for seg in segments)

        students[student_id] = StudentProgress(
            student_id=student_id,
            confirmed_seconds=confirmed_seconds,
            pending_seconds=pending_seconds,
            adjustment_seconds=adjustment_seconds,
            total_seconds=total_seconds,
            lesson_units=sum(seg.lesson_units for seg in segments),
            pending_lesson_units=sum(seg.pending_lesson_units for seg in segments),
            meets_requirement=total_seconds >= required_seconds,
            daily=[
                DayTotal(academic_day=day, seconds=secs)
                for day, secs in sorted(day_totals.items())
            ],
            checkins=sorted(records, key=lambda r: r.start_utc),
            adjustments=sorted(adjustments, key=lambda a: a.event_id),
            segments=segments,
        )

    return ReplayState(
        plan_version=plan_version,
        timezone=timezone_name,
        required_seconds=required_seconds,
        students=students,
    )


def explain_checkin(record: CheckinRecord, tz_name: str) -> dict[str, Any]:
    """执行确定性的业务处理。"""
    segments = split_by_academic_day(record.start_utc, record.end_utc, tz_name)
    return {
        "event_id": record.event_id,
        "activity_id": record.activity_id,
        "activity_type": record.activity_type,
        "status": record.status.value,
        "counts": record.counts,
        "rule_id": display_rule_id(record.rule_id),
        "check_in_at_utc": record.start_utc.astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "check_out_at_utc": record.end_utc.astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "raw_seconds": record.seconds,
        "academic_days": [
            {
                "day": day.isoformat(),
                "start_utc": seg_start.isoformat().replace("+00:00", "Z"),
                "end_utc": seg_end.isoformat().replace("+00:00", "Z"),
                "seconds": elapsed_seconds(seg_start, seg_end),
            }
            for day, seg_start, seg_end in segments
        ],
    }
