# core/signal_tracker.py - 信号成交和平仓跟踪系统(简化版)
import sqlite3
import ccxt
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Any, Optional

class SignalTracker:
    """信号跟踪器 - 负责检查成交和平仓"""
    
    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg
        
        # 初始化交易所
        ex_name = cfg.get("exchange", {}).get("name", "binance")
        self.ex = getattr(ccxt, ex_name)({
            'enableRateLimit': True,
            'options': {'defaultType': 'future'}
        })
        
        # 跟踪参数
        self.fill_timeout_min = cfg.get("tracking", {}).get("fill_timeout_min", 30)      # 30分钟未成交视为NO_FILL
        self.exit_timeout_hours = cfg.get("tracking", {}).get("exit_timeout_hours", 24)  # 24小时未平仓视为TIMEOUT
        self.slippage_tolerance = cfg.get("tracking", {}).get("slippage_tolerance", 0.005) # 0.5%滑点容忍
        
    def update_all_signals(self, db_path: str) -> Dict[str, int]:
        """更新所有待处理信号的状态"""
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        
        stats = {"checked": 0, "filled": 0, "no_fill": 0, "wins": 0, "losses": 0, "timeout": 0}
        
        try:
            # 1. 检查需要成交判定的信号
            pending_signals = self._get_pending_signals(cur)
            for signal in pending_signals:
                result = self._check_signal_fill(signal)
                if result:
                    self._update_signal_status(cur, signal['id'], result)
                    if result['outcome'] == 'FILLED':
                        stats["filled"] += 1
                    elif result['outcome'] == 'NO_FILL':
                        stats["no_fill"] += 1
                stats["checked"] += 1
            
            # 2. 检查需要平仓判定的信号
            filled_signals = self._get_filled_signals(cur)
            for signal in filled_signals:
                result = self._check_signal_exit(signal)
                if result:
                    self._update_signal_exit(cur, signal['id'], result)
                    if result['outcome'] == 'WIN':
                        stats["wins"] += 1
                    elif result['outcome'] == 'LOSS':
                        stats["losses"] += 1
                    elif result['outcome'] == 'TIMEOUT':
                        stats["timeout"] += 1
            
            conn.commit()
            
        except Exception as e:
            print(f"[TRACKER_ERR] 更新信号状态失败: {e}")
            import traceback
            traceback.print_exc()
            conn.rollback()
        finally:
            conn.close()
        
        return stats
    
    def _get_pending_signals(self, cur: sqlite3.Cursor) -> List[Dict]:
        """获取待检查成交的信号(30分钟内的PENDING信号)"""
        cur.execute("""
            SELECT * FROM signals 
            WHERE outcome = 'PENDING' 
            AND datetime(ts) >= datetime('now', '-2 hours')
            ORDER BY ts DESC
        """)
        
        return [dict(row) for row in cur.fetchall()]
    
    def _get_filled_signals(self, cur: sqlite3.Cursor) -> List[Dict]:
        """获取已成交但未平仓的信号(48小时内的FILLED信号)"""
        cur.execute("""
            SELECT * FROM signals 
            WHERE outcome = 'FILLED'
            AND exit_time IS NULL
            AND datetime(fill_time) >= datetime('now', '-2 days')
            ORDER BY fill_time DESC
        """)
        
        return [dict(row) for row in cur.fetchall()]
    
    def _check_signal_fill(self, signal: Dict) -> Optional[Dict]:
        """检查信号是否成交(价格是否触及适中价格)"""
        try:
            symbol = signal['symbol']
            signal_time_str = signal['ts'].replace('Z', '+00:00')
            signal_time = datetime.fromisoformat(signal_time_str)
            if signal_time.tzinfo is None:
                signal_time = signal_time.replace(tzinfo=timezone.utc)
            
            limit_price = signal.get('limit_moderate') or signal.get('entry')  # 使用适中价格
            if not limit_price:
                return {'outcome': 'NO_FILL', 'reason': 'NO_ENTRY_PRICE'}
            
            bias = signal['bias']
            
            # 检查是否超过30分钟
            now = datetime.now(timezone.utc)
            elapsed_min = (now - signal_time).total_seconds() / 60
            
            if elapsed_min > self.fill_timeout_min:
                return {'outcome': 'NO_FILL', 'reason': 'TIMEOUT_30MIN'}
            
            # 获取信号后的分钟级K线
            since = int(signal_time.timestamp() * 1000)
            limit_candles = int(elapsed_min) + 2
            
            ohlcv = self.ex.fetch_ohlcv(symbol, '1m', since=since, limit=limit_candles)
            
            if not ohlcv or len(ohlcv) < 2:
                return None  # 数据不足,继续等待
            
            # 检查价格是否触及成交价格(考虑滑点)
            slippage = limit_price * self.slippage_tolerance
            
            for candle in ohlcv[1:]:  # 跳过第一根K线(信号生成时的K线)
                timestamp, open_p, high, low, close, volume = candle
                candle_time = datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc)
                
                if candle_time <= signal_time:
                    continue
                
                # 检查是否触及成交价格
                if bias == 'long':
                    # 做多:价格跌到limit_price附近就成交
                    if low <= limit_price + slippage:
                        fill_price = min(limit_price, low + slippage/2)
                        return {
                            'outcome': 'FILLED',
                            'fill_price': fill_price,
                            'fill_time': candle_time.isoformat()
                        }
                else:
                    # 做空:价格涨到limit_price附近就成交
                    if high >= limit_price - slippage:
                        fill_price = max(limit_price, high - slippage/2)
                        return {
                            'outcome': 'FILLED',
                            'fill_price': fill_price,
                            'fill_time': candle_time.isoformat()
                        }
            
            # 还在30分钟内,继续等待
            return None
            
        except Exception as e:
            print(f"[TRACKER_ERR] 检查成交失败 {signal.get('symbol', 'UNKNOWN')}: {e}")
            return {'outcome': 'NO_FILL', 'reason': 'ERROR'}
    
    def _check_signal_exit(self, signal: Dict) -> Optional[Dict]:
        """检查已成交信号的平仓情况(止盈/止损判定)"""
        try:
            symbol = signal['symbol']
            fill_time_str = signal['fill_time'].replace('Z', '+00:00')
            fill_time = datetime.fromisoformat(fill_time_str)
            if fill_time.tzinfo is None:
                fill_time = fill_time.replace(tzinfo=timezone.utc)
            
            fill_price = signal['fill_price']
            tp_price = signal.get('tp_price') or signal.get('tp')
            sl_price = signal.get('sl_price') or signal.get('sl')
            bias = signal['bias']
            leverage = signal.get('leverage') or 5
            
            # 检查是否超过24小时
            now = datetime.now(timezone.utc)
            elapsed_hours = (now - fill_time).total_seconds() / 3600
            
            if elapsed_hours > self.exit_timeout_hours:
                # 超时算亏损,用当前价格计算
                current_price = self._get_current_price(symbol)
                if current_price:
                    return_pct = self._calculate_return(bias, fill_price, current_price, leverage)
                    return {
                        'outcome': 'TIMEOUT',  # 标记为超时
                        'exit_price': current_price,
                        'exit_time': now.isoformat(),
                        'exit_reason': 'TIMEOUT_24H',
                        'return_pct': return_pct
                    }
                else:
                    return {
                        'outcome': 'TIMEOUT',
                        'exit_price': fill_price,
                        'exit_time': now.isoformat(),
                        'exit_reason': 'TIMEOUT_NO_PRICE',
                        'return_pct': 0.0
                    }
            
            # 获取成交后的小时级K线
            since = int(fill_time.timestamp() * 1000)
            limit_candles = int(elapsed_hours) + 2
            
            ohlcv = self.ex.fetch_ohlcv(symbol, '1h', since=since, limit=limit_candles)
            
            if not ohlcv or len(ohlcv) < 2:
                return None  # 数据不足,继续等待
            
            # 检查是否触及止盈或止损
            for candle in ohlcv[1:]:  # 跳过第一根K线
                timestamp, open_p, high, low, close, volume = candle
                candle_time = datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc)
                
                if candle_time <= fill_time:
                    continue
                
                if bias == 'long':
                    # 做多:先检查止盈,后检查止损(按时间顺序)
                    if high >= tp_price:
                        return_pct = self._calculate_return(bias, fill_price, tp_price, leverage)
                        return {
                            'outcome': 'WIN',
                            'exit_price': tp_price,
                            'exit_time': candle_time.isoformat(),
                            'exit_reason': 'TP',
                            'return_pct': return_pct
                        }
                    elif low <= sl_price:
                        return_pct = self._calculate_return(bias, fill_price, sl_price, leverage)
                        return {
                            'outcome': 'LOSS',
                            'exit_price': sl_price,
                            'exit_time': candle_time.isoformat(),
                            'exit_reason': 'SL',
                            'return_pct': return_pct
                        }
                else:
                    # 做空:先检查止盈,后检查止损
                    if low <= tp_price:
                        return_pct = self._calculate_return(bias, fill_price, tp_price, leverage)
                        return {
                            'outcome': 'WIN',
                            'exit_price': tp_price,
                            'exit_time': candle_time.isoformat(),
                            'exit_reason': 'TP',
                            'return_pct': return_pct
                        }
                    elif high >= sl_price:
                        return_pct = self._calculate_return(bias, fill_price, sl_price, leverage)
                        return {
                            'outcome': 'LOSS',
                            'exit_price': sl_price,
                            'exit_time': candle_time.isoformat(),
                            'exit_reason': 'SL',
                            'return_pct': return_pct
                        }
            
            # 24小时内继续跟踪
            return None
            
        except Exception as e:
            print(f"[TRACKER_ERR] 检查平仓失败 {signal.get('symbol', 'UNKNOWN')}: {e}")
            return None
    
    def _calculate_return(self, bias: str, entry_price: float, exit_price: float, leverage: float) -> float:
        """计算收益率(含杠杆)"""
        if entry_price <= 0:
            return 0.0
        
        if bias == 'long':
            price_return = (exit_price - entry_price) / entry_price
        else:
            price_return = (entry_price - exit_price) / entry_price
        
        return price_return * leverage * 100  # 转换为百分比
    
    def _get_current_price(self, symbol: str) -> Optional[float]:
        """获取当前价格"""
        try:
            ticker = self.ex.fetch_ticker(symbol)
            return ticker['last']
        except Exception as e:
            print(f"[TRACKER_ERR] 获取价格失败 {symbol}: {e}")
            return None
    
    def _update_signal_status(self, cur: sqlite3.Cursor, signal_id: int, result: Dict):
        """更新信号成交状态"""
        if result['outcome'] == 'FILLED':
            cur.execute("""
                UPDATE signals 
                SET outcome = ?, fill_price = ?, fill_time = ?
                WHERE id = ?
            """, (result['outcome'], result['fill_price'], result['fill_time'], signal_id))
        else:
            cur.execute("""
                UPDATE signals 
                SET outcome = ?
                WHERE id = ?
            """, (result['outcome'], signal_id))
    
    def _update_signal_exit(self, cur: sqlite3.Cursor, signal_id: int, result: Dict):
        """更新信号平仓状态"""
        cur.execute("""
            UPDATE signals 
            SET outcome = ?, exit_price = ?, exit_time = ?, exit_reason = ?, return_pct = ?
            WHERE id = ?
        """, (
            result['outcome'], result['exit_price'], result['exit_time'],
            result['exit_reason'], result['return_pct'], signal_id
        ))


# ============ 在main.py中集成的函数 ============
def update_signal_tracking(cfg: Dict[str, Any], db_path: str):
    """在主循环中调用的信号跟踪更新 - 每4小时运行一次"""
    try:
        tracker = SignalTracker(cfg)
        stats = tracker.update_all_signals(db_path)
        
        if stats["checked"] > 0 or stats["filled"] > 0 or stats["wins"] > 0 or stats["losses"] > 0:
            print(f"[TRACKER] 检查{stats['checked']}个信号: "
                  f"成交{stats['filled']}个, 未成交{stats['no_fill']}个, "
                  f"止盈{stats['wins']}个, 止损{stats['losses']}个, 超时{stats['timeout']}个")
        
        return stats
    except Exception as e:
        print(f"[TRACKER_ERR] 信号跟踪失败: {e}")
        import traceback
        traceback.print_exc()
        return {"checked": 0, "filled": 0, "no_fill": 0, "wins": 0, "losses": 0, "timeout": 0}