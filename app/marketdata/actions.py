"""企业行动与账本调整。

支持的行动：

- ``CASH_DIVIDEND`` 现金分红：每股派息 ``cash_per_share``（除息日价格等额下调，
  持仓现金增加、持仓成本等额下调）；
- ``STOCK_DIVIDEND`` 送股/转股：``share_ratio`` 为每股送股比例（如 10 送 4 → 0.4）；
- ``SPLIT`` 拆股：``share_ratio`` 为每股拆分比例（如 1 拆 2 → 2.0）；
- ``CONSOLIDATION`` 合股：``share_ratio`` 为每股折合比例（如 10 合 1 → 0.1）。

所有调整都通过 :class:`LedgerAdjustment` 留下 before/after、计算公式与原因，
回测、模拟盘日终和估值接口共用同一份纯函数逻辑，保证口径一致。
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from typing import Dict, List, Optional, Sequence


class ActionType(str, Enum):
    CASH_DIVIDEND = "cash_dividend"
    STOCK_DIVIDEND = "stock_dividend"
    SPLIT = "split"
    CONSOLIDATION = "consolidation"


# 数量/金额统一保留 8 位小数，展示层再按市场精度截取
ADJUST_QUANT = Decimal("0.00000001")


def _D(value) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


@dataclass(frozen=True)
class CorporateAction:
    """一条企业行动的生效版本（不可变快照行）。"""

    stock_code: str
    action_type: ActionType
    ex_date: date
    cash_per_share: Decimal = Decimal("0")
    share_ratio: Decimal = Decimal("0")
    title: str = ""
    currency: str = "CNY"
    client_id: str = ""                 # 导入方提供的幂等键
    record_id: Optional[int] = None
    supersedes_record_id: Optional[int] = None
    created_at: Optional[datetime] = None
    created_by: str = "system"

    @property
    def share_factor(self) -> Decimal:
        """股本变动倍数：送股/拆股 >1，合股 <1，分红为 1。"""
        if self.action_type in (ActionType.STOCK_DIVIDEND, ActionType.SPLIT):
            return Decimal("1") + self.share_ratio
        if self.action_type == ActionType.CONSOLIDATION:
            return self.share_ratio  # 已为折合比例，如 0.1
        return Decimal("1")

    def describe(self) -> str:
        if self.action_type == ActionType.CASH_DIVIDEND:
            return f"每股派现 {self.cash_per_share} {self.currency}"
        if self.action_type == ActionType.STOCK_DIVIDEND:
            return f"每 10 股送 {self.share_ratio * 10} 股"
        if self.action_type == ActionType.SPLIT:
            return f"1 股拆为 {self.share_factor} 股"
        return f"{self.share_factor} 股合为 1 股"

    def to_dict(self) -> Dict[str, object]:
        return {
            "stock_code": self.stock_code,
            "action_type": self.action_type.value,
            "ex_date": self.ex_date.isoformat(),
            "cash_per_share": float(self.cash_per_share),
            "share_ratio": float(self.share_ratio),
            "share_factor": float(self.share_factor),
            "title": self.title,
            "currency": self.currency,
            "client_id": self.client_id,
            "record_id": self.record_id,
            "supersedes_record_id": self.supersedes_record_id,
            "description": self.describe(),
        }


@dataclass
class LedgerAdjustment:
    """一次账本调整的完整解释。"""

    kind: str                  # cash_dividend/share_factor/cost_basis/price_reference/order_roll
    stock_code: str
    day: str                   # 生效日 ISO
    field_name: str            # quantity | avg_cost | cash | price | execution_day
    before: object
    after: object
    reason: str
    formula: str
    action_client_id: Optional[str] = None
    action_type: Optional[str] = None
    detail: Dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


@dataclass
class HoldingAdjustment:
    """持仓应用企业行动后的结果（纯数据）。"""

    quantity_before: Decimal
    quantity_after: Decimal
    avg_cost_before: Decimal
    avg_cost_after: Decimal
    cash_delta: Decimal
    adjustments: List[LedgerAdjustment] = field(default_factory=list)
    fractional_delta: Decimal = Decimal("0")  # 整数股约束下被舍去的零股


class CorporateActionEngine:
    """企业行动快照：某 ``as_of`` 时点生效的全部行动。"""

    def __init__(self, actions: Sequence[CorporateAction] = ()):
        # 按 (股票, 除权日) 建索引，同日按 client_id 排序保证处理顺序确定
        self._actions: Dict[str, List[CorporateAction]] = {}
        for action in actions:
            key = action.stock_code
            self._actions.setdefault(key, []).append(action)
        for lst in self._actions.values():
            lst.sort(key=lambda a: (a.ex_date, a.client_id))

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    @property
    def actions(self) -> List[CorporateAction]:
        return [a for lst in self._actions.values() for a in lst]

    def for_stock(self, stock_code: str) -> List[CorporateAction]:
        return list(self._actions.get(stock_code, []))

    def on_day(self, stock_code: str, day: date) -> List[CorporateAction]:
        return [a for a in self._actions.get(stock_code, []) if a.ex_date == day]

    def between(
        self, stock_code: str, start: Optional[date], end: Optional[date]
    ) -> List[CorporateAction]:
        result = self._actions.get(stock_code, [])
        if start is not None:
            result = [a for a in result if a.ex_date >= start]
        if end is not None:
            result = [a for a in result if a.ex_date <= end]
        return list(result)

    def fingerprint_inputs(self) -> List[str]:
        """指纹规范输入：行动内容的稳定序列化。"""
        lines = []
        for action in sorted(
            self.actions, key=lambda a: (a.stock_code, a.ex_date, a.client_id)
        ):
            lines.append(
                "|".join(
                    [
                        action.stock_code,
                        action.action_type.value,
                        action.ex_date.isoformat(),
                        str(_D(action.cash_per_share)),
                        str(_D(action.share_ratio)),
                        action.currency,
                        action.client_id,
                        str(action.record_id),
                    ]
                )
            )
        return lines

    # ------------------------------------------------------------------
    # 纯计算：价格 / 持仓
    # ------------------------------------------------------------------

    def theoretical_ex_price(
        self, action: CorporateAction, prev_close: Decimal
    ) -> Dict[str, object]:
        """除权除息参考价及推导说明（接口据此解释价格为何变化）。"""
        prev_close = _D(prev_close)
        factor = action.share_factor
        if action.action_type == ActionType.CASH_DIVIDEND:
            new_price = prev_close - action.cash_per_share
            formula = (
                f"参考价 = 前收 {prev_close} - 每股派息 {action.cash_per_share}"
                f" = {new_price}"
            )
        else:
            new_price = (prev_close / factor).quantize(ADJUST_QUANT, ROUND_HALF_UP)
            formula = (
                f"参考价 = 前收 {prev_close} / 股本倍数 {factor} = {new_price}"
            )
        return {
            "stock_code": action.stock_code,
            "ex_date": action.ex_date.isoformat(),
            "action_type": action.action_type.value,
            "prev_close": float(prev_close),
            "theoretical_price": float(new_price),
            "share_factor": float(factor),
            "cash_per_share": float(action.cash_per_share),
            "formula": formula,
            "reason": action.describe(),
            "client_id": action.client_id,
        }

    def apply_to_holding(
        self,
        action: CorporateAction,
        quantity: Decimal,
        avg_cost: Decimal,
        *,
        integral_shares: bool = False,
    ) -> HoldingAdjustment:
        """把单条行动应用到持仓，返回调整后的数量/成本/现金变化与说明。

        ``integral_shares=True`` 用于 A 股现货账户（数量必须为整数），
        零股部分会显式记录在 ``fractional_delta``，绝不静默处理。
        """
        quantity = _D(quantity)
        avg_cost = _D(avg_cost)
        day = action.ex_date.isoformat()
        adjustments: List[LedgerAdjustment] = []

        qty_after = quantity
        cost_after = avg_cost
        cash_delta = Decimal("0")

        if action.action_type == ActionType.CASH_DIVIDEND:
            dps = _D(action.cash_per_share)
            cash_delta = (quantity * dps).quantize(ADJUST_QUANT, ROUND_HALF_UP)
            cost_after = (avg_cost - dps).quantize(ADJUST_QUANT, ROUND_HALF_UP)
            if cost_after < 0:
                cost_after = Decimal("0")
            adjustments.append(LedgerAdjustment(
                kind="cash_dividend",
                stock_code=action.stock_code,
                day=day,
                field_name="cash",
                before=0.0,
                after=float(cash_delta),
                reason=action.describe(),
                formula=f"现金分红 = 持仓 {quantity} × 每股 {dps} = {cash_delta}",
                action_client_id=action.client_id,
                action_type=action.action_type.value,
            ))
            adjustments.append(LedgerAdjustment(
                kind="cost_basis",
                stock_code=action.stock_code,
                day=day,
                field_name="avg_cost",
                before=float(avg_cost),
                after=float(cost_after),
                reason="除息后单位成本等额下调（成本法）",
                formula=f"新成本 = {avg_cost} - 每股派息 {dps} = {cost_after}",
                action_client_id=action.client_id,
                action_type=action.action_type.value,
            ))
        else:
            factor = action.share_factor
            raw_after = quantity * factor
            if integral_shares:
                qty_after = raw_after.to_integral_value(rounding=ROUND_HALF_UP)
            else:
                qty_after = raw_after.quantize(ADJUST_QUANT, ROUND_HALF_UP)
            fractional = raw_after - qty_after
            cost_after = (avg_cost / factor).quantize(ADJUST_QUANT, ROUND_HALF_UP)
            adjustments.append(LedgerAdjustment(
                kind="share_factor",
                stock_code=action.stock_code,
                day=day,
                field_name="quantity",
                before=float(quantity),
                after=float(qty_after),
                reason=action.describe(),
                formula=(
                    f"新数量 = {quantity} × 股本倍数 {factor} = {raw_after}"
                    + (f"，整股取整后 {qty_after}" if integral_shares else "")
                ),
                action_client_id=action.client_id,
                action_type=action.action_type.value,
                detail={"share_factor": float(factor),
                        "fractional_delta": float(fractional)},
            ))
            adjustments.append(LedgerAdjustment(
                kind="cost_basis",
                stock_code=action.stock_code,
                day=day,
                field_name="avg_cost",
                before=float(avg_cost),
                after=float(cost_after),
                reason="股本扩张后单位成本按倍数摊薄，总成本不变",
                formula=f"新成本 = {avg_cost} / {factor} = {cost_after}",
                action_client_id=action.client_id,
                action_type=action.action_type.value,
            ))

        return HoldingAdjustment(
            quantity_before=quantity,
            quantity_after=qty_after,
            avg_cost_before=avg_cost,
            avg_cost_after=cost_after,
            cash_delta=cash_delta,
            adjustments=adjustments,
            fractional_delta=(raw_after - qty_after)
            if action.action_type != ActionType.CASH_DIVIDEND
            else Decimal("0"),
        )
