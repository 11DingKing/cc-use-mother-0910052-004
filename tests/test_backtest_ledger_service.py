"""回测账本：幂等重跑复用原账本、封账结果不被改写、版本演进产生新账本。"""

from datetime import date, datetime
from pathlib import Path

import pytest

from app.config import db_session_scope, get_engine, init_database
from app.entities.backtest import Base as BacktestBase
from app.entities.analysis_result import Base as AnalysisBase
from app.chan.models import RawCandle, Signal, SignalType
from app.mappers.analysis_mapper import AnalysisMapper
from app.calendar import CalendarStore
from app.corporate_actions import ActionStore
from app.marketdata.provider import MarketDataProvider
from app.services.backtest_service import BacktestService

CONFIG_DIR = Path(__file__).resolve().parents[1] / "data" / "calendar"


@pytest.fixture(autouse=True)
def setup_database():
    init_database()
    engine = get_engine()
    BacktestBase.metadata.drop_all(bind=engine)
    AnalysisBase.metadata.drop_all(bind=engine)
    BacktestBase.metadata.create_all(bind=engine)
    AnalysisBase.metadata.create_all(bind=engine)
    yield


@pytest.fixture
def provider(tmp_path):
    return MarketDataProvider(
        calendar_store=CalendarStore(config_dir=CONFIG_DIR,
                                     state_dir=tmp_path / "cal"),
        action_store=ActionStore(state_dir=tmp_path / "act"),
    )


def _candles():
    # 2024-06-03(一) ~ 06-07(五)，均为 CN 交易日
    days = [date(2024, 6, d) for d in (3, 4, 5, 6, 7)]
    prices = [100, 101, 102, 103, 104]
    return [
        RawCandle(timestamp=datetime(d.year, d.month, d.day),
                  open=p, high=p + 1, low=p - 1, close=float(p), volume=1000)
        for d, p in zip(days, prices)
    ]


def _signals():
    return [
        Signal(stock_code="sz000001", signal_type=SignalType.BUY_1,
               timestamp=datetime(2024, 6, 3), price=100, level="daily"),
        Signal(stock_code="sz000001", signal_type=SignalType.SELL_1,
               timestamp=datetime(2024, 6, 7), price=104, level="daily"),
    ]


@pytest.fixture
def service(provider):
    svc = BacktestService(market_provider=provider)
    svc.stock_service.get_candles = lambda *a, **k: _candles()
    svc.analysis_service.run_analysis = lambda *a, **k: None

    with db_session_scope() as session:
        AnalysisMapper(session).save_analysis(
            stock_code="sz000001", period="daily",
            start_time=datetime(2024, 6, 1), end_time=datetime(2024, 6, 10),
            fractals=[], bis=[], duans=[], zhongshus=[], signals=_signals(),
        )
    return svc


def _run(service):
    return service.run_backtest(
        "sz000001", "daily",
        datetime(2024, 6, 1), datetime(2024, 6, 10),
    )


class TestIdempotentRerun:
    def test_rerun_reuses_original_ledger(self, service):
        first = _run(service)
        assert first["reused"] is False
        first_id = first["id"]

        second = _run(service)
        assert second["reused"] is True
        assert second["id"] == first_id
        # 账本数字逐位一致，证明可复现
        assert second["summary"]["final_capital"] == first["summary"]["final_capital"]

    def test_ledger_carries_manifest_and_fingerprint(self, service):
        report = _run(service)
        detail = service.get_result(report["id"])
        assert detail["input_fingerprint"]
        assert detail["market_manifest"]["calendar"]["version"] == "v1"
        assert detail["trading_days"] == 5
        # 交易记录含完整字段
        assert detail["trades"][0]["adjustments"] == []

    def test_sealed_result_not_rewritten(self, service):
        report = _run(service)
        rid = report["id"]
        original_final = report["summary"]["final_capital"]

        sealed = service.seal_result(rid, reason="月结封账")
        assert sealed["is_sealed"] is True

        # 封账后同参重跑：复用原账本，而不是改写
        rerun = _run(service)
        assert rerun["reused"] is True
        assert rerun["id"] == rid
        detail = service.get_result(rid)
        assert detail["is_sealed"] is True
        assert detail["seal_reason"] == "月结封账"
        assert detail["summary"]["final_capital"] == original_final

    def test_double_seal_rejected(self, service):
        rid = _run(service)["id"]
        service.seal_result(rid)
        from app.services.backtest_service import SealedLedgerError
        with pytest.raises(SealedLedgerError):
            service.seal_result(rid)

    def test_new_calendar_version_creates_new_ledger_keeps_old(self, service, provider):
        first = _run(service)
        old_id = first["id"]
        old_final = first["summary"]["final_capital"]

        # 发布日历新版本（新增一处临时休市，不影响本次区间）-> 指纹变化
        store = provider._calendar_store()
        head = store.get_version("CN")
        payload = head.canonical_payload()
        payload["special_sessions"] = list(payload["special_sessions"]) + [{
            "day": "2024-08-01", "name": "临时休市", "session_type": "closed",
        }]
        store.publish("CN", payload, description="8月临时休市补录")

        second = _run(service)
        assert second["reused"] is False
        assert second["id"] != old_id

        # 旧账本原样保留
        old_detail = service.get_result(old_id)
        assert old_detail["market_manifest"]["calendar"]["version"] == "v1"
        assert old_detail["summary"]["final_capital"] == old_final
        new_detail = service.get_result(second["id"])
        assert new_detail["market_manifest"]["calendar"]["version"] == "v2"

    def test_pinning_old_version_reproduces_old_ledger(self, service, provider):
        first = _run(service)
        old_fp = service.get_result(first["id"])["input_fingerprint"]

        store = provider._calendar_store()
        head = store.get_version("CN")
        payload = head.canonical_payload()
        payload["special_sessions"] = list(payload["special_sessions"]) + [{
            "day": "2024-09-02", "name": "临时休市", "session_type": "closed",
        }]
        store.publish("CN", payload, description="9月临时休市补录")

        # 默认生效版本已是 v2，但显式固定 v1 应复现原账本（复用同一记录）
        pinned = service.run_backtest(
            "sz000001", "daily",
            datetime(2024, 6, 1), datetime(2024, 6, 10),
            calendar_version="v1",
        )
        assert pinned["reused"] is True
        assert pinned["id"] == first["id"]
        assert service.get_result(first["id"])["input_fingerprint"] == old_fp
