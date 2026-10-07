"""业务模块说明。"""

import json
from datetime import datetime
from typing import Optional, Dict, Any, List
import logging

from app.config import db_session_scope
from app.entities.backtest import BacktestResult as BacktestResultEntity
from app.backtest.engine import BacktestEngine, BacktestConfig, BacktestResult
from app.backtest.report import BacktestReportGenerator
from app.services.stock_service import StockService
from app.services.analysis_service import AnalysisService
from app.marketdata.registry import MarketDataPin, get_registry
from app.marketdata.seal_store import stable_hash
from app.utils.validators import validate_stock_code, validate_time_range, validate_period
from app.middleware.exception_handler import (
    AppException,
    NotFoundException,
    AnalysisException,
)

logger = logging.getLogger(__name__)


class LedgerSealedException(AppException):
    """已封账的历史账本禁止改写。"""

    def __init__(self, result_id: int):
        super().__init__(
            message=f"Backtest result {result_id} is sealed and cannot be modified",
            code="LEDGER_SEALED",
            status_code=409,
            details={"result_id": result_id, "reason": "ledger_sealed"},
            user_message=(
                f"回测结果 #{result_id} 已封账，禁止改写；"
                "如需用最新日历/企业行动重算，请发起一次新的回测"
            ),
        )


