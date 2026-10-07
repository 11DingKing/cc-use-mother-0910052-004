"""交易日历存储：幂等导入、修订留痕、按时间点复现。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.marketdata.calendar import (
    DEFAULT_WEEKENDS,
    MARKET_SESSION_CLOSE,
    MARKET_TIMEZONES,
    CalendarEntry,
    TradingCalendar,
)
from app.marketdata.entities import CalendarOverrideRecord


@dataclass(frozen=True)
class ImportOutcome:
    """单次导入的结果，重复导入绝不会静默产生第二行。"""

    status: str                 # inserted | skipped_duplicate | superseded | revoked
    record_id: int
    superseded_id: Optional[int]
    detail: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "record_id": self.record_id,
            "superseded_id": self.superseded_id,
            "detail": self.detail,
        }


def _row_signature(row: CalendarOverrideRecord) -> str:
    payload = "|".join(
        [
            row.market, row.day, str(row.is_open), row.reason,
            row.kind, row.client_id, str(row.revoked),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class CalendarStore:
    def __init__(self, session: Session):
        self.session = session

    # ------------------------------------------------------------------
    # 写入：幂等 + 只追加
    # ------------------------------------------------------------------

    def upsert_override(
        self,
        market: str,
        day: date | str,
        is_open: bool,
        reason: str,
        kind: str = "holiday",
        *,
        source: str = "manual",
        client_id: str = "",
        created_by: str = "system",
        created_at: Optional[datetime] = None,
    ) -> ImportOutcome:
        """导入一条日历覆盖。

        - 同 ``client_id`` 且内容一致 → ``skipped_duplicate``，不新增行；
        - 同 ``client_id`` 但内容变化 → 追加修订行（``supersedes_id`` 指向旧行）；
        - 不带 ``client_id`` 的重复日按内容判重，内容一致同样跳过。
        """
        market = market.upper()
        day_str = day.isoformat() if isinstance(day, date) else str(day)
        client_id = client_id or f"day:{day_str}"

        existing = (
            self.session.query(CalendarOverrideRecord)
            .filter(
                CalendarOverrideRecord.market == market,
                CalendarOverrideRecord.day == day_str,
                CalendarOverrideRecord.client_id == client_id,
            )
            .order_by(CalendarOverrideRecord.id.desc())
            .first()
        )

        new_row = CalendarOverrideRecord(
            market=market,
            day=day_str,
            is_open=1 if is_open else 0,
            reason=reason,
            kind=kind,
            source=source,
            client_id=client_id,
            created_by=created_by,
            created_at=created_at or datetime.utcnow(),
        )

        if existing is not None:
            same = (
                existing.is_open == (1 if is_open else 0)
                and existing.reason == reason
                and existing.kind == kind
            )
            if same:
                return ImportOutcome(
                    "skipped_duplicate", existing.id, None,
                    f"{market} {day_str} 内容一致，跳过重复导入",
                )
            new_row.supersedes_id = existing.id
            self.session.add(new_row)
            self.session.flush()
            return ImportOutcome(
                "superseded", new_row.id, existing.id,
                f"{market} {day_str} 内容修订，旧行 #{existing.id} 保留可追溯",
            )

        self.session.add(new_row)
        self.session.flush()
        return ImportOutcome(
            "inserted", new_row.id, None,
            f"{market} {day_str} 新增{'开市' if is_open else '休市'}覆盖：{reason}",
        )

    def revoke_override(
        self,
        market: str,
        day: date | str,
        client_id: str,
        *,
        reason: str = "撤销此前的日历覆盖",
        created_by: str = "system",
        created_at: Optional[datetime] = None,
    ) -> ImportOutcome:
        """撤销某日某来源的覆盖（追加撤销行，而非删除旧行）。"""
        market = market.upper()
        day_str = day.isoformat() if isinstance(day, date) else str(day)
        client_id = client_id or f"day:{day_str}"

        current = (
            self.session.query(CalendarOverrideRecord)
            .filter(
                CalendarOverrideRecord.market == market,
                CalendarOverrideRecord.day == day_str,
                CalendarOverrideRecord.client_id == client_id,
            )
            .order_by(CalendarOverrideRecord.id.desc())
            .first()
        )
        if current is None:
            raise ValueError(f"无法撤销：{market} {day_str} client_id={client_id} 不存在")

        row = CalendarOverrideRecord(
            market=market, day=day_str, is_open=current.is_open,
            reason=reason, kind=current.kind, source=current.source,
            client_id=client_id, supersedes_id=current.id, revoked=1,
            created_by=created_by, created_at=created_at or datetime.utcnow(),
        )
        self.session.add(row)
        self.session.flush()
        return ImportOutcome(
            "revoked", row.id, current.id,
            f"{market} {day_str} 覆盖已撤销，恢复周末规则推导",
        )

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    def _effective_rows(
        self, market: str, as_of: Optional[datetime]
    ) -> List[CalendarOverrideRecord]:
        q = self.session.query(CalendarOverrideRecord).filter(
            CalendarOverrideRecord.market == market.upper()
        )
        if as_of is not None:
            q = q.filter(CalendarOverrideRecord.created_at <= as_of)
        rows = q.order_by(
            CalendarOverrideRecord.day, CalendarOverrideRecord.id
        ).all()

        # 每天取最新一行；撤销行表示该天无覆盖
        latest: Dict[str, CalendarOverrideRecord] = {}
        for row in rows:
            latest[row.day] = row
        return [r for r in latest.values() if not r.revoked]

    def load_calendar(
        self,
        market: str = "CN",
        as_of: Optional[datetime] = None,
    ) -> TradingCalendar:
        market = market.upper()
        rows = self._effective_rows(market, as_of)
        entries = [
            CalendarEntry(
                day=date.fromisoformat(r.day),
                is_open=bool(r.is_open),
                reason=r.reason,
                kind=r.kind,
                record_id=r.id,
                revised_record_id=r.supersedes_id,
            )
            for r in rows
        ]
        fingerprint = self.fingerprint(market, as_of)
        version_id = max((r.id for r in rows), default=0)
        return TradingCalendar.from_overrides(
            entries,
            market=market,
            version_id=version_id,
            generated_at=as_of,
            fingerprint=fingerprint,
            timezone=MARKET_TIMEZONES.get(market, "UTC"),
            weekends=tuple(DEFAULT_WEEKENDS.get(market, (6, 7))),
            session_close=MARKET_SESSION_CLOSE.get(market, time(16, 0)),
        )

    def fingerprint(self, market: str, as_of: Optional[datetime] = None) -> str:
        rows = self._effective_rows(market, as_of)
        canon = "\n".join(_row_signature(r) for r in sorted(rows, key=lambda r: (r.day, r.id)))
        h = hashlib.sha256(f"calendar:{market.upper()}\n{canon}".encode("utf-8")).hexdigest()
        return h[:16]

    def list_overrides(
        self, market: str, *, include_history: bool = True
    ) -> List[Dict[str, Any]]:
        q = self.session.query(CalendarOverrideRecord).filter(
            CalendarOverrideRecord.market == market.upper()
        ).order_by(CalendarOverrideRecord.day, CalendarOverrideRecord.id)
        rows = q.all()
        if not include_history:
            latest: Dict[Tuple[str, str], CalendarOverrideRecord] = {}
            for r in rows:
                latest[(r.day, r.client_id)] = r
            rows = list(latest.values())
        return [
            {
                "id": r.id,
                "market": r.market,
                "day": r.day,
                "is_open": bool(r.is_open),
                "reason": r.reason,
                "kind": r.kind,
                "source": r.source,
                "client_id": r.client_id,
                "supersedes_id": r.supersedes_id,
                "revoked": bool(r.revoked),
                "created_by": r.created_by,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
