"""市场数据统一注册表：下单、回测、日终估值、历史查询的唯一入口。

所有调用方都不应直接读库，而是通过这里按 ``as_of`` 时间点解析“当时生效”的
日历与企业行动版本，并拿到可固定的 :class:`MarketDataPin`（版本号+指纹）。
账本在创建时固定 pin；重新运行时校验 pin，若期间数据被修订则要么按旧 pin
复现（提供 ``as_of``），要么显式另存新版本，绝不静默改写。
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.config import db_session_scope
from app.marketdata.action_store import CorporateActionStore
from app.marketdata.actions import CorporateAction, CorporateActionEngine
from app.marketdata.calendar import TradingCalendar
from app.marketdata.calendar_store import CalendarStore, ImportOutcome
from app.marketdata.seal_store import LedgerSealStore, LedgerSealedError


@dataclass(frozen=True)
class MarketDataPin:
    """一次业务运行所引用的数据版本（可序列化进账本）。"""

    market: str
    as_of: Optional[str]
    calendar_version_id: int
    calendar_fingerprint: str
    actions_fingerprint: str
    resolved_at: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MarketDataPin":
        return cls(**data)


class MarketDataRegistry:
    """线程内共享的只读解析器；写操作走显式 import_* 方法并在独立事务提交。"""

    def __init__(self, default_market: Optional[str] = None):
        self.default_market = (
            default_market or os.getenv("MARKET_DEFAULT_MARKET", "CN")
        ).upper()

    # ------------------------------------------------------------------
    # 读取解析
    # ------------------------------------------------------------------

    def calendar(
        self, market: Optional[str] = None, as_of: Optional[datetime] = None
    ) -> TradingCalendar:
        market = (market or self.default_market).upper()
        with db_session_scope() as session:
            return CalendarStore(session).load_calendar(market, as_of)

    def actions(
        self,
        stock_code: Optional[str] = None,
        as_of: Optional[datetime] = None,
    ) -> CorporateActionEngine:
        with db_session_scope() as session:
            rows = CorporateActionStore(session).load_actions(as_of, stock_code)
        return CorporateActionEngine(rows)

    def pin(
        self,
        stock_code: Optional[str] = None,
        *,
        market: Optional[str] = None,
        as_of: Optional[datetime] = None,
    ) -> MarketDataPin:
        """解析当前（或指定时间点）生效版本并固定指纹。

        ``stock_code`` 为空时，企业行动指纹覆盖全部股票（交易会话/日终口径）；
        指定股票时只覆盖该股票（单标的回测口径）。
        """
        market = (market or self.default_market).upper()
        with db_session_scope() as session:
            cal_store = CalendarStore(session)
            act_store = CorporateActionStore(session)
            calendar = cal_store.load_calendar(market, as_of)
            actions_fp = act_store.fingerprint(as_of, stock_code or None)
        return MarketDataPin(
            market=market,
            as_of=as_of.isoformat() if as_of else None,
            calendar_version_id=calendar.version_id,
            calendar_fingerprint=calendar.fingerprint,
            actions_fingerprint=actions_fp,
            resolved_at=datetime.utcnow().isoformat(),
        )

    def verify_pin(self, pin: MarketDataPin) -> Dict[str, Any]:
        """校验 pin 引用的版本是否仍能复现。

        - pin 带 as_of：按该时间点重算指纹（即使之后修订过数据也应一致）；
        - pin 不带 as_of：与当前生效版本比较，不一致会明确报告差异。
        """
        as_of = datetime.fromisoformat(pin.as_of) if pin.as_of else None
        with db_session_scope() as session:
            cal_store = CalendarStore(session)
            act_store = CorporateActionStore(session)
            calendar = cal_store.load_calendar(pin.market, as_of)
            actions_fp = act_store.fingerprint(as_of, None)

        # 账本 pin 的 actions 指纹是在“全部行动”上计算还是单股票？
        # pin 时按单股票计算；校验时若无股票上下文则只校验日历，
        # 股票级校验请使用 verify_pin_for_stock。
        cal_ok = calendar.fingerprint == pin.calendar_fingerprint
        return {
            "calendar_ok": cal_ok,
            "expected_calendar_fingerprint": pin.calendar_fingerprint,
            "current_calendar_fingerprint": calendar.fingerprint,
            "as_of": pin.as_of,
            "note": (
                "按封账时 as_of 时间点复现" if pin.as_of
                else "按当前生效版本比较；数据修订后请改用历史 as_of 复现"
            ),
        }

    def verify_pin_for_stock(
        self, pin: MarketDataPin, stock_code: str
    ) -> Dict[str, Any]:
        as_of = datetime.fromisoformat(pin.as_of) if pin.as_of else None
        with db_session_scope() as session:
            cal_store = CalendarStore(session)
            act_store = CorporateActionStore(session)
            calendar = cal_store.load_calendar(pin.market, as_of)
            actions_fp = act_store.fingerprint(as_of, stock_code)
        return {
            "calendar_ok": calendar.fingerprint == pin.calendar_fingerprint,
            "actions_ok": actions_fp == pin.actions_fingerprint,
            "expected_actions_fingerprint": pin.actions_fingerprint,
            "current_actions_fingerprint": actions_fp,
            "expected_calendar_fingerprint": pin.calendar_fingerprint,
            "current_calendar_fingerprint": calendar.fingerprint,
            "as_of": pin.as_of,
        }

    def calendar_as_of_pin(self, pin: MarketDataPin) -> TradingCalendar:
        """按账本 pin 还原日历（优先用封账时的时间点）。"""
        as_of = datetime.fromisoformat(pin.as_of) if pin.as_of else None
        with db_session_scope() as session:
            calendar = CalendarStore(session).load_calendar(pin.market, as_of)
        if calendar.fingerprint != pin.calendar_fingerprint and pin.as_of:
            # 理论上不会发生：历史时间点之前的数据只追加不可改
            raise RuntimeError(
                "日历历史版本指纹不一致，数据存储可能被外部篡改"
            )
        return calendar

    def actions_as_of_pin(
        self, pin: MarketDataPin, stock_code: Optional[str] = None
    ) -> CorporateActionEngine:
        """按账本 pin 还原企业行动。

        ``stock_code`` 给定时只载入该股票（单标的回测）；为 None 时载入
        全部股票（交易会话/日终估值口径）。
        """
        as_of = datetime.fromisoformat(pin.as_of) if pin.as_of else None
        with db_session_scope() as session:
            rows = CorporateActionStore(session).load_actions(as_of, stock_code)
        engine = CorporateActionEngine(rows)
        return engine

    # ------------------------------------------------------------------
    # 写入（显式管理操作，API 层做权限/参数校验）
    # ------------------------------------------------------------------

    def import_calendar_override(self, **kwargs) -> Dict[str, Any]:
        with db_session_scope() as session:
            outcome = CalendarStore(session).upsert_override(**kwargs)
            return outcome.to_dict()

    def revoke_calendar_override(
        self, market: str, day: Any, client_id: str, **kwargs
    ) -> Dict[str, Any]:
        with db_session_scope() as session:
            outcome = CalendarStore(session).revoke_override(
                market, day, client_id, **kwargs
            )
            return outcome.to_dict()

    def import_action(self, **kwargs) -> Dict[str, Any]:
        with db_session_scope() as session:
            outcome = CorporateActionStore(session).add_action(**kwargs)
            return outcome.to_dict()

    def revoke_action(
        self, stock_code: str, action_type: str, ex_date: Any, client_id: str,
        **kwargs,
    ) -> Dict[str, Any]:
        with db_session_scope() as session:
            outcome = CorporateActionStore(session).revoke_action(
                stock_code, action_type, ex_date, client_id, **kwargs
            )
            return outcome.to_dict()

    def list_calendar(self, market: str, **kwargs) -> List[Dict[str, Any]]:
        with db_session_scope() as session:
            return CalendarStore(session).list_overrides(market, **kwargs)

    def list_actions(self, stock_code: Optional[str] = None, **kwargs) -> List[Dict[str, Any]]:
        with db_session_scope() as session:
            return CorporateActionStore(session).list_actions(stock_code, **kwargs)

    def list_effective_actions(
        self,
        as_of: Optional[datetime] = None,
        stock_code: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """按时间点返回生效企业行动（已撤销/之后修订的旧版不出现）。"""
        with db_session_scope() as session:
            store = CorporateActionStore(session)
            return [a.to_dict() for a in store.load_actions(as_of, stock_code)]

    # ------------------------------------------------------------------
    # 封账
    # ------------------------------------------------------------------

    def seal_ledger(
        self,
        ledger_type: str,
        ref_id: str,
        ledger_payload: Any,
        pin: Optional[MarketDataPin] = None,
        *,
        sealed_by: str = "system",
    ) -> Dict[str, Any]:
        with db_session_scope() as session:
            return LedgerSealStore(session).seal(
                ledger_type,
                ref_id,
                ledger_payload,
                pin.to_dict() if pin else None,
                None,
                sealed_by=sealed_by,
            )

    def require_ledger_not_sealed(self, ledger_type: str, ref_id: str) -> None:
        with db_session_scope() as session:
            LedgerSealStore(session).require_not_sealed(ledger_type, str(ref_id))

    def get_seal(self, ledger_type: str, ref_id: str) -> Optional[Dict[str, Any]]:
        with db_session_scope() as session:
            record = LedgerSealStore(session).get(ledger_type, str(ref_id))
            if record is None:
                return None
            return LedgerSealStore._seal_dict(record)

    def verify_ledger(
        self, ledger_type: str, ref_id: str, ledger_payload: Any
    ) -> Dict[str, Any]:
        with db_session_scope() as session:
            store = LedgerSealStore(session)
            seal = store.get(ledger_type, str(ref_id))
            if seal is None:
                return {"sealed": False, "matches": None}
            return {
                "sealed": True,
                "matches": store.verify(ledger_type, str(ref_id), ledger_payload),
                "ledger_hash": seal.ledger_hash,
                "sealed_at": seal.sealed_at.isoformat() if seal.sealed_at else None,
            }

    # ------------------------------------------------------------------
    # 本地种子数据（JSON 文件，离线可配置）
    # ------------------------------------------------------------------

    def load_seed_file(self, path: str | os.PathLike) -> Dict[str, int]:
        """幂等导入本地 JSON 种子文件，可反复执行。

        结构::

            {
              "calendar": [{"market": "CN", "day": "2024-10-01",
                            "is_open": false, "reason": "国庆节",
                            "kind": "holiday", "client_id": "state-2024"}],
              "actions": [{"stock_code": "600000", "action_type": "cash_dividend",
                           "ex_date": "2024-06-03", "cash_per_share": 0.3,
                           "client_id": "600000-2024-001"}]
            }
        """
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        counts = {"calendar_inserted": 0, "calendar_skipped": 0,
                  "actions_inserted": 0, "actions_skipped": 0}
        with db_session_scope() as session:
            cal_store = CalendarStore(session)
            act_store = CorporateActionStore(session)
            for item in data.get("calendar", []):
                outcome = cal_store.upsert_override(
                    market=item["market"],
                    day=item["day"],
                    is_open=bool(item["is_open"]),
                    reason=item.get("reason", ""),
                    kind=item.get("kind", "holiday"),
                    source=item.get("source", "seed"),
                    client_id=item.get("client_id", ""),
                )
                counts["calendar_inserted" if outcome.status == "inserted"
                       else "calendar_skipped"] += 1
            for item in data.get("actions", []):
                outcome = act_store.add_action(
                    stock_code=item["stock_code"],
                    action_type=item["action_type"],
                    ex_date=item["ex_date"],
                    cash_per_share=item.get("cash_per_share", 0.0),
                    share_ratio=item.get("share_ratio", 0.0),
                    title=item.get("title", ""),
                    currency=item.get("currency", "CNY"),
                    source=item.get("source", "seed"),
                    client_id=item["client_id"],
                )
                counts["actions_inserted" if outcome.status == "inserted"
                       else "actions_skipped"] += 1
        return counts


_registry: Optional[MarketDataRegistry] = None


def get_registry() -> MarketDataRegistry:
    global _registry
    if _registry is None:
        _registry = MarketDataRegistry()
    return _registry


def reset_registry() -> None:
    """测试辅助：丢弃进程内单例。"""
    global _registry
    _registry = None


__all__ = [
    "MarketDataRegistry",
    "MarketDataPin",
    "LedgerSealedError",
    "ImportOutcome",
    "CorporateAction",
    "get_registry",
    "reset_registry",
]
