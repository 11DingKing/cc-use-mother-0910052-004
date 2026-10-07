"""交易日历与企业行动的管理接口。

所有写入均为幂等导入：重复提交相同内容返回 ``skipped_duplicate``；
修订（如临时休市变更、派息额补录）返回 ``superseded`` 并保留旧行；
查询接口始终说明“为什么这一天开市/休市、价格或数量为何调整”。
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from app.marketdata.registry import get_registry
from app.marketdata.actions import ActionType
from app.middleware.exception_handler import ValidationException

router = APIRouter(prefix="/api/market", tags=["market-data"])
registry = get_registry()


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------

class CalendarOverrideRequest(BaseModel):
    market: str = Field(default="CN", description="市场：CN/HK/US")
    day: str = Field(description="交易所本地日期 YYYY-MM-DD")
    is_open: bool = Field(description="false=休市，true=补班开市")
    reason: str = Field(min_length=1, description="原因，如：国庆节、临时休市、周末补班")
    kind: str = Field(
        default="holiday",
        description="holiday/adhoc_close/makeup 之一",
    )
    client_id: str = Field(
        default="",
        description="来源幂等键，如 exchange-notice-2024-10；缺省按日期生成",
    )
    source: str = "manual"
    created_by: str = "api"


class CalendarRevokeRequest(BaseModel):
    market: str = "CN"
    day: str
    client_id: str = ""
    reason: str = "撤销此前的日历覆盖"


class CorporateActionRequest(BaseModel):
    stock_code: str
    action_type: str = Field(
        description="cash_dividend/stock_dividend/split/consolidation"
    )
    ex_date: str = Field(description="除权除息日，交易所本地日期 YYYY-MM-DD")
    cash_per_share: float = 0.0
    share_ratio: float = Field(
        default=0.0,
        description="送股=每股送股比例(10送4→0.4)；拆股=拆分比例(1拆2→2.0)；合股=折合比例(10合1→0.1)",
    )
    title: str = ""
    currency: str = "CNY"
    client_id: str = Field(description="公告号/交易所事件ID，必填，用于幂等去重")
    source: str = "manual"
    created_by: str = "api"


class ActionRevokeRequest(BaseModel):
    stock_code: str
    action_type: str
    ex_date: str
    client_id: str
    reason: str = "撤销此前的企业行动"


class SeedRequest(BaseModel):
    path: str = Field(description="服务器本地 JSON 种子文件路径，可重复导入")


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _parse_day(value: str, field_name: str = "day") -> date:
    try:
        return date.fromisoformat(value)
    except (ValueError, TypeError):
        raise ValidationException(
            message=f"Invalid date: {value!r}",
            field=field_name,
            value=value,
            details={"expected_format": "YYYY-MM-DD"},
        )


def _parse_as_of(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        raise ValidationException(
            message=f"Invalid as_of: {value!r}",
            field="as_of",
            value=value,
            details={"expected_format": "ISO-8601 datetime, e.g. 2024-10-01T00:00:00"},
        )


# ---------------------------------------------------------------------------
# 交易日历
# ---------------------------------------------------------------------------

@router.post("/calendar/overrides")
async def import_calendar_override(request: CalendarOverrideRequest):
    """导入/修订一条交易日历覆盖（休市、临时休市、补班开市）。幂等。"""
    day = _parse_day(request.day)
    try:
        outcome = registry.import_calendar_override(
            market=request.market,
            day=day,
            is_open=request.is_open,
            reason=request.reason,
            kind=request.kind,
            source=request.source,
            client_id=request.client_id,
            created_by=request.created_by,
        )
    except ValueError as exc:
        raise ValidationException(message=str(exc), field="client_id")
    return {"success": True, "outcome": outcome}


@router.post("/calendar/revoke")
async def revoke_calendar_override(request: CalendarRevokeRequest):
    """撤销某日某来源的覆盖（追加撤销行，旧行保留可追溯）。"""
    day = _parse_day(request.day)
    try:
        outcome = registry.revoke_calendar_override(
            request.market, day, request.client_id, reason=request.reason
        )
    except ValueError as exc:
        raise ValidationException(message=str(exc), field="client_id", value=request.client_id)
    return {"success": True, "outcome": outcome}


@router.get("/calendar/status")
async def calendar_status(
    day: str = Query(description="交易所本地日期 YYYY-MM-DD"),
    market: str = Query(default="CN"),
    as_of: Optional[str] = Query(default=None, description="按历史时间点复现版本"),
):
    """查询某日是否交易，并给出原因（常规交易日/周末/节假日/临时休市/补班）。"""
    d = _parse_day(day)
    cal = registry.calendar(market=market, as_of=_parse_as_of(as_of))
    entry = cal.explain_day(d)
    nxt = cal.next_trading_day(d)
    prv = cal.prev_trading_day(d)
    return {
        "market": cal.market,
        "timezone": cal.timezone,
        "day": d.isoformat(),
        "is_trading_day": entry.is_open,
        "reason": entry.reason,
        "kind": entry.kind,
        "record_id": entry.record_id,
        "revised_record_id": entry.revised_record_id,
        "next_trading_day": nxt.isoformat() if nxt else None,
        "prev_trading_day": prv.isoformat() if prv else None,
        "calendar_version": {
            "version_id": cal.version_id,
            "fingerprint": cal.fingerprint,
        },
    }


@router.get("/calendar/trading-days")
async def calendar_trading_days(
    start: str = Query(description="YYYY-MM-DD"),
    end: str = Query(description="YYYY-MM-DD"),
    market: str = Query(default="CN"),
    as_of: Optional[str] = None,
):
    """列出闭区间内的交易日（跨休市窗口的订单/估值以此为准）。"""
    s, e = _parse_day(start, "start"), _parse_day(end, "end")
    cal = registry.calendar(market=market, as_of=_parse_as_of(as_of))
    days = cal.trading_days(s, e)
    return {
        "market": market.upper(),
        "start": s.isoformat(),
        "end": e.isoformat(),
        "count": len(days),
        "trading_days": [d.isoformat() for d in days],
        "calendar_fingerprint": cal.fingerprint,
    }


@router.get("/calendar/overrides")
async def list_calendar_overrides(
    market: str = Query(default="CN"),
    include_history: bool = Query(default=True),
):
    """列出日历覆盖记录（含修订/撤销历史，默认包含）。"""
    return {
        "market": market.upper(),
        "records": registry.list_calendar(
            market, include_history=include_history
        ),
    }


# ---------------------------------------------------------------------------
# 企业行动
# ---------------------------------------------------------------------------

@router.post("/actions")
async def import_action(request: CorporateActionRequest):
    """导入/修订一条企业行动（分红、送股、拆股、合股）。幂等。"""
    ex_date = _parse_day(request.ex_date, "ex_date")
    try:
        ActionType(request.action_type)
    except ValueError:
        raise ValidationException(
            message=f"Unknown action_type: {request.action_type}",
            field="action_type",
            value=request.action_type,
            details={"allowed": [t.value for t in ActionType]},
        )
    try:
        outcome = registry.import_action(
            stock_code=request.stock_code,
            action_type=request.action_type,
            ex_date=ex_date,
            cash_per_share=request.cash_per_share,
            share_ratio=request.share_ratio,
            title=request.title,
            currency=request.currency,
            source=request.source,
            client_id=request.client_id,
            created_by=request.created_by,
        )
    except ValueError as exc:
        raise ValidationException(message=str(exc), field="action_type")
    return {"success": True, "outcome": outcome}


@router.post("/actions/revoke")
async def revoke_action(request: ActionRevokeRequest):
    """撤销一条企业行动（追加撤销行，旧行保留）。"""
    ex_date = _parse_day(request.ex_date, "ex_date")
    try:
        outcome = registry.revoke_action(
            request.stock_code,
            request.action_type,
            ex_date,
            request.client_id,
            reason=request.reason,
        )
    except ValueError as exc:
        raise ValidationException(message=str(exc), field="client_id", value=request.client_id)
    return {"success": True, "outcome": outcome}


@router.get("/actions")
async def list_actions(
    stock_code: Optional[str] = Query(default=None),
    include_history: bool = Query(default=True),
    as_of: Optional[str] = None,
):
    """列出企业行动。

    - 不带 ``as_of``：返回审计全量记录（含修订/撤销历史）；
    - 带 ``as_of``：返回该时间点的生效版本（已撤销/之后修订的旧版不出现）。
    """
    as_of_dt = _parse_as_of(as_of)
    if as_of_dt is not None:
        return {
            "as_of": as_of,
            "records": registry.list_effective_actions(as_of_dt, stock_code),
        }
    return {"records": registry.list_actions(stock_code, include_history=include_history)}


@router.get("/actions/explain")
async def explain_action_price(
    stock_code: str = Query(...),
    ex_date: str = Query(..., description="除权日 YYYY-MM-DD"),
    prev_close: float = Query(..., description="前收盘价"),
):
    """解释除权除息日参考价为何变化，返回公式与前后价。"""
    day = _parse_day(ex_date, "ex_date")
    engine = registry.actions(stock_code)
    actions_today = engine.on_day(stock_code, day)
    if not actions_today:
        return {
            "stock_code": stock_code,
            "ex_date": ex_date,
            "adjustments": [],
            "message": "该日无生效企业行动",
        }
    explanations = []
    price = prev_close
    for action in actions_today:
        ref = engine.theoretical_ex_price(action, Decimal(str(price)))
        explanations.append(ref)
        price = ref["theoretical_price"]
    return {
        "stock_code": stock_code,
        "ex_date": ex_date,
        "prev_close": prev_close,
        "adjustments": explanations,
    }


# ---------------------------------------------------------------------------
# 版本与种子
# ---------------------------------------------------------------------------

@router.get("/pin")
async def current_pin(
    stock_code: Optional[str] = Query(default=None),
    market: str = Query(default="CN"),
    as_of: Optional[str] = None,
):
    """返回当前（或历史时间点）生效版本的指纹，供账本钉住/核对。"""
    pin = registry.pin(
        stock_code, market=market, as_of=_parse_as_of(as_of)
    )
    return {"pin": pin.to_dict()}


@router.post("/seed")
async def load_seed(request: SeedRequest):
    """从本地 JSON 文件幂等导入日历与企业行动种子数据。"""
    try:
        counts = registry.load_seed_file(request.path)
    except FileNotFoundError:
        raise ValidationException(
            message=f"Seed file not found: {request.path}",
            field="path",
            value=request.path,
        )
    except (ValueError, KeyError) as exc:
        raise ValidationException(message=f"Invalid seed file: {exc}", field="path")
    return {"success": True, "counts": counts}
