"""企业行动领域模型与持仓调整计算（纯函数，无 IO，确定性）。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import List, Optional, Sequence

from app.corporate_actions.errors import ActionValidationError


class ActionType:
    CASH_DIVIDEND = "cash_dividend"
    STOCK_DIVIDEND = "stock_dividend"
    SPLIT = "split"
    ALL = (CASH_DIVIDEND, STOCK_DIVIDEND, SPLIT)


_Q = Decimal("0.001")  # 价格/成本保留三位小数


@dataclass(frozen=True)
class CorporateAction:
    """单只证券在某一除权除息日的一项行动。"""

    stock_code: str
    ex_date: date                 # 除权除息日（市场本地日历日）
    action_type: str
    # cash_dividend: 每股派现（税前）；stock_dividend: 每10股送股数；split: 拆分比例
    value: Decimal
    currency: str = "CNY"
    name: str = ""                # 方案名称，用于接口说明
    record_date: Optional[date] = None
    pay_date: Optional[date] = None

    def __post_init__(self):
        # value 接受 str/int/float/Decimal，统一为精确十进制
        if not isinstance(self.value, Decimal):
            try:
                object.__setattr__(self, "value", Decimal(str(self.value)))
            except Exception as exc:
                raise ActionValidationError(
                    f"{self.stock_code} {self.ex_date} 的行动数值无法解析: {self.value!r}"
                ) from exc
        if self.action_type not in ActionType.ALL:
            raise ActionValidationError(
                f"未知企业行动类型 {self.action_type!r}，支持 {ActionType.ALL}"
            )
        if self.value <= 0:
            raise ActionValidationError(
                f"{self.stock_code} {self.ex_date} {self.action_type} 的数值必须为正"
            )

    # -- 指纹（用于幂等去重）-----------------------------------------------

    def fingerprint(self) -> str:
        raw = "|".join([
            self.stock_code,
            self.ex_date.isoformat(),
            self.action_type,
            str(self.value),
        ])
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def describe(self) -> str:
        """人类可读的方案说明。"""
        if self.action_type == ActionType.CASH_DIVIDEND:
            return f"每股派现 {self.value} {self.currency}" + (f"（{self.name}）" if self.name else "")
        if self.action_type == ActionType.STOCK_DIVIDEND:
            return f"每10股送 {self.value} 股" + (f"（{self.name}）" if self.name else "")
        return f"拆股/转增比例 1:{self.value}" + (f"（{self.name}）" if self.name else "")

    def to_dict(self) -> dict:
        return {
            "stock_code": self.stock_code,
            "ex_date": self.ex_date.isoformat(),
            "action_type": self.action_type,
            "value": str(self.value),
            "currency": self.currency,
            "name": self.name,
            "scheme": self.describe(),
            "record_date": self.record_date.isoformat() if self.record_date else None,
            "pay_date": self.pay_date.isoformat() if self.pay_date else None,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "CorporateAction":
        return cls(
            stock_code=str(raw["stock_code"]),
            ex_date=date.fromisoformat(raw["ex_date"]),
            action_type=str(raw["action_type"]),
            value=Decimal(str(raw["value"])),
            currency=str(raw.get("currency", "CNY")),
            name=str(raw.get("name", "")),
            record_date=date.fromisoformat(raw["record_date"]) if raw.get("record_date") else None,
            pay_date=date.fromisoformat(raw["pay_date"]) if raw.get("pay_date") else None,
        )


@dataclass(frozen=True)
class AppliedAction:
    """一次行动对持仓/价格产生的实际调整——接口据此解释“为何变化”。"""

    action: CorporateAction
    quantity_before: Decimal
    quantity_after: Decimal
    avg_cost_before: Decimal
    avg_cost_after: Decimal
    cash_received: Decimal
    fx_price_factor: Decimal   # 行情前复权乘子（<除权日的历史价乘它得到可比价）
    note: str

    def to_dict(self) -> dict:
        return {
            "stock_code": self.action.stock_code,
            "ex_date": self.action.ex_date.isoformat(),
            "action_type": self.action.action_type,
            "scheme": self.action.describe(),
            "quantity_before": str(self.quantity_before),
            "quantity_after": str(self.quantity_after),
            "avg_cost_before": str(self.avg_cost_before),
            "avg_cost_after": str(self.avg_cost_after),
            "cash_received": str(self.cash_received),
            "price_factor": str(self.fx_price_factor),
            "note": self.note,
        }


# ---------------------------------------------------------------------------
# 调整计算
# ---------------------------------------------------------------------------

def _q2(x: Decimal) -> Decimal:
    return x.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def adjust_quantity(qty: Decimal, action: CorporateAction) -> Decimal:
    """送股/拆股后的股数（现金分红不动数量）。"""
    if action.action_type == ActionType.SPLIT:
        return (qty * action.value).quantize(Decimal("1"))
    if action.action_type == ActionType.STOCK_DIVIDEND:
        return (qty * (Decimal("1") + action.value / Decimal("10"))).quantize(Decimal("1"))
    return qty


def price_factor(action: CorporateAction) -> Decimal:
    """该行动对应的前复权价格乘子。"""
    if action.action_type == ActionType.SPLIT:
        return (Decimal("1") / action.value).quantize(_Q)
    if action.action_type == ActionType.STOCK_DIVIDEND:
        return (Decimal("1") / (Decimal("1") + action.value / Decimal("10"))).quantize(_Q)
    # 现金分红的价格乘子需要除权前收盘价，属于“行情侧复权”，账本侧只调成本，
    # 这里返回 1 表示数量口径不变。
    return Decimal("1")


def apply_action_to_position(
    action: CorporateAction,
    quantity: Decimal,
    avg_cost: Decimal,
) -> AppliedAction:
    """对一笔持仓应用除权除息，返回调整明细（纯函数，确定性）。

    - 现金分红：现金增加 ``qty * 每股派现``；每股成本等额下调；
    - 送股/拆股：股数按比例增加、总成本不变、每股成本摊薄。
    """
    qty_before = Decimal(quantity)
    cost_before = Decimal(avg_cost)
    cash = Decimal("0")
    qty_after = qty_before
    cost_after = cost_before

    if action.action_type == ActionType.CASH_DIVIDEND:
        cash = _q2(qty_before * action.value)
        per_share = action.value
        cost_after = (cost_before - per_share).quantize(_Q)
        if cost_after < 0:
            cost_after = Decimal("0.000")
        note = (
            f"除息 {action.ex_date}：{action.describe()}；"
            f"收到现金分红 {cash}，每股成本 {cost_before} -> {cost_after}"
        )
    else:
        qty_after = adjust_quantity(qty_before, action)
        if qty_after > 0:
            cost_after = (cost_before * qty_before / qty_after).quantize(_Q)
        note = (
            f"除权 {action.ex_date}：{action.describe()}；"
            f"股数 {qty_before} -> {qty_after}，总成本不变，"
            f"每股成本 {cost_before} -> {cost_after}"
        )

    return AppliedAction(
        action=action,
        quantity_before=qty_before,
        quantity_after=qty_after,
        avg_cost_before=cost_before,
        avg_cost_after=cost_after,
        cash_received=cash,
        fx_price_factor=price_factor(action),
        note=note,
    )


@dataclass
class ActionBatch:
    """一次导入的一批企业行动（不可变发布单元）。"""

    batch_id: str
    source: str
    actions: List[CorporateAction]
    published_at: Optional[datetime] = None
    description: str = ""
    content_hash: str = ""

    def __post_init__(self):
        if not self.actions:
            raise ActionValidationError("批次内至少包含一条企业行动")
        if not self.content_hash:
            self.content_hash = self.compute_hash()

    def compute_hash(self) -> str:
        payload = {
            "batch_id": self.batch_id,
            "source": self.source,
            "description": self.description,
            "actions": sorted(
                (a.to_dict() for a in self.actions),
                key=lambda d: (d["stock_code"], d["ex_date"], d["action_type"]),
            ),
        }
        blob = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return hashlib.sha256(blob).hexdigest()

    def fingerprints(self) -> Sequence[str]:
        return [a.fingerprint() for a in self.actions]
