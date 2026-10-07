"""交易日历核心逻辑测试：周末规则、时区、休市顺延、解释。"""

from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

import pytest

from app.marketdata.calendar import (
    CalendarEntry,
    MarketClosedError,
    TradingCalendar,
)


def make_calendar(overrides=(), market="CN", **kw):
    return TradingCalendar.from_overrides(overrides, market=market, **kw)


class TestWeekendAndRegular:
    def test_weekend_closed(self):
        cal = make_calendar()
        # 2024-10-05 周六，2024-10-06 周日
        assert cal.is_trading_day(date(2024, 10, 4)) is True
        assert cal.is_trading_day(date(2024, 10, 5)) is False
        assert cal.is_trading_day(date(2024, 10, 6)) is False
        assert cal.entry_for(date(2024, 10, 5)).kind == "weekend"
        assert cal.entry_for(date(2024, 10, 5)).reason == "周末休市"

    def test_regular_day_open(self):
        cal = make_calendar()
        entry = cal.entry_for(date(2024, 10, 7))
        assert entry.is_open
        assert entry.kind == "regular"


class TestOverrides:
    def test_holiday_override(self):
        cal = make_calendar([
            CalendarEntry(date(2024, 10, 1), False, "国庆节", "holiday", record_id=1),
            CalendarEntry(date(2024, 10, 12), True, "周末补班", "makeup", record_id=2),
        ])
        # 工作日国庆休市
        assert cal.is_trading_day(date(2024, 10, 1)) is False
        assert cal.entry_for(date(2024, 10, 1)).reason == "国庆节"
        # 周六补班开市
        assert cal.is_trading_day(date(2024, 10, 12)) is True
        assert cal.entry_for(date(2024, 10, 12)).kind == "makeup"

    def test_adhoc_close_with_next_day(self):
        cal = make_calendar([
            CalendarEntry(date(2024, 10, 11), False, "台风临时休市", "adhoc_close", record_id=1),
        ])
        assert cal.next_trading_day(date(2024, 10, 11)) == date(2024, 10, 14)
        assert cal.prev_trading_day(date(2024, 10, 11)) == date(2024, 10, 10)

    def test_trading_days_skips_holiday_window(self):
        cal = make_calendar([
            CalendarEntry(d, False, "国庆", "holiday")
            for d in [date(2024, 10, 1), date(2024, 10, 2),
                      date(2024, 10, 3), date(2024, 10, 4)]
        ])
        days = cal.trading_days(date(2024, 9, 30), date(2024, 10, 8))
        assert date(2024, 10, 1) not in days
        assert date(2024, 10, 4) not in days
        assert date(2024, 10, 7) in days
        assert date(2024, 9, 30) in days


class TestTimezone:
    def test_utc_moment_converts_to_us_local_date(self):
        cal = make_calendar(market="US")
        # UTC 2024-07-03 22:00 → 纽约 18:00，仍是 7 月 3 日
        moment = datetime(2024, 7, 3, 22, 0, tzinfo=timezone.utc)
        assert cal.local_date(moment) == date(2024, 7, 3)

    def test_utc_early_morning_rolls_back_us_date(self):
        cal = make_calendar(market="US")
        # UTC 2024-07-04 03:00 → 纽约 2024-07-03 23:00（夏令时 UTC-4）
        moment = datetime(2024, 7, 4, 3, 0, tzinfo=timezone.utc)
        assert cal.local_date(moment) == date(2024, 7, 3)

    def test_shanghai_offset(self):
        cal = make_calendar(market="CN")
        # UTC 2024-10-07 18:00 → 上海 2024-10-08 02:00
        moment = datetime(2024, 10, 7, 18, 0, tzinfo=timezone.utc)
        assert cal.local_date(moment) == date(2024, 10, 8)

    def test_naive_datetime_treated_as_local(self):
        cal = make_calendar(market="US")
        assert cal.local_date(datetime(2024, 7, 4, 10, 0)) == date(2024, 7, 4)

    def test_session_close_moment_is_timezone_aware(self):
        cal = make_calendar(market="CN")
        m = cal.session_close_moment(date(2024, 10, 8))
        assert m.tzinfo == ZoneInfo("Asia/Shanghai")
        assert m.timetz() == time(15, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


class TestOrderDayResolution:
    def test_closed_day_raises_with_next_day(self):
        cal = make_calendar([
            CalendarEntry(date(2024, 10, 1), False, "国庆节", "holiday"),
        ])
        with pytest.raises(MarketClosedError) as exc:
            cal.resolve_order_day(datetime(2024, 10, 1, 10, 0))
        assert exc.value.next_day == date(2024, 10, 2)
        assert "国庆节" in str(exc.value)

    def test_closed_day_auto_roll_records_origin(self):
        golden_week = [
            CalendarEntry(d, False, "国庆", "holiday")
            for d in [date(2024, 10, 1), date(2024, 10, 2),
                      date(2024, 10, 3), date(2024, 10, 4),
                      date(2024, 10, 7)]
        ]
        cal = make_calendar(golden_week)
        exec_day, origin = cal.resolve_order_day(
            datetime(2024, 10, 1, 10, 0), auto_roll=True
        )
        assert exec_day == date(2024, 10, 8)
        assert origin == date(2024, 10, 1)

    def test_open_day_no_roll(self):
        cal = make_calendar()
        exec_day, origin = cal.resolve_order_day(datetime(2024, 10, 7, 10, 0))
        assert exec_day == date(2024, 10, 7)
        assert origin is None


class TestRevisionChain:
    def test_latest_record_wins_and_keeps_source(self):
        cal = make_calendar([
            CalendarEntry(date(2024, 10, 1), False, "国庆节", "holiday", record_id=1),
            CalendarEntry(date(2024, 10, 1), True, "临时开市", "makeup", record_id=2),
        ])
        entry = cal.entry_for(date(2024, 10, 1))
        assert entry.is_open is True
        assert entry.record_id == 2
        assert entry.revised_record_id == 1
