"""交易日历/企业行动/封账复现的 API 集成测试。"""

import pytest
from fastapi.testclient import TestClient

import app.config as config_module
from app.config import init_database
from app.main import app
from app.marketdata.registry import reset_registry


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "api_test.db"
    monkeypatch.setattr(config_module, "DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setattr(config_module, "_engine", None)
    monkeypatch.setattr(config_module, "_SessionLocal", None)
    init_database()
    reset_registry()
    with TestClient(app) as c:
        yield c
    reset_registry()
    monkeypatch.setattr(config_module, "_engine", None)
    monkeypatch.setattr(config_module, "_SessionLocal", None)


class TestCalendarAPI:
    def test_import_idempotent_and_explain(self, client):
        payload = {
            "market": "CN", "day": "2024-10-04",
            "is_open": False, "reason": "国庆调休", "kind": "holiday",
            "client_id": "state-2024",
        }
        r1 = client.post("/api/market/calendar/overrides", json=payload)
        r2 = client.post("/api/market/calendar/overrides", json=payload)
        assert r1.json()["outcome"]["status"] == "inserted"
        assert r2.json()["outcome"]["status"] == "skipped_duplicate"

        status = client.get(
            "/api/market/calendar/status", params={"day": "2024-10-04"}
        ).json()
        assert status["is_trading_day"] is False
        assert status["reason"] == "国庆调休"
        assert status["next_trading_day"] == "2024-10-07"

    def test_trading_days_window(self, client):
        client.post("/api/market/calendar/overrides", json={
            "market": "CN", "day": "2024-10-01", "is_open": False,
            "reason": "国庆节", "kind": "holiday", "client_id": "g",
        })
        data = client.get("/api/market/calendar/trading-days", params={
            "start": "2024-09-30", "end": "2024-10-08",
        }).json()
        assert "2024-10-01" not in data["trading_days"]
        assert "2024-10-08" in data["trading_days"]
        assert data["count"] == len(data["trading_days"])

    def test_bad_date_returns_400(self, client):
        r = client.get("/api/market/calendar/status", params={"day": "10/01/2024"})
        assert r.status_code == 400

    def test_revision_and_history_listing(self, client):
        kwargs = {
            "market": "CN", "day": "2024-10-11", "client_id": "n1",
        }
        client.post("/api/market/calendar/overrides", json={
            **kwargs, "is_open": False, "reason": "预计休市", "kind": "adhoc_close",
        })
        client.post("/api/market/calendar/overrides", json={
            **kwargs, "is_open": True, "reason": "休市取消", "kind": "regular",
        })
        records = client.get("/api/market/calendar/overrides").json()["records"]
        assert len(records) == 2
        current = [r for r in records if r["supersedes_id"] is not None]
        assert len(current) == 1


class TestActionAPI:
    def test_import_and_explain_price(self, client):
        r = client.post("/api/market/actions", json={
            "stock_code": "600000", "action_type": "cash_dividend",
            "ex_date": "2024-06-03", "cash_per_share": 0.3,
            "client_id": "d1", "title": "年度分红",
        })
        assert r.json()["outcome"]["status"] == "inserted"

        explain = client.get("/api/market/actions/explain", params={
            "stock_code": "600000", "ex_date": "2024-06-03", "prev_close": 10.0,
        }).json()
        adj = explain["adjustments"][0]
        assert adj["theoretical_price"] == 9.7
        assert adj["formula"]

    def test_duplicate_action_skipped(self, client):
        payload = {
            "stock_code": "600000", "action_type": "split",
            "ex_date": "2024-07-01", "share_ratio": 1.0, "client_id": "s1",
        }
        client.post("/api/market/actions", json=payload)
        r = client.post("/api/market/actions", json=payload)
        assert r.json()["outcome"]["status"] == "skipped_duplicate"

    def test_action_requires_client_id(self, client):
        r = client.post("/api/market/actions", json={
            "stock_code": "600000", "action_type": "split",
            "ex_date": "2024-07-01", "share_ratio": 1.0,
        })
        assert r.status_code == 422  # pydantic 缺字段

    def test_revoke_action(self, client):
        client.post("/api/market/actions", json={
            "stock_code": "600000", "action_type": "split",
            "ex_date": "2024-07-01", "share_ratio": 1.0, "client_id": "s1",
        })
        r = client.post("/api/market/actions/revoke", json={
            "stock_code": "600000", "action_type": "split",
            "ex_date": "2024-07-01", "client_id": "s1",
        })
        assert r.json()["outcome"]["status"] == "revoked"
        records = client.get("/api/market/actions").json()["records"]
        assert records[-1]["revoked"] is True


class TestTradingCalendarIntegration:
    def test_closed_day_order_rejected(self, client):
        client.post("/api/market/calendar/overrides", json={
            "market": "CN", "day": "2024-10-08", "is_open": False,
            "reason": "临时休市", "kind": "adhoc_close", "client_id": "t1",
        })
        client.post("/api/trading/connect", json={
            "adapter_type": "simulation",
            "config": {"initial_cash": 100000},
        })
        # 直接用适配器在休市日下单（账户与风控的 created_at 默认今天，
        # 因此通过底层适配器并指定订单时间验证）
        from datetime import datetime
        from decimal import Decimal
        from app.controllers.trading_controller import trading_service
        from app.trading.base import Order, OrderSide, OrderType, OrderStatus

        adapter = trading_service.adapter
        adapter.set_quote("sh600000", 10.0)
        order = Order(
            order_id="X1", stock_code="sh600000", side=OrderSide.BUY,
            order_type=OrderType.LIMIT, quantity=100,
            price=Decimal("10.0"),
            created_at=datetime(2024, 10, 8, 10, 0),
        )
        result = adapter.place_order(order)
        assert result.status == OrderStatus.REJECTED
        assert "2024-10-09" in result.error_message

    def test_pin_endpoint(self, client):
        client.post("/api/market/calendar/overrides", json={
            "market": "CN", "day": "2024-12-31", "is_open": False,
            "reason": "年末休市", "kind": "adhoc_close", "client_id": "y1",
        })
        pin1 = client.get("/api/market/pin").json()["pin"]
        pin2 = client.get("/api/market/pin").json()["pin"]
        assert pin1["calendar_fingerprint"] == pin2["calendar_fingerprint"]
        assert pin1["market"] == "CN"
