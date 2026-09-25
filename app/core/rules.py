"""学时口径规则版本的纯领域逻辑。

规则版本（RuleVersion）描述"学时怎么算"，与培养方案（plan_version，描述
事件流分区、学术日时区和毕业要求）分离。核心事实：

* 每条事件在**导入时**按其业务发生时间绑定一个规则版本，之后永不改变；
* 版本的适用区间为 ``[effective_at, end_at)``，``end_at`` 为空表示当前仍有效；
* 撤回（withdraw）/被取代（supersede）只会把区间上界截断，历史窗口内发生
  的事件（包括重启后才导入的迟到事件）仍按该版本解释。

本模块只依赖标准库，方便对版本分叉、跨时区生效等场景做确定性测试。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Iterable, Mapping

# 一学时等于多少秒；沿用历史默认的 45 分钟。
DEFAULT_SECONDS_PER_LESSON = 45 * 60

# 拥有"适用窗口"的状态：草稿/待审/定时版本尚未生效，绝不能被事件解析到。
WINDOWED_STATES = frozenset({"active", "superseded", "withdrawn"})


class RuleValidationError(ValueError):
    """规则口径参数不合法。"""


class RuleResolutionError(ValueError):
    """规则版本解析失败。"""


def utc(value: datetime) -> datetime:
    """把带时区的时间戳归一化为 UTC；拒绝 naive 时间（跨时区安全的前提）。"""
    if value.tzinfo is None:
        raise RuleResolutionError("生效时间必须携带时区信息")
    return value.astimezone(UTC)


def coerce_utc(value: datetime | None) -> datetime | None:
    """归一化从数据库读出的时间戳。

    SQLite 的 DATETIME 不保留时区，SQLAlchemy 读回为 naive（写入时已统一
    为 UTC）；这里把 naive 视为 UTC，其余值转换时区。
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


@dataclass(frozen=True)
class RuleSpec:
    """某一版本的学时口径。内容不可变，修改口径必须产生新版本。"""

    required_seconds: int = 0
    seconds_per_lesson_unit: int = DEFAULT_SECONDS_PER_LESSON
    # 每个学术日计入秒数上限；None 表示不封顶。
    daily_cap_seconds: int | None = None
    internship_requires_confirmation: bool = True

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RuleSpec":
        return cls(
            required_seconds=int(data.get("required_seconds", 0)),
            seconds_per_lesson_unit=int(
                data.get(
                    "seconds_per_lesson_unit", DEFAULT_SECONDS_PER_LESSON
                )
            ),
            daily_cap_seconds=data.get("daily_cap_seconds"),
            internship_requires_confirmation=bool(
                data.get("internship_requires_confirmation", True)
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "required_seconds": self.required_seconds,
            "seconds_per_lesson_unit": self.seconds_per_lesson_unit,
            "daily_cap_seconds": self.daily_cap_seconds,
            "internship_requires_confirmation": self.internship_requires_confirmation,
        }

    def validate(self) -> None:
        if self.required_seconds < 0:
            raise RuleValidationError("required_seconds 不能为负")
        if self.seconds_per_lesson_unit <= 0:
            raise RuleValidationError("seconds_per_lesson_unit 必须为正数")
        if self.daily_cap_seconds is not None and self.daily_cap_seconds <= 0:
            raise RuleValidationError("daily_cap_seconds 必须为正数或为空")


def baseline_spec(required_seconds: int) -> RuleSpec:
    """没有任何已发布规则版本时使用的历史基线口径（45 分钟一学时、不封顶）。"""
    return RuleSpec(required_seconds=required_seconds)


@dataclass(frozen=True)
class EffectiveRule:
    """一条带适用窗口的规则版本（纯数据，供重放与解析使用）。"""

    rule_version_id: str
    spec: RuleSpec
    effective_at: datetime
    end_at: datetime | None
    state: str

    def contains(self, moment_utc: datetime) -> bool:
        if self.state not in WINDOWED_STATES:
            return False
        if moment_utc < self.effective_at:
            return False
        return self.end_at is None or moment_utc < self.end_at


def resolve_rule_version(
    occurred_at: datetime, rules: Iterable[EffectiveRule]
) -> str | None:
    """按事件业务时间返回应当绑定的规则版本 id；无覆盖时返回 None（基线）。

    已经撤回或被取代的版本，其截断后的历史窗口仍然可解析——这样服务重启
    后才导入的迟到签到也能绑定"签到当时有效"的版本。正常数据中窗口互不
    重叠；若出现分叉重叠，取生效时间最晚者，并由调用方负责审计异常。
    """
    moment = utc(occurred_at)
    candidates = [r for r in rules if r.contains(moment)]
    if not candidates:
        return None
    winner = max(candidates, key=lambda r: (r.effective_at, r.rule_version_id))
    return winner.rule_version_id


_SPEC_FIELDS = (
    "required_seconds",
    "seconds_per_lesson_unit",
    "daily_cap_seconds",
    "internship_requires_confirmation",
)


def diff_specs(old: RuleSpec, new: RuleSpec) -> dict[str, dict[str, Any]]:
    """字段级口径差异。"""
    old_d, new_d = old.to_dict(), new.to_dict()
    changes: dict[str, dict[str, Any]] = {}
    for field_name in _SPEC_FIELDS:
        if old_d[field_name] != new_d[field_name]:
            changes[field_name] = {
                "before": old_d[field_name],
                "after": new_d[field_name],
            }
    return changes
