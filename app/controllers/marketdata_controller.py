"""交易日历与企业行动管理接口。

写入接口均为“发布新版本/新批次”，不提供任何修改已发布数据的入口；
查询接口返回所依据的版本号与内容哈希，便于审计与复现。
"""

from typing import List, Optional

from fastapi import APIRouter, Query
from pydantic import BaseModel

from app.services.marketdata_service import MarketDataService

router = APIRouter(prefix="/api/market-data", tags=["market-data"])
service = MarketDataService()


# --------------------------------------------------------------------------- 请求模型

class SpecialSessionRequest(BaseModel):
    market: str
    day: str                          # YYYY-MM-DD（市场本地日历日）
    session_type: str                 # closed / makeup / half
    name: str
    reason: str = ""
    close_time: Optional[str] = None  # half 时的收市时间 HH:MM
    description: str = ""


class CalendarVersionRequest(BaseModel):
    market: str
    version: Optional[str] = None
    description: str = ""
    tzname: str = "Asia/Shanghai"
    weekend: List[int] = [5, 6]
    holidays: List[dict] = []
    special_sessions: List[dict] = []


class CorporateActionItem(BaseModel):
    stock_code: str
    ex_date: str
    action_type: str          # cash_dividend / stock_dividend / split
    value: str                # 用字符串承载精确十进制
    currency: str = "CNY"
    name: str = ""
    record_date: Optional[str] = None
    pay_date: Optional[str] = None


class ActionBatchRequest(BaseModel):
    batch_id: str
    source: str = "manual"
    description: str = ""
    actions: List[CorporateActionItem]


# --------------------------------------------------------------------------- 日历查询

@router.get("/calendar/markets")
async def list_markets():
    """列出已配置市场及其版本链。"""
    return {"markets": service.list_markets()}


@router.get("/calendar/{market}/versions")
async def list_versions(market: str):
    """列出某市场全部日历版本（含内容哈希、发布时间）。"""
    return {"market": market.upper(), "versions": service.list_versions(market)}


@router.get("/calendar/{market}/day/{day}")
async def trading_day_info(market: str, day: str):
    """查询某日是否开市、休市原因及顺延后的交易日。"""
    return service.trading_day_info(market, day)


@router.get("/calendar/{market}/next")
async def next_trading_day(
    market: str,
    day: str = Query(..., description="基准日 YYYY-MM-DD"),
    count: int = Query(default=1, ge=1, le=500),
):
    """返回基准日之后的第 N 个交易日。"""
    return service.next_trading_day(market, day, count)


@router.get("/calendar/{market}/trading-days")
async def trading_days(
    market: str,
    start: str = Query(..., description="开始日 YYYY-MM-DD"),
    end: str = Query(..., description="结束日 YYYY-MM-DD"),
):
    """枚举区间内交易日（日终估值/历史查询按此对齐）。"""
    return service.trading_days(market, start, end)


# --------------------------------------------------------------------------- 日历发布

@router.post("/calendar/special-session")
async def publish_special_session(request: SpecialSessionRequest):
    """补录临时休市/补班/半日市：以新版本追加，不改写历史版本。"""
    return service.publish_special_session(
        market=request.market,
        day_str=request.day,
        session_type=request.session_type,
        name=request.name,
        reason=request.reason,
        close_time=request.close_time,
        description=request.description,
    )


@router.post("/calendar/versions")
async def publish_calendar_version(request: CalendarVersionRequest):
    """发布完整日历新版本（内容自包含，parent 自动指向当前链头）。"""
    payload = {
        "tzname": request.tzname,
        "weekend": request.weekend,
        "holidays": request.holidays,
        "special_sessions": request.special_sessions,
    }
    return service.publish_calendar(
        request.market, payload,
        description=request.description, version=request.version,
    )


# ------------------------------------------------------------------------- 企业行动

@router.get("/actions")
async def list_actions(stock_code: Optional[str] = Query(default=None)):
    """列出生效企业行动（可按股票过滤）。"""
    return {"actions": service.list_actions(stock_code)}


@router.get("/actions/batches")
async def list_batches():
    """列出已发布企业行动批次（不可变、含哈希）。"""
    return {"batches": service.list_batches()}


@router.post("/actions/batches")
async def publish_actions(request: ActionBatchRequest):
    """发布企业行动批次：重复导入幂等，冲突数值拒绝。"""
    return service.publish_actions(
        batch_id=request.batch_id,
        actions=[a.model_dump() for a in request.actions],
        source=request.source,
        description=request.description,
    )


@router.get("/manifest")
async def manifest():
    """当前生效数据集指纹（全部日历链头 + 企业行动批次）。"""
    return service.manifest()
