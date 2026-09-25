"""服务端业务模块。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Iterable, Mapping

from .clock import (
    elapsed_seconds,
    merge_intervals,
    split_by_academic_day,
    to_utc,
    union_seconds,
)
from .rules import RuleResolutionError, RuleSpec, utc


class EventType(StrEnum):
    CHECKIN = "checkin"
    MENTOR_CONFIRM = "mentor_confirm"
    LEAVE_CORRECTION = "leave_correction"


class CheckinStatus(StrEnum):
    CONFIRMED = "CONFIRMED"
    PENDING = "PENDING"


INTERNSHIP_TYPE = "internship"

# 影响预览时使用的虚拟绑定 id（不落库、不与真实版本冲突）。
PREVIEW_RULE_ID = "__preview__"


@dataclass(frozen=True)
class Event:
    """封装领域状态与业务约束。"""

    event_id: str
    plan_version: str
    event_type: EventType
    student_id: str
    payload: dict[str, Any]
    created_at: datetime
    # 导入时固化的适用规则版本；None 表示按培养方案基线口径计算。
    rule_version_id: str | None = None


@dataclass
class CheckinRecord:
    event_id: str
    student_id: str
    activity_id: str
    activity_type: str
    start_utc: datetime
    end_utc: datetime
    status: CheckinStatus
    rule_version_id: str | None = None
    # 绑定版本的口径快照，历史解释永远不依赖规则表当前状态。
    spec: RuleSpec | None = None

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
    rule_version_id: str | None = None


@dataclass
class DayTotal:
    academic_day: str
    seconds: int
    # 该学术日各规则版本分别贡献的（封顶后）秒数，便于解释来源。
    by_rule: dict[str | None, int] = field(default_factory=dict)


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
    # 各绑定规则版本贡献的确认秒数（封顶后）与修正秒数。
    seconds_by_rule: dict[str | None, int] = field(default_factory=dict)
    adjustments_by_rule: dict[str | None, int] = field(default_factory=dict)


@dataclass
class ReplayState:
    plan_version: str
    timezone: str
    required_seconds: int
    students: dict[str, StudentProgress]


def _parse_checkin(
    event: Event, tz_name: str, spec: RuleSpec | None
) -> CheckinRecord:
    start = to_utc(datetime.fromisoformat(event.payload["check_in_at"]))
    end = to_utc(datetime.fromisoformat(event.payload["check_out_at"]))
    activity_type = event.payload.get("activity_type", "regular")
    requires_confirmation = (
        spec.internship_requires_confirmation if spec is not None else True
    )
    if activity_type == INTERNSHIP_TYPE and requires_confirmation:
        status = CheckinStatus.PENDING
    else:
        status = CheckinStatus.CONFIRMED
    return CheckinRecord(
        event_id=event.event_id,
        student_id=event.student_id,
        activity_id=event.payload.get("activity_id", ""),
        activity_type=activity_type,
        start_utc=start,
        end_utc=end,
        status=status,
        rule_version_id=event.rule_version_id,
        spec=spec,
    )


def event_occurred_at(event: Event) -> datetime:
    """事件的业务发生时间（UTC），用于导入时解析适用规则版本。

    签到取签到开始时间；确认与修正优先取 payload 中的 ``occurred_at``，
    缺省回退到服务端接收时间。
    """
    if event.event_type == EventType.CHECKIN:
        raw = event.payload.get("check_in_at")
    else:
        raw = event.payload.get("occurred_at")
    if raw is None:
        raw = event.created_at
    moment = raw if isinstance(raw, datetime) else datetime.fromisoformat(raw)
    if moment.tzinfo is None:
        raise RuleResolutionError("事件业务时间必须携带时区信息")
    return utc(moment)


def replay(
    events: Iterable[Event],
    *,
    plan_version: str,
    timezone_name: str,
    required_seconds: int,
    up_to_event_id: str | None = None,
    rule_specs: Mapping[str, RuleSpec] | None = None,
    override: tuple[RuleSpec, datetime] | None = None,
) -> ReplayState:
    """执行确定性的业务处理。

    ``rule_specs`` 以事件上固化的规则版本 id 为键提供口径快照；未绑定版本
    的历史事件使用基线口径（45 分钟一学时、实习需确认、不封顶）。

    ``override=(spec, boundary)`` 仅供影响预览使用：业务时间不早于
    ``boundary`` 的事件改按候选口径重算，不落库、不改变固化绑定。
    """
    specs: dict[str, RuleSpec] = dict(rule_specs or {})
    materialized: list[Event] = []
    if override is not None:
        candidate, boundary = override
        boundary = utc(boundary)
        specs[PREVIEW_RULE_ID] = candidate
        for event in events:
            if event.plan_version != plan_version:
                materialized.append(event)
                continue
            try:
                moment = event_occurred_at(event)
            except (RuleResolutionError, TypeError, ValueError):
                # 历史行/异常数据缺少时区时，回退到接收时间并按 UTC 处理。
                if event.created_at.tzinfo is None:
                    moment = event.created_at.replace(tzinfo=timezone.utc)
                else:
                    moment = event.created_at.astimezone(timezone.utc)
            if moment >= boundary:
                event = Event(
                    event_id=event.event_id,
                    plan_version=event.plan_version,
                    event_type=event.event_type,
                    student_id=event.student_id,
                    payload=event.payload,
                    created_at=event.created_at,
                    rule_version_id=PREVIEW_RULE_ID,
                )
            materialized.append(event)
        events = materialized

    baseline = RuleSpec()  # 与历史行为保持一致

    def spec_for(rule_id: str | None) -> RuleSpec:
        if rule_id is None:
            return baseline
        return specs.get(rule_id, baseline)

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
        if event.event_type == EventType.CHECKIN:
            record = _parse_checkin(
                event, timezone_name, spec_for(event.rule_version_id)
            )
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
                    rule_version_id=event.rule_version_id,
                )
            )

    all_students = set(checkins_by_student) | set(adjustments_by_student)
    students: dict[str, StudentProgress] = {}
    for student_id in all_students:
        records = checkins_by_student.get(student_id, [])
        adjustments = adjustments_by_student.get(student_id, [])

        confirmed = [r for r in records if r.counts]
        pending = [r for r in records if r.status == CheckinStatus.PENDING]
        pending_intervals = [(r.start_utc, r.end_utc) for r in pending]

        # 原始确认秒数（未封顶），按版本分组。
        confirmed_by_rule: dict[str | None, list[CheckinRecord]] = {}
        for record in confirmed:
            confirmed_by_rule.setdefault(record.rule_version_id, []).append(record)

        pending_seconds = union_seconds(pending_intervals)
        adjustment_seconds = sum(a.seconds for a in adjustments)
        adjustments_by_rule: dict[str | None, int] = {}
        for adj in adjustments:
            adjustments_by_rule[adj.rule_version_id] = (
                adjustments_by_rule.get(adj.rule_version_id, 0) + adj.seconds
            )

        # 第一步：day -> rule -> 当日原始秒数（多段签到先合并求和）。
        day_rule_raw: dict[str, dict[str | None, int]] = {}
        for rule_id, group in confirmed_by_rule.items():
            intervals = [(r.start_utc, r.end_utc) for r in group]
            for start, end in merge_intervals(intervals):
                for day, seg_start, seg_end in split_by_academic_day(
                    start, end, timezone_name
                ):
                    key = day.isoformat()
                    secs = elapsed_seconds(seg_start, seg_end)
                    bucket = day_rule_raw.setdefault(key, {})
                    bucket[rule_id] = bucket.get(rule_id, 0) + secs

        # 第二步：按版本口径对"当日总量"封顶。
        day_rule_seconds: dict[str, dict[str | None, int]] = {}
        for key, bucket in day_rule_raw.items():
            capped_bucket = day_rule_seconds.setdefault(key, {})
            for rule_id, raw_secs in bucket.items():
                cap = spec_for(rule_id).daily_cap_seconds
                secs = raw_secs if cap is None else min(raw_secs, cap)
                capped_bucket[rule_id] = secs

        seconds_by_rule: dict[str | None, int] = {}
        day_totals: dict[str, int] = {}
        for key, bucket in day_rule_seconds.items():
            day_sum = 0
            for rule_id, secs in bucket.items():
                seconds_by_rule[rule_id] = (
                    seconds_by_rule.get(rule_id, 0) + secs
                )
                day_sum += secs
            day_totals[key] = day_sum

        confirmed_seconds = sum(seconds_by_rule.values())
        total_seconds = confirmed_seconds + adjustment_seconds
        if total_seconds < 0:
            total_seconds = 0

        # 学时按各规则自己的折算单位分别向下取整后求和（负数修正会冲减学时）。
        lesson_units = 0
        rule_ids = set(seconds_by_rule) | set(adjustments_by_rule)
        for rule_id in rule_ids:
            contributed = seconds_by_rule.get(rule_id, 0) + adjustments_by_rule.get(
                rule_id, 0
            )
            lesson_units += contributed // spec_for(rule_id).seconds_per_lesson_unit
        if lesson_units < 0:
            lesson_units = 0

        pending_units = 0
        pending_by_rule: dict[str | None, int] = {}
        for record in pending:
            pending_by_rule[record.rule_version_id] = (
                pending_by_rule.get(record.rule_version_id, 0) + record.seconds
            )
        for rule_id, secs in pending_by_rule.items():
            pending_units += secs // spec_for(rule_id).seconds_per_lesson_unit

        daily = [
            DayTotal(
                academic_day=day,
                seconds=secs,
                by_rule=dict(day_rule_seconds[day]),
            )
            for day, secs in sorted(day_totals.items())
        ]

        students[student_id] = StudentProgress(
            student_id=student_id,
            confirmed_seconds=confirmed_seconds,
            pending_seconds=pending_seconds,
            adjustment_seconds=adjustment_seconds,
            total_seconds=total_seconds,
            lesson_units=lesson_units,
            pending_lesson_units=pending_units,
            meets_requirement=total_seconds >= required_seconds,
            daily=daily,
            checkins=sorted(records, key=lambda r: r.start_utc),
            adjustments=sorted(adjustments, key=lambda a: a.event_id),
            seconds_by_rule=seconds_by_rule,
            adjustments_by_rule=adjustments_by_rule,
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
        "rule_version_id": record.rule_version_id,
        "rule_spec": record.spec.to_dict() if record.spec is not None else None,
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
