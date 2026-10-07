"""企业行动批次的本地 JSON 存储。

不变量：

1. 批次文件一旦写入不可修改；同 ``batch_id`` 不同内容再次导入 →
   :class:`ImmutableBatchError`；完全相同 → 幂等返回；
2. 单条行动按指纹（股票+除权日+类型+数值）去重，重复导入只计数、不生效第二次；
3. 同一 (股票, 除权日, 类型) 出现数值冲突的两条行动会被拒绝，
   防止补录时把同一笔分红按两个金额各应用一次。
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from app.config import BASE_DIR
from app.corporate_actions.errors import (
    ActionValidationError,
    ImmutableBatchError,
)
from app.corporate_actions.models import ActionBatch, CorporateAction

logger = logging.getLogger(__name__)

DEFAULT_ACTION_DIR = Path(
    os.getenv("CORP_ACTION_STATE_DIR", str(BASE_DIR / "data" / "state" / "actions"))
)


class ActionStore:
    def __init__(self, state_dir: Path | str = DEFAULT_ACTION_DIR):
        self._dir = Path(state_dir)
        self._lock = threading.RLock()
        self._batches: Dict[str, ActionBatch] = {}
        self._index: Dict[str, CorporateAction] = {}  # fingerprint -> action
        self.reload()

    # ------------------------------------------------------------------ 载入

    def reload(self) -> None:
        with self._lock:
            batches: Dict[str, ActionBatch] = {}
            index: Dict[str, CorporateAction] = {}
            if self._dir.exists():
                for path in sorted(self._dir.glob("*.json")):
                    batch = self._load_file(path)
                    if batch.batch_id in batches:
                        raise ImmutableBatchError(
                            f"企业行动批次 {batch.batch_id} 在多个文件中重复出现",
                            details={"batch_id": batch.batch_id},
                        )
                    batches[batch.batch_id] = batch
                    for action in batch.actions:
                        index.setdefault(action.fingerprint(), action)
            self._batches = batches
            self._index = index

    def _load_file(self, path: Path) -> ActionBatch:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            raise ActionValidationError(f"企业行动文件 {path} 无法解析: {e}") from e

        actions = [CorporateAction.from_dict(a) for a in raw.get("actions", [])]
        published_raw = raw.get("published_at")
        batch = ActionBatch(
            batch_id=str(raw["batch_id"]),
            source=str(raw.get("source", "unknown")),
            actions=actions,
            published_at=datetime.fromisoformat(published_raw) if published_raw else None,
            description=str(raw.get("description", "")),
            content_hash=str(raw.get("content_hash", "")),
        )
        stored_hash = raw.get("content_hash")
        if stored_hash and stored_hash != batch.compute_hash():
            raise ActionValidationError(
                f"企业行动批次 {batch.batch_id} 内容哈希校验失败：文件可能被非发布流程改动",
                details={
                    "batch_id": batch.batch_id,
                    "expected_hash": stored_hash,
                    "actual_hash": batch.compute_hash(),
                    "file": str(path),
                },
            )
        return batch

    # ------------------------------------------------------------------ 查询

    def manifest(self) -> List[dict]:
        """生效数据集指纹，写入回测账本。"""
        with self._lock:
            return [
                {
                    "batch_id": b.batch_id,
                    "source": b.source,
                    "content_hash": b.content_hash,
                    "published_at": b.published_at.isoformat() if b.published_at else None,
                }
                for b in sorted(self._batches.values(), key=lambda x: x.batch_id)
            ]

    def list_batches(self) -> List[dict]:
        with self._lock:
            return [
                {
                    "batch_id": b.batch_id,
                    "source": b.source,
                    "description": b.description,
                    "action_count": len(b.actions),
                    "content_hash": b.content_hash,
                    "published_at": b.published_at.isoformat() if b.published_at else None,
                }
                for b in sorted(self._batches.values(), key=lambda x: x.batch_id)
            ]

    def actions_for(
        self,
        stock_code: str,
        *,
        on_or_after: Optional[date] = None,
        on_or_before: Optional[date] = None,
    ) -> List[CorporateAction]:
        """返回某股票按 (除权日, 类型) 排序的生效行动（指纹去重）。"""
        with self._lock:
            actions = [a for a in self._index.values() if a.stock_code == stock_code]
        if on_or_after:
            actions = [a for a in actions if a.ex_date >= on_or_after]
        if on_or_before:
            actions = [a for a in actions if a.ex_date <= on_or_before]
        return sorted(actions, key=lambda a: (a.ex_date, a.action_type, str(a.value)))

    def all_actions(self) -> List[Tuple[str, CorporateAction]]:
        """返回 (batch_id, action) 形式的全部生效行动（指纹去重，确定性排序）。"""
        with self._lock:
            fps = set()
            out: List[Tuple[str, CorporateAction]] = []
            for batch in sorted(self._batches.values(), key=lambda b: b.batch_id):
                for action in batch.actions:
                    fp = action.fingerprint()
                    if fp in fps:
                        continue
                    fps.add(fp)
                    out.append((batch.batch_id, action))
        out.sort(key=lambda x: (x[1].stock_code, x[1].ex_date, x[1].action_type, x[0]))
        return out

    # ------------------------------------------------------------------ 发布

    def publish_batch(
        self,
        batch_id: str,
        actions: Sequence[CorporateAction],
        *,
        source: str = "manual",
        description: str = "",
    ) -> dict:
        """发布一批企业行动，返回导入/幂等跳过的计数。"""
        if not batch_id:
            raise ActionValidationError("batch_id 不能为空")
        actions = list(actions)
        if not actions:
            raise ActionValidationError("批次内至少包含一条企业行动")

        with self._lock:
            if batch_id in self._batches:
                existing = self._batches[batch_id]
                candidate = ActionBatch(
                    batch_id=batch_id, source=source, actions=actions,
                    description=description,
                )
                if existing.content_hash == candidate.content_hash:
                    logger.info("企业行动批次重复导入（幂等忽略）: %s", batch_id)
                    return {"batch_id": batch_id, "imported": 0,
                            "skipped_duplicates": len(actions), "status": "identical"}
                raise ImmutableBatchError(
                    f"批次 {batch_id} 已发布且内容不同，已封版批次不能改写；请使用新批次号",
                    details={"batch_id": batch_id},
                )

            imported, skipped = self._dedup_and_check_conflicts(actions)

            batch = ActionBatch(
                batch_id=batch_id,
                source=source,
                actions=actions,
                published_at=datetime.utcnow(),
                description=description,
            )
            self._write_file(batch)
            self.reload()
            logger.info(
                "企业行动批次已发布: %s（新增 %d，幂等跳过 %d）",
                batch_id, imported, skipped,
            )
            return {"batch_id": batch_id, "imported": imported,
                    "skipped_duplicates": skipped, "status": "published",
                    "content_hash": batch.content_hash}

    def _dedup_and_check_conflicts(
        self, actions: Sequence[CorporateAction]
    ) -> Tuple[int, int]:
        # 批次内自查：同指纹重复 / 同键不同值冲突
        seen_fingerprints: Dict[str, CorporateAction] = {}
        key_index: Dict[Tuple[str, date, str], CorporateAction] = {
            (a.stock_code, a.ex_date, a.action_type): a for a in self._index.values()
        }
        imported = 0
        skipped = 0
        for action in actions:
            fp = action.fingerprint()
            if fp in seen_fingerprints:
                skipped += 1
                continue
            seen_fingerprints[fp] = action

            if fp in self._index:
                skipped += 1
                continue

            key = (action.stock_code, action.ex_date, action.action_type)
            prior = key_index.get(key)
            if prior is not None and prior.value != action.value:
                raise ActionValidationError(
                    f"{action.stock_code} 在 {action.ex_date} 的 {action.action_type} "
                    f"已存在数值 {prior.value}，与本次 {action.value} 冲突；"
                    f"补录勘误请先发更正批次并联系封账处理，不得静默覆盖",
                    details={
                        "stock_code": action.stock_code,
                        "ex_date": action.ex_date.isoformat(),
                        "action_type": action.action_type,
                        "existing_value": str(prior.value),
                        "new_value": str(action.value),
                    },
                )
            key_index[key] = action
            imported += 1
        return imported, skipped

    def _write_file(self, batch: ActionBatch) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._dir / f"{batch.batch_id}.json"
        payload = {
            "batch_id": batch.batch_id,
            "source": batch.source,
            "description": batch.description,
            "published_at": batch.published_at.isoformat(),
            "content_hash": batch.content_hash,
            "actions": [a.to_dict() for a in batch.actions],
        }
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(tmp, path)
