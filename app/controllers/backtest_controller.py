"""业务模块说明。"""

from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Query
from pydantic import BaseModel

from app.services.backtest_service import BacktestService

router = APIRouter(prefix="/api/backtest", tags=["backtest"])
backtest_service = BacktestService()


class RunBacktestRequest(BaseModel):
    """业务模块说明。"""
    stock_code: str
    period: str = "daily"
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    initial_capital: float = 100000.0
    position_size: float = 1.0
    market: str = "CN"
    # 钉住历史生效版本（ISO-8601）；缺省使用当前版本。
    # 重跑历史账本时传封账 pin 中的 as_of，可复现原账本。
    as_of: Optional[str] = None


@router.post("/run")
async def run_backtest(request: RunBacktestRequest):
    """运行业务模块说明。

    响应包含 ``market_data_pin``（本次使用的日历/企业行动版本指纹）、
    ``adjustments``（休市顺延、除权、分红等每一笔调整的原因与公式）、
    ``skipped_candles``（因休市跳过的行情日）与封账标记。
    """
    start = datetime.strptime(request.start_date, "%Y-%m-%d") if request.start_date else None
    end = datetime.strptime(request.end_date, "%Y-%m-%d") if request.end_date else None
    as_of = datetime.fromisoformat(request.as_of) if request.as_of else None

    return backtest_service.run_backtest(
        request.stock_code,
        request.period,
        start,
        end,
        request.initial_capital,
        request.position_size,
        market=request.market,
        as_of=as_of,
    )


@router.get("/{result_id}/report")
async def get_report(result_id: int):
    """业务模块说明。"""
    return backtest_service.get_result(result_id)


@router.get("/{result_id}/verify")
async def verify_reproducibility(result_id: int):
    """按封账时钉住的数据版本重跑并比对账本哈希，验证可复现性。"""
    return backtest_service.verify_reproducibility(result_id)


@router.get("/list")
async def list_results(
    stock_code: Optional[str] = Query(default=None, description="股票代码"),
    limit: int = Query(default=20, description="返回数量"),
):
    """业务模块说明。"""
    results = backtest_service.list_results(stock_code, limit)
    return {
        "count": len(results),
        "results": results,
    }
