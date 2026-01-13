# core/xgboost_collector.py - XGBoost数据收集器 v2.0
# 🔥 v2.0 更新: 修复标签计算（从随机数改为真实盈亏判断）

import sqlite3
import json
import numpy as np
import ccxt
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta, timezone
from pathlib import Path


class XGBoostDataCollector:
    """
    XGBoost数据收集器 v2.0
    
    🔥 v2.0 核心修复:
    - 标签不再使用随机数
    - 基于真实的止盈止损触发情况计算标签
    - 记录完整的盈亏信息用于训练
    
    工作流程:
    1. 信号推送时记录完整特征数据 + 止盈止损价格
    2. 30分钟后检查价格是否触及入场价（判断是否"成交"）
    3. 成交后24小时检查是否触及止盈/止损
    4. 根据实际盈亏情况标记标签
    
    标签定义:
    - 1 = 盈利（触达止盈 或 24h后浮盈）
    - 0 = 亏损（触达止损 或 24h后浮亏）
    - -1 = 无效（数据不足，不用于训练）
    """
    
    def __init__(self, config: Dict, exchange: ccxt.Exchange = None):
        """
        初始化数据收集器
        
        Args:
            config: 配置字典
            exchange: CCXT交易所实例（用于获取历史K线）
        """
        self.config = config
        
        # 如果没有传入exchange，创建一个只读实例
        if exchange is None:
            self.exchange = ccxt.binance({
                'enableRateLimit': True,
                'options': {'defaultType': 'future'}
            })
        else:
            self.exchange = exchange
        
        self.db_path = Path("data/xgboost_training.db")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        
        # 🔥 从配置读取标签计算参数
        xgb_cfg = config.get("xgboost", {})
        self.label_check_hours = xgb_cfg.get("label_check_hours", 24)
        self.label_check_interval = xgb_cfg.get("label_check_interval", "1h")
        self.fill_check_minutes = xgb_cfg.get("fill_check_minutes", 30)
        self.fill_tolerance_pct = xgb_cfg.get("fill_tolerance_pct", 0.005)  # 0.5%容差
        
        self._init_database()
        
        print(f"[XGBOOST_COLLECTOR] v2.0 初始化完成")
        print(f"  标签检查: {self.label_check_hours}小时后")
        print(f"  成交检查: {self.fill_check_minutes}分钟后")
    
    def _init_database(self):
        """🔥 初始化数据库表结构（增强版）"""
        conn = sqlite3.connect(self.db_path, timeout=30); conn.execute("PRAGMA journal_mode=WAL")
        cursor = conn.cursor()
        
        # 🔥 待检查信号表（增加止盈止损价格）
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS xgb_signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            entry_price REAL NOT NULL,
            tp_price REAL,
            sl_price REAL,
            signal_time TEXT NOT NULL,
            check_time TEXT NOT NULL,
            features TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            filled_price REAL,
            filled_time TEXT,
            label INTEGER,
            label_reason TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """)
        
        # 🔥 训练数据表（增加盈亏详情）
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS xgb_training_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            features TEXT NOT NULL,
            label INTEGER NOT NULL,
            label_reason TEXT,
            filled_price REAL,
            exit_price REAL,
            profit_loss_pct REAL,
            holding_minutes INTEGER,
            signal_time TEXT NOT NULL,
            exit_time TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """)
        
        # 创建索引
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_xgb_signals_status ON xgb_signals(status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_xgb_signals_check_time ON xgb_signals(check_time)")
        
        conn.commit()
        conn.close()
        
        print("[XGBOOST_COLLECTOR] 数据库初始化完成")
    
    def record_signal(self, payload: Dict, approval_result: Dict):
        """
        记录已批准的信号
        
        🔥 v2.0: 同时记录止盈止损价格用于后续标签计算
        """
        if not approval_result.get("approved"):
            return
        
        symbol = payload.get("symbol")
        side = payload.get("side") or payload.get("bias")
        
        if not side:
            print("[COLLECTOR] ⚠️ 信号缺少方向(side/bias)，跳过记录")
            return
        
        entry_price = float(approval_result.get("entry_price", 0))
        
        # 🔥 获取止盈止损价格
        stops = payload.get("calculated_stops", {})
        tp_price = float(approval_result.get("take_profit", stops.get("tp_price", 0)))
        sl_price = float(approval_result.get("stop_loss", stops.get("sl_price", 0)))
        
        # 如果没有止盈止损，使用默认值
        if tp_price <= 0:
            tp_price = entry_price * (1.08 if side == "long" else 0.92)
        if sl_price <= 0:
            sl_price = entry_price * (0.98 if side == "long" else 1.02)
        
        features = self._extract_features(payload)
        
        signal_time = datetime.now(timezone.utc)
        check_time = signal_time + timedelta(minutes=self.fill_check_minutes)
        
        conn = sqlite3.connect(self.db_path, timeout=30); conn.execute("PRAGMA journal_mode=WAL")
        cursor = conn.cursor()
        
        cursor.execute("""
        INSERT INTO xgb_signals 
        (symbol, side, entry_price, tp_price, sl_price, signal_time, check_time, features, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')
        """, (
            symbol,
            side,
            entry_price,
            tp_price,
            sl_price,
            signal_time.isoformat(),
            check_time.isoformat(),
            json.dumps(features)
        ))
        
        conn.commit()
        conn.close()
        
        print(f"[COLLECTOR] 记录信号: {symbol} {side}")
        print(f"  入场: ${entry_price:.6f} | TP: ${tp_price:.6f} | SL: ${sl_price:.6f}")
        print(f"  {self.fill_check_minutes}分钟后检查成交")
    
    def check_pending_signals(self):
        """
        检查所有待处理的信号
        
        🔥 v2.0 工作流程:
        1. 检查pending状态：判断是否"成交"（价格触及）
        2. 检查filled状态：判断是否触及止盈止损
        """
        conn = sqlite3.connect(self.db_path, timeout=30); conn.execute("PRAGMA journal_mode=WAL")
        cursor = conn.cursor()
        
        now = datetime.now(timezone.utc)
        now_str = now.isoformat()
        
        # ========== 第一阶段：检查pending信号是否成交 ==========
        cursor.execute("""
        SELECT id, symbol, side, entry_price, tp_price, sl_price, signal_time, features
        FROM xgb_signals
        WHERE status = 'pending' AND check_time <= ?
        """, (now_str,))
        
        pending = cursor.fetchall()
        
        if pending:
            print(f"\n[COLLECTOR] 检查 {len(pending)} 个待成交信号...")
        
        for row in pending:
            signal_id, symbol, side, entry_price, tp_price, sl_price, signal_time_str, features_json = row
            
            try:
                filled, filled_price, filled_time = self._check_price_touched(
                    symbol, side, entry_price, signal_time_str
                )
                
                if filled:
                    # 更新为filled状态，等待标签计算
                    label_check_time = datetime.fromisoformat(filled_time) + timedelta(hours=self.label_check_hours)
                    
                    cursor.execute("""
                    UPDATE xgb_signals 
                    SET status = 'filled', filled_price = ?, filled_time = ?, check_time = ?
                    WHERE id = ?
                    """, (filled_price, filled_time, label_check_time.isoformat(), signal_id))
                    
                    print(f"  ✅ {symbol} {side} 成交@{filled_price:.6f} | {self.label_check_hours}h后计算标签")
                else:
                    # 未成交
                    cursor.execute("""
                    UPDATE xgb_signals SET status = 'no_fill' WHERE id = ?
                    """, (signal_id,))
                    print(f"  ⏭️ {symbol} {side} 未触及入场价")
                    
            except Exception as e:
                print(f"  ⚠️ {symbol} 检查失败: {e}")
        
        # ========== 第二阶段：检查filled信号的标签 ==========
        cursor.execute("""
        SELECT id, symbol, side, entry_price, tp_price, sl_price, filled_price, filled_time, features
        FROM xgb_signals
        WHERE status = 'filled' AND check_time <= ?
        """, (now_str,))
        
        filled_signals = cursor.fetchall()
        
        if filled_signals:
            print(f"\n[COLLECTOR] 计算 {len(filled_signals)} 个已成交信号的标签...")
        
        for row in filled_signals:
            signal_id, symbol, side, entry_price, tp_price, sl_price, filled_price, filled_time_str, features_json = row
            
            try:
                label, reason, exit_price, exit_time, pnl_pct = self._calculate_real_label(
                    symbol, side, filled_price, tp_price, sl_price, filled_time_str
                )
                
                if label >= 0:  # 有效标签
                    # 更新信号状态
                    cursor.execute("""
                    UPDATE xgb_signals 
                    SET status = 'labeled', label = ?, label_reason = ?
                    WHERE id = ?
                    """, (label, reason, signal_id))
                    
                    # 计算持仓时间
                    filled_time = datetime.fromisoformat(filled_time_str.replace('Z', '+00:00'))
                    if exit_time:
                        exit_dt = datetime.fromisoformat(exit_time.replace('Z', '+00:00'))
                        holding_minutes = int((exit_dt - filled_time).total_seconds() / 60)
                    else:
                        holding_minutes = self.label_check_hours * 60
                    
                    # 保存训练数据
                    cursor.execute("""
                    INSERT INTO xgb_training_data
                    (symbol, side, features, label, label_reason, filled_price, exit_price, 
                     profit_loss_pct, holding_minutes, signal_time, exit_time)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        symbol, side, features_json, label, reason,
                        filled_price, exit_price, pnl_pct, holding_minutes,
                        filled_time_str, exit_time
                    ))
                    
                    emoji = "✅" if label == 1 else "❌"
                    print(f"  {emoji} {symbol} {side} 标签={label} | {reason} | PnL:{pnl_pct:+.2f}%")
                else:
                    # 无效标签（数据不足）
                    cursor.execute("""
                    UPDATE xgb_signals SET status = 'invalid', label_reason = ?
                    WHERE id = ?
                    """, (reason, signal_id))
                    print(f"  ⚠️ {symbol} 标签无效: {reason}")
                    
            except Exception as e:
                print(f"  ⚠️ {symbol} 标签计算失败: {e}")
        
        conn.commit()
        conn.close()
    
    def _check_price_touched(
        self,
        symbol: str,
        side: str,
        entry_price: float,
        signal_time_str: str
    ) -> Tuple[bool, float, str]:
        """
        🔥 检查价格是否触及入场价（判断是否"成交"）
        
        逻辑:
        - 获取信号后30分钟内的1分钟K线
        - 检查价格是否触及入场价（±0.5%容差）
        - 做多：价格跌到entry_price或以下
        - 做空：价格涨到entry_price或以上
        
        Returns:
            (是否成交, 成交价格, 成交时间)
        """
        try:
            signal_time = datetime.fromisoformat(signal_time_str.replace('Z', '+00:00'))
            since = int(signal_time.timestamp() * 1000)
            
            # 获取1分钟K线
            ohlcv = self.exchange.fetch_ohlcv(
                symbol, '1m', since=since, limit=35
            )
            
            if not ohlcv or len(ohlcv) < 5:
                return False, 0, ""
            
            tolerance = entry_price * self.fill_tolerance_pct
            
            for candle in ohlcv:
                timestamp, open_p, high, low, close, volume = candle
                candle_time = datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc)
                
                # 跳过信号时间之前的K线
                if candle_time <= signal_time:
                    continue
                
                # 检查是否触及入场价
                if side == "long":
                    # 做多：价格跌到入场价或以下
                    if low <= entry_price + tolerance:
                        filled_price = min(low, entry_price)
                        return True, filled_price, candle_time.isoformat()
                else:
                    # 做空：价格涨到入场价或以上
                    if high >= entry_price - tolerance:
                        filled_price = max(high, entry_price)
                        return True, filled_price, candle_time.isoformat()
            
            return False, 0, ""
            
        except Exception as e:
            print(f"    检查成交失败: {e}")
            return False, 0, ""
    
    def _calculate_real_label(
        self,
        symbol: str,
        side: str,
        filled_price: float,
        tp_price: float,
        sl_price: float,
        filled_time_str: str
    ) -> Tuple[int, str, float, str, float]:
        """
        🔥 计算真实标签（核心修复）
        
        逻辑:
        1. 获取成交后24小时的1小时K线
        2. 检查每根K线是否触及止盈/止损
        3. 如果24小时内未触及，根据最后价格判断
        
        Returns:
            (标签, 原因, 出场价, 出场时间, 盈亏百分比)
            标签: 1=盈利, 0=亏损, -1=无效
        """
        try:
            filled_time = datetime.fromisoformat(filled_time_str.replace('Z', '+00:00'))
            since = int(filled_time.timestamp() * 1000)
            
            # 获取成交后24小时的1小时K线
            ohlcv = self.exchange.fetch_ohlcv(
                symbol, self.label_check_interval, since=since, limit=self.label_check_hours + 2
            )
            
            if not ohlcv or len(ohlcv) < 3:
                return -1, "K线数据不足", 0, "", 0
            
            # 检查每根K线
            for candle in ohlcv:
                timestamp, open_p, high, low, close, volume = candle
                candle_time = datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc)
                
                # 跳过成交时间之前的K线
                if candle_time <= filled_time:
                    continue
                
                if side == "long":
                    # 做多：先检查止损，再检查止盈
                    if low <= sl_price:
                        pnl = (sl_price - filled_price) / filled_price * 100
                        return 0, f"触发止损@{sl_price:.6f}", sl_price, candle_time.isoformat(), pnl
                    if high >= tp_price:
                        pnl = (tp_price - filled_price) / filled_price * 100
                        return 1, f"触发止盈@{tp_price:.6f}", tp_price, candle_time.isoformat(), pnl
                else:
                    # 做空：先检查止损，再检查止盈
                    if high >= sl_price:
                        pnl = (filled_price - sl_price) / filled_price * 100
                        return 0, f"触发止损@{sl_price:.6f}", sl_price, candle_time.isoformat(), pnl
                    if low <= tp_price:
                        pnl = (filled_price - tp_price) / filled_price * 100
                        return 1, f"触发止盈@{tp_price:.6f}", tp_price, candle_time.isoformat(), pnl
            
            # 24小时内未触及止盈止损，根据最后价格判断
            last_candle = ohlcv[-1]
            last_price = last_candle[4]  # close
            last_time = datetime.fromtimestamp(last_candle[0] / 1000, tz=timezone.utc)
            
            if side == "long":
                pnl = (last_price - filled_price) / filled_price * 100
                label = 1 if last_price > filled_price else 0
            else:
                pnl = (filled_price - last_price) / filled_price * 100
                label = 1 if last_price < filled_price else 0
            
            reason = f"{self.label_check_hours}h后浮{'盈' if label == 1 else '亏'}{pnl:+.2f}%"
            return label, reason, last_price, last_time.isoformat(), pnl
            
        except Exception as e:
            return -1, f"计算失败: {str(e)[:50]}", 0, "", 0
    
    def _extract_features(self, payload: Dict) -> Dict:
        """提取特征向量"""
        m = payload.get("metrics", {}) or {}
        fingpt = m.get("fingpt", {}) or {}
        subs = payload.get("subscores", {}) or {}
        btc_status = payload.get("btc_status", {}) or {}
        correlation = payload.get("correlation_analysis", {}) or {}
        funding = payload.get("funding", {}) or {}
        oi_data = payload.get("oi_data", {}) or {}
        
        features = {
            "symbol": payload.get("symbol"),
            "side": payload.get("side") or payload.get("bias"),
            "score": float(payload.get("score", 0)),
            
            # 技术指标
            "adx": float(m.get("adx", 25)),
            "rsi": float(m.get("rsi", fingpt.get("rsi", 50))),
            "vol_ratio": float(m.get("vol_spike_ratio", 1.0)),
            "bb_width": float(m.get("bb_width", 0.03)),
            "atr_pct": float(m.get("atr_pct", 2.0)),
            
            # MACD
            "macd_cross": m.get("macd_cross", "none"),
            "macd_histogram": float(m.get("macd_histogram", 0)),
            
            # 背离
            "bullish_divergence": bool(m.get("bullish_divergence", False)),
            "bearish_divergence": bool(m.get("bearish_divergence", False)),
            
            # 情绪
            "sentiment": float(fingpt.get("sentiment_score", subs.get("sentiment", 0.5))),
            "fear_greed": int(fingpt.get("fear_greed", 50)),
            
            # 资金费率
            "funding_rate": float(funding.get("rate", 0)),
            
            # 持仓量
            "oi_change": float(oi_data.get("change_24h", 0)),
            
            # 订单簿
            "orderbook_score": float(subs.get("orderbook", 0.5)),
            
            # BTC状态
            "btc_trend": btc_status.get("trend", "unknown"),
            "btc_change_1h": float(btc_status.get("price_change_1h", 0)),
            
            # 相关性
            "btc_correlation": float(correlation.get("correlation_value", 0)),
        }
        
        return features
    
    def export_training_data(self, output_path: str = "data/xgboost_training.csv"):
        """导出训练数据为CSV格式"""
        import pandas as pd
        
        conn = sqlite3.connect(self.db_path, timeout=30); conn.execute("PRAGMA journal_mode=WAL")
        
        df = pd.read_sql_query("""
        SELECT features, label, label_reason, profit_loss_pct, holding_minutes, created_at
        FROM xgb_training_data
        WHERE label >= 0
        ORDER BY created_at DESC
        """, conn)
        
        conn.close()
        
        if df.empty:
            print("[COLLECTOR] 无训练数据可导出")
            return None
        
        # 解析features JSON
        features_list = []
        for features_json in df['features']:
            features = json.loads(features_json)
            features_list.append(features)
        
        features_df = pd.DataFrame(features_list)
        
        # 合并所有数据
        result_df = pd.concat([
            features_df, 
            df[['label', 'label_reason', 'profit_loss_pct', 'holding_minutes']]
        ], axis=1)
        
        # 保存CSV
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        result_df.to_csv(output_path, index=False)
        
        # 统计
        win_count = len(df[df['label'] == 1])
        loss_count = len(df[df['label'] == 0])
        win_rate = win_count / len(df) * 100 if len(df) > 0 else 0
        
        print(f"[COLLECTOR] 导出 {len(result_df)} 条训练数据 → {output_path}")
        print(f"  胜率: {win_rate:.1f}% ({win_count}胜/{loss_count}负)")
        
        return result_df
    
    def get_stats(self) -> Dict:
        """获取数据收集统计"""
        conn = sqlite3.connect(self.db_path, timeout=30); conn.execute("PRAGMA journal_mode=WAL")
        cursor = conn.cursor()
        
        stats = {}
        
        # 统计各状态信号数
        cursor.execute("SELECT status, COUNT(*) FROM xgb_signals GROUP BY status")
        status_counts = dict(cursor.fetchall())
        
        stats["pending_signals"] = status_counts.get("pending", 0)
        stats["filled_signals"] = status_counts.get("filled", 0)
        stats["labeled_signals"] = status_counts.get("labeled", 0)
        stats["no_fill_signals"] = status_counts.get("no_fill", 0)
        stats["invalid_signals"] = status_counts.get("invalid", 0)
        
        # 统计训练数据
        cursor.execute("SELECT COUNT(*) FROM xgb_training_data")
        stats["training_samples"] = cursor.fetchone()[0]
        
        cursor.execute("SELECT label, COUNT(*) FROM xgb_training_data GROUP BY label")
        label_dist = dict(cursor.fetchall())
        stats["positive_samples"] = label_dist.get(1, 0)
        stats["negative_samples"] = label_dist.get(0, 0)
        
        # 胜率
        total = stats["positive_samples"] + stats["negative_samples"]
        stats["win_rate"] = (stats["positive_samples"] / total * 100) if total > 0 else 0
        
        # 平均盈亏
        cursor.execute("""
        SELECT AVG(profit_loss_pct) FROM xgb_training_data WHERE label = 1
        """)
        avg_win = cursor.fetchone()[0]
        stats["avg_win_pct"] = round(avg_win, 2) if avg_win else 0
        
        cursor.execute("""
        SELECT AVG(profit_loss_pct) FROM xgb_training_data WHERE label = 0
        """)
        avg_loss = cursor.fetchone()[0]
        stats["avg_loss_pct"] = round(avg_loss, 2) if avg_loss else 0
        
        conn.close()
        
        return stats
    
    def print_stats(self):
        """打印统计信息"""
        stats = self.get_stats()
        
        print("\n" + "=" * 50)
        print("📊 XGBoost数据收集统计")
        print("=" * 50)
        print(f"待检查信号: {stats['pending_signals']}")
        print(f"已成交待标签: {stats['filled_signals']}")
        print(f"已标签: {stats['labeled_signals']}")
        print(f"未成交: {stats['no_fill_signals']}")
        print(f"无效: {stats['invalid_signals']}")
        print("-" * 50)
        print(f"训练样本总数: {stats['training_samples']}")
        print(f"  正样本(盈利): {stats['positive_samples']}")
        print(f"  负样本(亏损): {stats['negative_samples']}")
        print(f"  胜率: {stats['win_rate']:.1f}%")
        print(f"  平均盈利: {stats['avg_win_pct']:+.2f}%")
        print(f"  平均亏损: {stats['avg_loss_pct']:+.2f}%")
        print("=" * 50)


# ==================== 测试代码 ====================
if __name__ == "__main__":
    import yaml
    
    print("XGBoost数据收集器 v2.0 测试")
    print("=" * 50)
    
    # 加载配置
    try:
        with open("config.yaml", "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
    except FileNotFoundError:
        config = {"xgboost": {"enabled": True}}
    
    # 初始化收集器
    collector = XGBoostDataCollector(config)
    
    # 打印统计
    collector.print_stats()
    
    # 检查待处理信号
    print("\n检查待处理信号...")
    collector.check_pending_signals()
    
    # 导出训练数据
    print("\n导出训练数据...")
    collector.export_training_data()