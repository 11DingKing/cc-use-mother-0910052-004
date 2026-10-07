"""业务模块说明。"""

from datetime import datetime
from decimal import Decimal
from typing import List, Optional, Dict, Any
from dataclasses import dataclass, field
import logging
import statistics

from app.chan.models import Signal, SignalType, RawCandle
from app.marketdata.actions import CorporateActionEngine, LedgerAdjustment
from app.marketdata.calendar import TradingCalendar

logger = logging.getLogger(__name__)


@dataclass
class Trade:
    """业务模块说明。"""
    entry_time: datetime
    entry_price: float
    entry_signal: SignalType
    exit_time: Optional[datetime] = None
    exit_price: Optional[float] = None
    exit_signal: Optional[SignalType] = None
    shares: float = 0.0
    profit: float = 0.0
    profit_pct: float = 0.0
    is_closed: bool = False
    # 企业行动导致的数量/成本调整留痕（送股、分红等）
    adjustments: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class BacktestConfig:
    """业务模块说明。"""
    stock_code: str
    period: str
    start_date: datetime
    end_date: datetime
    initial_capital: float = 100000.0
    position_size: float = 1.0  # 仓位比例 0-1
    commission_rate: float = 0.001  # 手续费率
    slippage: float = 0.001  # 滑点
    # 市场与数据版本：回测固定引用某个交易日历/企业行动生效版本
    market: str = "CN"
    as_of: Optional[datetime] = None


@dataclass
class BacktestResult:
    """业务模块说明。"""
    config: BacktestConfig
    trades: List[Trade] = field(default_factory=list)
    equity_curve: List[Dict[str, Any]] = field(default_factory=list)

    # 统计指标
    final_capital: float = 0.0
    total_return: float = 0.0
    annual_return: float = 0.0
    max_drawdown: float = 0.0
    win_rate: float = 0.0
    profit_loss_ratio: float = 0.0
    sharpe_ratio: float = 0.0
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0

    # 账本调整痕迹（休市顺延、分红、送股、拆合股）与数据版本钉住
    adjustments: List[Dict[str, Any]] = field(default_factory=list)
    skipped_candles: List[Dict[str, Any]] = field(default_factory=list)
    market_data_pin: Optional[Dict[str, Any]] = None
    calendar_info: Optional[Dict[str, Any]] = None
    trading_days_used: int = 0


class BacktestStatistics:
    """业务模块说明。"""
    
    @classmethod
    def calculate(
        cls,
        config: BacktestConfig,
        trades: List[Trade],
        equity_curve: List[Dict[str, Any]],
        final_capital: float,
        trading_days_used: int = 0,
    ) -> BacktestResult:
        """业务模块说明。"""
        result = BacktestResult(config=config)
        result.trades = trades
        result.equity_curve = equity_curve
        result.trading_days_used = trading_days_used
        
        result.final_capital = final_capital
        result.total_return = (final_capital - config.initial_capital) / config.initial_capital
        result.total_trades = len(trades)
        
        closed_trades = [t for t in trades if t.is_closed]
        if closed_trades:
            result.winning_trades = sum(1 for t in closed_trades if t.profit > 0)
            result.losing_trades = sum(1 for t in closed_trades if t.profit <= 0)
            result.win_rate = result.winning_trades / len(closed_trades)
            
            avg_win = sum(t.profit for t in closed_trades if t.profit > 0) / max(result.winning_trades, 1)
            avg_loss = abs(sum(t.profit for t in closed_trades if t.profit <= 0)) / max(result.losing_trades, 1)
            result.profit_loss_ratio = avg_win / avg_loss if avg_loss > 0 else 0
        
        if equity_curve:
            equities = [e["equity"] for e in equity_curve]
            result.max_drawdown = cls._calculate_max_drawdown(equities)
        
        if config.start_date and config.end_date:
            days = (config.end_date - config.start_date).days
            # 优先按实际交易日数折算年化，休市窗口不再被当作计息自然日
            if trading_days_used > 1:
                result.annual_return = (
                    (1 + result.total_return) ** (252 / (trading_days_used - 1)) - 1
                )
            elif days > 0:
                result.annual_return = (1 + result.total_return) ** (365 / days) - 1
        
        if equity_curve and len(equity_curve) > 1:
            returns = []
            for i in range(1, len(equity_curve)):
                prev = equity_curve[i-1]["equity"]
                curr = equity_curve[i]["equity"]
                if prev > 0:
                    returns.append((curr - prev) / prev)
            
            if returns:
                avg_return = statistics.mean(returns)
                std_return = statistics.stdev(returns) if len(returns) > 1 else 0
                risk_free_rate = 0.03 / 252
                if std_return > 0:
                    result.sharpe_ratio = (avg_return - risk_free_rate) / std_return * (252 ** 0.5)
        
        return result
    
    @staticmethod
    def _calculate_max_drawdown(equities: List[float]) -> float:
        """业务模块说明。"""
        if not equities:
            return 0.0
        
        max_equity = equities[0]
        max_drawdown = 0.0
        
        for equity in equities:
            if equity > max_equity:
                max_equity = equity
            drawdown = (max_equity - equity) / max_equity if max_equity > 0 else 0
            if drawdown > max_drawdown:
                max_drawdown = drawdown
        
        return max_drawdown


