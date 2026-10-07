"""市场数据基础设施：交易日历与企业行动。

本包为下单、回测、日终估值和历史查询提供同一份、带生效版本的
交易日历与企业行动数据，保证：

- 所有入口通过 ``MarketDataRegistry`` 按 ``as_of`` 时间点解析“当时生效”的版本；
- 日历的临时休市/补班、企业行动的补录/修订都只追加修订行，旧版本可按时间点复现；
- 任何价格或数量的调整都会生成带原因与计算公式的 ``LedgerAdjustment``；
- 已封账（seal）的历史账本永远不会被新版本数据静默改写。
"""

from app.marketdata.calendar import (
    TradingCalendar,
    CalendarEntry,
    MarketClosedError,
    MARKET_TIMEZONES,
    MARKET_SESSION_CLOSE,
)
from app.marketdata.actions import (
    ActionType,
    CorporateAction,
    LedgerAdjustment,
    CorporateActionEngine,
)
from app.marketdata.registry import (
    MarketDataRegistry,
    MarketDataPin,
    get_registry,
    reset_registry,
)

__all__ = [
    "TradingCalendar",
    "CalendarEntry",
    "MarketClosedError",
    "MARKET_TIMEZONES",
    "MARKET_SESSION_CLOSE",
    "ActionType",
    "CorporateAction",
    "LedgerAdjustment",
    "CorporateActionEngine",
    "MarketDataRegistry",
    "MarketDataPin",
    "get_registry",
    "reset_registry",
]
