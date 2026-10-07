"""统一的市场数据生效版本提供者。"""

from __future__ import annotations

import logging
import os
import threading
from datetime import date, datetime
from typing import List, Optional

from app.calendar import CalendarStore, TradingCalendar
from app.corporate_actions import ActionStore, CorporateAction

logger = logging.getLogger(__name__)


def resolve_market(stock_code: str) -> str:
    """根据股票代码推断市场。

    - 6 位数字（可带 sh/sz/bj 前缀）→ CN
    - 0 开头 5 位数字 → HK
    - 1-5 位字母 → US
    推断失败默认 CN（A股系统主场景）。
    """
    code = (stock_code or "").strip().lower()
    if code.startswith(("sh", "sz", "bj")):
        return "CN"
    if code.isdigit():
        if len(code) == 6:
            return "CN"
        if len(code) == 5 and code.startswith("0"):
            return "HK"
    if code.isalpha() and 1 <= len(code) <= 5:
        return "US"
    return "CN"


class MarketDataProvider:
    """日历与企业行动的同一生效版本门面。"""

    def __init__(
        self,
        calendar_store: Optional[CalendarStore] = None,
        action_store: Optional[ActionStore] = None,
        *,
        calendar_enabled: Optional[bool] = None,
    ):
        if calendar_enabled is None:
            calendar_enabled = os.getenv("CALENDAR_ENABLED", "true").lower() == "true"
        self._calendar_enabled = calendar_enabled
        self._calendars = calendar_store
        self._actions = action_store
        self._lock = threading.RLock()

    # -- 延迟初始化（避免无数据库/无配置环境下导入即失败）-------------------

    def _calendar_store(self) -> Optional[CalendarStore]:
        if not self._calendar_enabled:
            return None
        with self._lock:
            if self._calendars is None:
                self._calendars = CalendarStore()
            return self._calendars

    def _action_store(self) -> ActionStore:
        with self._lock:
            if self._actions is None:
                self._actions = ActionStore()
            return self._actions

    # -- 日历 ---------------------------------------------------------------

    def calendar_for(self, market_or_code: str) -> TradingCalendar:
        """返回某市场（或根据代码推断）当前生效日历。

        未启用日历或本地未配置该市场时，回退为宽松日历（每日皆交易日），
        回退事实会记录在日历 manifest 的 ``fallback`` 字段中。
        """
        market = market_or_code.upper() if market_or_code in ("CN", "US", "HK") \
            else resolve_market(market_or_code)
        store = self._calendar_store()
        if store is not None and market in store.markets():
            return store.get_calendar(market)
        logger.debug("市场 %s 使用宽松内置日历（无本地生效版本）", market)
        return TradingCalendar.permissive()

    def calendar_manifest(self, market: str) -> dict:
        cal = self.calendar_for(market)
        manifest = cal.manifest()
        if manifest["version"] == "builtin-permissive-v1":
            manifest["fallback"] = True
            manifest["market"] = market
        else:
            manifest["fallback"] = False
        return manifest

    # -- 企业行动 -----------------------------------------------------------

    def actions_for(
        self,
        stock_code: str,
        start: Optional[date] = None,
        end: Optional[date] = None,
    ) -> List[CorporateAction]:
        return self._action_store().actions_for(
            stock_code, on_or_after=start, on_or_before=end
        )

    def action_manifest(self) -> List[dict]:
        return self._action_store().manifest()

    # -- 账本指纹（复现用）--------------------------------------------------

    def ledger_manifest(self, market: str) -> dict:
        """写入订单/回测/估值结果的生效版本指纹。

        重新运行时凭此指纹可以校验：使用的日历版本与企业行动批次与当初完全一致。
        """
        return {
            "calendar": self.calendar_manifest(market),
            "corporate_action_batches": self.action_manifest(),
            "recorded_at": datetime.utcnow().isoformat(),
        }


# 单例
_default: Optional[MarketDataProvider] = None


def get_provider() -> MarketDataProvider:
    global _default
    if _default is None:
        _default = MarketDataProvider()
    return _default