class BacktestEngine:
    """业务模块说明。

    可选注入 ``TradingCalendar`` 与 ``CorporateActionEngine``：注入后回测按
    交易日历过滤非交易日、休市日信号顺延到下一交易日、除权除息日调整持仓
    数量/成本与现金；不注入时保持“每根K线都是交易日”的旧口径。
    生产路径（BacktestService）始终注入同一套生效版本。
    """

    def __init__(
        self,
        calendar: Optional[TradingCalendar] = None,
        actions: Optional[CorporateActionEngine] = None,
        pin: Optional[Dict[str, Any]] = None,
    ):
        self.calendar = calendar
        self.actions = actions
        self.pin = pin
        self.position = 0.0
        self.capital = 0.0
        self.trades: List[Trade] = []
        self.equity_curve: List[Dict[str, Any]] = []
        self.all_adjustments: List[Dict[str, Any]] = []

    def run(
        self,
        config: BacktestConfig,
        candles: List[RawCandle],
        signals: List[Signal],
        calendar: Optional[TradingCalendar] = None,
        actions: Optional[CorporateActionEngine] = None,
        pin: Optional[Dict[str, Any]] = None,
    ) -> BacktestResult:
        """业务模块说明。"""
        self.calendar = calendar if calendar is not None else self.calendar
        self.actions = actions if actions is not None else self.actions
        self.pin = pin if pin is not None else self.pin
        self.position = 0.0
        self.capital = config.initial_capital
        self.trades = []
        self.equity_curve = []
        self.all_adjustments = []
        skipped: List[Dict[str, Any]] = []

        candles = sorted(candles, key=lambda x: x.timestamp)
        signals = sorted(signals, key=lambda x: x.timestamp)

        candle_by_date = {
            c.timestamp.strftime("%Y-%m-%d"): c for c in candles
        }

        # 信号 → 实际执行日：休市日信号顺延到“下一有K线的交易日”，
        # 顺延本身记录为 order_roll 调整，绝不静默改单。
        signal_map: Dict[str, Signal] = {}
        for s in signals:
            exec_date = self._resolve_signal_execution_day(s, candle_by_date)
            signal_map[exec_date.strftime("%Y-%m-%d")] = s

        logger.info(f"Backtest signals: {len(signals)}, signal_dates: {list(signal_map.keys())}")

        current_trade: Optional[Trade] = None
        trading_days_used = 0
        prev_close: Optional[float] = None

        for candle in candles:
            day = candle.timestamp.date()

            if self.calendar is not None:
                entry = self.calendar.entry_for(day)
                if not entry.is_open:
                    skipped.append({
                        "date": day.isoformat(),
                        "reason": entry.reason,
                        "kind": entry.kind,
                    })
                    continue

            trading_days_used += 1

            # 除权除息在开盘前作用于持仓
            if self.actions is not None and current_trade is not None:
                current_trade = self._apply_actions(
                    config.stock_code, day, candle, current_trade, prev_close
                )

            candle_date = candle.timestamp.strftime("%Y-%m-%d")
            signal = signal_map.get(candle_date)

            if signal:
                if signal.signal_type in (SignalType.BUY_1, SignalType.BUY_2, SignalType.BUY_3):
                    if self.position == 0:
                        current_trade = self._open_position(
                            config, candle, signal.signal_type
                        )

                elif signal.signal_type in (SignalType.SELL_1, SignalType.SELL_2, SignalType.SELL_3):
                    if self.position > 0 and current_trade:
                        self._close_position(
                            config, candle, signal.signal_type, current_trade
                        )
                        current_trade = None

            equity = self.capital + self.position * candle.close
            curve_point = {
                "timestamp": candle.timestamp.isoformat(),
                "equity": equity,
                "position": self.position,
                "price": candle.close,
            }
            if day_actions := (
                self.actions.on_day(config.stock_code, day) if self.actions else []
            ):
                curve_point["corporate_actions"] = [a.client_id for a in day_actions]
            self.equity_curve.append(curve_point)
            prev_close = candle.close

        if self.position > 0 and current_trade and candles:
            self._close_position(
                config, candles[-1], None, current_trade
            )

        result = BacktestStatistics.calculate(
            config=config,
            trades=self.trades,
            equity_curve=self.equity_curve,
            final_capital=self.capital,
            trading_days_used=trading_days_used,
        )
        result.adjustments = self.all_adjustments
        result.skipped_candles = skipped
        result.market_data_pin = self.pin
        result.calendar_info = self.calendar.describe() if self.calendar else None

        return result

    # ------------------------------------------------------------------
    # 交易日历
    # ------------------------------------------------------------------

    def _resolve_signal_execution_day(
        self, signal: Signal, candle_by_date: Dict[str, RawCandle]
    ):
        """休市日的信号顺延到下一个有行情的交易日，并留痕。"""
        day = signal.timestamp.date()
        if self.calendar is None:
            return day

        if self.calendar.is_trading_day(day):
            return day

        nxt = self.calendar.next_trading_day(day)
        target = nxt
        # 若下一交易日没有K线（数据缺口），继续找后续有K线的交易日
        if target is not None:
            for _ in range(60):
                if target.strftime("%Y-%m-%d") in candle_by_date:
                    break
                nxt2 = self.calendar.next_trading_day(target)
                if nxt2 is None:
                    target = None
                    break
                target = nxt2

        closed_entry = self.calendar.entry_for(day)
        if target is None:
            logger.warning(
                f"Signal on closed day {day} has no later trading candle, dropped"
            )
            self._record(LedgerAdjustment(
                kind="order_roll",
                stock_code=signal.stock_code,
                day=day.isoformat(),
                field_name="execution_day",
                before=day.isoformat(),
                after=None,
                reason=f"休市信号无后续交易日行情可承接：{closed_entry.reason}",
                formula="信号丢弃，不产生订单",
            ))
            return day

        self._record(LedgerAdjustment(
            kind="order_roll",
            stock_code=signal.stock_code,
            day=target.isoformat(),
            field_name="execution_day",
            before=day.isoformat(),
            after=target.isoformat(),
            reason=f"信号日 {day} 休市（{closed_entry.reason}），订单顺延",
            formula=f"{day} -> 下一交易日 {target}",
            detail={"signal_type": signal.signal_type.value},
        ))
        return target

    # ------------------------------------------------------------------
    # 企业行动
    # ------------------------------------------------------------------

    def _apply_actions(
        self, stock_code: str, day, candle: RawCandle, trade: Trade,
        prev_close: Optional[float],
    ) -> Trade:
        """对持仓应用当日全部企业行动（除权除息先于盘中交易）。"""
        for action in self.actions.on_day(stock_code, day):
            holding = self.actions.apply_to_holding(
                action,
                Decimal(str(self.position)),
                Decimal(str(trade.entry_price)),
            )
            # 记录除权参考价推导，解释行情价格为何跳变（不修改实际K线价）
            ref_base = prev_close if prev_close is not None else candle.open
            ref = self.actions.theoretical_ex_price(action, Decimal(str(ref_base)))

            self.position = float(holding.quantity_after)
            trade.shares = float(holding.quantity_after)
            trade.entry_price = float(holding.avg_cost_after)
            self.capital += float(holding.cash_delta)

            for adj in holding.adjustments:
                self._record(adj)
                trade.adjustments.append(adj.to_dict())
            self._record(LedgerAdjustment(
                kind="price_reference",
                stock_code=stock_code,
                day=day.isoformat(),
                field_name="price",
                before=ref["prev_close"],
                after=ref["theoretical_price"],
                reason=action.describe(),
                formula=ref["formula"],
                action_client_id=action.client_id,
                action_type=action.action_type.value,
                detail={"reference_only": True,
                        "note": "仅为除权参考价说明，成交仍采用行情实际价格"},
            ))
        return trade

    def _record(self, adjustment: LedgerAdjustment) -> None:
        payload = adjustment.to_dict()
        self.all_adjustments.append(payload)

    def _open_position(
        self,
        config: BacktestConfig,
        candle: RawCandle,
        signal_type: SignalType,
    ) -> Trade:
        """业务模块说明。"""
        price = candle.close * (1 + config.slippage)
        
        available = self.capital * config.position_size
        commission = available * config.commission_rate
        shares = (available - commission) / price
        
        cost = shares * price + commission
        self.capital -= cost
        self.position = shares
        
        trade = Trade(
            entry_time=candle.timestamp,
            entry_price=price,
            entry_signal=signal_type,
            shares=shares,
        )
        self.trades.append(trade)
        
        logger.debug(f"Open position: {shares:.2f} shares at {price:.2f}")
        
        return trade
    
    def _close_position(
        self,
        config: BacktestConfig,
        candle: RawCandle,
        signal_type: Optional[SignalType],
        trade: Trade,
    ) -> None:
        """业务模块说明。"""
        price = candle.close * (1 - config.slippage)
        
        revenue = self.position * price
        commission = revenue * config.commission_rate
        net_revenue = revenue - commission
        
        cost = trade.shares * trade.entry_price
        profit = net_revenue - cost
        profit_pct = profit / cost if cost > 0 else 0
        
        trade.exit_time = candle.timestamp
        trade.exit_price = price
        trade.exit_signal = signal_type
        trade.profit = profit
        trade.profit_pct = profit_pct
        trade.is_closed = True
        
        self.capital += net_revenue
        self.position = 0.0
        
        logger.debug(f"Close position: {trade.shares:.2f} shares at {price:.2f}, profit: {profit:.2f}")
