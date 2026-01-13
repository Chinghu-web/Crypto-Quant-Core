# core/backtester.py - 回测系统 v1.0
# 用途：基于历史信号数据进行策略回测，计算绩效指标

import sqlite3
import json
import math
import ccxt
from typing import Dict, List, Optional, Tuple, Any
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
from pathlib import Path
import numpy as np


@dataclass
class Trade:
    """单笔交易记录"""
    symbol: str
    side: str  # long/short
    entry_price: float
    entry_time: datetime
    exit_price: float = 0.0
    exit_time: datetime = None
    tp_price: float = 0.0
    sl_price: float = 0.0
    pnl_pct: float = 0.0
    exit_reason: str = ""
    holding_minutes: int = 0
    leverage: int = 1
    
    @property
    def is_win(self) -> bool:
        return self.pnl_pct > 0
    
    @property
    def gross_pnl_pct(self) -> float:
        """含杠杆的盈亏"""
        return self.pnl_pct * self.leverage


@dataclass
class BacktestResult:
    """回测结果"""
    # 基础统计
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    
    # 收益指标
    total_pnl_pct: float = 0.0
    avg_pnl_pct: float = 0.0
    avg_win_pct: float = 0.0
    avg_loss_pct: float = 0.0
    
    # 风险指标
    win_rate: float = 0.0
    profit_loss_ratio: float = 0.0
    sharpe_ratio: float = 0.0
    max_drawdown_pct: float = 0.0
    max_consecutive_losses: int = 0
    
    # 时间指标
    avg_holding_minutes: float = 0.0
    total_days: int = 0
    
    # 详细数据
    trades: List[Trade] = field(default_factory=list)
    equity_curve: List[float] = field(default_factory=list)
    daily_returns: List[float] = field(default_factory=list)
    
    def to_dict(self) -> Dict:
        return {
            "total_trades": self.total_trades,
            "winning_trades": self.winning_trades,
            "losing_trades": self.losing_trades,
            "win_rate": round(self.win_rate * 100, 2),
            "total_pnl_pct": round(self.total_pnl_pct, 2),
            "avg_pnl_pct": round(self.avg_pnl_pct, 2),
            "avg_win_pct": round(self.avg_win_pct, 2),
            "avg_loss_pct": round(self.avg_loss_pct, 2),
            "profit_loss_ratio": round(self.profit_loss_ratio, 2),
            "sharpe_ratio": round(self.sharpe_ratio, 2),
            "max_drawdown_pct": round(self.max_drawdown_pct, 2),
            "max_consecutive_losses": self.max_consecutive_losses,
            "avg_holding_minutes": round(self.avg_holding_minutes, 1),
            "total_days": self.total_days,
        }
    
    def print_report(self):
        """打印回测报告"""
        print("\n" + "=" * 60)
        print("📊 回测报告")
        print("=" * 60)
        
        print(f"\n📈 交易统计:")
        print(f"  总交易数: {self.total_trades}")
        print(f"  盈利交易: {self.winning_trades}")
        print(f"  亏损交易: {self.losing_trades}")
        print(f"  胜率: {self.win_rate * 100:.1f}%")
        
        print(f"\n💰 收益指标:")
        print(f"  总盈亏: {self.total_pnl_pct:+.2f}%")
        print(f"  平均盈亏: {self.avg_pnl_pct:+.2f}%")
        print(f"  平均盈利: {self.avg_win_pct:+.2f}%")
        print(f"  平均亏损: {self.avg_loss_pct:+.2f}%")
        print(f"  盈亏比: {self.profit_loss_ratio:.2f}")
        
        print(f"\n📉 风险指标:")
        print(f"  Sharpe Ratio: {self.sharpe_ratio:.2f}")
        print(f"  最大回撤: {self.max_drawdown_pct:.2f}%")
        print(f"  最大连亏: {self.max_consecutive_losses}笔")
        
        print(f"\n⏱️ 时间指标:")
        print(f"  平均持仓: {self.avg_holding_minutes:.0f}分钟")
        print(f"  回测天数: {self.total_days}天")
        
        # 评级
        print(f"\n🏆 策略评级:")
        if self.sharpe_ratio >= 2.0 and self.win_rate >= 0.5 and self.max_drawdown_pct <= 10:
            print("  ⭐⭐⭐⭐⭐ 优秀")
        elif self.sharpe_ratio >= 1.5 and self.win_rate >= 0.45:
            print("  ⭐⭐⭐⭐ 良好")
        elif self.sharpe_ratio >= 1.0 and self.win_rate >= 0.40:
            print("  ⭐⭐⭐ 中等")
        elif self.sharpe_ratio >= 0.5:
            print("  ⭐⭐ 较差")
        else:
            print("  ⭐ 需要优化")
        
        print("=" * 60)


