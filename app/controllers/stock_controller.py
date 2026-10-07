"""业务模块说明。"""

from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Query

from app.services.stock_service import StockService
from app.marketdata.provider import get_provider, resolve_market

router = APIRouter(prefix="/api/stocks", tags=["stocks"])
stock_service = StockService()
_market = get_provider()


@router.get("/{code}/candles")
async def get_candles(
    code: str,
    period: str = Query(default="daily", description="K线周期: daily, 60min, 30min"),
    start_date: Optional[str] = Query(default=None, description="开始日期 YYYY-MM-DD"),
    end_date: Optional[str] = Query(default=None, description="结束日期 YYYY-MM-DD"),
):
    """历史K线查询。

    日线结果附带每根K线的市场本地交易日、是否为开市日及依据的日历版本，
    使历史查询与下单/回测/日终估值使用同一套生效版本；落在休市日的数据会被
    显式标注（``is_trading_day=false`` 与休市原因），不做静默处理。
    """
    start = datetime.strptime(start_date, "%Y-%m-%d") if start_date else None
    end = datetime.strptime(end_date, "%Y-%m-%d") if end_date else None

    candles = stock_service.get_candles(code, period, start, end)

    market = resolve_market(code)
    calendar = _market.calendar_for(market)
    items = []
    non_sessions = []
    for c in candles:
        day = calendar.local_date(c.timestamp)
        is_session = calendar.is_trading_day(day)
        item = {
            "timestamp": c.timestamp.isoformat(),
            "trading_day": day.isoformat(),
            "is_trading_day": is_session,
            "open": c.open,
            "high": c.high,
            "low": c.low,
            "close": c.close,
            "volume": c.volume,
        }
        if not is_session:
            reason = calendar.reason_for_day(day) or "非交易日"
            item["non_session_reason"] = reason
            non_sessions.append({"trading_day": day.isoformat(), "reason": reason})
        items.append(item)

    return {
        "stock_code": code,
        "period": period,
        "count": len(candles),
        "candles": items,
        "calendar": _market.calendar_manifest(market),
        "non_trading_sessions": non_sessions,
    }


@router.post("/{code}/fetch")
async def fetch_candles(
    code: str,
    period: str = Query(default="daily", description="K线周期"),
    start_date: Optional[str] = Query(default=None, description="开始日期"),
    end_date: Optional[str] = Query(default=None, description="结束日期"),
):
    """业务模块说明。"""
    start = datetime.strptime(start_date, "%Y-%m-%d") if start_date else None
    end = datetime.strptime(end_date, "%Y-%m-%d") if end_date else None
    
    result = stock_service.fetch_and_update(code, period, start, end)
    return result