class BacktestService:
    """业务模块说明。"""

    def __init__(self, registry=None):
        self.stock_service = StockService()
        self.analysis_service = AnalysisService()
        self.registry = registry or get_registry()
        self.report_generator = BacktestReportGenerator()

    def run_backtest(
        self,
        stock_code: str,
        period: str,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        initial_capital: float = 100000.0,
        position_size: float = 1.0,
        *,
        market: Optional[str] = None,
        as_of: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """业务模块说明。"""
        stock_code = validate_stock_code(stock_code)
        period = validate_period(period)
        start_date, end_date = validate_time_range(start_date, end_date)

        # 统一生效版本：下单/回测/估值/查询都凭这个 pin 取数
        pin = self.registry.pin(stock_code, market=market, as_of=as_of)
        calendar = self.registry.calendar_as_of_pin(pin)
        actions = self.registry.actions_as_of_pin(pin, stock_code)

        # 创建回测配置
        config = BacktestConfig(
            stock_code=stock_code,
            period=period,
            start_date=start_date,
            end_date=end_date,
            initial_capital=initial_capital,
            position_size=position_size,
            market=pin.market,
            as_of=as_of,
        )

        try:
            # 获取K线数据
            candles = self.stock_service.get_candles(
                stock_code, period, start_date, end_date
            )

            if not candles:
                raise AnalysisException(
                    message="No candle data available for backtest",
                    stock_code=stock_code,
                    period=period,
                )

            # 执行分析获取信号（使用完整数据范围以获得更多信号）
            self.analysis_service.run_analysis(stock_code, period, None, None)

            # 获取信号
            with db_session_scope() as session:
                from app.mappers.analysis_mapper import AnalysisMapper
                mapper = AnalysisMapper(session)
                analysis_result = mapper.get_latest(stock_code, period)

                if not analysis_result:
                    raise AnalysisException(
                        message="No analysis result available",
                        stock_code=stock_code,
                        period=period,
                    )

                signals = mapper.load_signals(analysis_result)

            # 执行回测（固定日历/企业行动版本）
            engine = BacktestEngine(calendar, actions, pin=pin.to_dict())
            result = engine.run(config, candles, signals)

            # 保存结果（写入即封账）
            result_id = self._save_result(result, pin)

            # 生成报告
            report = self.report_generator.generate(result)
            report["id"] = result_id
            report["market_data_pin"] = pin.to_dict()
            report["sealed"] = True

            return report

        except AnalysisException:
            raise
        except Exception as e:
            logger.error(f"Backtest error: {e}", exc_info=True)
            raise AnalysisException(
                message=f"Backtest failed: {str(e)}",
                stock_code=stock_code,
                period=period,
            )

    # ------------------------------------------------------------------
    # 持久化与封账
    # ------------------------------------------------------------------

    @staticmethod
    def _ledger_payload(result: BacktestResult) -> Dict[str, Any]:
        """账本封账内容：指标 + 交易 + 曲线 + 数据版本，序列化口径稳定。"""
        return {
            "stock_code": result.config.stock_code,
            "period": result.config.period,
            "start_date": result.config.start_date.isoformat(),
            "end_date": result.config.end_date.isoformat(),
            "initial_capital": result.config.initial_capital,
            "final_capital": result.final_capital,
            "total_return": result.total_return,
            "annual_return": result.annual_return,
            "max_drawdown": result.max_drawdown,
            "win_rate": result.win_rate,
            "profit_loss_ratio": result.profit_loss_ratio,
            "sharpe_ratio": result.sharpe_ratio,
            "trades": [
                {
                    "entry_time": t.entry_time.isoformat(),
                    "entry_price": t.entry_price,
                    "entry_signal": t.entry_signal.value if t.entry_signal else None,
                    "exit_time": t.exit_time.isoformat() if t.exit_time else None,
                    "exit_price": t.exit_price,
                    "exit_signal": t.exit_signal.value if t.exit_signal else None,
                    "shares": t.shares,
                    "profit": t.profit,
                    "profit_pct": t.profit_pct,
                    "adjustments": t.adjustments,
                }
                for t in result.trades
            ],
            "equity_curve": result.equity_curve,
            "adjustments": result.adjustments,
        }

    def _save_result(self, result: BacktestResult, pin: MarketDataPin) -> int:
        """业务模块说明。"""
        try:
            payload = self._ledger_payload(result)
            ledger_hash = stable_hash(payload)
            trades_json = json.dumps(payload["trades"], ensure_ascii=False)
            equity_json = json.dumps(result.equity_curve, ensure_ascii=False)
            with db_session_scope() as session:
                entity = BacktestResultEntity(
                    stock_code=result.config.stock_code,
                    period=result.config.period,
                    start_date=result.config.start_date,
                    end_date=result.config.end_date,
                    initial_capital=result.config.initial_capital,
                    final_capital=result.final_capital,
                    total_return=result.total_return,
                    annual_return=result.annual_return,
                    max_drawdown=result.max_drawdown,
                    win_rate=result.win_rate,
                    profit_loss_ratio=result.profit_loss_ratio,
                    sharpe_ratio=result.sharpe_ratio,
                    total_trades=result.total_trades,
                    winning_trades=result.winning_trades,
                    losing_trades=result.losing_trades,
                    trades_json=trades_json,
                    equity_curve_json=equity_json,
                    market_data_pin_json=json.dumps(pin.to_dict(), ensure_ascii=False),
                    adjustments_json=json.dumps(
                        result.adjustments, ensure_ascii=False
                    ),
                    skipped_candles_json=json.dumps(
                        result.skipped_candles, ensure_ascii=False
                    ),
                    ledger_hash=ledger_hash,
                    sealed=1,
                    sealed_at=datetime.utcnow(),
                    status="completed",
                    completed_at=datetime.utcnow(),
                )
                session.add(entity)
                session.flush()
                return entity.id
        except Exception as e:
            logger.warning(f"Save backtest result error: {e}")
            return 0

    def get_result(self, result_id: int) -> Dict[str, Any]:
        """业务模块说明。"""
        with db_session_scope() as session:
            entity = session.query(BacktestResultEntity).filter(
                BacktestResultEntity.id == result_id
            ).first()

            if not entity:
                raise NotFoundException(
                    message="Backtest result not found",
                    resource_type="BacktestResult",
                    resource_id=str(result_id),
                )

            pin = (
                json.loads(entity.market_data_pin_json)
                if entity.market_data_pin_json else None
            )
            return {
                "id": entity.id,
                "summary": {
                    "stock_code": entity.stock_code,
                    "period": entity.period,
                    "start_date": entity.start_date.isoformat(),
                    "end_date": entity.end_date.isoformat(),
                    "initial_capital": entity.initial_capital,
                    "final_capital": entity.final_capital,
                },
                "performance": {
                    "total_return": entity.total_return,
                    "annual_return": entity.annual_return,
                    "max_drawdown": entity.max_drawdown,
                    "sharpe_ratio": entity.sharpe_ratio,
                    "win_rate": entity.win_rate,
                    "profit_loss_ratio": entity.profit_loss_ratio,
                    "total_trades": entity.total_trades,
                    "winning_trades": entity.winning_trades,
                    "losing_trades": entity.losing_trades,
                },
                "trades": json.loads(entity.trades_json) if entity.trades_json else [],
                "equity_curve": json.loads(entity.equity_curve_json) if entity.equity_curve_json else [],
                "adjustments": json.loads(entity.adjustments_json) if entity.adjustments_json else [],
                "skipped_candles": json.loads(entity.skipped_candles_json) if entity.skipped_candles_json else [],
                "market_data_pin": pin,
                "ledger_hash": entity.ledger_hash,
                "sealed": bool(entity.sealed),
                "sealed_at": entity.sealed_at.isoformat() if entity.sealed_at else None,
                "status": entity.status,
                "created_at": entity.created_at.isoformat(),
                "completed_at": entity.completed_at.isoformat() if entity.completed_at else None,
            }

    def list_results(
        self,
        stock_code: Optional[str] = None,
        limit: int = 20,
    ) -> list:
        """业务模块说明。"""
        with db_session_scope() as session:
            query = session.query(BacktestResultEntity)

            if stock_code:
                stock_code = validate_stock_code(stock_code)
                query = query.filter(BacktestResultEntity.stock_code == stock_code)

            results = query.order_by(
                BacktestResultEntity.created_at.desc()
            ).limit(limit).all()

            return [
                {
                    "id": r.id,
                    "stock_code": r.stock_code,
                    "period": r.period,
                    "total_return": r.total_return,
                    "win_rate": r.win_rate,
                    "status": r.status,
                    "sealed": bool(r.sealed),
                    "market_data_pin": json.loads(r.market_data_pin_json)
                    if r.market_data_pin_json else None,
                    "created_at": r.created_at.isoformat(),
                }
                for r in results
            ]

    # ------------------------------------------------------------------
    # 可复现性：按封账时 pin 重跑并比对账本哈希
    # ------------------------------------------------------------------

    def verify_reproducibility(self, result_id: int) -> Dict[str, Any]:
        """用结果封账时钉住的日历/企业行动版本重新运行，比对账本哈希。

        - 重跑按 pin.as_of 解析数据，之后补录/修订的休市与企业行动不参与；
        - 日历/行动指纹先独立校验，再比较完整账本哈希；
        - 任何不一致都会明确指出是数据版本变了还是账本被改动。
        """
        stored = self.get_result(result_id)
        pin_data = stored.get("market_data_pin")
        if not pin_data:
            raise AnalysisException(
                message=f"Result {result_id} predates market-data pinning and cannot be replayed",
                details={"result_id": result_id},
            )
        pin = MarketDataPin.from_dict(pin_data)
        as_of = datetime.fromisoformat(pin.as_of) if pin.as_of else None

        version_check = self.registry.verify_pin_for_stock(pin, stored["summary"]["stock_code"])

        # 按封账版本重跑
        report = self.run_backtest(
            stored["summary"]["stock_code"],
            stored["summary"]["period"],
            datetime.fromisoformat(stored["summary"]["start_date"]),
            datetime.fromisoformat(stored["summary"]["end_date"]),
            initial_capital=stored["summary"]["initial_capital"],
            market=pin.market,
            as_of=as_of,
        )

        recomputed_entity_id = report["id"]
        with db_session_scope() as session:
            new_entity = session.query(BacktestResultEntity).filter(
                BacktestResultEntity.id == recomputed_entity_id
            ).first()
            recomputed_hash = new_entity.ledger_hash if new_entity else None

        return {
            "result_id": result_id,
            "replayed_result_id": recomputed_entity_id,
            "stored_ledger_hash": stored["ledger_hash"],
            "recomputed_ledger_hash": recomputed_hash,
            "ledger_matches": stored["ledger_hash"] == recomputed_hash,
            "data_version_check": version_check,
            "pin": pin_data,
            "note": (
                "按封账时 as_of 时间点复现；补录的休市/企业行动不影响历史账本"
                if pin.as_of
                else "封账时未固定 as_of，按当前生效版本复现"
            ),
        }
