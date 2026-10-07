"""服务层测试：同一生效版本驱动回测、封账与历史重放。"""

import json
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from app.chan.models import RawCandle, Signal, SignalType
from app.chan.serializer import ChanSerializer
from app.config import db_session_scope
from app.entities.analysis_result import AnalysisResult
from app.entities.backtest import BacktestResult as BacktestEntity
from app.marketdata.registry import MarketDataPin, get_registry
from app.services.backtest_service import BacktestService
from app.utils.validators import validate_stock_code


BASE = datetime(2024, 6, 3)
CODE = validate_stock_code("600000")  # 规范化为 sh600000，与服务层口径一致


def _fixture_candles():
    prices = [10, 10.2, 10.4, 10.6, 10.1, 10.3, 10.5, 10.7, 10.9, 11.1]
    return [
        RawCandle(
            timestamp=BASE + timedelta(days=i),
            open=p, high=p * 1.01, low=p * 0.99, close=p, volume=1000.0,
        )
        for i, p in enumerate(prices)
    ]


def _fixture_signals():
    return [
        Signal(CODE, SignalType.BUY_1, BASE + timedelta(days=0), 10.0, "daily"),
        Signal(CODE, SignalType.SELL_1, BASE + timedelta(days=9), 11.1, "daily"),
    ]


@pytest.fixture
def backtest_service(temp_db, monkeypatch):
    service = BacktestService()
    monkeypatch.setattr(
        service.stock_service, "get_candles",
        lambda *a, **k: _fixture_candles(),
    )
    monkeypatch.setattr(
        service.analysis_service, "run_analysis",
        lambda *a, **k: None,
    )

    serializer = ChanSerializer()
    signals_json = json.dumps(
        [serializer.serialize(s) for s in _fixture_signals()],
        ensure_ascii=False,
    )
    with db_session_scope() as session:
        session.add(AnalysisResult(
            stock_code=CODE,
            period="daily",
            start_time=BASE,
            end_time=BASE + timedelta(days=9),
            signals_json=signals_json,
            signal_count=2,
        ))
    return service


class TestSealedBacktest:
    def test_run_seals_result_and_pins_version(self, backtest_service):
        report = backtest_service.run_backtest(
            CODE, "daily", BASE, BASE + timedelta(days=9),
            market="CN", as_of=datetime(2024, 9, 1),
        )
        assert report["sealed"] is True
        pin = report["market_data_pin"]
        assert pin["market"] == "CN"
        assert pin["as_of"] == datetime(2024, 9, 1).isoformat()

        stored = backtest_service.get_result(report["id"])
        assert stored["sealed"] is True
        assert stored["ledger_hash"]
        # 周末被日历过滤（6-08/09）
        skipped_days = {s["date"] for s in stored["skipped_candles"]}
        assert "2024-06-08" in skipped_days

    def test_rerun_reproduces_original_ledger_after_data_revisions(
        self, backtest_service
    ):
        reg = get_registry()
        # 封账前已存在的数据（除息 0.3，6-05）
        reg.import_action(
            stock_code=CODE, action_type="cash_dividend",
            ex_date="2024-06-05", cash_per_share=0.3, client_id="d1",
            created_at=datetime(2024, 5, 1),
        )
        first = backtest_service.run_backtest(
            CODE, "daily", BASE, BASE + timedelta(days=9),
            market="CN", as_of=datetime(2024, 9, 1),
        )
        original_hash = first["market_data_pin"]

        # —— 封账后补录/修订数据 ——
        # 1) 派息额修订（不影响 as_of=9-1 的历史版本？会影响：修订行 created_at 在 10 月）
        reg.import_action(
            stock_code=CODE, action_type="cash_dividend",
            ex_date="2024-06-05", cash_per_share=0.99, client_id="d1",
            created_at=datetime(2024, 10, 1),
        )
        # 2) 新增临时休市
        reg.import_calendar_override(
            market="CN", day="2024-06-06", is_open=False,
            reason="补录的临时休市", kind="adhoc_close", client_id="late-1",
            created_at=datetime(2024, 10, 1),
        )

        verify = backtest_service.verify_reproducibility(first["id"])
        assert verify["ledger_matches"] is True, verify
        assert verify["stored_ledger_hash"] == verify["recomputed_ledger_hash"]
        # 按 as_of 复现时日历与行动指纹均与封账时一致
        assert verify["data_version_check"]["calendar_ok"] is True
        assert verify["data_version_check"]["actions_ok"] is True

    def test_replay_without_as_of_detects_current_version_change(
        self, backtest_service
    ):
        reg = get_registry()
        first = backtest_service.run_backtest(
            CODE, "daily", BASE, BASE + timedelta(days=9),
            market="CN",  # 不固定 as_of
        )
        reg.import_calendar_override(
            market="CN", day="2024-12-31", is_open=False,
            reason="临时休市", kind="adhoc_close", client_id="late",
        )
        stored = backtest_service.get_result(first["id"])
        pin_dto = MarketDataPin(**stored["market_data_pin"])
        check = reg.verify_pin_for_stock(pin_dto, CODE)
        assert check["calendar_ok"] is False
