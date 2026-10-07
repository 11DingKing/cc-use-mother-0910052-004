"""日历/企业行动存储与注册表测试：幂等导入、修订留痕、按时间点复现、封账。"""

import json
from datetime import date, datetime
from decimal import Decimal

import pytest

from app.config import db_session_scope
from app.marketdata.action_store import CorporateActionStore
from app.marketdata.calendar_store import CalendarStore
from app.marketdata.entities import CalendarOverrideRecord, LedgerSealRecord
from app.marketdata.registry import MarketDataPin, get_registry
from app.marketdata.seal_store import LedgerSealedError, LedgerSealStore


class TestCalendarStore:
    def test_idempotent_import(self, temp_db):
        reg = get_registry()
        o1 = reg.import_calendar_override(
            market="CN", day="2024-10-01", is_open=False,
            reason="国庆节", kind="holiday", client_id="state-2024",
        )
        o2 = reg.import_calendar_override(
            market="CN", day="2024-10-01", is_open=False,
            reason="国庆节", kind="holiday", client_id="state-2024",
        )
        assert o1["status"] == "inserted"
        assert o2["status"] == "skipped_duplicate"
        with db_session_scope() as s:
            assert s.query(CalendarOverrideRecord).count() == 1

    def test_revision_appends_and_preserves_history(self, temp_db):
        reg = get_registry()
        o1 = reg.import_calendar_override(
            market="CN", day="2024-10-11", is_open=False,
            reason="预计休市", kind="adhoc_close", client_id="notice-9",
        )
        o2 = reg.import_calendar_override(
            market="CN", day="2024-10-11", is_open=True,
            reason="休市取消，正常开市", kind="regular", client_id="notice-9",
        )
        assert o2["status"] == "superseded"
        assert o2["superseded_id"] == o1["record_id"]
        with db_session_scope() as s:
            rows = CalendarStore(s).list_overrides("CN")
        assert len(rows) == 2
        cal = reg.calendar()
        assert cal.is_trading_day(date(2024, 10, 11)) is True
        assert cal.entry_for(date(2024, 10, 11)).revised_record_id == o1["record_id"]

    def test_revoke_restores_weekend_rule(self, temp_db):
        reg = get_registry()
        reg.import_calendar_override(
            market="CN", day="2024-10-12", is_open=True,
            reason="补班", kind="makeup", client_id="mk-1",
        )
        assert reg.calendar().is_trading_day(date(2024, 10, 12)) is True
        out = reg.revoke_calendar_override("CN", date(2024, 10, 12), "mk-1")
        assert out["status"] == "revoked"
        # 撤销后 10-12（周六）恢复周末休市
        assert reg.calendar().is_trading_day(date(2024, 10, 12)) is False

    def test_revoke_missing_raises(self, temp_db):
        reg = get_registry()
        with pytest.raises(ValueError):
            reg.revoke_calendar_override("CN", date(2024, 10, 12), "nope")

    def test_fingerprint_stable_and_changes_with_revision(self, temp_db):
        reg = get_registry()
        fp0 = reg.calendar().fingerprint
        reg.import_calendar_override(
            market="CN", day="2024-12-31", is_open=False,
            reason="年末休市", kind="adhoc_close", client_id="x1",
        )
        fp1 = reg.calendar().fingerprint
        reg.import_calendar_override(
            market="CN", day="2024-12-31", is_open=False,
            reason="年末休市", kind="adhoc_close", client_id="x1",
        )
        fp2 = reg.calendar().fingerprint
        assert fp0 != fp1
        assert fp1 == fp2  # 重复导入不改指纹


