"""账本封账存储：封账后的历史结果不可被静默改写。"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from app.marketdata.entities import LedgerSealRecord


class LedgerSealedError(Exception):
    """账本已封账，任何修改/覆盖操作都必须显式解封或另存新版本。"""

    def __init__(self, ledger_type: str, ref_id: str, sealed_at: datetime):
        self.ledger_type = ledger_type
        self.ref_id = ref_id
        self.sealed_at = sealed_at
        super().__init__(
            f"账本 {ledger_type}:{ref_id} 已于 {sealed_at.isoformat()} 封账，"
            "禁止改写；如需修正请另存新版本"
        )


class LedgerSealStore:
    def __init__(self, session: Session):
        self.session = session

    def get(self, ledger_type: str, ref_id: str) -> Optional[LedgerSealRecord]:
        return (
            self.session.query(LedgerSealRecord)
            .filter(
                LedgerSealRecord.ledger_type == ledger_type,
                LedgerSealRecord.ref_id == str(ref_id),
            )
            .first()
        )

    def is_sealed(self, ledger_type: str, ref_id: str) -> bool:
        return self.get(ledger_type, ref_id) is not None

    def require_not_sealed(self, ledger_type: str, ref_id: str) -> None:
        seal = self.get(ledger_type, ref_id)
        if seal is not None:
            raise LedgerSealedError(ledger_type, ref_id, seal.sealed_at)

    def seal(
        self,
        ledger_type: str,
        ref_id: str,
        ledger_payload: Any,
        calendar_pin: Optional[Dict[str, Any]] = None,
        actions_pin: Optional[Dict[str, Any]] = None,
        *,
        sealed_by: str = "system",
    ) -> Dict[str, Any]:
        """封账：固化账本内容哈希与其引用的数据版本。重复封账必须内容一致。"""
        existing = self.get(ledger_type, ref_id)
        ledger_hash = stable_hash(ledger_payload)
        if existing is not None:
            if existing.ledger_hash != ledger_hash:
                raise LedgerSealedError(ledger_type, ref_id, existing.sealed_at)
            return self._seal_dict(existing)

        record = LedgerSealRecord(
            ledger_type=ledger_type,
            ref_id=str(ref_id),
            ledger_hash=ledger_hash,
            calendar_pin_json=json.dumps(calendar_pin, ensure_ascii=False, sort_keys=True)
            if calendar_pin else None,
            actions_pin_json=json.dumps(actions_pin, ensure_ascii=False, sort_keys=True)
            if actions_pin else None,
            sealed_by=sealed_by,
        )
        self.session.add(record)
        self.session.flush()
        return self._seal_dict(record)

    def verify(self, ledger_type: str, ref_id: str, ledger_payload: Any) -> bool:
        """校验当前账本内容是否与封账时一致（重跑复现校验）。"""
        seal = self.get(ledger_type, ref_id)
        if seal is None:
            return False
        return seal.ledger_hash == stable_hash(ledger_payload)

    @staticmethod
    def _seal_dict(r: LedgerSealRecord) -> Dict[str, Any]:
        return {
            "ledger_type": r.ledger_type,
            "ref_id": r.ref_id,
            "ledger_hash": r.ledger_hash,
            "calendar_pin": json.loads(r.calendar_pin_json) if r.calendar_pin_json else None,
            "actions_pin": json.loads(r.actions_pin_json) if r.actions_pin_json else None,
            "sealed_by": r.sealed_by,
            "sealed_at": r.sealed_at.isoformat() if r.sealed_at else None,
        }


def stable_hash(payload: Any) -> str:
    """对 JSON 可序列化账本内容计算稳定哈希（键排序、不依赖插入顺序）。"""
    canon = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()
