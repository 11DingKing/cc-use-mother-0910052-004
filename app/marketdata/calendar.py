"""版本化交易日历。

日历以“交易所本地日期”为唯一键，跨时区的输入时间会先换算到交易所时区
再取日期。日历快照不可变：临时休市、补班通过覆盖行实现，覆盖行带原因与
修订来源，解析结果保留“为什么这一天开市/休市”的完整痕迹。

同一个日历快照由 ``version_id`` 与 ``fingerprint`` 标识；回测/账本固定引用
某个快照后，后续导入的新版本不会影响它。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo


# 各市场默认时区（全部为本地可配置项，这里仅提供开箱默认值）
MARKET_TIMEZONES = {
    "CN": "Asia/Shanghai",
    "HK": "Asia/Hong_Kong",
    "US": "America/New_York",
}

# 各市场默认收盘时刻（交易所本地时间），日终估值以此为界
MARKET_SESSION_CLOSE = {
    "CN": time(15, 0),
    "HK": time(16, 0),
    "US": time(16, 0),
}

# 各市场常规周末：ISO 星期，周一=1 ... 周日=7
DEFAULT_WEEKENDS = {
    "CN": (6, 7),
    "HK": (6, 7),
    "US": (6, 7),
}


@dataclass(frozen=True)
class CalendarEntry:
    """某一个交易日/休市日的生效记录。"""

    day: date
    is_open: bool
    reason: str                       # 开市/休市原因，例如“国庆节”“周末补班”
    kind: str                         # weekend | holiday | makeup | adhoc_close | regular
    record_id: Optional[int] = None   # 持久化记录主键，便于追溯
    revised_record_id: Optional[int] = None  # 若覆盖了更早的记录，指向被覆盖记录


class MarketClosedError(Exception):
    """在非交易日下单时抛出，携带下一交易日，绝不静默顺延。"""

    def __init__(self, day: date, reason: str, next_day: Optional[date] = None):
        self.day = day
        self.reason = reason
        self.next_day = next_day
        message = f"{day.isoformat()} 非交易日（{reason}）"
        if next_day is not None:
            message += f"，下一交易日为 {next_day.isoformat()}"
        super().__init__(message)


@dataclass(frozen=True)
class TradingCalendar:
    """不可变的交易日历版本快照。

    ``entries`` 只保存有显式覆盖（休市/补班/临时休市）的日期；其余日期按
    周末规则推导。快照内容由 ``fingerprint`` 标识，账本可凭它复现。
    """

    market: str
    timezone: str
    weekends: Tuple[int, ...]
    session_close: time
    entries: Dict[date, CalendarEntry] = field(default_factory=dict)
    version_id: int = 1
    generated_at: Optional[datetime] = None
    fingerprint: str = ""

    # ------------------------------------------------------------------
    # 构造
    # ------------------------------------------------------------------

    @classmethod
    def from_overrides(
        cls,
        overrides: Iterable[CalendarEntry],
        market: str = "CN",
        version_id: int = 1,
        generated_at: Optional[datetime] = None,
        fingerprint: str = "",
        timezone: Optional[str] = None,
        weekends: Optional[Tuple[int, ...]] = None,
        session_close: Optional[time] = None,
    ) -> "TradingCalendar":
        entries: Dict[date, CalendarEntry] = {}
        for entry in overrides:
            # 同一日期多条记录时，record_id 最大者（最新修订）生效，并保留来源
            old = entries.get(entry.day)
            if old is None or (entry.record_id or 0) >= (old.record_id or 0):
                entries[entry.day] = (
                    entry if entry.revised_record_id or old is None
                    else CalendarEntry(
                        day=entry.day,
                        is_open=entry.is_open,
                        reason=entry.reason,
                        kind=entry.kind,
                        record_id=entry.record_id,
                        revised_record_id=old.record_id,
                    )
                )
        return cls(
            market=market,
            timezone=timezone or MARKET_TIMEZONES.get(market, "UTC"),
            weekends=tuple(weekends or DEFAULT_WEEKENDS.get(market, (6, 7))),
            session_close=session_close or MARKET_SESSION_CLOSE.get(market, time(16, 0)),
            entries=entries,
            version_id=version_id,
            generated_at=generated_at,
            fingerprint=fingerprint,
        )

    # ------------------------------------------------------------------
    # 时区与日期归一化
    # ------------------------------------------------------------------

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def local_date(self, moment: datetime) -> date:
        """把任意时刻换算成交易所本地日期。

        - 带时区信息：换算到交易所时区后取日期（跨时区安全）；
        - 无时区信息：按接口约定视为交易所本地时间（订单/行情统一口径）。
        """
        if moment.tzinfo is not None:
            return moment.astimezone(self.tz).date()
        return moment.date()

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def entry_for(self, day: date) -> CalendarEntry:
        override = self.entries.get(day)
        if override is not None:
            return override
        if day.isoweekday() in self.weekends:
            return CalendarEntry(
                day=day, is_open=False, reason="周末休市", kind="weekend"
            )
        return CalendarEntry(
            day=day, is_open=True, reason="常规交易日", kind="regular"
        )

    def is_trading_day(self, day: date) -> bool:
        return self.entry_for(day).is_open

    def explain_day(self, day: date) -> CalendarEntry:
        """返回某日开市/休市的完整解释（含修订链）。"""
        return self.entry_for(day)

    def next_trading_day(self, day: date, max_skip: int = 60) -> Optional[date]:
        cur = day + timedelta(days=1)
        for _ in range(max_skip):
            if self.is_trading_day(cur):
                return cur
            cur += timedelta(days=1)
        return None

    def prev_trading_day(self, day: date, max_skip: int = 60) -> Optional[date]:
        cur = day - timedelta(days=1)
        for _ in range(max_skip):
            if self.is_trading_day(cur):
                return cur
            cur -= timedelta(days=1)
        return None

    def trading_days(self, start: date, end: date) -> List[date]:
        """闭区间 [start, end] 内的交易日列表。"""
        if end < start:
            return []
        result = []
        cur = start
        while cur <= end:
            if self.is_trading_day(cur):
                result.append(cur)
            cur += timedelta(days=1)
        return result

    def count_trading_days(self, start: date, end: date) -> int:
        return len(self.trading_days(start, end))

    # ------------------------------------------------------------------
    # 下单口径
    # ------------------------------------------------------------------

    def resolve_order_day(
        self, moment: Optional[datetime] = None, *, auto_roll: bool = False
    ) -> Tuple[date, Optional[date]]:
        """解析订单的成交归属交易日。

        休市日下单默认抛出 :class:`MarketClosedError`（不静默顺延）；
        ``auto_roll=True`` 时（例如回测引擎显式要求跨休市窗口顺延）
        返回 ``(成交日, 原始日)``，调用方必须在调整说明中记录该顺延。
        """
        moment = moment or datetime.now(self.tz)
        day = self.local_date(moment)
        entry = self.entry_for(day)
        if entry.is_open:
            return day, None
        nxt = self.next_trading_day(day)
        if not auto_roll:
            raise MarketClosedError(day, entry.reason, nxt)
        if nxt is None:
            raise MarketClosedError(day, entry.reason, None)
        return nxt, day

    def session_close_moment(self, day: date) -> datetime:
        """某日收盘时刻（交易所本地、带时区），日终估值的分界线。"""
        return datetime.combine(day, self.session_close, tzinfo=self.tz)

    def describe(self) -> Dict[str, object]:
        return {
            "market": self.market,
            "timezone": self.timezone,
            "version_id": self.version_id,
            "fingerprint": self.fingerprint,
            "generated_at": self.generated_at.isoformat() if self.generated_at else None,
            "override_count": len(self.entries),
        }
