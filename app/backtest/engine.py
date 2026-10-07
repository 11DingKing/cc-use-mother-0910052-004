"""业务模块说明。"""

from datetime import datetime
from typing import List, Optional, Dict, Any
from dataclasses import dataclass, field
import logging
import statistics

from app.chan.models import Signal, SignalType, RawCandle
from app.marketdata.context import MarketContext

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
    # 持仓期间发生的除权除息调整说明（解释数量/成本为何变化）
    adjustments: List[Dict[str, Any]] = field(default_factory=list)
    # 持仓期间收到的现金分红合计
    cash_dividend: float = 0.0
    # 剩余持仓成本总额（送转不变、现金分红等额下调）
    cost_basis: float = 0.0
    # 建仓时总成本（用于收益率口径）
    original_cost: float = 0.0


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
    # 固定的市场数据生效版本（日历 + 企业行动）；None 时行为同旧版（每日皆交易日）
    market_context: Optional[MarketContext] = None


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

    # 市场数据生效版本与调整审计（可复现账本）
    market_manifest: Dict[str, Any] = field(default_factory=dict)
    adjustments: List[Dict[str, Any]] = field(default_factory=list)
    data_notes: List[Dict[str, Any]] = field(default_factory=list)
    input_fingerprint: str = ""
    trading_days: int = 0


