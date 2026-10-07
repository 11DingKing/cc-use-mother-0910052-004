"""企业行动存储：幂等导入、修订/撤销留痕、按时间点复现。"""

from __future__ import annotations

import hashlib
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.marketdata.actions import ActionType, CorporateAction
from app.marketdata.calendar_store import ImportOutcome
from app.marketdata.entities import CorporateActionRecord


def _action_signature(r: CorporateActionRecord) -> str:
    return "|".join(
        [
            r.stock_code, r.action_type, r.ex_date,
            str(r.cash_per_share), str(r.share_ratio),
            r.currency, r.client_id, str(r.revoked),
        ]
    )


class CorporateActionStore:
    def __init__(self, session: Session):
        self.session = session

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    def add_action(
        self,
        stock_code: str,
        action_type: str | ActionType,
        ex_date: date | str,
        *,
        cash_per_share: float | str | Decimal = 0.0,
        share_ratio: float | str | Decimal = 0.0,
        title: str = "",
        currency: str = "CNY",
        source: str = "manual",
        client_id: str = "",
        created_by: str = "system",
        created_at: Optional[datetime] = None,
    ) -> ImportOutcome:
        """导入一条企业行动。

        - 相同业务键 (股票, 除权日, 类型, client_id) 且内容一致 → 跳过（幂等）；
        - 内容变化（补录/修正派息额、比例）→ 追加新行，旧行经 supersedes_id 留痕；
        - 必须显式提供 client_id（建议：公告号/交易所事件 ID），防止跨来源重复。
        """
        atype = action_type if isinstance(action_type, ActionType) else ActionType(action_type)
        day_str = ex_date.isoformat() if isinstance(ex_date, date) else str(ex_date)
        if not client_id:
            raise ValueError("企业行动必须提供 client_id（公告号/事件ID），用于幂等去重")
        self._validate_params(atype, Decimal(str(cash_per_share)), Decimal(str(share_ratio)))

        existing = (
            self.session.query(CorporateActionRecord)
            .filter(
                CorporateActionRecord.stock_code == stock_code,
                CorporateActionRecord.ex_date == day_str,
                CorporateActionRecord.action_type == atype.value,
                CorporateActionRecord.client_id == client_id,
            )
            .order_by(CorporateActionRecord.id.desc())
            .first()
        )

        new_row = CorporateActionRecord(
            stock_code=stock_code,
            action_type=atype.value,
            ex_date=day_str,
            cash_per_share=float(cash_per_share),
            share_ratio=float(share_ratio),
            title=title,
            currency=currency.upper(),
            source=source,
            client_id=client_id,
            created_by=created_by,
            created_at=created_at or datetime.utcnow(),
        )

        if existing is not None:
            same = (
                Decimal(str(existing.cash_per_share)) == Decimal(str(cash_per_share))
                and Decimal(str(existing.share_ratio)) == Decimal(str(share_ratio))
                and existing.currency == currency.upper()
            )
            if same:
                return ImportOutcome(
                    "skipped_duplicate", existing.id, None,
                    f"{stock_code} {day_str} {atype.value} 内容一致，跳过重复导入",
                )
            new_row.supersedes_id = existing.id
            self.session.add(new_row)
            self.session.flush()
            return ImportOutcome(
                "superseded", new_row.id, existing.id,
                f"{stock_code} {day_str} {atype.value} 参数修正，旧行 #{existing.id} 保留",
            )

        self.session.add(new_row)
        self.session.flush()
        return ImportOutcome(
            "inserted", new_row.id, None,
            f"{stock_code} {day_str} 新增 {atype.value}：{title or ''}",
        )

    def revoke_action(
        self,
        stock_code: str,
        action_type: str | ActionType,
        ex_date: date | str,
        client_id: str,
        *,
        reason: str = "撤销此前的企业行动",
        created_by: str = "system",
        created_at: Optional[datetime] = None,
    ) -> ImportOutcome:
        atype = action_type if isinstance(action_type, ActionType) else ActionType(action_type)
        day_str = ex_date.isoformat() if isinstance(ex_date, date) else str(ex_date)
        current = (
            self.session.query(CorporateActionRecord)
            .filter(
                CorporateActionRecord.stock_code == stock_code,
                CorporateActionRecord.ex_date == day_str,
                CorporateActionRecord.action_type == atype.value,
                CorporateActionRecord.client_id == client_id,
            )
            .order_by(CorporateActionRecord.id.desc())
            .first()
        )
        if current is None:
            raise ValueError(
                f"无法撤销：{stock_code} {day_str} {atype.value} client_id={client_id} 不存在"
            )
        row = CorporateActionRecord(
            stock_code=stock_code, action_type=atype.value, ex_date=day_str,
            cash_per_share=current.cash_per_share, share_ratio=current.share_ratio,
            title=current.title, currency=current.currency, source=current.source,
            client_id=client_id, supersedes_id=current.id, revoked=1,
            created_by=created_by, created_at=created_at or datetime.utcnow(),
        )
        self.session.add(row)
        self.session.flush()
        return ImportOutcome(
            "revoked", row.id, current.id,
            f"{stock_code} {day_str} {atype.value} 已撤销，除权处理将不再应用",
        )

    @staticmethod
    def _validate_params(atype: ActionType, cash: Decimal, ratio: Decimal) -> None:
        if atype == ActionType.CASH_DIVIDEND:
            if cash <= 0:
                raise ValueError("现金分红的 cash_per_share 必须为正数")
        else:
            if ratio <= 0:
                raise ValueError("送股/拆股/合股的 share_ratio 必须为正数")
            if atype == ActionType.CONSOLIDATION and ratio >= 1:
                raise ValueError("合股 share_ratio 应为折合比例（小于 1，如 10合1 为 0.1）")
            if atype in (ActionType.STOCK_DIVIDEND, ActionType.SPLIT) and ratio > 100:
                raise ValueError("送股/拆股比例异常（share_ratio > 100），请核对单位")

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    def _effective_rows(
        self, as_of: Optional[datetime], stock_code: Optional[str] = None
    ) -> List[CorporateActionRecord]:
        q = self.session.query(CorporateActionRecord)
        if stock_code:
            q = q.filter(CorporateActionRecord.stock_code == stock_code)
        if as_of is not None:
            q = q.filter(CorporateActionRecord.created_at <= as_of)
        rows = q.order_by(
            CorporateActionRecord.stock_code,
            CorporateActionRecord.ex_date,
            CorporateActionRecord.id,
        ).all()

        # 每个业务键取最新行（可能是撤销行）
        latest: Dict[tuple, CorporateActionRecord] = {}
        for r in rows:
            latest[(r.stock_code, r.ex_date, r.action_type, r.client_id)] = r
        return [r for r in latest.values() if not r.revoked]

    def load_actions(
        self,
        as_of: Optional[datetime] = None,
        stock_code: Optional[str] = None,
    ) -> List[CorporateAction]:
        rows = self._effective_rows(as_of, stock_code)
        return [self._to_domain(r) for r in rows]

    def fingerprint(
        self, as_of: Optional[datetime] = None, stock_code: Optional[str] = None
    ) -> str:
        rows = self._effective_rows(as_of, stock_code)
        canon = "\n".join(_action_signature(r) for r in sorted(
            rows, key=lambda r: (r.stock_code, r.ex_date, r.action_type, r.client_id)
        ))
        h = hashlib.sha256(f"actions\n{canon}".encode("utf-8")).hexdigest()
        return h[:16]

    def list_actions(
        self,
        stock_code: Optional[str] = None,
        *,
        include_history: bool = True,
    ) -> List[Dict[str, Any]]:
        q = self.session.query(CorporateActionRecord)
        if stock_code:
            q = q.filter(CorporateActionRecord.stock_code == stock_code)
        rows = q.order_by(
            CorporateActionRecord.ex_date, CorporateActionRecord.id
        ).all()
        if not include_history:
            latest: Dict[tuple, CorporateActionRecord] = {}
            for r in rows:
                latest[(r.stock_code, r.ex_date, r.action_type, r.client_id)] = r
            rows = list(latest.values())
        return [
            {
                "id": r.id,
                "stock_code": r.stock_code,
                "action_type": r.action_type,
                "ex_date": r.ex_date,
                "cash_per_share": r.cash_per_share,
                "share_ratio": r.share_ratio,
                "title": r.title,
                "currency": r.currency,
                "source": r.source,
                "client_id": r.client_id,
                "supersedes_id": r.supersedes_id,
                "revoked": bool(r.revoked),
                "created_by": r.created_by,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]

    @staticmethod
    def _to_domain(r: CorporateActionRecord) -> CorporateAction:
        return CorporateAction(
            stock_code=r.stock_code,
            action_type=ActionType(r.action_type),
            ex_date=date.fromisoformat(r.ex_date),
            cash_per_share=Decimal(str(r.cash_per_share)),
            share_ratio=Decimal(str(r.share_ratio)),
            title=r.title or "",
            currency=r.currency or "CNY",
            client_id=r.client_id,
            record_id=r.id,
            supersedes_record_id=r.supersedes_id,
            created_at=r.created_at,
            created_by=r.created_by,
        )