class TestActionStore:
    def _action_kwargs(self, cash=0.3, created_at=None):
        return dict(
            stock_code="600000", action_type="cash_dividend",
            ex_date="2024-06-03", cash_per_share=cash,
            title="年度分红", client_id="600000-2024-001",
            **({"created_at": created_at} if created_at else {}),
        )

    def test_idempotent_import(self, temp_db):
        reg = get_registry()
        o1 = reg.import_action(**self._action_kwargs())
        o2 = reg.import_action(**self._action_kwargs())
        assert o1["status"] == "inserted"
        assert o2["status"] == "skipped_duplicate"

    def test_client_id_required(self, temp_db):
        reg = get_registry()
        with pytest.raises(ValueError):
            reg.import_action(
                stock_code="600000", action_type="cash_dividend",
                ex_date="2024-06-03", cash_per_share=0.3,
            )

    def test_revision_appends_and_as_of_replay(self, temp_db):
        reg = get_registry()
        reg.import_action(**self._action_kwargs(cash=0.30, created_at=datetime(2024, 5, 20)))
        reg.import_action(**self._action_kwargs(cash=0.35, created_at=datetime(2024, 6, 10)))

        # 当前生效版本为修订后的 0.35
        current = reg.actions("600000")
        assert current.on_day("600000", date(2024, 6, 3))[0].cash_per_share == Decimal("0.35")

        # 按历史时间点复现：6-05 只能看到 0.30
        old = reg.actions("600000", as_of=datetime(2024, 6, 5))
        assert old.on_day("600000", date(2024, 6, 3))[0].cash_per_share == Decimal("0.30")

        with db_session_scope() as s:
            rows = CorporateActionStore(s).list_actions("600000")
        assert len(rows) == 2  # 旧行保留

    def test_revoke_removes_from_effective_set(self, temp_db):
        reg = get_registry()
        reg.import_action(
            stock_code="600000", action_type="split",
            ex_date="2024-07-01", share_ratio=1.0, client_id="s1",
        )
        reg.revoke_action("600000", "split", date(2024, 7, 1), "s1")
        assert reg.actions("600000").on_day("600000", date(2024, 7, 1)) == []

    def test_invalid_params_rejected(self, temp_db):
        reg = get_registry()
        with pytest.raises(ValueError):
            reg.import_action(
                stock_code="X", action_type="cash_dividend",
                ex_date="2024-06-03", cash_per_share=0, client_id="x",
            )
        with pytest.raises(ValueError):
            reg.import_action(
                stock_code="X", action_type="consolidation",
                ex_date="2024-06-03", share_ratio=1.0, client_id="x",
            )

    def test_seed_file_is_idempotent(self, temp_db, tmp_path):
        seed = tmp_path / "seed.json"
        seed.write_text(json.dumps({
            "calendar": [
                {"market": "CN", "day": "2024-10-01", "is_open": False,
                 "reason": "国庆节", "kind": "holiday", "client_id": "g"},
            ],
            "actions": [
                {"stock_code": "600000", "action_type": "cash_dividend",
                 "ex_date": "2024-06-03", "cash_per_share": 0.3,
                 "client_id": "d1"},
            ],
        }), encoding="utf-8")
        reg = get_registry()
        c1 = reg.load_seed_file(seed)
        c2 = reg.load_seed_file(seed)
        assert c1["calendar_inserted"] == 1
        assert c2["calendar_inserted"] == 0
        assert c2["calendar_skipped"] == 1
        assert c2["actions_skipped"] == 1


class TestPin:
    def test_pin_tracks_version_and_replays(self, temp_db):
        reg = get_registry()
        pin_before = reg.pin("600000")
        reg.import_action(
            stock_code="600000", action_type="cash_dividend",
            ex_date="2024-06-03", cash_per_share=0.3, client_id="d1",
            created_at=datetime(2024, 5, 1),
        )
        pin_after = reg.pin("600000", as_of=datetime(2024, 5, 15))
        assert pin_before.actions_fingerprint != pin_after.actions_fingerprint
        # 旧 pin（as_of=None 时记录当前）不可复现已变化数据，但带 as_of 可以
        check = reg.verify_pin_for_stock(pin_after, "600000")
        assert check["calendar_ok"] is True
        assert check["actions_ok"] is True

    def test_pin_serialization_roundtrip(self, temp_db):
        reg = get_registry()
        pin = reg.pin("600000")
        again = MarketDataPin.from_dict(pin.to_dict())
        assert again == pin


class TestSeal:
    def test_sealed_ledger_rejects_modification(self, temp_db):
        with db_session_scope() as s:
            store = LedgerSealStore(s)
            store.seal("backtest", "42", {"final_capital": 100000}, None)
            with pytest.raises(LedgerSealedError):
                store.seal("backtest", "42", {"final_capital": 99999}, None)
            assert store.verify("backtest", "42", {"final_capital": 100000}) is True
            assert store.verify("backtest", "42", {"final_capital": 1}) is False

    def test_reseal_same_content_is_idempotent(self, temp_db):
        with db_session_scope() as s:
            store = LedgerSealStore(s)
            payload = {"a": 1}
            s1 = store.seal("backtest", "7", payload)
            s2 = store.seal("backtest", "7", payload)
            assert s1["ledger_hash"] == s2["ledger_hash"]
            assert s.query(LedgerSealRecord).count() == 1

    def test_hash_stable_across_key_order(self, temp_db):
        from app.marketdata.seal_store import stable_hash
        assert stable_hash({"a": 1, "b": 2}) == stable_hash({"b": 2, "a": 1})
