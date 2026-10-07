"""回测引擎与交易日历/企业行动的集成测试。"""

from datetime import date, datetime, timedelta
from decimal import Decimal

from app.chan.models import RawCandle, Signal, SignalType
from app.backtest.engine import BacktestEngine, BacktestConfig
from app.marketdata.actions import (
    ActionType,
    CorporateAction,
    CorporateActionEngine,
)
from app.marketdata.calendar import CalendarEntry, TradingCalendar


BASE = datetime(2024, 6, 3)  # 周一


def candles(prices):
    return [
        RawCandle(
            timestamp=BASE + timedelta(days=i),
            open=p, high=p * 1.01, low=p * 0.99, close=p, volume=1000.0,
        )
        for i, p in enumerate(prices)
    ]


def sig(day, stype):
    return Signal("T", stype, BASE + timedelta(days=day), 10.0, "daily")


def config(days=None):
    end = BASE + timedelta(days=days if days is not None else 9)
    return BacktestConfig("T", "daily", BASE, end)


class TestCalendarInBacktest:
    def test_weekend_candles_skipped(self):
        # 无日历时全部计入；有日历时周末K线被跳过
        prices = [10 + i for i in range(10)]
        cal = TradingCalendar.from_overrides([])
        res = BacktestEngine(cal).run(config(), candles(prices), [])
        # 6-03(一)..6-12(三) 共 10 天，含一个周末 2 天
        assert res.trading_days_used == 8
        assert len(res.skipped_candles) == 2
        assert all(s["kind"] == "weekend" for s in res.skipped_candles)

    def test_signal_on_closed_day_rolls_to_next_trading_day(self):
        # 6-06（周四）临时休市；买入信号落在当天 → 顺延至 6-07
        cal = TradingCalendar.from_overrides([
            CalendarEntry(date(2024, 6, 6), False, "临时休市", "adhoc_close", record_id=1),
        ])
        res = BacktestEngine(cal).run(
            config(),
            candles([10] * 10),
            [sig(3, SignalType.BUY_1)],
        )
        assert res.trades[0].entry_time.date() == date(2024, 6, 7)
        roll = [a for a in res.adjustments if a["kind"] == "order_roll"]
        assert len(roll) == 1
        assert roll[0]["before"] == "2024-06-06"
        assert roll[0]["after"] == "2024-06-07"

    def test_signal_on_weekend_rolls_to_monday(self):
        # 6-08 周六买入信号 → 顺延到 6-10 周一
        cal = TradingCalendar.from_overrides([])
        res = BacktestEngine(cal).run(
            config(), candles([10] * 10), [sig(5, SignalType.BUY_1)]
        )
        assert res.trades[0].entry_time.date() == date(2024, 6, 10)

    def test_closed_candles_excluded_from_equity_curve(self):
        cal = TradingCalendar.from_overrides([
            CalendarEntry(date(2024, 6, 4), False, "休市", "adhoc_close"),
        ])
        res = BacktestEngine(cal).run(config(), candles([10] * 10), [])
        curve_days = {p["timestamp"][:10] for p in res.equity_curve}
        assert "2024-06-04" not in curve_days


class TestActionsInBacktest:
    def test_dividend_credits_cash_and_lowers_cost(self):
        # 6-03 买入持有到 6-12；6-06 每股派 0.5
        action = CorporateAction(
            "T", ActionType.CASH_DIVIDEND, date(2024, 6, 6),
            cash_per_share=Decimal("0.5"), client_id="d1",
        )
        eng = BacktestEngine(
            None, CorporateActionEngine([action])
        )
        res = eng.run(
            config(),
            candles([10] * 10),
            [sig(0, SignalType.BUY_1)],
        )
        trade = res.trades[0]
        # 成本下调
        assert trade.entry_price < 10 * 1.001
        cash_adjs = [
            a for a in res.adjustments
            if a["kind"] == "cash_dividend" and a["field_name"] == "cash"
        ]
        assert len(cash_adjs) == 1
        assert cash_adjs[0]["after"] > 0
        # 分红现金留在账户，最终资金包含派息
        assert res.final_capital > 100000

    def test_stock_dividend_scales_shares_in_trade(self):
        action = CorporateAction(
            "T", ActionType.STOCK_DIVIDEND, date(2024, 6, 5),
            share_ratio=Decimal("0.4"), client_id="s1",
        )
        res = BacktestEngine(
            None, CorporateActionEngine([action])
        ).run(config(), candles([10] * 10), [sig(0, SignalType.BUY_1)])
        trade = res.trades[0]
        # 全部成交约 initial/price 股，送股后 1.4 倍
        share_adj = next(
            a for a in trade.adjustments if a["kind"] == "share_factor"
        )
        assert abs(float(share_adj["after"]) / float(share_adj["before"]) - 1.4) < 1e-6

    def test_price_reference_adjustment_explains_gap(self):
        action = CorporateAction(
            "T", ActionType.CASH_DIVIDEND, date(2024, 6, 5),
            cash_per_share=Decimal("0.5"), client_id="d1",
        )
        res = BacktestEngine(
            None, CorporateActionEngine([action])
        ).run(config(), candles([10] * 10), [sig(0, SignalType.BUY_1)])
        price_adj = next(a for a in res.adjustments if a["kind"] == "price_reference")
        assert price_adj["before"] == 10.0
        assert price_adj["after"] == 9.5
        assert price_adj["detail"]["reference_only"] is True

    def test_equity_curve_marks_action_days(self):
        action = CorporateAction(
            "T", ActionType.CASH_DIVIDEND, date(2024, 6, 5),
            cash_per_share=Decimal("0.5"), client_id="d1",
        )
        res = BacktestEngine(
            None, CorporateActionEngine([action])
        ).run(config(), candles([10] * 10), [])
        marked = [p for p in res.equity_curve if p.get("corporate_actions")]
        assert len(marked) == 1
        assert marked[0]["timestamp"].startswith("2024-06-05")


class TestReproducibility:
    def test_same_inputs_deterministic(self):
        cal = TradingCalendar.from_overrides([
            CalendarEntry(date(2024, 6, 6), False, "休市", "adhoc_close"),
        ])
        action = CorporateAction(
            "T", ActionType.CASH_DIVIDEND, date(2024, 6, 5),
            cash_per_share=Decimal("0.5"), client_id="d1",
        )
        cs = candles([10 + (i % 3) for i in range(10)])
        ss = [sig(0, SignalType.BUY_1), sig(8, SignalType.SELL_1)]
        r1 = BacktestEngine(cal, CorporateActionEngine([action])).run(config(), cs, ss)
        r2 = BacktestEngine(cal, CorporateActionEngine([action])).run(config(), cs, ss)
        assert (
            [round(t.profit, 6) for t in r1.trades]
            == [round(t.profit, 6) for t in r2.trades]
        )
        assert r1.final_capital == r2.final_capital

    def test_result_carries_pin_when_provided(self):
        pin = {"calendar_fingerprint": "abc", "actions_fingerprint": "def"}
        res = BacktestEngine(pin=pin).run(config(), candles([10] * 10), [])
        assert res.market_data_pin == pin
