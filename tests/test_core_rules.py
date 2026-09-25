"""规则版本解析、口径差异与重放绑定的核心测试。"""

from __future__ import annotations

from datetime import datetime

import pytest

from app.core.replay import (
    PREVIEW_RULE_ID,
    Event,
    EventType,
    replay,
)
from app.core.rules import (
    EffectiveRule,
    RuleResolutionError,
    RuleSpec,
    diff_specs,
    resolve_rule_version,
)

T = lambda s: datetime.fromisoformat(s)  # noqa: E731


def _rule(rid, effective, end=None, state="active", spec=None):
    return EffectiveRule(
        rule_version_id=rid,
        spec=spec or RuleSpec(),
        effective_at=effective,
        end_at=end,
        state=state,
    )


def test_resolves_version_whose_window_contains_event_time():
    v1 = _rule("RV-1", T("2024-01-01T00:00:00+00:00"), T("2024-03-01T00:00:00+00:00"), "superseded")
    v2 = _rule("RV-2", T("2024-03-01T00:00:00+00:00"), None, "active")
    rules = [v1, v2]
    assert resolve_rule_version(T("2024-02-28T23:59:59+00:00"), rules) == "RV-1"
    # 窗口为左闭右开：边界时刻属于新版本。
    assert resolve_rule_version(T("2024-03-01T00:00:00+00:00"), rules) == "RV-2"
    assert resolve_rule_version(T("2024-09-01T00:00:00+00:00"), rules) == "RV-2"


def test_withdrawn_version_still_covers_history_but_not_future():
    v1 = _rule("RV-1", T("2024-03-01T00:00:00+00:00"), T("2024-03-10T00:00:00+00:00"), "withdrawn")
    # 撤回后窗口截断：窗口内的迟到事件仍绑定该版本。
    assert resolve_rule_version(T("2024-03-05T00:00:00+00:00"), [v1]) == "RV-1"
    # 撤回时刻之后不再解析到它。
    assert resolve_rule_version(T("2024-03-10T00:00:00+00:00"), [v1]) is None
    assert resolve_rule_version(T("2024-03-01T00:00:00+00:00"), [v1]) == "RV-1"


def test_never_effective_states_never_resolve():
    for state in ("draft", "pending_approval", "scheduled"):
        v = _rule("RV-X", T("2020-01-01T00:00:00+00:00"), None, state)
        assert resolve_rule_version(T("2024-03-05T00:00:00+00:00"), [v]) is None


def test_cross_timezone_effective_instant_is_comparable_in_utc():
    # 北京时间 3 月 1 日 08:00 == UTC 3 月 1 日 00:00；同一物理时刻两种表达。
    v = _rule("RV-1", T("2024-03-01T00:00:00+00:00"), None)
    assert resolve_rule_version(T("2024-03-01T07:59:00+08:00"), [v]) is None
    assert resolve_rule_version(T("2024-03-01T08:00:00+08:00"), [v]) == "RV-1"
    assert resolve_rule_version(T("2024-02-29T19:00:00-05:00"), [v]) == "RV-1"


def test_naive_datetime_is_rejected():
    v = _rule("RV-1", T("2024-03-01T00:00:00+00:00"), None)
    with pytest.raises(RuleResolutionError):
        resolve_rule_version(datetime(2024, 3, 5), [v])


def test_overlapping_fork_picks_latest_effective():
    # 异常分叉（窗口重叠）时确定性地选择生效更晚的版本。
    v1 = _rule("RV-1", T("2024-01-01T00:00:00+00:00"), None)
    v2 = _rule("RV-2", T("2024-02-01T00:00:00+00:00"), None)
    assert resolve_rule_version(T("2024-03-01T00:00:00+00:00"), [v1, v2]) == "RV-2"


def test_diff_specs_reports_changed_fields():
    old = RuleSpec(required_seconds=3600, seconds_per_lesson_unit=2700)
    new = RuleSpec(required_seconds=7200, seconds_per_lesson_unit=2400, daily_cap_seconds=3600)
    changes = diff_specs(old, new)
    assert set(changes) == {"required_seconds", "seconds_per_lesson_unit", "daily_cap_seconds"}
    assert changes["required_seconds"] == {"before": 3600, "after": 7200}
    assert diff_specs(old, old) == {}


