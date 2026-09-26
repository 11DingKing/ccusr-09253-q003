"""规则版本与生效区间的纯函数核心。

学时口径（seconds_per_lesson / 需要导师确认的活动类型 / 每日计入学时上限）
以规则版本形式发布，每个已发布版本占据一条生效区间
[effective_from, 下一版本 effective_from)。导入事件时按事件发生时刻解析
适用版本并固定在事件上；撤回版本后该版本不再参与解析。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

BASELINE_RULE_ID = "baseline"
DEFAULT_SECONDS_PER_LESSON = 45 * 60
DEFAULT_CONFIRMATION_TYPES: tuple[str, ...] = ("internship",)


class RuleError(ValueError):
    """规则参数或时间线不合法。"""


@dataclass(frozen=True)
class RuleSpec:
    """一个规则版本承载的学时口径参数。"""

    seconds_per_lesson: int = DEFAULT_SECONDS_PER_LESSON
    confirmation_required_types: tuple[str, ...] = DEFAULT_CONFIRMATION_TYPES
    daily_cap_seconds: int | None = None

    def __post_init__(self) -> None:
        if self.seconds_per_lesson <= 0:
            raise RuleError("seconds_per_lesson 必须大于零")
        if self.daily_cap_seconds is not None and self.daily_cap_seconds <= 0:
            raise RuleError("daily_cap_seconds 必须大于零")
        normalized = tuple(
            dict.fromkeys(t.strip() for t in self.confirmation_required_types)
        )
        if any(not t for t in normalized):
            raise RuleError("确认类型不能为空字符串")
        object.__setattr__(self, "confirmation_required_types", normalized)


BASELINE_RULE = RuleSpec()


def spec_from_params(params: Mapping[str, Any] | None) -> RuleSpec:
    """从存储的 JSON 参数还原 RuleSpec，缺省字段回落到基线值。"""
    data = dict(params or {})
    return RuleSpec(
        seconds_per_lesson=int(
            data.get("seconds_per_lesson", DEFAULT_SECONDS_PER_LESSON)
        ),
        confirmation_required_types=tuple(
            data.get("confirmation_required_types", DEFAULT_CONFIRMATION_TYPES)
        ),
        daily_cap_seconds=data.get("daily_cap_seconds"),
    )


def spec_to_params(spec: RuleSpec) -> dict[str, Any]:
    return {
        "seconds_per_lesson": spec.seconds_per_lesson,
        "confirmation_required_types": sorted(spec.confirmation_required_types),
        "daily_cap_seconds": spec.daily_cap_seconds,
    }


def ensure_utc(moment: datetime) -> datetime:
    """统一按 UTC 绝对瞬间比较，容忍数据库回读出的朴素时间。"""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


@dataclass(frozen=True)
class RuleInterval:
    """已发布规则在时间线上的锚点；区间上界由下一锚点决定。"""

    rule_id: str
    effective_from: datetime


def pick_applicable(
    intervals: Iterable[RuleInterval], at: datetime
) -> str | None:
    """解析某个绝对瞬间适用的规则版本。

    取 effective_from <= at 中生效时刻最晚的版本；生效时刻相同（仅可能
    出现在历史遗留数据）时按 rule_id 字典序取大者，保证解析确定性。
    返回值 None 表示落入基线口径（尚无任何已发布版本覆盖该时刻）。
    """
    if at.tzinfo is None:
        raise RuleError("解析时间必须包含时区")
    instant = at.astimezone(timezone.utc)
    best: RuleInterval | None = None
    for interval in intervals:
        effective = ensure_utc(interval.effective_from)
        if effective > instant:
            continue
        if best is None or (effective, interval.rule_id) > (
            ensure_utc(best.effective_from),
            best.rule_id,
        ):
            best = RuleInterval(rule_id=interval.rule_id, effective_from=effective)
    return best.rule_id if best is not None else None


def diff_params(before: RuleSpec, after: RuleSpec) -> dict[str, Any]:
    """两个规则版本之间的参数级差异。"""
    changes: dict[str, Any] = {}
    if before.seconds_per_lesson != after.seconds_per_lesson:
        changes["seconds_per_lesson"] = {
            "before": before.seconds_per_lesson,
            "after": after.seconds_per_lesson,
        }
    before_types = sorted(before.confirmation_required_types)
    after_types = sorted(after.confirmation_required_types)
    if before_types != after_types:
        changes["confirmation_required_types"] = {
            "before": before_types,
            "after": after_types,
        }
    if before.daily_cap_seconds != after.daily_cap_seconds:
        changes["daily_cap_seconds"] = {
            "before": before.daily_cap_seconds,
            "after": after.daily_cap_seconds,
        }
    return changes
