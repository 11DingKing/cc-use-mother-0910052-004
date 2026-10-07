"""回测引擎接入版本化日历与企业行动的集成测试。"""

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from app.backtest.engine import BacktestConfig, BacktestEngine
from app.calendar import CalendarStore
from app.corporate_actions import ActionStore, ActionType, CorporateAction
from app.chan.models import RawCandle, Signal, SignalType
from app.marketdata.context import MarketContext
from app.marketdata.provider import MarketDataProvider
from decimal import Decimal

CONFIG_DIR = Path(__file__).resolve().parents[1] / "data" / "calendar"


@pytest.fixture
def provider(tmp_path):
    return MarketDataProvider(
        calendar_store=CalendarStore(config_dir=CONFIG_DIR,
                                     state_dir=tmp_path / "cal_state"),
        action_store=ActionStore(state_dir=tmp_path / "actions"),
    )


def candle(day: date, price: float) -> RawCandle:
    return RawCandle(
        timestamp=datetime(day.year, day.month, day.day),
        open=price, high=price * 1.01, low=price * 0.99,
        close=price, volume=1000.0,
    )


def signal(day: date, stype, strength=0.5):
    return Signal(
        stock_code="sz000001", signal_type=stype,
        timestamp=datetime(day.year, day.month, day.day),
        price=100.0, level="daily", strength=strength,
    )


def config(ctx, start=date(2024, 1, 1), end=date(2024, 12, 31),
           commission_rate=0.001, slippage=0.001):
    return BacktestConfig(
        stock_code="sz000001", period="daily",
        start_date=datetime(start.year, start.month, start.day),
        end_date=datetime(end.year, end.month, end.day),
        market_context=ctx,
        commission_rate=commission_rate,
        slippage=slippage,
    )


class TestHolidayWindow:
    def test_equity_curve_skips_non_sessions(self, provider):
        # 2024 春节窗口：含休市日与周末的自然日序列
        days = [date(2024, 2, d) for d in (5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 19, 20)]
        candles = [candle(d, 100.0) for d in days]
        ctx = MarketContext.build("sz000001", provider,
                                  start=days[0], end=days[-1])
        result = BacktestEngine().run(config(ctx, days[0], days[-1]), candles, [])

        curve_days = [e["trading_day"] for e in result.equity_curve]
        assert curve_days == ["2024-02-05", "2024-02-06", "2024-02-07",
                              "2024-02-08", "2024-02-19", "2024-02-20"]
        assert result.trading_days == 6
        # 被剔除的休市K线有说明，未被静默忽略
        skipped = [n for n in result.data_notes if n["type"] == "non_trading_candle"]
        assert len(skipped) == 6
        assert any("春节" in n["reason"] for n in skipped)

    def test_signal_on_holiday_shifts_to_next_session(self, provider):
        days = [date(2024, 2, d) for d in (5, 6, 7, 8, 19, 20, 21, 22)]
        prices = {5: 100, 6: 100, 7: 100, 8: 100, 19: 100, 20: 102, 21: 104, 22: 106}
        candles = [candle(d, prices[d.day]) for d in days]
        # 买入信号落在 2/14（春节假期中的工作日休市），应顺延到 2/19；卖出 2/22
        signals = [signal(date(2024, 2, 14), SignalType.BUY_1),
                   signal(date(2024, 2, 22), SignalType.SELL_1)]
        ctx = MarketContext.build("sz000001", provider,
                                  start=days[0], end=days[-1])
        result = BacktestEngine().run(config(ctx, days[0], days[-1]), candles, signals)

        assert result.total_trades == 1
        trade = result.trades[0]
        assert trade.entry_time.date() == date(2024, 2, 19)
        shift_notes = [n for n in result.data_notes if n["type"] == "signal_shift"]
        assert shift_notes[0]["trading_day"] == "2024-02-19"
        assert shift_notes[0]["reason"] == "closed"
        assert "春节" in shift_notes[0]["event"]

    def test_manifest_records_pinned_calendar_version(self, provider):
        ctx = MarketContext.build("sz000001", provider,
                                  start=date(2024, 2, 1), end=date(2024, 2, 28))
        days = [date(2024, 2, 5), date(2024, 2, 6)]
        result = BacktestEngine().run(config(ctx), [candle(d, 100.0) for d in days], [])
        cal_meta = result.market_manifest["calendar"]
        assert cal_meta["version"] == "v1"
        assert cal_meta["market"] == "CN"
        assert cal_meta["content_hash"]
        assert result.market_manifest["pinned"] is True