class BacktestStatistics:
    """业务模块说明。"""
    
    @classmethod
    def calculate(
        cls,
        config: BacktestConfig,
        trades: List[Trade],
        equity_curve: List[Dict[str, Any]],
        final_capital: float,
    ) -> BacktestResult:
        """业务模块说明。"""
        result = BacktestResult(config=config)
        result.trades = trades
        result.equity_curve = equity_curve
        
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
            # 优先按交易日数（每年252个交易日）折算，避免把跨节假日/休市的
            # 自然日计入年化；无收益曲线时退回自然日口径。
            trading_days = len(equity_curve)
            if trading_days > 1:
                result.annual_return = (1 + result.total_return) ** (252 / trading_days) - 1
            else:
                days = (config.end_date - config.start_date).days
                if days > 0:
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
    """业务模块说明。"""

    def __init__(self):
        self.position = 0.0
        self.capital = 0.0
        self.trades: List[Trade] = []
        self.equity_curve: List[Dict[str, Any]] = []
        self.cost_basis = 0.0       # 当前持仓剩余成本总额
        self.original_cost = 0.0    # 建仓总成本
        self.adjustments: List[Dict[str, Any]] = []
        self.data_notes: List[Dict[str, Any]] = []

    def run(
        self,
        config: BacktestConfig,
        candles: List[RawCandle],
        signals: List[Signal],
    ) -> BacktestResult:
        """业务模块说明。"""
        self.position = 0.0
        self.capital = config.initial_capital
        self.trades = []
        self.equity_curve = []
        self.cost_basis = 0.0
        self.original_cost = 0.0
        self.adjustments = []
        self.data_notes = []

        ctx = config.market_context or MarketContext.build(config.stock_code)
        calendar = ctx.calendar

        candles = sorted(candles, key=lambda x: x.timestamp)
        signals = sorted(signals, key=lambda x: x.timestamp)

        # 1) 仅保留交易日K线；落在休市日的K线被剔除并记录，绝不静默忽略
        session_candles: List[RawCandle] = []
        for c in candles:
            day = calendar.local_date(c.timestamp)
            if calendar.is_trading_day(day):
                session_candles.append(c)
            else:
                self.data_notes.append({
                    "type": "non_trading_candle",
                    "timestamp": c.timestamp.isoformat(),
                    "reason": calendar.reason_for_day(day) or "非交易日",
                })
        if candles and not session_candles:
            self.data_notes.append({
                "type": "no_trading_sessions",
                "reason": "请求区间内没有任何交易日",
            })

        # 2) 信号日归一化：休市日信号顺延到下一交易日，并记录调整原因
        signal_map: Dict[str, Signal] = {}
        for s in signals:
            resolution = calendar.resolve_moment(s.timestamp)
            key = resolution.trading_day.isoformat()
            if resolution.shifted:
                self.data_notes.append({
                    "type": "signal_shift",
                    "signal_type": s.signal_type.value,
                    "requested_day": resolution.requested_day.isoformat(),
                    "trading_day": resolution.trading_day.isoformat(),
                    "reason": resolution.reason,
                    "event": resolution.event_name,
                })
            # 同一交易日多个信号时保留最强的一个，保证确定性
            if key not in signal_map or s.strength > signal_map[key].strength:
                signal_map[key] = s

        logger.info(
            "Backtest signals: %s, calendar: %s@%s, actions: %d",
            len(signal_map), calendar.market, calendar.version, len(ctx.actions),
        )

        current_trade: Optional[Trade] = None

        for candle in session_candles:
            day = calendar.local_date(candle.timestamp)
            day_key = day.isoformat()

            # 3) 除权除息在除权日开盘前作用于持仓
            if self.position > 0 and current_trade is not None:
                day_actions = ctx.actions_on(config.stock_code, day)
                if day_actions:
                    current_trade = self._apply_corporate_actions(
                        day_actions, current_trade, day_key
                    )

            signal = signal_map.get(day_key)
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
                "trading_day": day_key,
                "equity": equity,
                "cash": self.capital,
                "position": self.position,
                "price": candle.close,
            }
            self.equity_curve.append(curve_point)

        if self.position > 0 and current_trade and session_candles:
            self._close_position(
                config, session_candles[-1], None, current_trade
            )

        result = BacktestStatistics.calculate(
            config=config,
            trades=self.trades,
            equity_curve=self.equity_curve,
            final_capital=self.capital,
        )

        # 4) 记录生效版本与账本指纹，供重跑校验与复现
        result.market_manifest = ctx.manifest
        result.adjustments = self.adjustments
        result.data_notes = self.data_notes
        result.trading_days = len(session_candles)
        result.input_fingerprint = self._fingerprint(config, session_candles, signal_map, ctx)
        return result

    def _apply_corporate_actions(self, day_actions, trade: Trade, day_key: str) -> Trade:
        """除权除息日对在仓持仓应用调整，并记录审计明细。"""
        from app.marketdata.context import apply_actions

        qty, cost, cash, notes = apply_actions(
            day_actions, self.position, self._avg_cost()
        )
        self.position = float(qty)
        self.cost_basis = float(cost) * float(qty)
        self.capital += float(cash)
        trade.shares = float(qty)
        trade.cash_dividend += float(cash)
        for applied in notes:
            detail = applied.to_dict()
            detail["applied_on"] = day_key
            trade.adjustments.append(detail)
            self.adjustments.append(detail)
        return trade

    def _avg_cost(self) -> float:
        return self.cost_basis / self.position if self.position > 0 else 0.0

    @staticmethod
    def _fingerprint(
        config: BacktestConfig,
        candles: List[RawCandle],
        signal_map: Dict[str, Any],
        ctx: MarketContext,
    ) -> str:
        """账本输入指纹：配置 + 交易日序列 + 信号 + 生效版本。"""
        from app.marketdata.context import fingerprint_payload

        payload = {
            "stock_code": config.stock_code,
            "period": config.period,
            "start_date": config.start_date.isoformat(),
            "end_date": config.end_date.isoformat(),
            "initial_capital": config.initial_capital,
            "position_size": config.position_size,
            "commission_rate": config.commission_rate,
            "slippage": config.slippage,
            "sessions": [
                [c.timestamp.isoformat(), c.open, c.high, c.low, c.close, c.volume]
                for c in candles
            ],
            "signals": {
                k: [v.signal_type.value, v.strength] for k, v in signal_map.items()
            },
            "manifest": ctx.manifest,
        }
        return fingerprint_payload(payload)

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
        self.cost_basis = shares * price
        self.original_cost = shares * price

        trade = Trade(
            entry_time=candle.timestamp,
            entry_price=price,
            entry_signal=signal_type,
            shares=shares,
            cost_basis=shares * price,
            original_cost=shares * price,
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

        # 盈亏对照原始建仓成本；送转/拆股不改总成本，现金分红作为单独现金收益，
        # 避免“成本下调 + 现金入账”双重计算分红。
        cost = trade.original_cost
        profit = net_revenue - cost + trade.cash_dividend
        profit_pct = profit / trade.original_cost if trade.original_cost > 0 else 0

        trade.exit_time = candle.timestamp
        trade.exit_price = price
        trade.exit_signal = signal_type
        trade.profit = profit
        trade.profit_pct = profit_pct
        trade.is_closed = True

        self.capital += net_revenue
        self.position = 0.0
        self.cost_basis = 0.0
        self.original_cost = 0.0

        logger.debug(f"Close position: {trade.shares:.2f} shares at {price:.2f}, profit: {profit:.2f}")
