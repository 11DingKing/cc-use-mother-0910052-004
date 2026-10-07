"""回测/估值使用的市场数据上下文。

把 :class:`MarketDataProvider` 固定到某一具体生效版本，供一次回测全程使用；
账本中记录 :attr:`manifest`，重新运行时据此校验版本未被静默替换。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from typing import List, Optional

from app.calendar import TradingCalendar
from app.corporate_actions import AppliedAction, CorporateAction, apply_action_to_position
from app.marketdata.provider import MarketDataProvider, resolve_market


@dataclass(frozen=True)
class MarketContext:
    """一次计算（回测/估值）绑定的日历版本 + 企业行动集合。"""

    calendar: TradingCalendar
    actions: List[CorporateAction]
    manifest: dict

    @property
    def calendar_version(self) -> str:
        return self.calendar.version

    def actions_on(self, stock_code: str, day: date) -> List[CorporateAction]:
        return [
            a for a in self.actions
            if a.stock_code == stock_code and a.ex_date == day
        ]

    def actions_between(
        self, stock_code: str, start: date, end: date
    ) -> List[CorporateAction]:
        return [
            a for a in self.actions
            if a.stock_code == stock_code and start <= a.ex_date <= end
        ]

    @staticmethod
    def build(
        stock_code: str,
        provider: Optional[MarketDataProvider] = None,
        *,
        start: Optional[date] = None,
        end: Optional[date] = None,
        calendar_version: Optional[str] = None,
    ) -> "MarketContext":
        """根据股票代码解析市场并固定生效版本。

        ``provider=None`` 时给出宽松上下文（每日皆交易日、无企业行动），
        保证旧调用方与单元测试的行为不变。
        """
        if provider is None:
            cal = TradingCalendar.permissive()
            return MarketContext(calendar=cal, actions=[], manifest={
                "calendar": cal.manifest(),
                "corporate_action_batches": [],
                "pinned": False,
            })

        market = resolve_market(stock_code)
        store = provider._calendar_store()  # noqa: SLF001 (同包协作)
        if store is not None and market in store.markets():
            cal = store.get_calendar(market, calendar_version)
        else:
            cal = TradingCalendar.permissive()
        actions = provider.actions_for(stock_code, start, end)
        # 指纹只记录实际解析到的版本（version + content_hash），不记录“如何选中”
        # （默认链头 vs 显式固定同一版本必须复现同一账本）。
        manifest = {
            "calendar": cal.manifest(),
            "corporate_action_batches": provider.action_manifest(),
            "pinned": True,
            "market": market,
        }
        return MarketContext(calendar=cal, actions=actions, manifest=manifest)


def apply_actions(
    actions: List[CorporateAction],
    quantity,
    avg_cost,
) -> tuple:
    """顺序应用某一日的全部行动，返回 (股数, 成本, 现金, 调整说明列表)。"""
    from decimal import Decimal

    qty = Decimal(str(quantity))
    cost = Decimal(str(avg_cost))
    cash = Decimal("0")
    notes: List[AppliedAction] = []
    for action in actions:
        applied = apply_action_to_position(action, qty, cost)
        qty = applied.quantity_after
        cost = applied.avg_cost_after
        cash += applied.cash_received
        notes.append(applied)
    return qty, cost, cash, notes


def fingerprint_payload(payload: dict) -> str:
    """对账本输入（配置/K线/信号/版本指纹）计算确定性哈希。"""
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()
