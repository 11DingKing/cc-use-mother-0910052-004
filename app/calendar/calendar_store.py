"""日历版本的本地配置加载与发布。

两个目录：

- ``config_dir``（随仓库发布、只读）：内置日历版本，如 CN/US 年度节假日；
- ``state_dir``（运行时可写，默认在仓库 ``data/state`` 下）：临时休市补录等
  事后调整以**新版本**形式追加到这里。

版本一旦发布即不可变：

- 已存在的版本号不得用不同内容再次发布（抛 :class:`ImmutableVersionError`）；
- 已发布版本不可删除/覆盖，纠错只能追加新版本；
- 重复导入相同内容是幂等的（按版本号 + 内容哈希去重），不会产生新版本；
- 载入时校验内容哈希，文件被手改会被发现。
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from app.calendar.errors import CalendarValidationError, ImmutableVersionError
from app.calendar.models import CalendarVersion, CalendarView, build_view
from app.calendar.trading_calendar import TradingCalendar
from app.config import BASE_DIR

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_DIR = Path(
    os.getenv("CALENDAR_CONFIG_DIR", str(BASE_DIR / "data" / "calendar"))
)
DEFAULT_STATE_DIR = Path(
    os.getenv("CALENDAR_STATE_DIR", str(BASE_DIR / "data" / "state" / "calendar"))
)


@dataclass
class _MarketChain:
    """单个市场的版本链（父指针串联，线性）。"""

    market: str
    versions: List[CalendarVersion]  # 按版本链顺序，head 为最新

    @property
    def head(self) -> CalendarVersion:
        return self.versions[-1]


class CalendarStore:
    """线程安全的内存版本库 + 本地 JSON 持久化。"""

    def __init__(
        self,
        config_dir: Path | str = DEFAULT_CONFIG_DIR,
        state_dir: Path | str = DEFAULT_STATE_DIR,
    ):
        self._config_dir = Path(config_dir)
        self._state_dir = Path(state_dir)
        self._chains: Dict[str, _MarketChain] = {}
        self._lock = threading.RLock()
        self.reload()

    # ------------------------------------------------------------------ 载入

    def reload(self) -> None:
        """重新从 config/state 目录载入全部市场（主要用于测试与配置更新）。"""
        with self._lock:
            chains: Dict[str, List[CalendarVersion]] = {}
            for directory, allow_state in ((self._config_dir, False), (self._state_dir, True)):
                if not directory.exists():
                    continue
                for path in sorted(directory.glob("*.json")):
                    versions = self._load_file(path, allow_state)
                    for v in versions:
                        chains.setdefault(v.market, []).append(v)
            self._chains = {
                market: _MarketChain(market, self._build_chain(market, versions))
                for market, versions in chains.items()
            }
            logger.info(
                "日历已载入: %s",
                ", ".join(f"{m}({len(c.versions)}版)" for m, c in sorted(self._chains.items()))
                or "(空)",
            )

    def _load_file(self, path: Path, from_state: bool) -> List[CalendarVersion]:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            raise CalendarValidationError(f"日历文件 {path} 无法解析: {e}") from e

        market = str(raw.get("market", "")).upper()
        entries = raw.get("versions")
        if not market or not isinstance(entries, list) or not entries:
            raise CalendarValidationError(
                f"日历文件 {path} 缺少 market 或 versions 为空"
            )

        versions: List[CalendarVersion] = []
        for entry in entries:
            entry = dict(entry)
            entry.setdefault("market", market)
            stored_hash = entry.pop("content_hash", None)
            published_raw = entry.pop("published_at", None)
            published_at = (
                datetime.fromisoformat(published_raw) if published_raw else None
            )
            version = CalendarVersion.from_dict(entry, published_at=published_at)
            if version.market != market:
                raise CalendarValidationError(
                    f"日历文件 {path} 中版本 {version.version} 的 market 与文件声明不一致"
                )
            if stored_hash and stored_hash != version.content_hash():
                raise CalendarValidationError(
                    f"市场 {market} 版本 {version.version} 内容哈希校验失败："
                    f"文件可能被非发布流程改动（已封版数据不允许静默改写）",
                    details={
                        "market": market,
                        "version": version.version,
                        "expected_hash": stored_hash,
                        "actual_hash": version.content_hash(),
                        "file": str(path),
                    },
                )
            if version.published_at is None and from_state:
                raise CalendarValidationError(
                    f"状态目录中的版本 {market}@{version.version} 缺少 published_at"
                )
            versions.append(version)
        return versions

    def _build_chain(self, market: str, versions: List[CalendarVersion]) -> List[CalendarVersion]:
        by_id: Dict[str, CalendarVersion] = {}
        for v in versions:
            if v.version in by_id:
                if by_id[v.version].content_hash() != v.content_hash():
                    raise ImmutableVersionError(
                        f"市场 {market} 版本 {v.version} 已以不同内容存在，"
                        f"已发布版本不可修改；请发布新版本（如 {v.version}.1）",
                        details={"market": market, "version": v.version},
                    )
                # 完全相同的重复导入：幂等忽略
                continue
            by_id[v.version] = v

        # 找链头（没人把它当 parent）
        ids = set(by_id)
        parents = {v.parent_version for v in by_id.values() if v.parent_version}
        unknown = parents - ids
        if unknown:
            raise CalendarValidationError(
                f"市场 {market} 的版本链引用了不存在的父版本: {sorted(unknown)}"
            )
        heads = ids - parents
        if not heads:
            raise CalendarValidationError(f"市场 {market} 的版本链存在环")
        if len(heads) > 1:
            raise CalendarValidationError(
                f"市场 {market} 存在多条版本链头: {sorted(heads)}，每个市场只允许一条线性版本链"
            )

        head = next(iter(heads))
        # 从 head 反推
        chain_rev: List[CalendarVersion] = []
        cur: Optional[str] = head
        seen = set()
        while cur is not None:
            if cur in seen:
                raise CalendarValidationError(f"市场 {market} 的版本链存在环")
            seen.add(cur)
            chain_rev.append(by_id[cur])
            cur = by_id[cur].parent_version
        chain = list(reversed(chain_rev))
        if len(chain) != len(by_id):
            raise CalendarValidationError(
                f"市场 {market} 存在游离于版本链之外的版本"
            )
        return chain

    # ------------------------------------------------------------------ 查询

    def markets(self) -> List[str]:
        with self._lock:
            return sorted(self._chains)

    def list_versions(self, market: str) -> List[dict]:
        with self._lock:
            chain = self._require_chain(market)
            return [
                {
                    "version": v.version,
                    "parent_version": v.parent_version,
                    "description": v.description,
                    "content_hash": v.content_hash(),
                    "published_at": v.published_at.isoformat() if v.published_at else None,
                }
                for v in chain.versions
            ]

    def get_version(self, market: str, version: Optional[str] = None) -> CalendarVersion:
        with self._lock:
            chain = self._require_chain(market)
            if version is None:
                return chain.head
            for v in chain.versions:
                if v.version == version:
                    return v
            raise CalendarValidationError(
                f"市场 {market} 不存在日历版本 {version}",
                details={"market": market, "requested_version": version,
                         "available": [v.version for v in chain.versions]},
            )

    def get_view(self, market: str, version: Optional[str] = None) -> CalendarView:
        return build_view(self.get_version(market, version))

    def get_calendar(
        self, market: str, version: Optional[str] = None
    ) -> TradingCalendar:
        return TradingCalendar(self.get_view(market, version))

    def _require_chain(self, market: str) -> _MarketChain:
        key = (market or "").upper()
        if key not in self._chains:
            raise CalendarValidationError(
                f"本地未配置市场 {market} 的交易日历（已配置: {self.markets() or '无'}）"
            )
        return self._chains[key]

    # ------------------------------------------------------------------ 发布

    def publish(
        self,
        market: str,
        payload: dict,
        *,
        description: str = "",
        version: Optional[str] = None,
    ) -> CalendarVersion:
        """发布一个新版本（临时休市、补班补录的唯一合法入口）。

        相同版本号 + 相同内容：幂等返回既有版本；
        相同版本号 + 不同内容：拒绝（不可变）。
        """
        market = (market or "").upper()
        if not market:
            raise CalendarValidationError("market 不能为空")

        with self._lock:
            chain = self._chains.get(market)
            parent = chain.head.version if chain else None
            new_version = version or self._next_version_id(parent)

            entry = dict(payload)
            entry["market"] = market
            entry["version"] = new_version
            entry.setdefault("parent_version", parent)
            if description:
                entry["description"] = description
            candidate = CalendarVersion.from_dict(
                entry, published_at=datetime.utcnow()
            )
            build_view(candidate)  # 触发市态冲突校验

            if chain:
                for existing in chain.versions:
                    if existing.version == new_version:
                        if existing.content_hash() == candidate.content_hash():
                            logger.info(
                                "日历重复导入（幂等忽略）: %s@%s", market, new_version
                            )
                            return existing
                        raise ImmutableVersionError(
                            f"市场 {market} 的版本 {new_version} 已发布且内容不同，"
                            f"禁止改写已生效版本；请使用新版本号",
                            details={"market": market, "version": new_version},
                        )
                # 父版本必须是当前链头（线性追加）
                if candidate.parent_version != parent:
                    raise CalendarValidationError(
                        f"新版本必须接在当前最新版本 {parent} 之后，"
                        f"不能基于 {candidate.parent_version} 分叉"
                    )

            self._append_state(market, candidate)
            self.reload()
            logger.info("日历新版本已发布: %s@%s", market, new_version)
            return self.get_version(market, new_version)

    def _next_version_id(self, parent: Optional[str]) -> str:
        if parent is None:
            return "v1"
        # v1 -> v2；v2.1 -> v2.2
        if parent.startswith("v") and parent[1:].isdigit():
            return f"v{int(parent[1:]) + 1}"
        head = parent.rsplit(".", 1)
        if len(head) == 2 and head[-1].isdigit():
            return f"{head[0]}.{int(head[-1]) + 1}"
        return f"{parent}.1"

    def _append_state(self, market: str, version: CalendarVersion) -> None:
        self._state_dir.mkdir(parents=True, exist_ok=True)
        path = self._state_dir / f"{market}.json"
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
        else:
            data = {"market": market, "versions": []}
        entry = version.canonical_payload()
        entry.update({
            "version": version.version,
            "parent_version": version.parent_version,
            "description": version.description,
            "content_hash": version.content_hash(),
            "published_at": version.published_at.isoformat(),
        })
        data["versions"].append(entry)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(tmp, path)
