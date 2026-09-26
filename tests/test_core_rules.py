"""规则版本核心：生效区间解析、参数差异与分版本重放。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.core.replay import Event, EventType, replay
from app.core.rules import (
    BASELINE_RULE,
    RuleError,
    RuleInterval,
    RuleSpec,
    diff_params,
    pick_applicable,
    spec_from_params,
    spec_to_params,
)

UTC = timezone.utc
SH = timezone(timedelta(hours=8))


def _event(
    event_id: str,
    event_type: EventType,
    student_id: str,
    payload: dict,
    *,
    plan_version: str = "P1",
    rule_id: str | None = None,
) -> Event:
    return Event(
        event_id=event_id,
        plan_version=plan_version,
        event_type=event_type,
        student_id=student_id,
        payload=payload,
        created_at=datetime.now(UTC),
        rule_id=rule_id,
    )


def _checkin(
    eid: str,
    student: str,
    start: str,
    end: str,
    *,
    activity_type: str = "regular",
    rule_id: str | None = None,
) -> Event:
    return _event(
        eid,
        EventType.CHECKIN,
        student,
        {
            "activity_id": "A1",
            "activity_type": activity_type,
            "check_in_at": start,
            "check_out_at": end,
        },
        rule_id=rule_id,
    )


def _replay(events, rules=None):
    return replay(
        events,
        plan_version="P1",
        timezone_name="Asia/Shanghai",
        required_seconds=0,
        rules=rules,
    )


# ---------------------------------------------------------------------------
# RuleSpec 与参数
# ---------------------------------------------------------------------------


def test_baseline_spec_matches_legacy_constants():
    assert BASELINE_RULE.seconds_per_lesson == 45 * 60
    assert BASELINE_RULE.confirmation_required_types == ("internship",)
    assert BASELINE_RULE.daily_cap_seconds is None


def test_spec_validation_rejects_bad_params():
    with pytest.raises(RuleError):
        RuleSpec(seconds_per_lesson=0)
    with pytest.raises(RuleError):
        RuleSpec(daily_cap_seconds=0)
    with pytest.raises(RuleError):
        RuleSpec(confirmation_required_types=("  ",))


def test_spec_params_roundtrip_and_dedup():
    spec = spec_from_params(
        {
            "seconds_per_lesson": 3000,
            "confirmation_required_types": ["lab", "lab", "internship"],
            "daily_cap_seconds": 7200,
        }
    )
    assert spec.confirmation_required_types == ("lab", "internship")
    assert spec_to_params(spec) == {
        "seconds_per_lesson": 3000,
        "confirmation_required_types": ["internship", "lab"],
        "daily_cap_seconds": 7200,
    }
    # 缺省字段回落到基线值
    assert spec_from_params({}) == BASELINE_RULE


def test_diff_params_reports_only_changes():
    before = RuleSpec()
    after = RuleSpec(
        seconds_per_lesson=3000,
        confirmation_required_types=("internship", "lab"),
        daily_cap_seconds=7200,
    )
    changes = diff_params(before, after)
    assert changes["seconds_per_lesson"] == {"before": 2700, "after": 3000}
    assert changes["confirmation_required_types"] == {
        "before": ["internship"],
        "after": ["internship", "lab"],
    }
    assert changes["daily_cap_seconds"] == {"before": None, "after": 7200}
    assert diff_params(before, before) == {}
    # 确认类型顺序不影响差异判断
    assert "confirmation_required_types" not in diff_params(
        RuleSpec(confirmation_required_types=("a", "b")),
        RuleSpec(confirmation_required_types=("b", "a")),
    )


# ---------------------------------------------------------------------------
# 生效区间解析
# ---------------------------------------------------------------------------


def test_pick_applicable_selects_latest_effective_version():
    t1 = datetime(2024, 3, 1, 0, 0, tzinfo=UTC)
    t2 = datetime(2024, 6, 1, 0, 0, tzinfo=UTC)
    timeline = [
        RuleInterval("R-50min", t2),
        RuleInterval("R-45min", t1),
    ]
    assert pick_applicable(timeline, datetime(2024, 2, 1, tzinfo=UTC)) is None
    assert pick_applicable(timeline, t1) == "R-45min"
    assert pick_applicable(timeline, datetime(2024, 5, 1, tzinfo=UTC)) == "R-45min"
    assert pick_applicable(timeline, t2) == "R-50min"
    assert pick_applicable(timeline, datetime(2024, 7, 1, tzinfo=UTC)) == "R-50min"


def test_pick_applicable_normalizes_offsets_to_absolute_instants():
    # 生效时刻用 +08:00 表达，查询用 -05:00 表达，比较的是同一绝对瞬间。
    effective = datetime(2024, 6, 1, 0, 0, tzinfo=SH)
    timeline = [RuleInterval("R1", effective)]
    before = datetime(2024, 5, 31, 10, 30, tzinfo=timezone(timedelta(hours=-5)))
    after = datetime(2024, 5, 31, 12, 30, tzinfo=timezone(timedelta(hours=-5)))
    assert pick_applicable(timeline, before) is None
    assert pick_applicable(timeline, after) == "R1"


def test_pick_applicable_requires_aware_time_and_breaks_ties_deterministically():
    with pytest.raises(RuleError):
        pick_applicable([], datetime(2024, 1, 1))
    t = datetime(2024, 6, 1, tzinfo=UTC)
    timeline = [RuleInterval("R-A", t), RuleInterval("R-B", t)]
    assert pick_applicable(timeline, t) == "R-B"
    assert pick_applicable(list(reversed(timeline)), t) == "R-B"


# ---------------------------------------------------------------------------
# 分版本重放
# ---------------------------------------------------------------------------


def test_replay_computes_lesson_units_per_rule_version():
    rules = {"R-50min": RuleSpec(seconds_per_lesson=3000)}
    events = [
        # 5400s 按基线 45 分钟口径 -> 2 学时
        _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T09:30:00+08:00"),
        # 5400s 按新版 50 分钟口径 -> 1 学时
        _checkin(
            "E-02",
            "S1",
            "2024-06-15T08:00:00+08:00",
            "2024-06-15T09:30:00+08:00",
            rule_id="R-50min",
        ),
    ]
    progress = _replay(events, rules).students["S1"]
    assert progress.confirmed_seconds == 5400 * 2
    assert progress.lesson_units == 2 + 1
    segments = {seg.rule_id: seg for seg in progress.segments}
    assert segments[None].lesson_units == 2
    assert segments["R-50min"].lesson_units == 1


def test_replay_applies_daily_cap_per_rule_and_academic_day():
    rules = {"R-cap": RuleSpec(daily_cap_seconds=3600)}
    events = [
        # 同一教学日 3 小时， capped 到 1 小时
        _checkin(
            "E-01",
            "S1",
            "2024-06-15T08:00:00+08:00",
            "2024-06-15T11:00:00+08:00",
            rule_id="R-cap",
        ),
        # 跨午夜 22:00-01:00 共 3 小时，两个教学日各 capped 到 1 小时
        _checkin(
            "E-02",
            "S1",
            "2024-06-16T22:00:00+08:00",
            "2024-06-17T01:00:00+08:00",
            rule_id="R-cap",
        ),
    ]
    progress = _replay(events, rules).students["S1"]
    assert progress.confirmed_seconds == 3600 + 2 * 3600
    days = {d.academic_day: d.seconds for d in progress.daily}
    assert days == {"2024-06-15": 3600, "2024-06-16": 3600, "2024-06-17": 3600}


def test_replay_confirmation_types_come_from_pinned_rule():
    rules = {"R-lab": RuleSpec(confirmation_required_types=("lab",))}
    events = [
        # 新版口径下 lab 需要导师确认
        _checkin(
            "E-01",
            "S1",
            "2024-06-15T08:00:00+08:00",
            "2024-06-15T10:00:00+08:00",
            activity_type="lab",
            rule_id="R-lab",
        ),
        # 新版口径下 internship 反而无需确认
        _checkin(
            "E-02",
            "S1",
            "2024-06-15T10:00:00+08:00",
            "2024-06-15T12:00:00+08:00",
            activity_type="internship",
            rule_id="R-lab",
        ),
        # 基线口径下 internship 仍需确认
        _checkin(
            "E-03",
            "S1",
            "2024-03-15T08:00:00+08:00",
            "2024-03-15T10:00:00+08:00",
            activity_type="internship",
        ),
    ]
    progress = _replay(events, rules).students["S1"]
    assert progress.confirmed_seconds == 2 * 3600
    assert progress.pending_seconds == 2 * 3600 + 2 * 3600


def test_replay_groups_adjustments_by_pinned_rule():
    rules = {"R-50min": RuleSpec(seconds_per_lesson=3000)}
    events = [
        _event(
            "E-01",
            EventType.LEAVE_CORRECTION,
            "S1",
            {"adjustment_seconds": 3000, "reason": "补时"},
            rule_id="R-50min",
        ),
        _event(
            "E-02",
            EventType.LEAVE_CORRECTION,
            "S1",
            {"adjustment_seconds": 2700, "reason": "补时"},
        ),
    ]
    progress = _replay(events, rules).students["S1"]
    assert progress.adjustment_seconds == 3000 + 2700
    assert progress.total_seconds == 3000 + 2700
    # 各版本分段独立换算学时：3000//3000 + 2700//2700
    assert progress.lesson_units == 2


def test_replay_negative_segment_does_not_offset_other_rule():
    rules = {"R-50min": RuleSpec(seconds_per_lesson=3000)}
    events = [
        _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"),
        # 新版本分段内的负向修正把该分段清零，但不侵蚀基线分段
        _event(
            "E-02",
            EventType.LEAVE_CORRECTION,
            "S1",
            {"adjustment_seconds": -99999, "reason": "作废"},
            rule_id="R-50min",
        ),
    ]
    progress = _replay(events, rules).students["S1"]
    assert progress.total_seconds == 7200
    assert progress.lesson_units == 7200 // 2700


def test_version_fork_replay_is_deterministic_per_segment():
    """版本分叉：同一学员横跨三个版本区间，各段独立结算、顺序无关。"""
    rules = {
        "R-mid": RuleSpec(seconds_per_lesson=2700, daily_cap_seconds=3600),
        "R-new": RuleSpec(seconds_per_lesson=3000),
    }
    events = [
        _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"),
        _checkin(
            "E-02",
            "S1",
            "2024-05-15T08:00:00+08:00",
            "2024-05-15T11:00:00+08:00",
            rule_id="R-mid",
        ),
        _checkin(
            "E-03",
            "S1",
            "2024-07-15T08:00:00+08:00",
            "2024-07-15T10:00:00+08:00",
            rule_id="R-new",
        ),
    ]
    state_a = _replay(events, rules)
    state_b = _replay(list(reversed(events)), rules)
    for state in (state_a, state_b):
        progress = state.students["S1"]
        # 基线 7200 + R-mid capped 3600 + R-new 7200
        assert progress.confirmed_seconds == 7200 + 3600 + 7200
        assert progress.lesson_units == 7200 // 2700 + 3600 // 2700 + 7200 // 3000
    assert (
        state_a.students["S1"].total_seconds == state_b.students["S1"].total_seconds
    )


def test_late_event_keeps_rule_pinned_at_import():
    """迟到事件：导入晚于规则切换，但固定的是发生时刻的旧版本。"""
    rules = {"R-new": RuleSpec(seconds_per_lesson=3000)}
    events = [
        # 发生在旧口径时期，导入时固定为基线（None）
        _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T09:30:00+08:00"),
        # 发生在新口径时期
        _checkin(
            "E-02",
            "S1",
            "2024-07-15T08:00:00+08:00",
            "2024-07-15T09:30:00+08:00",
            rule_id="R-new",
        ),
    ]
    progress = _replay(events, rules).students["S1"]
    assert progress.lesson_units == 5400 // 2700 + 5400 // 3000
    rule_ids = {c.rule_id for c in progress.checkins}
    assert rule_ids == {None, "R-new"}
