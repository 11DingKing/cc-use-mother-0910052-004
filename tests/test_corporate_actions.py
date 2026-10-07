"""企业行动与账本调整引擎测试。"""

from datetime import date
from decimal import Decimal

import pytest

from app.marketdata.actions import (
    ActionType,
    CorporateAction,
    CorporateActionEngine,
)


def action(atype, ex_date, **kw):
    return CorporateAction(
        stock_code="600000",
        action_type=atype,
        ex_date=ex_date,
        client_id=kw.pop("client_id", "evt-1"),
        **kw,
    )


class TestCashDividend:
    def test_cash_and_cost_adjustment(self):
        eng = CorporateActionEngine([
            action(ActionType.CASH_DIVIDEND, date(2024, 6, 3),
                   cash_per_share=Decimal("0.5"))
        ])
        a = eng.on_day("600000", date(2024, 6, 3))[0]
        h = eng.apply_to_holding(a, Decimal("1000"), Decimal("10.0"))

        assert h.quantity_after == Decimal("1000")
        assert h.cash_delta == Decimal("500.00000000")
        assert h.avg_cost_after == Decimal("9.50000000")

    def test_adjustment_records_explain_formula(self):
        eng = CorporateActionEngine([
            action(ActionType.CASH_DIVIDEND, date(2024, 6, 3),
                   cash_per_share=Decimal("0.3"))
        ])
        a = eng.on_day("600000", date(2024, 6, 3))[0]
        h = eng.apply_to_holding(a, Decimal("1000"), Decimal("10.0"))
        kinds = {(adj.kind, adj.field_name) for adj in h.adjustments}
        assert ("cash_dividend", "cash") in kinds
        assert ("cost_basis", "avg_cost") in kinds
        cash_adj = next(x for x in h.adjustments if x.field_name == "cash")
        assert cash_adj.before == 0.0
        assert cash_adj.after == 300.0
        assert "1000" in cash_adj.formula and "0.3" in cash_adj.formula
        assert cash_adj.reason  # 非空原因说明

    def test_theoretical_ex_price_dividend(self):
        eng = CorporateActionEngine([
            action(ActionType.CASH_DIVIDEND, date(2024, 6, 3),
                   cash_per_share=Decimal("0.3"))
        ])
        a = eng.on_day("600000", date(2024, 6, 3))[0]
        ref = eng.theoretical_ex_price(a, Decimal("10.0"))
        assert ref["theoretical_price"] == 9.7
        assert "前收" in ref["formula"]


class TestShareChanges:
    def test_stock_dividend_increases_shares_thins_cost(self):
        eng = CorporateActionEngine([
            action(ActionType.STOCK_DIVIDEND, date(2024, 6, 6),
                   share_ratio=Decimal("0.4"))  # 10送4
        ])
        a = eng.on_day("600000", date(2024, 6, 6))[0]
        h = eng.apply_to_holding(a, Decimal("1000"), Decimal("14.0"))
        assert h.quantity_after == Decimal("1400.00000000")
        assert h.avg_cost_after == Decimal("10.00000000")
        # 总成本不变：1000*14 == 1400*10
        assert h.quantity_after * h.avg_cost_after == Decimal("14000.00000000")

    def test_split_factor(self):
        eng = CorporateActionEngine([
            action(ActionType.SPLIT, date(2024, 7, 1),
                   share_ratio=Decimal("1"))  # 1拆2
        ])
        a = eng.on_day("600000", date(2024, 7, 1))[0]
        assert a.share_factor == Decimal("2")
        h = eng.apply_to_holding(a, Decimal("300"), Decimal("20.0"))
        assert h.quantity_after == Decimal("600.00000000")
        assert h.avg_cost_after == Decimal("10.00000000")

    def test_consolidation(self):
        eng = CorporateActionEngine([
            action(ActionType.CONSOLIDATION, date(2024, 8, 1),
                   share_ratio=Decimal("0.1"))  # 10合1
        ])
        a = eng.on_day("600000", date(2024, 8, 1))[0]
        assert a.share_factor == Decimal("0.1")
        h = eng.apply_to_holding(a, Decimal("1000"), Decimal("1.0"))
        assert h.quantity_after == Decimal("100.00000000")
        assert h.avg_cost_after == Decimal("10.00000000")

    def test_integral_shares_rounding_is_explicit(self):
        eng = CorporateActionEngine([
            action(ActionType.STOCK_DIVIDEND, date(2024, 6, 6),
                   share_ratio=Decimal("0.3"))
        ])
        a = eng.on_day("600000", date(2024, 6, 6))[0]
        h = eng.apply_to_holding(a, Decimal("105"), Decimal("13.0"),
                                 integral_shares=True)
        # 105 * 1.3 = 136.5 → 整股取整 137（HALF_UP），零股显式记录
        assert h.quantity_after == Decimal("137")
        assert h.fractional_delta != 0
        detail_adj = next(x for x in h.adjustments if x.kind == "share_factor")
        assert detail_adj.detail["fractional_delta"] != 0

    def test_fractional_holdings_keep_precision(self):
        eng = CorporateActionEngine([
            action(ActionType.STOCK_DIVIDEND, date(2024, 6, 6),
                   share_ratio=Decimal("0.3"))
        ])
        a = eng.on_day("600000", date(2024, 6, 6))[0]
        h = eng.apply_to_holding(a, Decimal("105"), Decimal("13.0"))
        assert h.quantity_after == Decimal("136.50000000")


class TestActionQuery:
    def test_between_filters_by_date(self):
        eng = CorporateActionEngine([
            action(ActionType.CASH_DIVIDEND, date(2024, 1, 2),
                   cash_per_share=Decimal("0.1"), client_id="a"),
            action(ActionType.SPLIT, date(2024, 3, 1),
                   share_ratio=Decimal("1"), client_id="b"),
        ])
        hits = eng.between("600000", date(2024, 2, 1), date(2024, 12, 31))
        assert len(hits) == 1
        assert hits[0].client_id == "b"

    def test_multiple_actions_sorted_deterministically(self):
        eng = CorporateActionEngine([
            action(ActionType.CASH_DIVIDEND, date(2024, 6, 3),
                   cash_per_share=Decimal("0.1"), client_id="z"),
            action(ActionType.CASH_DIVIDEND, date(2024, 6, 3),
                   cash_per_share=Decimal("0.2"), client_id="a"),
        ])
        ids = [x.client_id for x in eng.on_day("600000", date(2024, 6, 3))]
        assert ids == ["a", "z"]
