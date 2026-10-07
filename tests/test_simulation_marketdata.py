"""模拟交易适配器：订单交易日归属、除权除息应用、日终估值。"""

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.calendar import CalendarStore
from app.corporate_actions import ActionStore, ActionType, CorporateAction
from app.marketdata.provider import MarketDataProvider
from app.trading.base import OrderSide, OrderType, Order
from app.trading.simulation_adapter import SimulationAdapter

CONFIG_DIR = Path(__file__).resolve().parents[1] / "data" / "calendar"


@pytest.fixture
def provider(tmp_path):
    return MarketDataProvider(
        calendar_store=CalendarStore(config_dir=CONFIG_DIR,
                                     state_dir=tmp_path / "cal"),
        action_store=ActionStore(state_dir=tmp_path / "act"),
    )


@pytest.fixture
def adapter(provider):
    a = SimulationAdapter({"initial_cash": 100000}, market_provider=provider)
    a.connect()
    return a


class TestOrderTradingDay:
    def test_order_on_trading_day(self, adapter):
        adapter.set_quote("sz000001", 10.0)
        order = Order(
            order_id="O1", stock_code="sz000001", side=OrderSide.BUY,
            order_type=OrderType.LIMIT, quantity=1000, price=Decimal("10"),
            created_at=datetime(2024, 2, 8, 10, 0),
        )
        filled = adapter.place_order(order)
        assert filled.trading_day == "2024-02-08"
        assert filled.requested_day == "2024-02-08"
        assert filled.market_manifest["calendar"]["version"] == "v1"

    def test_order_on_holiday_is_annotated_shifted(self, adapter):
        adapter.set_quote("sz000001", 10.0)
        order = Order(
            order_id="O2", stock_code="sz000001", side=OrderSide.BUY,
            order_type=OrderType.LIMIT, quantity=1000, price=Decimal("10"),
            created_at=datetime(2024, 2, 14, 10, 0),  # 春节休市工作日
        )
        filled = adapter.place_order(order)
        assert filled.trading_day == "2024-02-19"
        assert filled.requested_day == "2024-02-14"
        assert "顺延" in filled.calendar_note
        assert "春节" in filled.calendar_note

    def test_order_without_provider_has_no_annotation(self):
        a = SimulationAdapter({"initial_cash": 100000})
        a.connect()
        a.set_quote("sz000001", 10.0)
        order = Order(
            order_id="O3", stock_code="sz000001", side=OrderSide.BUY,
            order_type=OrderType.LIMIT, quantity=1000, price=Decimal("10"),
        )
        filled = a.place_order(order)
        assert filled.trading_day is None
        assert filled.calendar_note is None


class TestCorporateActionsOnPositions:
    def _buy(self, adapter):
        adapter.set_quote("sz000001", 10.0)
        order = Order(
            order_id="B1", stock_code="sz000001", side=OrderSide.BUY,
            order_type=OrderType.LIMIT, quantity=1000, price=Decimal("10"),
            created_at=datetime(2024, 6, 3, 10, 0),
        )
        adapter.place_order(order)

    def test_split_application_and_idempotency(self, adapter, provider):
        self._buy(adapter)
        provider._action_store().publish_batch("s1", [CorporateAction(
            stock_code="sz000001", ex_date=date(2024, 6, 5),
            action_type=ActionType.SPLIT, value=Decimal("2"),
        )])
        notes = adapter.apply_corporate_actions("sz000001", date(2024, 6, 5))
        assert len(notes) == 1
        pos = adapter.get_position("sz000001")
        assert pos.quantity == 2000
        assert pos.avg_cost == Decimal("5.000")
        assert len(pos.adjustments) == 1

        # 重复执行同日行动：幂等，不再调整
        again = adapter.apply_corporate_actions("sz000001", date(2024, 6, 5))
        assert again == []
        pos = adapter.get_position("sz000001")
        assert pos.quantity == 2000
        assert len(pos.adjustments) == 1

    def test_cash_dividend_credits_account(self, adapter, provider):
        self._buy(adapter)
        cash_before = adapter.get_account().available_cash
        provider._action_store().publish_batch("d1", [CorporateAction(
            stock_code="sz000001", ex_date=date(2024, 6, 5),
            action_type=ActionType.CASH_DIVIDEND, value=Decimal("0.50"),
        )])
        notes = adapter.apply_corporate_actions("sz000001", date(2024, 6, 5))
        assert Decimal(notes[0]["cash_received"]) == Decimal("500.00")
        assert adapter.get_account().available_cash == cash_before + Decimal("500.00")
        assert adapter.get_position("sz000001").avg_cost == Decimal("9.500")

    def test_no_position_no_change(self, adapter, provider):
        provider._action_store().publish_batch("d2", [CorporateAction(
            stock_code="sz000001", ex_date=date(2024, 6, 5),
            action_type=ActionType.CASH_DIVIDEND, value=Decimal("0.50"),
        )])
        assert adapter.apply_corporate_actions("sz000001", date(2024, 6, 5)) == []


class TestMarkToMarket:
    def test_snapshot_valuation_and_manifest(self, adapter):
        adapter.set_quote("sz000001", 10.0)
        order = Order(
            order_id="B2", stock_code="sz000001", side=OrderSide.BUY,
            order_type=OrderType.LIMIT, quantity=1000, price=Decimal("10"),
            created_at=datetime(2024, 6, 3, 10, 0),
        )
        adapter.place_order(order)

        snapshot = adapter.mark_to_market({"sz000001": 11.0})
        pos = next(p for p in snapshot["positions"] if p["stock_code"] == "sz000001")
        assert pos["market_value"] == 11000.0
        assert pos["profit_loss"] == 1000.0
        assert snapshot["market_manifests"]["CN"]["calendar"]["version"] == "v1"
