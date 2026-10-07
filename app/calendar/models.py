"""交易日历领域模型。

日历以 *市场（market）* 为单位（如 ``CN``/``US``/``HK``），每个市场有自己的
时区与周末定义。日历的演进通过**版本链**表达：每个版本是一份不可变快照，
临时休市、补班等调整只能追加新版本，不能改写已发布版本。

所有日期均以市场所在时区的“本地日历日”（naive ``date``）表示，避免跨时区
订单/估值日期错位；外部传入的 ``datetime`` 统一先经
:meth:`TradingCalendar.local_date` 归一化。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import FrozenSet, Optional, Sequence

from app.calendar.errors import CalendarValidationError


# ---------------------------------------------------------------------------
# 事件
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class HolidayEvent:
    """法定节假日休市（整日不交易）。"""

    day: date
    name: str

    def to_dict(self) -> dict:
        return {"day": self.day.isoformat(), "name": self.name}

    @classmethod
    def from_dict(cls, raw: dict) -> "HolidayEvent":
        return cls(day=date.fromisoformat(raw["day"]), name=str(raw["name"]))


@dataclass(frozen=True)
class SpecialSession:
    """特殊交易日安排。

    ``session_type`` 取值：

    - ``closed``      临时休市（突发休市，事后补录也走新版本）
    - ``makeup``      补班交易日（周末补开市）
    - ``half``        半日市（仍是交易日，仅提前收市，``close_time`` 生效）
    """

    day: date
    name: str
    session_type: str  # closed / makeup / half
    close_time: Optional[str] = None  # HH:MM，半日市收市时间
    reason: Optional[str] = None      # 调整原因，接口需能说明“为何调整”

    def to_dict(self) -> dict:
        return {
            "day": self.day.isoformat(),
            "name": self.name,
            "session_type": self.session_type,
            "close_time": self.close_time,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "SpecialSession":
        stype = str(raw["session_type"])
        if stype not in ("closed", "makeup", "half"):
            raise CalendarValidationError(
                f"未知特殊市态类型: {stype!r}（仅支持 closed/makeup/half）"
            )
        return cls(
            day=date.fromisoformat(raw["day"]),
            name=str(raw["name"]),
            session_type=stype,
            close_time=raw.get("close_time"),
            reason=raw.get("reason"),
        )


# ---------------------------------------------------------------------------
# 版本
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CalendarVersion:
    """某市场日历的一个不可变版本快照。"""

    market: str
    version: str
    tzname: str
    weekend: FrozenSet[int]                          # Monday=0 ... Sunday=6
    holidays: FrozenSet[HolidayEvent] = field(default_factory=frozenset)
    special_sessions: FrozenSet[SpecialSession] = field(default_factory=frozenset)
    parent_version: Optional[str] = None
    description: str = ""
    published_at: Optional[datetime] = None

    # -- 规范化与哈希 -------------------------------------------------------

    def canonical_payload(self) -> dict:
        """用于计算内容哈希的规范化字典（键序、日期格式均确定）。"""
        return {
            "market": self.market,
            "tzname": self.tzname,
            "weekend": sorted(self.weekend),
            "holidays": sorted((h.to_dict() for h in self.holidays),
                               key=lambda x: x["day"]),
            "special_sessions": sorted(
                (s.to_dict() for s in self.special_sessions), key=lambda x: x["day"]
            ),
        }

    def content_hash(self) -> str:
        # 哈希覆盖市态内容与描述，保证已发布版本任何字段被改动都能被发现；
        # version/parent_version 是链上的身份字段，不纳入内容哈希。
        payload = self.canonical_payload()
        payload["description"] = self.description
        blob = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return hashlib.sha256(blob).hexdigest()

    def to_dict(self) -> dict:
        payload = self.canonical_payload()
        payload.update({
            "version": self.version,
            "parent_version": self.parent_version,
            "description": self.description,
            "content_hash": self.content_hash(),
            "published_at": self.published_at.isoformat() if self.published_at else None,
        })
        return payload

    @classmethod
    def from_dict(cls, raw: dict, published_at: Optional[datetime] = None) -> "CalendarVersion":
        market = str(raw["market"])
        version = str(raw["version"])
        tzname = str(raw.get("tzname", "Asia/Shanghai"))
        weekend_raw = raw.get("weekend", [5, 6])
        try:
            weekend = frozenset(int(d) for d in weekend_raw)
        except (TypeError, ValueError):
            raise CalendarValidationError(f"市场 {market} 版本 {version} 的 weekend 必须是整数列表")
        if not weekend <= frozenset(range(7)):
            raise CalendarValidationError(f"市场 {market} 版本 {version} 的 weekend 取值须在 0-6")

        holidays = frozenset(HolidayEvent.from_dict(h) for h in raw.get("holidays", []))
        specials = frozenset(SpecialSession.from_dict(s) for s in raw.get("special_sessions", []))
        return cls(
            market=market,
            version=version,
            tzname=tzname,
            weekend=weekend,
            holidays=holidays,
            special_sessions=specials,
            parent_version=raw.get("parent_version"),
            description=raw.get("description", ""),
            published_at=published_at,
        )


# ---------------------------------------------------------------------------
# 查询视图（由版本解析得到）
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CalendarResolution:
    """对一个外部日期的归一化结果，解释订单日为何落在目标交易日。"""

    requested_day: date
    trading_day: date
    shifted: bool
    reason: str
    event_name: Optional[str] = None


@dataclass(frozen=True)
class CalendarView:
    """某个日历版本在内存中的查询视图。"""

    version: CalendarVersion
    closed_days: FrozenSet[date]
    makeup_days: FrozenSet[date]
    half_days: FrozenSet[date]
    events_by_day: dict  # date -> 说明文本

    def is_trading_day(self, day: date) -> bool:
        if day in self.makeup_days:
            return True
        if day in self.closed_days:
            return False
        return day.weekday() not in self.version.weekend

    def reason_for_day(self, day: date) -> Optional[str]:
        """返回该日非标准市态的人类可读说明；标准交易日返回 None。"""
        return self.events_by_day.get(day)

    def trading_days(self, start: date, end: date) -> Sequence[date]:
        if end < start:
            return []
        days = []
        cur = start
        while cur <= end:
            if self.is_trading_day(cur):
                days.append(cur)
            cur = _add_days(cur, 1)
        return days

    def next_trading_day(self, day: date) -> date:
        cur = _add_days(day, 1)
        for _ in range(_MAX_SCAN_DAYS):
            if self.is_trading_day(cur):
                return cur
            cur = _add_days(cur, 1)
        raise CalendarValidationError(
            f"在 {_MAX_SCAN_DAYS} 天内未找到 {day} 之后的交易日，日历覆盖范围可能不足"
        )

    def previous_trading_day(self, day: date) -> date:
        cur = _add_days(day, -1)
        for _ in range(_MAX_SCAN_DAYS):
            if self.is_trading_day(cur):
                return cur
            cur = _add_days(cur, -1)
        raise CalendarValidationError(
            f"在 {_MAX_SCAN_DAYS} 天内未找到 {day} 之前的交易日，日历覆盖范围可能不足"
        )

    def resolve(self, day: date) -> CalendarResolution:
        """把请求日归一化到实际交易日，并说明调整原因。"""
        if self.is_trading_day(day):
            return CalendarResolution(
                requested_day=day, trading_day=day, shifted=False, reason="trading_day"
            )
        target = self.next_trading_day(day)
        if day in self.closed_days:
            label = self.events_by_day.get(day, "休市日")
            reason = "closed"
        else:
            label = "周末休市"
            reason = "weekend"
        return CalendarResolution(
            requested_day=day,
            trading_day=target,
            shifted=True,
            reason=reason,
            event_name=label,
        )


_MAX_SCAN_DAYS = 3660


def _add_days(day: date, delta: int) -> date:
    from datetime import timedelta

    return day + timedelta(days=delta)


def build_view(version: CalendarVersion) -> CalendarView:
    """根据版本快照构建查询视图，校验同一日期上的事件不冲突。"""
    closed: dict = {}
    makeup: dict = {}
    half: dict = {}
    # day -> (来源桶, 说明)，用于检测同一天出现互斥市态
    owner: dict = {}

    def _claim(day: date, bucket: str, label: str, target: dict):
        if day in owner:
            prior_bucket, prior_label = owner[day]
            if prior_bucket == bucket:
                raise CalendarValidationError(
                    f"市场 {version.market} 版本 {version.version} 中 {day} "
                    f"市态重复定义：{prior_label} / {label}"
                )
            raise CalendarValidationError(
                f"市场 {version.market} 版本 {version.version} 中 {day} 市态冲突："
                f"{prior_label} vs {label}"
            )
        owner[day] = (bucket, label)
        target[day] = label

    for h in version.holidays:
        _claim(h.day, "closed", f"法定节假日休市：{h.name}", closed)

    for s in version.special_sessions:
        label = _special_label(s)
        if s.session_type == "closed":
            _claim(s.day, "closed", label, closed)
        elif s.session_type == "makeup":
            _claim(s.day, "makeup", label, makeup)
        else:  # half
            _claim(s.day, "half", label, half)

    # 查询说明统一映射为人类可读文本（非元组）
    events = {day: label for day, (_bucket, label) in owner.items()}
    return CalendarView(
        version=version,
        closed_days=frozenset(closed),
        makeup_days=frozenset(makeup),
        half_days=frozenset(half),
        events_by_day=events,
    )


def _special_label(s: SpecialSession) -> str:
    if s.session_type == "closed":
        base = f"临时休市：{s.name}"
    elif s.session_type == "makeup":
        base = f"补班交易日：{s.name}"
    else:
        base = f"半日市：{s.name}"
        if s.close_time:
            base += f"（{s.close_time} 收市）"
    if s.reason:
        base += f"（{s.reason}）"
    return base