class TestCorporateActionsInBacktest:
    def test_cash_dividend_pays_cash_and_thins_cost(self, provider):
        provider._action_store().publish_batch("div1", [CorporateAction(
            stock_code="sz000001", ex_date=date(2024, 6, 5),
            action_type=ActionType.CASH_DIVIDEND, value=Decimal("1.00"),
        )])
        days = [date(2024, 6, 3), date(2024, 6, 4), date(2024, 6, 5),
                date(2024, 6, 6), date(2024, 6, 7)]
        candles = [candle(d, 100.0) for d in days]
        signals = [signal(days[0], SignalType.BUY_1), signal(days[-1], SignalType.SELL_1)]
        ctx = MarketContext.build("sz000001", provider, start=days[0], end=days[-1])
        cfg = config(ctx, days[0], days[-1], commission_rate=0.0, slippage=0.0)
        result = BacktestEngine().run(cfg, candles, signals)

        trade = result.trades[0]
        assert trade.cash_dividend > 0
        assert len(trade.adjustments) == 1
        adj = trade.adjustments[0]
        assert adj["action_type"] == "cash_dividend"
        assert Decimal(adj["avg_cost_after"]) < Decimal(adj["avg_cost_before"])
        assert Decimal(adj["cash_received"]) > 0
        # 无费用、价格不变、成本等额下调：盈利恰好等于收到的现金分红
        assert trade.profit == pytest.approx(trade.cash_dividend, abs=0.01)

    def test_split_doubles_shares(self, provider):
        provider._action_store().publish_batch("split1", [CorporateAction(
            stock_code="sz000001", ex_date=date(2024, 6, 5),
            action_type=ActionType.SPLIT, value=Decimal("2"),
        )])
        days = [date(2024, 6, 3), date(2024, 6, 4), date(2024, 6, 5), date(2024, 6, 6)]
        # 交易所原始价在除权日自然跳低：100 -> 50
        prices = [100.0, 100.0, 50.0, 50.0]
        candles = [candle(d, p) for d, p in zip(days, prices)]
        signals = [signal(days[0], SignalType.BUY_1), signal(days[-1], SignalType.SELL_1)]
        ctx = MarketContext.build("sz000001", provider, start=days[0], end=days[-1])
        cfg = config(ctx, days[0], days[-1], commission_rate=0.0, slippage=0.0)
        result = BacktestEngine().run(cfg, candles, signals)

        trade = result.trades[0]
        before = Decimal(trade.adjustments[0]["quantity_before"])
        after = Decimal(trade.adjustments[0]["quantity_after"])
        # 拆股按整股取整：after = round(before * 2)
        assert after == (before * 2).quantize(Decimal("1"))
        assert after == Decimal("2000")
        assert Decimal(trade.adjustments[0]["price_factor"]) == Decimal("0.500")
        # 股数翻倍、价格腰斩 -> 除权前后市值连续，不产生虚假盈亏
        assert result.equity_curve[2]["position"] == float(after)
        assert trade.profit == pytest.approx(0.0, abs=1.0)

    def test_no_actions_context_keeps_legacy_behaviour(self):
        # 不传 provider：宽松日历、无企业行动，旧回测口径不变
        days = [date(2024, 2, d) for d in (9, 10, 11)]  # 自然日含春节
        candles = [candle(d, 100.0) for d in days]
        result = BacktestEngine().run(config(MarketContext.build("sz000001"),
                                             days[0], days[-1]), candles, [])
        assert len(result.equity_curve) == 3


class TestReproducibility:
    def test_same_inputs_same_fingerprint(self, provider):
        days = [date(2024, 2, 5), date(2024, 2, 6), date(2024, 2, 7)]
        candles = [candle(d, 100.0 + i) for i, d in enumerate(days)]
        sigs = [signal(days[0], SignalType.BUY_1), signal(days[-1], SignalType.SELL_1)]

        def run():
            ctx = MarketContext.build("sz000001", provider,
                                      start=days[0], end=days[-1])
            return BacktestEngine().run(config(ctx, days[0], days[-1]), candles, sigs)

        r1, r2 = run(), run()
        assert r1.input_fingerprint == r2.input_fingerprint
        assert r1.final_capital == r2.final_capital

    def test_new_calendar_version_changes_fingerprint(self, provider):
        days = [date(2024, 2, 5), date(2024, 2, 6)]
        candles = [candle(d, 100.0) for d in days]

        def run_with(version):
            ctx = MarketContext.build("sz000001", provider,
                                      start=days[0], end=days[-1],
                                      calendar_version=version)
            return BacktestEngine().run(config(ctx, days[0], days[-1]), candles, [])

        r1 = run_with("v1")
        # 发布临时休市新版本（不影响这两天，但版本指纹不同）
        store = provider._calendar_store()
        head = store.get_version("CN")
        payload = head.canonical_payload()
        payload["special_sessions"] = list(payload["special_sessions"]) + [{
            "day": "2024-03-01", "name": "临时休市", "session_type": "closed",
        }]
        store.publish("CN", payload, description="补录")
        r2 = run_with(None)
        assert r1.input_fingerprint != r2.input_fingerprint
        assert r1.market_manifest["calendar"]["version"] == "v1"
        assert r2.market_manifest["calendar"]["version"] == "v2"
