"""企业行动：除权除息计算、批次不可变、重复导入幂等、冲突拒绝。"""

from datetime import date
from decimal import Decimal

import pytest

from app.corporate_actions import (
    ActionStore,
    ActionType,
    CorporateAction,
    apply_action_to_position,
)
from app.corporate_actions.errors import (
    ActionValidationError,
    ImmutableBatchError,
)


def div(value="0.50", ex=date(2024, 6, 3)):
    return CorporateAction(
        stock_code="sz000001", ex_date=ex,
        action_type=ActionType.CASH_DIVIDEND, value=Decimal(value),
        name="2023年度分红",
    )


class TestAdjustmentMath:
    def test_cash_dividend_lowers_cost_and_pays_cash(self):
        applied = apply_action_to_position(div(), Decimal("1000"), Decimal("10.000"))
        assert applied.cash_received == Decimal("500.00")
        assert applied.quantity_after == Decimal("1000")
        assert applied.avg_cost_after == Decimal("9.500")
        assert "每股派现" in applied.note

    def test_stock_dividend_increases_shares_thins_cost(self):
        action = CorporateAction(
            stock_code="sz000001", ex_date=date(2024, 6, 3),
            action_type=ActionType.STOCK_DIVIDEND, value=Decimal("2"),  # 10送2
        )
        applied = apply_action_to_position(action, Decimal("1000"), Decimal("12.000"))
        assert applied.quantity_after == Decimal("1200")
        # 总成本不变：1000*12 = 1200*10
        assert applied.avg_cost_after == Decimal("10.000")

    def test_split_doubles_shares_halves_cost(self):
        action = CorporateAction(
            stock_code="sz000001", ex_date=date(2024, 6, 3),
            action_type=ActionType.SPLIT, value=Decimal("2"),
        )
        applied = apply_action_to_position(action, Decimal("300"), Decimal("20.000"))
        assert applied.quantity_after == Decimal("600")
        assert applied.avg_cost_after == Decimal("10.000")
        assert applied.fx_price_factor == Decimal("0.500")

    def test_invalid_action_rejected(self):
        with pytest.raises(ActionValidationError):
            CorporateAction(
                stock_code="x", ex_date=date(2024, 1, 1),
                action_type="reverse_ipo", value=Decimal("1"),
            )
        with pytest.raises(ActionValidationError):
            CorporateAction(
                stock_code="x", ex_date=date(2024, 1, 1),
                action_type=ActionType.SPLIT, value=Decimal("0"),
            )


@pytest.fixture
def store(tmp_path):
    return ActionStore(state_dir=tmp_path / "actions")


class TestBatchPublishing:
    def test_publish_and_query(self, store):
        result = store.publish_batch("b1", [div()], source="test", description="首批")
        assert result["status"] == "published"
        assert result["imported"] == 1
        actions = store.actions_for("sz000001")
        assert len(actions) == 1
        assert store.manifest()[0]["batch_id"] == "b1"

    def test_duplicate_same_batch_id_same_content_is_idempotent(self, store):
        store.publish_batch("b1", [div()])
        again = store.publish_batch("b1", [div()])
        assert again["status"] == "identical"
        assert again["skipped_duplicates"] == 1
        assert len(store.actions_for("sz000001")) == 1

    def test_same_batch_id_different_content_rejected(self, store):
        store.publish_batch("b1", [div("0.50")])
        with pytest.raises(ImmutableBatchError):
            store.publish_batch("b1", [div("0.60")])

    def test_same_action_in_two_batches_deduped(self, store):
        store.publish_batch("b1", [div("0.50")])
        result = store.publish_batch("b2", [div("0.50")])
        # 不同批次号，但行动指纹一致 -> 跳过，不重复生效
        assert result["imported"] == 0
        assert result["skipped_duplicates"] == 1
        assert len(store.actions_for("sz000001")) == 1

    def test_conflicting_value_same_day_rejected(self, store):
        store.publish_batch("b1", [div("0.50")])
        with pytest.raises(ActionValidationError) as exc:
            store.publish_batch("b2", [div("0.80")])
        detail = exc.value.details
        assert detail["existing_value"] == "0.50"
        assert detail["new_value"] == "0.80"

    def test_empty_batch_rejected(self, store):
        with pytest.raises(ActionValidationError):
            store.publish_batch("empty", [])

    def test_persistence_and_hash_tamper_detection(self, tmp_path):
        state_dir = tmp_path / "actions"
        s1 = ActionStore(state_dir=state_dir)
        s1.publish_batch("b1", [div()])
        # 篡改文件内容但保留旧哈希
        import json
        path = state_dir / "b1.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["actions"][0]["value"] = "9.99"
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        with pytest.raises(ActionValidationError) as exc:
            ActionStore(state_dir=state_dir)
        assert "哈希" in str(exc.value)

    def test_date_range_filter(self, store):
        a1 = CorporateAction("sz000001", date(2024, 3, 1), ActionType.SPLIT, Decimal("2"))
        a2 = CorporateAction("sz000001", date(2024, 6, 1), ActionType.CASH_DIVIDEND, Decimal("0.5"))
        store.publish_batch("b1", [a1, a2])
        assert len(store.actions_for("sz000001", on_or_after=date(2024, 5, 1))) == 1
        assert len(store.actions_for("sz000001",
                                     on_or_after=date(2024, 1, 1),
                                     on_or_before=date(2024, 4, 1))) == 1
