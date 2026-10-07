"""交易日历与企业行动的持久化实体。

两张表均为“只追加”（append-only）：补录、修订、撤销都新增一行并通过
``supersedes_id`` 指向旧行，旧行永不更新或删除。任何时点的生效版本都可以
用 ``created_at <= as_of`` 的行集合重新还原。
"""

from datetime import datetime

from sqlalchemy import (
    Column,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Index,
)
from sqlalchemy.orm import declarative_base

MarketDataBase = declarative_base()


class CalendarOverrideRecord(MarketDataBase):
    """交易日历覆盖行（休市/补班/临时休市/撤销）。"""

    __tablename__ = "market_calendar_overrides"

    id = Column(Integer, primary_key=True, autoincrement=True)
    market = Column(String(8), nullable=False)           # CN / HK / US
    day = Column(String(10), nullable=False)             # 交易所本地日期 YYYY-MM-DD
    is_open = Column(Integer, nullable=False)            # 1=开市(补班) 0=休市
    reason = Column(String(200), nullable=False, default="")
    kind = Column(String(20), nullable=False, default="holiday")
    source = Column(String(50), nullable=False, default="manual")
    client_id = Column(String(80), nullable=False, default="")
    supersedes_id = Column(Integer, nullable=True)
    revoked = Column(Integer, nullable=False, default=0)  # 1=该行是撤销标记
    created_by = Column(String(50), nullable=False, default="system")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    # 注意：不设 (market, day, client_id) 唯一约束——修订必须以新行追加，
    # 幂等性（相同内容跳过）由应用层 upsert_override 保证。
    __table_args__ = (
        Index("ix_calendar_market_day_client", "market", "day", "client_id"),
        Index("ix_calendar_market_created", "market", "created_at"),
    )


class CorporateActionRecord(MarketDataBase):
    """企业行动行（分红/送股/拆股/合股），修订时追加新行。"""

    __tablename__ = "corporate_actions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    stock_code = Column(String(20), nullable=False)
    action_type = Column(String(20), nullable=False)
    ex_date = Column(String(10), nullable=False)
    cash_per_share = Column(Float, nullable=False, default=0.0)
    share_ratio = Column(Float, nullable=False, default=0.0)
    title = Column(String(200), nullable=False, default="")
    currency = Column(String(8), nullable=False, default="CNY")
    client_id = Column(String(80), nullable=False)
    source = Column(String(50), nullable=False, default="manual")
    supersedes_id = Column(Integer, nullable=True)
    revoked = Column(Integer, nullable=False, default=0)
    created_by = Column(String(50), nullable=False, default="system")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    # 注意：不设业务键唯一约束——参数修正必须以新行追加（supersedes_id
    # 指向旧行），幂等性由应用层 add_action 保证。
    __table_args__ = (
        Index(
            "ix_action_stock_date_type_client",
            "stock_code", "ex_date", "action_type", "client_id",
        ),
        Index("ix_action_stock_exdate", "stock_code", "ex_date"),
    )


class LedgerSealRecord(MarketDataBase):
    """已封账账本：封账后任何改写请求都会被拒绝。"""

    __tablename__ = "ledger_seals"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ledger_type = Column(String(20), nullable=False)       # backtest / account
    ref_id = Column(String(64), nullable=False)
    ledger_hash = Column(String(64), nullable=False)
    calendar_pin_json = Column(Text, nullable=True)
    actions_pin_json = Column(Text, nullable=True)
    detail_json = Column(Text, nullable=True)
    sealed_by = Column(String(50), nullable=False, default="system")
    sealed_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("ledger_type", "ref_id", name="uix_seal_ledger_ref"),
    )
