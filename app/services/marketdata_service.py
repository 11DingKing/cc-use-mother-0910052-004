"""市场数据管理服务：交易日历查询/补录发布、企业行动批次发布。

所有写入都走不可变版本语义；查询接口显式返回所依据的生效版本指纹，
让调用方能够说明“某价格/数量为何调整”。
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from app.calendar import CalendarStore
from app.calendar.errors import CalendarValidationError
from app.corporate_actions import ActionStore, CorporateAction
from app.marketdata.provider import get_provider

logger = logging.getLogger(__name__)


class MarketDataService:
    def __init__(
        self,
        calendar_store: Optional[CalendarStore] = None,
        action_store: Optional[ActionStore] = None,
    ):
        self._calendar_store_override = calendar_store
        self._action_store_override = action_store

    @property
    def calendars(self) -> Optional[CalendarStore]:
        if self._calendar_store_override is not None:
            return self._calendar_store_override
        store = get_provider()._calendar_store()
        self._calendar_store_override = store
        return store

    @calendars.setter
    def calendars(self, value: Optional[CalendarStore]) -> None:
        self._calendar_store_override = value

    @property
    def actions(self) -> ActionStore:
        if self._action_store_override is not None:
            return self._action_store_override
        store = get_provider()._action_store()
        self._action_store_override = store
        return store

    @actions.setter
    def actions(self, value: ActionStore) -> None:
        self._action_store_override = value

    # ------------------------------------------------------------ 日历查询

    def list_markets(self) -> List[dict]:
        if self.calendars is None:
            return []
        return [
            {"market": m, "versions": self.calendars.list_versions(m)}
            for m in self.calendars.markets()
        ]

    def list_versions(self, market: str) -> List[dict]:
        self._require_calendars()
        return self.calendars.list_versions(market.upper())

    def trading_day_info(self, market: str, day_str: str) -> Dict[str, Any]:
        """查询某日市态及归一化结果（说明休市/半日/顺延）。"""
        self._require_calendars()
        day = date.fromisoformat(day_str)
        cal = self.calendars.get_calendar(market.upper())
        is_session = cal.is_trading_day(day)
        resolution = cal.resolve(day)
        return {
            "market": market.upper(),
            "date": day_str,
            "is_trading_day": is_session,
            "reason": cal.reason_for_day(day),
            "resolved_trading_day": resolution.trading_day.isoformat(),
            "shifted": resolution.shifted,
            "calendar_version": cal.version,
            "content_hash": cal.content_hash,
        }

    def next_trading_day(self, market: str, day_str: str, count: int = 1) -> Dict[str, Any]:
        self._require_calendars()
        day = date.fromisoformat(day_str)
        cal = self.calendars.get_calendar(market.upper())
        cur = day
        for _ in range(max(count, 1)):
            cur = cal.next_trading_day(cur)
        return {"market": market.upper(), "from": day_str, "next": cur.isoformat(),
                "calendar_version": cal.version}

    def trading_days(self, market: str, start_str: str, end_str: str) -> Dict[str, Any]:
        self._require_calendars()
        start = date.fromisoformat(start_str)
        end = date.fromisoformat(end_str)
        cal = self.calendars.get_calendar(market.upper())
        days = cal.trading_days(start, end)
        return {
            "market": market.upper(),
            "start": start_str,
            "end": end_str,
            "trading_days": [d.isoformat() for d in days],
            "count": len(days),
            "calendar_version": cal.version,
            "content_hash": cal.content_hash,
        }

    # ------------------------------------------------------------ 日历发布

    def publish_calendar(self, market: str, payload: dict, description: str = "",
                         version: Optional[str] = None) -> Dict[str, Any]:
        """发布日历新版本（临时休市/补班补录的唯一合法入口）。"""
        self._require_calendars()
        v = self.calendars.publish(
            market.upper(), payload, description=description, version=version
        )
        return {
            "market": v.market,
            "version": v.version,
            "parent_version": v.parent_version,
            "content_hash": v.content_hash(),
            "description": v.description,
        }

    def publish_special_session(
        self,
        market: str,
        day_str: str,
        session_type: str,
        name: str,
        reason: str = "",
        close_time: Optional[str] = None,
        description: str = "",
    ) -> Dict[str, Any]:
        """便捷入口：以“当前生效版本 + 一处调整”发布新版本。

        例如临时休市：不修改历史版本，而是复制当前节假日集合后追加 closed。
        """
        self._require_calendars()
        market = market.upper()
        head = self.calendars.get_version(market)
        new_event = {
            "day": day_str,
            "name": name,
            "session_type": session_type,
            "close_time": close_time,
            "reason": reason,
        }
        payload = head.canonical_payload()
        specials = list(payload.get("special_sessions", []))
        # 相同调整重复提交（字段完全一致）幂等返回当前链头，不产生新版本
        for existing in specials:
            if all(existing.get(k) == v for k, v in new_event.items()):
                return {
                    "market": market,
                    "version": head.version,
                    "parent_version": head.parent_version,
                    "content_hash": head.content_hash(),
                    "description": head.description,
                    "deduplicated": True,
                }
        specials.append(new_event)
        payload["special_sessions"] = specials
        published = self.publish_calendar(market, payload, description or f"补录 {day_str} {name}")
        published["deduplicated"] = False
        return published

    # --------------------------------------------------------- 企业行动查询

    def list_batches(self) -> List[dict]:
        return self.actions.list_batches()

    def list_actions(self, stock_code: Optional[str] = None) -> List[dict]:
        """列出生效企业行动（指纹去重）；指定股票时只返回该股票。"""
        if stock_code:
            return [a.to_dict() for a in self.actions.actions_for(stock_code)]
        return self._all_actions()

    def _all_actions(self) -> List[dict]:
        out = []
        for batch_id, action in self.actions.all_actions():
            d = action.to_dict()
            d["batch_id"] = batch_id
            out.append(d)
        return out

    # --------------------------------------------------------- 企业行动发布

    def publish_actions(
        self,
        batch_id: str,
        actions: List[dict],
        source: str = "manual",
        description: str = "",
    ) -> Dict[str, Any]:
        """发布企业行动批次；重复导入幂等，冲突数值或同批次不同内容将被拒绝。"""
        parsed = []
        for raw in actions:
            parsed.append(CorporateAction(
                stock_code=str(raw["stock_code"]),
                ex_date=date.fromisoformat(raw["ex_date"]),
                action_type=str(raw["action_type"]),
                value=raw["value"],
                currency=str(raw.get("currency", "CNY")),
                name=str(raw.get("name", "")),
                record_date=date.fromisoformat(raw["record_date"]) if raw.get("record_date") else None,
                pay_date=date.fromisoformat(raw["pay_date"]) if raw.get("pay_date") else None,
            ))
        return self.actions.publish_batch(
            batch_id, parsed, source=source, description=description
        )

    def manifest(self) -> Dict[str, Any]:
        """当前生效数据集指纹（日历链头 + 全部行动批次）。"""
        calendars = []
        if self.calendars is not None:
            for market in self.calendars.markets():
                cal = self.calendars.get_calendar(market)
                calendars.append(cal.manifest())
        return {
            "generated_at": datetime.utcnow().isoformat(),
            "calendars": calendars,
            "corporate_action_batches": self.actions.manifest(),
        }

    def _require_calendars(self) -> None:
        if self.calendars is None:
            raise CalendarValidationError(
                "交易日历功能未启用（CALENDAR_ENABLED=false 或无可用日历存储）"
            )
