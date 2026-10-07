"""交易日历查询入口。

:class:`TradingCalendar` 包一个不可变版本视图，对外统一提供：

- 时区归一化（外部 ``datetime`` 先转到市场本地日历日再判断市态）；
- 交易日判定、相邻/区间交易日；
- 把落在休市日的订单或信号日期**顺延**到下一交易日，并给出解释。

另提供 :meth:`TradingCalendar.permissive` —— 每天都是交易日的内置日历，
用于未配置本地日历或纯数据驱动回测（交易日 = 有K线的日子），保证旧行为不变。
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Iterable, Optional, Sequence
from zoneinfo import ZoneInfo

from app.calendar.models import (
    CalendarResolution,
    CalendarVersion,
    CalendarView,
    build_view,
)


class TradingCalendar:
    """版本化交易日历。"""

    def __init__(self, view: CalendarView):
        self._view = view
        self._tz = ZoneInfo(view.version.tzname)

    # -- 构造器 -------------------------------------------------------------

    @classmethod
    def from_version(cls, version: CalendarVersion) -> "TradingCalendar":
        return cls(build_view(version))

    @classmethod
    def permissive(cls, tzname: str = "UTC") -> "TradingCalendar":
        """所有自然日均视为交易日（不做任何休市调整）。"""
        version = CalendarVersion(
            market="*",
            version="builtin-permissive-v1",
            tzname=tzname,
            weekend=frozenset(),
            holidays=frozenset(),
            special_sessions=frozenset(),
            description="内置日历：不区分休市日，所有日期均按交易日处理",
        )
        return cls(build_view(version))

    # -- 版本信息 -----------------------------------------------------------

    @property
    def market(self) -> str:
        return self._view.version.market

    @property
    def version(self) -> str:
        return self._view.version.version

    @property
    def content_hash(self) -> str:
        return self._view.version.content_hash()

    @property
    def tzname(self) -> str:
        return self._view.version.tzname

    def manifest(self) -> dict:
        """账本/回测结果中记录的日历版本指纹。"""
        return {
            "market": self.market,
            "version": self.version,
            "tzname": self.tzname,
            "content_hash": self.content_hash,
        }

    # -- 时区与日期 ---------------------------------------------------------

    def local_date(self, moment: datetime) -> date:
        """把时间点归一化为市场本地日历日。

        naive datetime 约定本身就是市场当地挂钟时间；aware datetime 做时区换算。
        """
        if moment.tzinfo is None:
            return moment.date()
        return moment.astimezone(self._tz).date()

    def is_trading_day(self, day: date) -> bool:
        return self._view.is_trading_day(day)

    def is_session_moment(self, moment: datetime) -> bool:
        """对带时分的时间点做半日市收市时间判断（日线场景等价于交易日判定）。"""
        day = self.local_date(moment)
        if not self._view.is_trading_day(day):
            return False
        close_at = self.session_close(day)
        if close_at is not None:
            # naive 挂钟时间直接比较；aware 先转市场时区
            wall = moment if moment.tzinfo is None else moment.astimezone(self._tz)
            return wall.time() <= close_at
        return True

    def session_close(self, day: date) -> Optional[time]:
        label_day = day
        for s in self._view.version.special_sessions:
            if s.day == label_day and s.session_type == "half" and s.close_time:
                hh, mm = s.close_time.split(":")
                return time(int(hh), int(mm))
        return None

    def reason_for_day(self, day: date) -> Optional[str]:
        return self._view.reason_for_day(day)

    def next_trading_day(self, day: date) -> date:
        return self._view.next_trading_day(day)

    def previous_trading_day(self, day: date) -> date:
        return self._view.previous_trading_day(day)

    def trading_days(self, start: date, end: date) -> Sequence[date]:
        return self._view.trading_days(start, end)

    def resolve(self, day: date) -> CalendarResolution:
        """把请求日归一化到实际交易日（休市则顺延），并解释原因。"""
        return self._view.resolve(day)

    def resolve_moment(self, moment: datetime) -> CalendarResolution:
        """:meth:`resolve` 的 datetime 版本，先做跨时区归一化。"""
        return self._view.resolve(self.local_date(moment))

    def filter_sessions(self, days: Iterable[date]) -> Sequence[date]:
        return [d for d in days if self._view.is_trading_day(d)]