class Backtester:
    """
    回测引擎
    
    功能：
    1. 从数据库读取历史信号
    2. 从币安获取历史K线验证止盈止损
    3. 计算完整的绩效指标
    
    使用方式：
    ```python
    backtester = Backtester(config)
    
    # 回测最近30天的信号
    result = backtester.run(days=30)
    result.print_report()
    ```
    """
    
    def __init__(self, config: Dict[str, Any]):
        """
        初始化回测引擎
        
        Args:
            config: 配置字典
        """
        self.config = config
        
        # 数据库路径
        self.db_path = config.get("analytics", {}).get("storage", {}).get("path", "./signals.db")
        
        # 回测配置
        bt_cfg = config.get("backtest", {})
        self.default_days = bt_cfg.get("default_days", 30)
        self.commission_rate = bt_cfg.get("commission_rate", 0.0004)
        self.slippage_rate = bt_cfg.get("slippage_rate", 0.001)
        self.initial_capital = bt_cfg.get("initial_capital", 10000)
        
        # 交易所实例
        self.exchange = ccxt.binance({
            'enableRateLimit': True,
            'options': {'defaultType': 'future'}
        })
        
        # K线缓存
        self._kline_cache: Dict[str, List] = {}
        
        print(f"[BACKTEST] 初始化完成 | DB: {self.db_path}")
    
    def run(
        self,
        days: int = None,
        start_date: datetime = None,
        end_date: datetime = None,
        symbols: List[str] = None,
        min_score: float = 0.0,
    ) -> BacktestResult:
        """
        运行回测
        
        Args:
            days: 回测天数（从今天往前）
            start_date: 开始日期
            end_date: 结束日期
            symbols: 筛选币种列表
            min_score: 最小评分筛选
            
        Returns:
            BacktestResult对象
        """
        # 确定时间范围
        if end_date is None:
            end_date = datetime.now(timezone.utc)
        
        if start_date is None:
            if days is None:
                days = self.default_days
            start_date = end_date - timedelta(days=days)
        
        print(f"[BACKTEST] 回测区间: {start_date.date()} ~ {end_date.date()}")
        
        # 加载历史信号
        signals = self._load_signals(start_date, end_date, symbols, min_score)
        print(f"[BACKTEST] 加载信号数: {len(signals)}")
        
        if not signals:
            print("[BACKTEST] 没有找到符合条件的信号")
            return BacktestResult()
        
        # 模拟交易
        trades = self._simulate_trades(signals)
        print(f"[BACKTEST] 模拟交易数: {len(trades)}")
        
        # 计算绩效指标
        result = self._calculate_metrics(trades, start_date, end_date)
        
        return result
    
    def _load_signals(
        self,
        start_date: datetime,
        end_date: datetime,
        symbols: List[str] = None,
        min_score: float = 0.0,
    ) -> List[Dict]:
        """从数据库加载历史信号"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # 检查表结构
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [t[0] for t in cursor.fetchall()]
        
        signals = []
        
        # 尝试从不同的表加载
        if "pushed_signals" in tables:
            signals.extend(self._load_from_pushed_signals(
                cursor, start_date, end_date, symbols, min_score
            ))
        
        if "watch_signals" in tables:
            signals.extend(self._load_from_watch_signals(
                cursor, start_date, end_date, symbols, min_score
            ))
        
        conn.close()
        
        # 按时间排序
        signals.sort(key=lambda x: x.get("signal_time", ""))
        
        return signals
    
    def _load_from_pushed_signals(
        self,
        cursor,
        start_date: datetime,
        end_date: datetime,
        symbols: List[str] = None,
        min_score: float = 0.0,
    ) -> List[Dict]:
        """从pushed_signals表加载"""
        query = """
        SELECT symbol, side, score, entry_price, tp_price, sl_price, 
               leverage, created_at, payload
        FROM pushed_signals
        WHERE created_at BETWEEN ? AND ?
          AND score >= ?
        """
        params = [start_date.isoformat(), end_date.isoformat(), min_score]
        
        if symbols:
            placeholders = ",".join(["?" for _ in symbols])
            query += f" AND symbol IN ({placeholders})"
            params.extend(symbols)
        
        query += " ORDER BY created_at ASC"
        
        try:
            cursor.execute(query, params)
            rows = cursor.fetchall()
        except sqlite3.OperationalError as e:
            print(f"[BACKTEST] pushed_signals查询失败: {e}")
            return []
        
        signals = []
        for row in rows:
            symbol, side, score, entry_price, tp_price, sl_price, leverage, created_at, payload_json = row
            
            # 解析payload
            try:
                payload = json.loads(payload_json) if payload_json else {}
            except:
                payload = {}
            
            # 如果没有止盈止损，从payload中获取
            if not tp_price and payload:
                tp_price = payload.get("calculated_stops", {}).get("tp_price", 0)
            if not sl_price and payload:
                sl_price = payload.get("calculated_stops", {}).get("sl_price", 0)
            
            signals.append({
                "symbol": symbol,
                "side": side or "long",
                "score": float(score or 0),
                "entry_price": float(entry_price or 0),
                "tp_price": float(tp_price or 0),
                "sl_price": float(sl_price or 0),
                "leverage": int(leverage or 5),
                "signal_time": created_at,
                "source": "pushed_signals",
            })
        
        return signals
    
    def _load_from_watch_signals(
        self,
        cursor,
        start_date: datetime,
        end_date: datetime,
        symbols: List[str] = None,
        min_score: float = 0.0,
    ) -> List[Dict]:
        """从watch_signals表加载"""
        query = """
        SELECT symbol, side, score, entry_price, tp_price, sl_price,
               leverage, created_at, status
        FROM watch_signals
        WHERE created_at BETWEEN ? AND ?
          AND score >= ?
          AND status IN ('executed', 'filled', 'completed')
        """
        params = [start_date.isoformat(), end_date.isoformat(), min_score]
        
        if symbols:
            placeholders = ",".join(["?" for _ in symbols])
            query += f" AND symbol IN ({placeholders})"
            params.extend(symbols)
        
        query += " ORDER BY created_at ASC"
        
        try:
            cursor.execute(query, params)
            rows = cursor.fetchall()
        except sqlite3.OperationalError as e:
            print(f"[BACKTEST] watch_signals查询失败: {e}")
            return []
        
        signals = []
        for row in rows:
            symbol, side, score, entry_price, tp_price, sl_price, leverage, created_at, status = row
            
            signals.append({
                "symbol": symbol,
                "side": side or "long",
                "score": float(score or 0),
                "entry_price": float(entry_price or 0),
                "tp_price": float(tp_price or 0),
                "sl_price": float(sl_price or 0),
                "leverage": int(leverage or 5),
                "signal_time": created_at,
                "source": "watch_signals",
            })
        
        return signals
    
    def _simulate_trades(self, signals: List[Dict]) -> List[Trade]:
        """模拟交易执行"""
        trades = []
        
        for i, signal in enumerate(signals):
            print(f"  处理信号 {i+1}/{len(signals)}: {signal['symbol']} {signal['side']}", end="\r")
            
            trade = self._simulate_single_trade(signal)
            if trade:
                trades.append(trade)
        
        print()  # 换行
        return trades
    
    def _simulate_single_trade(self, signal: Dict) -> Optional[Trade]:
        """模拟单笔交易"""
        symbol = signal["symbol"]
        side = signal["side"].lower()
        entry_price = signal["entry_price"]
        tp_price = signal["tp_price"]
        sl_price = signal["sl_price"]
        leverage = signal["leverage"]
        signal_time_str = signal["signal_time"]
        
        # 验证数据
        if entry_price <= 0:
            return None
        
        # 如果没有止盈止损，使用默认值
        if tp_price <= 0:
            if side == "long":
                tp_price = entry_price * 1.08  # 默认8%止盈
            else:
                tp_price = entry_price * 0.92
        
        if sl_price <= 0:
            if side == "long":
                sl_price = entry_price * 0.98  # 默认2%止损
            else:
                sl_price = entry_price * 1.02
        
        # 解析时间
        try:
            signal_time = datetime.fromisoformat(signal_time_str.replace('Z', '+00:00'))
            if signal_time.tzinfo is None:
                signal_time = signal_time.replace(tzinfo=timezone.utc)
        except:
            return None
        
        # 获取K线数据
        klines = self._get_klines(symbol, signal_time)
        if not klines:
            return None
        
        # 模拟入场（加入滑点）
        if side == "long":
            actual_entry = entry_price * (1 + self.slippage_rate)
        else:
            actual_entry = entry_price * (1 - self.slippage_rate)
        
        # 寻找出场点
        exit_price = 0.0
        exit_time = None
        exit_reason = ""
        
        for kline in klines:
            timestamp, open_p, high, low, close, volume = kline
            kline_time = datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc)
            
            if kline_time <= signal_time:
                continue
            
            if side == "long":
                # 先检查止损
                if low <= sl_price:
                    exit_price = sl_price * (1 - self.slippage_rate)
                    exit_time = kline_time
                    exit_reason = "stop_loss"
                    break
                # 再检查止盈
                if high >= tp_price:
                    exit_price = tp_price * (1 - self.slippage_rate)
                    exit_time = kline_time
                    exit_reason = "take_profit"
                    break
            else:
                # 做空：先检查止损
                if high >= sl_price:
                    exit_price = sl_price * (1 + self.slippage_rate)
                    exit_time = kline_time
                    exit_reason = "stop_loss"
                    break
                # 再检查止盈
                if low <= tp_price:
                    exit_price = tp_price * (1 + self.slippage_rate)
                    exit_time = kline_time
                    exit_reason = "take_profit"
                    break
        
        # 如果24小时内未触发，按最后价格平仓
        if not exit_time and klines:
            last_kline = klines[-1]
            exit_price = last_kline[4]  # close
            exit_time = datetime.fromtimestamp(last_kline[0] / 1000, tz=timezone.utc)
            exit_reason = "timeout_24h"
        
        if not exit_price:
            return None
        
        # 计算盈亏
        if side == "long":
            pnl_pct = (exit_price - actual_entry) / actual_entry * 100
        else:
            pnl_pct = (actual_entry - exit_price) / actual_entry * 100
        
        # 扣除手续费
        pnl_pct -= self.commission_rate * 100 * 2  # 开仓+平仓
        
        # 计算持仓时间
        holding_minutes = int((exit_time - signal_time).total_seconds() / 60)
        
        return Trade(
            symbol=symbol,
            side=side,
            entry_price=actual_entry,
            entry_time=signal_time,
            exit_price=exit_price,
            exit_time=exit_time,
            tp_price=tp_price,
            sl_price=sl_price,
            pnl_pct=pnl_pct,
            exit_reason=exit_reason,
            holding_minutes=holding_minutes,
            leverage=leverage,
        )
    
    def _get_klines(self, symbol: str, since: datetime, hours: int = 24) -> List:
        """获取K线数据（带缓存）"""
        cache_key = f"{symbol}_{since.date()}"
        
        if cache_key in self._kline_cache:
            return self._kline_cache[cache_key]
        
        try:
            since_ts = int(since.timestamp() * 1000)
            klines = self.exchange.fetch_ohlcv(
                symbol, 
                '1h', 
                since=since_ts, 
                limit=hours + 2
            )
            
            self._kline_cache[cache_key] = klines
            return klines
            
        except Exception as e:
            print(f"\n[BACKTEST] 获取K线失败 {symbol}: {e}")
            return []
    
    def _calculate_metrics(
        self,
        trades: List[Trade],
        start_date: datetime,
        end_date: datetime
    ) -> BacktestResult:
        """计算绩效指标"""
        result = BacktestResult()
        result.trades = trades
        result.total_days = (end_date - start_date).days
        
        if not trades:
            return result
        
        # 基础统计
        result.total_trades = len(trades)
        result.winning_trades = sum(1 for t in trades if t.is_win)
        result.losing_trades = result.total_trades - result.winning_trades
        
        # 胜率
        result.win_rate = result.winning_trades / result.total_trades if result.total_trades > 0 else 0
        
        # 收益统计
        pnls = [t.pnl_pct for t in trades]
        result.total_pnl_pct = sum(pnls)
        result.avg_pnl_pct = np.mean(pnls) if pnls else 0
        
        wins = [t.pnl_pct for t in trades if t.is_win]
        losses = [t.pnl_pct for t in trades if not t.is_win]
        
        result.avg_win_pct = np.mean(wins) if wins else 0
        result.avg_loss_pct = np.mean(losses) if losses else 0
        
        # 盈亏比
        if result.avg_loss_pct != 0:
            result.profit_loss_ratio = abs(result.avg_win_pct / result.avg_loss_pct)
        else:
            result.profit_loss_ratio = float('inf') if result.avg_win_pct > 0 else 0
        
        # 持仓时间
        holding_times = [t.holding_minutes for t in trades]
        result.avg_holding_minutes = np.mean(holding_times) if holding_times else 0
        
        # 权益曲线
        equity = self.initial_capital
        equity_curve = [equity]
        peak = equity
        max_dd = 0
        
        for trade in trades:
            # 计算本次交易的资金变化
            position_size = equity * 0.1  # 每次使用10%资金
            trade_pnl = position_size * trade.pnl_pct / 100
            equity += trade_pnl
            equity_curve.append(equity)
            
            # 计算回撤
            if equity > peak:
                peak = equity
            dd = (peak - equity) / peak * 100 if peak > 0 else 0
            max_dd = max(max_dd, dd)
        
        result.equity_curve = equity_curve
        result.max_drawdown_pct = max_dd
        
        # 日收益率（用于Sharpe计算）
        daily_returns = []
        trades_by_date = {}
        for trade in trades:
            date_key = trade.entry_time.date()
            if date_key not in trades_by_date:
                trades_by_date[date_key] = []
            trades_by_date[date_key].append(trade.pnl_pct)
        
        for date_key in sorted(trades_by_date.keys()):
            daily_pnl = sum(trades_by_date[date_key])
            daily_returns.append(daily_pnl)
        
        result.daily_returns = daily_returns
        
        # Sharpe Ratio (假设无风险利率为0)
        if len(daily_returns) > 1:
            returns_std = np.std(daily_returns)
            if returns_std > 0:
                result.sharpe_ratio = (np.mean(daily_returns) / returns_std) * np.sqrt(252)  # 年化
            else:
                result.sharpe_ratio = 0
        
        # 最大连亏
        max_consecutive = 0
        current_consecutive = 0
        for trade in trades:
            if not trade.is_win:
                current_consecutive += 1
                max_consecutive = max(max_consecutive, current_consecutive)
            else:
                current_consecutive = 0
        result.max_consecutive_losses = max_consecutive
        
        return result


# ==================== 便捷函数 ====================

def run_backtest(
    config: Dict[str, Any] = None,
    days: int = 30,
    min_score: float = 0.0,
) -> BacktestResult:
    """
    快速运行回测
    
    Args:
        config: 配置字典，为None则从config.yaml加载
        days: 回测天数
        min_score: 最小评分筛选
    """
    if config is None:
        import yaml
        with open("config.yaml", "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
    
    backtester = Backtester(config)
    return backtester.run(days=days, min_score=min_score)


# ==================== 测试代码 ====================

if __name__ == "__main__":
    import yaml
    
    print("回测系统测试")
    print("=" * 60)
    
    # 加载配置
    try:
        with open("config.yaml", "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
    except FileNotFoundError:
        print("未找到config.yaml，使用默认配置")
        config = {
            "analytics": {"storage": {"path": "./signals.db"}},
            "backtest": {"default_days": 30}
        }
    
    # 创建回测引擎
    backtester = Backtester(config)
    
    # 运行回测
    print("\n开始回测...")
    result = backtester.run(days=30, min_score=0.5)
    
    # 打印报告
    result.print_report()
    
    # 显示最近5笔交易
    if result.trades:
        print("\n📋 最近5笔交易:")
        for trade in result.trades[-5:]:
            emoji = "✅" if trade.is_win else "❌"
            print(f"  {emoji} {trade.symbol} {trade.side} | {trade.pnl_pct:+.2f}% | {trade.exit_reason}")
