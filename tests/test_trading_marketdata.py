"""模拟交易与交易日历/企业行动的集成测试。"""

from datetime import date, datetime
from decimal import Decimal

import pytest

from app.trading.base import Order, OrderSide, OrderType, OrderStatus
from app.trading.simulation_adapter import SimulationAdapter
from app.marketdata.actions import (
    ActionType,
    CorporateAction,
    CorporateActionEngine,
)
from app.marketdata.calendar import CalendarEntry, TradingCalendar


def make_adapter(calendar=None, actions=None, cash="100000"):
    adapter = SimulationAdapter({"initial_cash": cash})
    adapter.connect()
    adapter.bind_market_data(calendar, actions)
    return adapter


def order(code="600000", qty=100, price=10.0, created_at=None):
    return Order(
        order_id="O1",
        stock_code=code,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=qty,
        price=Decimal(str(price)),
        created_at=created_at or datetime(2024, 10, 8, 10, 0),
    )


class TestClosedDayRejection:
    def test_order_on_holiday_rejected_with_next_day(self):
        cal = TradingCalendar.from_overrides([
            CalendarEntry(date(2024, 10, 8), False, "临时休市", "adhoc_close"),
        ])
        adapter = make_adapter(cal)
        adapter.set_quote("600000", 10.0)
        result = adapter.place_order(order(created_at=datetime(2024, 10, 8, 10, 0)))
        assert result.status == OrderStatus.REJECTED
        assert "非交易日" in result.error_message
        assert "2024-10-09" in result.error_message
        # 没有成交、没有持仓
        assert adapter.get_position("600000") is None

    def test_order_on_weekend_rejected(self):
        cal = TradingCalendar.from_overrides([])
        adapter = make_adapter(cal)
        adapter.set_quote("600000", 10.0)
        # 2024-10-05 周六
        result = adapter.place_order(order(created_at=datetime(2024, 10, 5, 10, 0)))
        assert result.status == OrderStatus.REJECTED
        assert "周末休市" in result.error_message

    def test_order_on_open_day_fills(self):
        cal = TradingCalendar.from_overrides([])
        adapter = make_adapter(cal)
        adapter.set_quote("600000", 10.0)
        result = adapter.place_order(order(created_at=datetime(2024, 10, 8, 10, 0)))
        assert result.status == OrderStatus.FILLED

    def test_without_calendar_legacy_behavior_kept(self):
        adapter = make_adapter(calendar=None)
        adapter.set_quote("600000", 10.0)
        result = adapter.place_order(order(created_at=datetime(2024, 10, 5, 10, 0)))
        assert result.status == OrderStatus.FILLED


class TestEndOfDayActions:
    def test_cash_dividend_end_of_day(self):
        actions = CorporateActionEngine([
            CorporateAction(
                "600000", ActionType.CASH_DIVIDEND, date(2024, 6, 3),
                cash_per_share=Decimal("0.5"), client_id="d1",
            )
        ])
        adapter = make_adapter(actions=actions)
        adapter.set_quote("600000", 10.0)
        adapter.buy("600000", 1000, 10.0)
        cash_before = adapter.get_account().available_cash

        results = adapter.apply_corporate_actions(date(2024, 6, 3))
        assert len(results) == 1
        pos = adapter.get_position("600000")
        assert pos.quantity == 1000
        assert pos.avg_cost == Decimal("9.5")
        assert adapter.get_account().available_cash == cash_before + Decimal("500")
        # 调整留痕
        kinds = {a["kind"] for a in adapter.adjustments}
        assert "cash_dividend" in kinds and "price_reference" in kinds

    def test_stock_dividend_integral_shares(self):
        actions = CorporateActionEngine([
            CorporateAction(
                "600000", ActionType.STOCK_DIVIDEND, date(2024, 6, 6),
                share_ratio=Decimal("0.4"), client_id="s1",
            )
        ])
        adapter = make_adapter(actions=actions)
        adapter.set_quote("600000", 10.0)
        adapter.buy("600000", 1000, 10.0)

        results = adapter.apply_corporate_actions(date(2024, 6, 6))
        assert results[0]["quantity_after"] == 1400
        pos = adapter.get_position("600000")
        assert pos.quantity == 1400
        assert pos.available_quantity == 1400
        assert pos.avg_cost == (Decimal("10") / Decimal("1.4")).quantize(
            Decimal("0.00000001")
        )

    def test_no_actions_day_is_noop(self):
        actions = CorporateActionEngine([
            CorporateAction(
                "600000", ActionType.CASH_DIVIDEND, date(2024, 6, 3),
                cash_per_share=Decimal("0.5"), client_id="d1",
            )
        ])
        adapter = make_adapter(actions=actions)
        adapter.set_quote("600000", 10.0)
        adapter.buy("600000", 100, 10.0)
        assert adapter.apply_corporate_actions(date(2024, 7, 1)) == []

    def test_result_includes_formula_and_before_after(self):
        actions = CorporateActionEngine([
            CorporateAction(
                "600000", ActionType.SPLIT, date(2024, 7, 1),
                share_ratio=Decimal("1"), client_id="sp1",
            )
        ])
        adapter = make_adapter(actions=actions)
        adapter.set_quote("600000", 20.0)
        adapter.buy("600000", 100, 20.0)
        results = adapter.apply_corporate_actions(date(2024, 7, 1))
        adj = results[0]["adjustments"][0]
        assert adj["before"] == 100.0
        assert adj["after"] == 200.0
        assert adj["formula"]
        assert results[0]["price_reference"]["theoretical_price"] == 10.0
