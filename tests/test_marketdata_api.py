"""市场数据管理 API：版本发布、不可变、幂等、企业行动批次。"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.config import init_database
from app.calendar import CalendarStore
from app.corporate_actions import ActionStore
from app.services.marketdata_service import MarketDataService
from app import controllers

CONFIG_DIR = Path(__file__).resolve().parents[1] / "data" / "calendar"


@pytest.fixture(scope="module")
def client():
    init_database()
    with TestClient(app) as c:
        yield c


@pytest.fixture
def md_service(tmp_path, monkeypatch):
    svc = MarketDataService(
        calendar_store=CalendarStore(config_dir=CONFIG_DIR,
                                     state_dir=tmp_path / "cal"),
        action_store=ActionStore(state_dir=tmp_path / "act"),
    )
    monkeypatch.setattr(controllers.marketdata_controller, "service", svc)
    return svc


class TestCalendarAPI:
    def test_seed_markets_and_holiday(self, client, md_service):
        r = client.get("/api/market-data/calendar/markets")
        assert r.status_code == 200
        markets = {m["market"] for m in r.json()["markets"]}
        assert {"CN", "US"} <= markets

        r = client.get("/api/market-data/calendar/CN/day/2024-02-09")
        body = r.json()
        assert body["is_trading_day"] is False
        assert body["resolved_trading_day"] == "2024-02-19"
        assert body["calendar_version"] == "v1"

    def test_publish_special_session_appends_version(self, client, md_service):
        body = {
            "market": "CN", "day": "2024-03-01",
            "session_type": "closed", "name": "突发临时休市",
            "reason": "极端天气", "description": "补录临时休市",
        }
        r = client.post("/api/market-data/calendar/special-session", json=body)
        assert r.status_code == 200
        assert r.json()["version"] == "v2"
        assert r.json()["deduplicated"] is False

        # 完全相同的调整重复提交：幂等，不产生 v3
        r2 = client.post("/api/market-data/calendar/special-session", json=body)
        assert r2.status_code == 200
        assert r2.json()["version"] == "v2"
        assert r2.json()["deduplicated"] is True

        versions = client.get("/api/market-data/calendar/CN/versions").json()["versions"]
        assert [v["version"] for v in versions] == ["v1", "v2"]

        # 新版本生效
        r = client.get("/api/market-data/calendar/CN/day/2024-03-01")
        assert r.json()["is_trading_day"] is False

    def test_same_day_duplicate_event_in_one_version_rejected(self, client, md_service):
        # 同一版本里给同一天放两个 closed：构建视图时拒绝
        payload = {
            "market": "CN", "version": "vd1",
            "tzname": "Asia/Shanghai", "weekend": [5, 6],
            "holidays": [],
            "special_sessions": [
                {"day": "2024-03-01", "name": "休市A", "session_type": "closed"},
                {"day": "2024-03-01", "name": "休市A重复", "session_type": "closed"},
            ],
        }
        r = client.post("/api/market-data/calendar/versions", json=payload)
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "CALENDAR_VALIDATION_ERROR"

    def test_republish_same_payload_is_idempotent(self, client, md_service):
        payload = {
            "market": "CN", "version": "v9",
            "tzname": "Asia/Shanghai", "weekend": [5, 6],
            "holidays": [{"day": "2024-01-01", "name": "元旦"}],
            "special_sessions": [],
            "description": "固定版本",
        }
        r1 = client.post("/api/market-data/calendar/versions", json=payload)
        assert r1.status_code == 200
        r2 = client.post("/api/market-data/calendar/versions", json=payload)
        assert r2.status_code == 200
        versions = client.get("/api/market-data/calendar/CN/versions").json()["versions"]
        assert sum(1 for v in versions if v["version"] == "v9") == 1

    def test_republish_different_payload_same_version_rejected(self, client, md_service):
        payload = {
            "market": "CN", "version": "v8",
            "tzname": "Asia/Shanghai", "weekend": [5, 6],
            "holidays": [{"day": "2024-01-01", "name": "元旦"}],
            "special_sessions": [],
        }
        assert client.post("/api/market-data/calendar/versions", json=payload).status_code == 200
        payload["holidays"].append({"day": "2024-01-02", "name": "偷偷加的休市"})
        r = client.post("/api/market-data/calendar/versions", json=payload)
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "IMMUTABLE_VERSION_ERROR"

    def test_trading_days_enumeration(self, client, md_service):
        r = client.get(
            "/api/market-data/calendar/CN/trading-days",
            params={"start": "2024-02-05", "end": "2024-02-20"},
        )
        days = r.json()["trading_days"]
        assert "2024-02-09" not in days
        assert "2024-02-19" in days


class TestActionAPI:
    def _batch(self, value="0.50", batch_id="ba1"):
        return {
            "batch_id": batch_id, "source": "test", "description": "分红批次",
            "actions": [{
                "stock_code": "sz000001", "ex_date": "2024-06-03",
                "action_type": "cash_dividend", "value": value,
                "currency": "CNY", "name": "年度分红",
            }],
        }

    def test_publish_list_manifest(self, client, md_service):
        r = client.post("/api/market-data/actions/batches", json=self._batch())
        assert r.status_code == 200
        assert r.json()["imported"] == 1

        r = client.get("/api/market-data/actions", params={"stock_code": "sz000001"})
        actions = r.json()["actions"]
        assert len(actions) == 1
        assert actions[0]["scheme"]  # 含人类可读方案说明

        r = client.get("/api/market-data/manifest")
        batches = r.json()["corporate_action_batches"]
        assert any(b["batch_id"] == "ba1" for b in batches)

    def test_duplicate_import_idempotent(self, client, md_service):
        client.post("/api/market-data/actions/batches", json=self._batch())
        r = client.post("/api/market-data/actions/batches", json=self._batch())
        assert r.json()["status"] == "identical"
        assert len(client.get("/api/market-data/actions").json()["actions"]) == 1

    def test_conflicting_value_rejected(self, client, md_service):
        client.post("/api/market-data/actions/batches", json=self._batch("0.50", "c1"))
        r = client.post("/api/market-data/actions/batches",
                        json=self._batch("0.90", "c2"))
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "ACTION_VALIDATION_ERROR"