def _checkin(eid, start, end, rule_id=None, activity_type="regular"):
    return Event(
        event_id=eid,
        plan_version="P1",
        event_type=EventType.CHECKIN,
        student_id="S1",
        payload={
            "activity_id": "A",
            "activity_type": activity_type,
            "check_in_at": start,
            "check_out_at": end,
        },
        created_at=T("2024-01-01T00:00:00+00:00"),
        rule_version_id=rule_id,
    )


def test_replay_uses_pinned_spec_for_cap_and_lesson_units():
    events = [
        # 旧口径：3 小时，2700 秒一学时。
        _checkin("E-01", "2024-03-01T08:00:00+08:00", "2024-03-01T11:00:00+08:00", "RV-1"),
        # 新口径：3 小时但每日封顶 1 小时，2400 秒一学时。
        _checkin("E-02", "2024-04-01T08:00:00+08:00", "2024-04-01T11:00:00+08:00", "RV-2"),
    ]
    specs = {
        "RV-1": RuleSpec(seconds_per_lesson_unit=2700),
        "RV-2": RuleSpec(seconds_per_lesson_unit=2400, daily_cap_seconds=3600),
    }
    state = replay(
        events, plan_version="P1", timezone_name="Asia/Shanghai",
        required_seconds=0, rule_specs=specs,
    )
    p = state.students["S1"]
    assert p.seconds_by_rule["RV-1"] == 3 * 3600
    assert p.seconds_by_rule["RV-2"] == 3600  # 封顶
    assert p.confirmed_seconds == 4 * 3600
    # 学时按版本各自的折算单位分别取整后求和。
    assert p.lesson_units == (3 * 3600) // 2700 + 3600 // 2400
    assert p.daily[0].by_rule["RV-1"] == 3 * 3600
    assert p.daily[1].by_rule["RV-2"] == 3600


def test_daily_cap_applies_to_day_total_not_each_segment():
    # 同一天两段不重叠的 45 分钟签到，当日封顶 1 小时 -> 合计只计 1 小时。
    events = [
        _checkin("E-01", "2024-03-01T08:00:00+08:00", "2024-03-01T08:45:00+08:00", "RV-1"),
        _checkin("E-02", "2024-03-01T18:00:00+08:00", "2024-03-01T18:45:00+08:00", "RV-1"),
    ]
    specs = {"RV-1": RuleSpec(daily_cap_seconds=3600)}
    state = replay(
        events, plan_version="P1", timezone_name="Asia/Shanghai",
        required_seconds=0, rule_specs=specs,
    )
    p = state.students["S1"]
    assert p.seconds_by_rule["RV-1"] == 3600
    assert p.daily[0].by_rule["RV-1"] == 3600


def test_replay_internship_flag_is_version_specific():
    events = [
        _checkin("E-01", "2024-03-01T08:00:00+08:00", "2024-03-01T12:00:00+08:00",
                 "RV-1", activity_type="internship"),
    ]
    # RV-1 口径下实习无需导师确认 -> 直接计入。
    specs = {"RV-1": RuleSpec(internship_requires_confirmation=False)}
    state = replay(
        events, plan_version="P1", timezone_name="Asia/Shanghai",
        required_seconds=0, rule_specs=specs,
    )
    p = state.students["S1"]
    assert p.confirmed_seconds == 4 * 3600
    assert p.pending_seconds == 0


def test_preview_override_does_not_mutate_pinned_bindings():
    events = [
        _checkin("E-01", "2024-03-01T08:00:00+08:00", "2024-03-01T11:00:00+08:00", "RV-1"),
        _checkin("E-02", "2024-04-01T08:00:00+08:00", "2024-04-01T11:00:00+08:00", "RV-1"),
    ]
    specs = {"RV-1": RuleSpec()}
    candidate = RuleSpec(daily_cap_seconds=3600)
    state = replay(
        events, plan_version="P1", timezone_name="Asia/Shanghai",
        required_seconds=0, rule_specs=specs,
        override=(candidate, T("2024-03-15T00:00:00+00:00")),
    )
    p = state.students["S1"]
    # E-01 在边界之前保持原口径；E-02 按候选口径封顶。
    assert p.seconds_by_rule["RV-1"] == 3 * 3600
    assert p.seconds_by_rule[PREVIEW_RULE_ID] == 3600
    assert p.confirmed_seconds == 4 * 3600
