"""
信号观察系统 v5.1.2 - 动态阈值 + 趋势预判 + 完整时区修复
功能：
1. 管理信号观察队列（分级观察期）
2. 硬规则快速判断时机（不依赖AI，快速可靠）
3. 动态阈值（根据ATR自动调整）
4. Claude最终决策（全量实时数据）
5. 触发时创建交易信号

🔥 v5.1.2 更新:
- 🔥 完整修复时区bug：所有datetime.now()改为datetime.utcnow()
- 🔥 修复_should_check_now中的时区问题（导致"第480分钟评估"）
- 🔥 修复信号创建时expire_time和last_check_time的时区

🔥 v5.1.1 更新:
- 🔥 修复时区bug：SQLite CURRENT_TIMESTAMP是UTC，需要用datetime.utcnow()比较
- 🔥 之前480分钟问题是因为UTC+8时区差异导致

🔥 v5.1 更新:
- 🔥 修复过期时间显示bug（之前显示480分钟是因为用了秒数）
- 🔥 根据信号类型显示正确的过期时间

🔥 v5.0 核心改动:
- 🔥 动态阈值：根据ATR波动率自动调整观察期阈值
- 🔥 趋势预判信号：8分钟观察期，更宽松阈值
- 🔥 分级观察期：极端信号5分钟/普通8分钟/趋势6-8分钟
- 时机判断: 硬规则（快速可靠）
- 最终决策: Claude（全量实时数据）
"""

import sqlite3
import json
import anthropic
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Optional
from core.utils import rsi
from core.notifier import tg_send  # 🔥 添加推送功能


def _convert_numpy_types(obj):
    """
    🔥 将numpy类型转换为Python原生类型，解决JSON序列化问题
    """
    if isinstance(obj, dict):
        return {k: _convert_numpy_types(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_convert_numpy_types(item) for item in obj]
    elif isinstance(obj, (np.bool_, np.bool8)):
        return bool(obj)
    elif isinstance(obj, (np.integer, np.int64, np.int32)):
        return int(obj)
    elif isinstance(obj, (np.floating, np.float64, np.float32)):
        return float(obj)
    elif isinstance(obj, np.ndarray):
        return obj.tolist()
    else:
        return obj


class SignalWatcher:
    """信号观察器 v5.0 - 动态阈值 + 趋势预判"""

    def __init__(self, config: dict, db_path: str, exchange, claude_api_key: str, deepseek_config: dict, full_config: dict = None):
        """
        初始化信号观察器
        """
        self.config = config
        self.full_config = full_config or {}
        self.db_path = db_path
        self.exchange = exchange
        self.enabled = config.get("enabled", True)

        # AI配置
        self.claude_api_key = claude_api_key
        self.claude_model = full_config.get("claude", {}).get("model", "claude-sonnet-4-5-20250929")
        self.deepseek_enabled = deepseek_config.get("enabled", True)
        self.deepseek_api_key = deepseek_config.get("api_key", "")
        self.deepseek_base_url = deepseek_config.get("base_url", "https://api.deepseek.com/v1")
        self.deepseek_model = deepseek_config.get("model", "deepseek-chat")
        self.deepseek_timeout = deepseek_config.get("timeout", 30)

        # 观察期配置
        self.expire_minutes = config.get("expire_minutes", 4)
        self.check_interval_sec = config.get("check_interval_seconds", 60)

        # 订单配置
        self.limit_order_timeout_min = config.get("limit_order_timeout_minutes", 3)

        # AI分工
        self.timing_ai = "deepseek"
        self.price_ai_rotation = False

        # 中途放弃
        self.allow_ai_abandon = config.get("allow_ai_abandon", True)
        
        # 🔥🔥🔥 v5.0: 分级观察期配置
        tiered_cfg = config.get("tiered_observation", {})
        self.tiered_enabled = tiered_cfg.get("enabled", True)
        
        # 极端信号配置 - 🔥放宽阈值
        extreme_cfg = tiered_cfg.get("extreme", {})
        self.extreme_rsi_long_threshold = extreme_cfg.get("rsi_long_threshold", 15)
        self.extreme_rsi_short_threshold = extreme_cfg.get("rsi_short_threshold", 85)
        self.extreme_expire_minutes = extreme_cfg.get("expire_minutes", 5)  # 🔥 2->5
        self.extreme_price_abandon_pct = extreme_cfg.get("price_abandon_pct", 3.5)  # 🔥 2.0->3.5
        self.extreme_rsi_recover_long = extreme_cfg.get("rsi_recover_long_abandon", 55)  # 🔥 40->55
        self.extreme_rsi_recover_short = extreme_cfg.get("rsi_recover_short_abandon", 45)  # 🔥 60->45
        self.extreme_price_miss_pct = extreme_cfg.get("price_miss_pct", 4.0)  # 🔥 2.5->4.0
        
        # 普通信号配置 - 🔥放宽阈值
        normal_cfg = tiered_cfg.get("normal", {})
        self.normal_expire_minutes = normal_cfg.get("expire_minutes", 8)  # 🔥 4->8
        self.normal_price_abandon_pct = normal_cfg.get("price_abandon_pct", 3.0)  # 🔥 1.5->3.0
        self.normal_rsi_recover_long = normal_cfg.get("rsi_recover_long_abandon", 55)  # 🔥 45->55
        self.normal_rsi_recover_short = normal_cfg.get("rsi_recover_short_abandon", 45)  # 🔥 55->45
        self.normal_price_miss_pct = normal_cfg.get("price_miss_pct", 3.5)  # 🔥 2.0->3.5
        
        # 🔥 v7.9: 趋势延续已弃用，保留配置以兼容旧数据
        trend_cont_cfg = tiered_cfg.get("trend_continuation", {})
        self.trend_cont_expire_minutes = trend_cont_cfg.get("expire_minutes", 6)
        self.trend_cont_price_abandon_pct = trend_cont_cfg.get("price_abandon_pct", 3.0)
        self.trend_cont_price_miss_pct = trend_cont_cfg.get("price_miss_pct", 4.0)
        
        # 🔥🔥🔥 趋势预判配置 - 放宽阈值
        trend_anti_cfg = tiered_cfg.get("trend_anticipation", {})
        self.trend_anti_expire_minutes = trend_anti_cfg.get("expire_minutes", 8)
        self.trend_anti_price_abandon_pct = trend_anti_cfg.get("price_abandon_pct", 4.0)  # 🔥 2.5->4.0
        self.trend_anti_price_miss_pct = trend_anti_cfg.get("price_miss_pct", 5.0)  # 🔥 3.5->5.0
        
        # 🔥🔥🔥 动态阈值配置
        dynamic_cfg = config.get("dynamic_thresholds", {})
        self.dynamic_enabled = dynamic_cfg.get("enabled", True)
        self.atr_period = dynamic_cfg.get("atr_period", 14)
        self.atr_low = dynamic_cfg.get("atr_low", 0.015)
        self.atr_normal = dynamic_cfg.get("atr_normal", 0.025)
        self.atr_high = dynamic_cfg.get("atr_high", 0.035)
        self.low_vol_multiplier = dynamic_cfg.get("low_volatility_multiplier", 0.8)
        self.normal_vol_multiplier = dynamic_cfg.get("normal_volatility_multiplier", 1.0)
        self.high_vol_multiplier = dynamic_cfg.get("high_volatility_multiplier", 1.5)
        self.extreme_vol_multiplier = dynamic_cfg.get("extreme_volatility_multiplier", 2.0)
        self.dynamic_exclude_types = dynamic_cfg.get("exclude_signal_types", [])

        # 初始化数据库
        self._init_database()

        print(f"[WATCHER] v5.0 初始化完成 | 启用: {self.enabled}")
        if self.enabled:
            print(f"[WATCHER] 基础观察期: {self.expire_minutes}分钟 | 评估间隔: {self.check_interval_sec}秒")
            if self.tiered_enabled:
                print(f"[WATCHER] 🔥 分级观察期: 极端{self.extreme_expire_minutes}分/普通{self.normal_expire_minutes}分/趋势{self.trend_cont_expire_minutes}分/预判{self.trend_anti_expire_minutes}分")
            if self.dynamic_enabled:
                print(f"[WATCHER] 🔥 动态阈值: 已启用 (ATR自适应)")

    def _init_database(self):
        """🔥 初始化数据库表结构"""
        import os
        os.makedirs(os.path.dirname(self.db_path) if os.path.dirname(self.db_path) else ".", exist_ok=True)
        
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        cur = conn.cursor()
        
        # 🔥 完整的表结构
        cur.execute("""
            CREATE TABLE IF NOT EXISTS watch_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                signal_type TEXT,
                detected_price REAL,
                detected_rsi REAL,
                detected_adx REAL,
                sl_price REAL,
                tp_price REAL,
                original_payload TEXT,
                expire_time TEXT,
                last_check_time TEXT,
                status TEXT DEFAULT 'watching',
                triggered_time TEXT,
                triggered_price REAL,
                trigger_reason TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # 🔥 尝试添加缺失的列（兼容旧数据库）
        try:
            cur.execute("ALTER TABLE watch_signals ADD COLUMN triggered_time TEXT")
        except:
            pass
        try:
            cur.execute("ALTER TABLE watch_signals ADD COLUMN triggered_price REAL")
        except:
            pass
        try:
            cur.execute("ALTER TABLE watch_signals ADD COLUMN trigger_reason TEXT")
        except:
            pass
        
        conn.commit()
        conn.close()
        print(f"[WATCHER] 数据库初始化完成: {self.db_path}")

    def add_signal_to_watch(
        self,
        symbol: str,
        side: str,
        signal_type: str,
        price: float,
        rsi: float,
        adx: float,
        sl_price: float,
        tp_price: float,
        metrics: Dict,
        original_payload: Dict
    ) -> int:
        """
        🔥 v2.1修复版: 将信号加入观察队列（带冷却检查）

        Args:
            symbol: 交易对
            side: 方向（long/short）
            signal_type: 信号类型（reversal/trend）
            price: 当前价格
            rsi: 当前 RSI
            adx: 当前 ADX
            sl_price: 止损价
            tp_price: 止盈价
            metrics: 完整指标数据
            original_payload: 原始信号payload（供完整评估使用）

        Returns:
            信号ID
        """
        if not self.enabled:
            print(f"[WATCHER] ⚠️ 观察模式未启用，跳过")
            return 0

        try:
            conn = sqlite3.connect(self.db_path, timeout=30); conn.execute("PRAGMA journal_mode=WAL")
            cur = conn.cursor()
            
            # 🔥 新增: 信号冷却检查（10分钟内同币种同方向不重复加入）
            cur.execute("""
                SELECT COUNT(*) FROM watch_signals
                WHERE symbol = ? AND side = ?
                AND created_at >= datetime('now', '-10 minutes')
                AND status IN ('watching', 'triggered')
            """, (symbol, side))
            
            recent_count = cur.fetchone()[0]
            if recent_count > 0:
                print(f"[WATCHER] ⏭️ 冷却中跳过: {symbol} {side} (10分钟内已有{recent_count}个信号)")
                conn.close()
                return 0

            # 🔥 根据信号类型计算过期时间
            if signal_type == "trend_anticipation":
                expire_minutes = self.trend_anti_expire_minutes  # 8分钟
            elif signal_type == "trend_continuation":
                expire_minutes = self.trend_cont_expire_minutes  # 6分钟
            else:
                # 反转信号：根据RSI极端程度分级
                is_extreme = False
                if side == "long" and rsi <= self.extreme_rsi_long_threshold:
                    is_extreme = True
                elif side == "short" and rsi >= self.extreme_rsi_short_threshold:
                    is_extreme = True
                
                if is_extreme:
                    expire_minutes = self.extreme_expire_minutes  # 5分钟
                else:
                    expire_minutes = self.normal_expire_minutes  # 8分钟
            
            # 🔥🔥 修复v5.1.2：expire_time和last_check_time都用UTC
            expire_time = datetime.utcnow() + timedelta(minutes=expire_minutes)

            # 写入数据库
            cur.execute("""
                INSERT INTO watch_signals
                (symbol, side, signal_type, detected_price, detected_rsi, detected_adx,
                 sl_price, tp_price, original_payload, expire_time, last_check_time)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                symbol, side, signal_type, price, rsi, adx,
                sl_price, tp_price,
                json.dumps(_convert_numpy_types(original_payload), ensure_ascii=False),  # 🔥 转换numpy类型
                expire_time.isoformat(),
                datetime.utcnow().isoformat()  # 🔥 初始检查时间也用UTC
            ))

            signal_id = cur.lastrowid
            conn.commit()
            conn.close()

            print(f"[WATCH] 📝 {symbol} {side} 加入观察 (ID={signal_id})")
            print(f"[WATCH]    当前价: ${price:.6f} | 过期: {expire_minutes}分钟 | 信号类型: {signal_type}")

            return signal_id

        except Exception as e:
            print(f"[WATCHER_ERR] 添加观察信号失败: {e}")
            import traceback
            traceback.print_exc()
            return 0

    def monitor(self):
        """
        监控观察队列（每轮扫描时调用）
        主动AI评估入场时机
        """
        if not self.enabled:
            return

        try:
            conn = sqlite3.connect(self.db_path, timeout=30); conn.execute("PRAGMA journal_mode=WAL")
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()

            # 获取所有观察中的信号
            cur.execute("""
                SELECT * FROM watch_signals
                WHERE status = 'watching'
                ORDER BY created_at ASC
            """)

            watching_signals = cur.fetchall()

            if not watching_signals:
                return

            print(f"\n[WATCH] 🔍 监控 {len(watching_signals)} 个信号...")

            for signal in watching_signals:
                signal_dict = dict(signal)

                # 检查是否过期
                if self._is_expired(signal_dict):
                    self._handle_expired_signal(signal_dict, cur)
                    continue

                # 检查距离上次检查是否满1分钟（跳过0分钟，从1分钟开始）
                should_check, elapsed_minutes = self._should_check_now(signal_dict)

                if not should_check:
                    continue

                # 主动AI评估
                self._evaluate_entry_timing(signal_dict, elapsed_minutes, cur)

            conn.commit()
            conn.close()

        except Exception as e:
            print(f"[WATCHER_ERR] 监控失败: {e}")
            import traceback
            traceback.print_exc()

    def _should_check_now(self, signal: Dict) -> Tuple[bool, int]:
        """
        检查是否应该进行AI评估

        Returns:
            (是否检查, 已观察分钟数)
        """
        created_at = datetime.fromisoformat(signal["created_at"])
        last_check = datetime.fromisoformat(signal["last_check_time"])
        # 🔥🔥 修复v5.1.2：SQLite CURRENT_TIMESTAMP是UTC，必须用utcnow()比较
        now = datetime.utcnow()

        # 计算已观察时长（分钟）
        elapsed_seconds = (now - created_at).total_seconds()
        elapsed_minutes = int(elapsed_seconds / 60)

        # 距离上次检查的时长
        since_last_check = (now - last_check).total_seconds()

        # 跳过0分钟，从1分钟开始，每1分钟检查一次
        if elapsed_minutes >= 1 and since_last_check >= self.check_interval_sec:
            return True, elapsed_minutes

        return False, elapsed_minutes

    def _evaluate_entry_timing(self, signal: Dict, elapsed_minutes: int, cursor):
        """
        🔥 v3.1: 硬规则判断时机 + Claude最终决策（全量实时数据）

        Args:
            signal: 信号字典
            elapsed_minutes: 已观察分钟数
            cursor: 数据库游标
        """
        symbol = signal["symbol"]
        side = signal["side"]
        signal_id = signal["id"]
        signal_type = signal.get("signal_type", "unknown")

        print(f"[WATCH] ⏱️ {symbol} {side} 第{elapsed_minutes}分钟评估...")

        # 获取当前价格和RSI（硬规则需要）
        try:
            current_price = self._get_current_price(symbol)
            current_rsi = self._get_current_rsi(symbol)
        except Exception as e:
            print(f"[WATCHER] ⚠️ 获取市场数据失败: {e}")
            return

        # 🔥 步骤1：硬规则快速判断（不用AI，快速可靠）
        timing_decision = self._hard_rules_timing_check(
            signal=signal,
            current_price=current_price,
            current_rsi=current_rsi,
            elapsed_minutes=elapsed_minutes
        )

        # 更新最后检查时间
        cursor.execute("""
            UPDATE watch_signals
            SET last_check_time = datetime('now')
            WHERE id = ?
        """, (signal_id,))

        if timing_decision == "YES":
            print(f"[WATCH] ✅ 硬规则通过 - 获取实时数据...")

            # 🔥 步骤2：获取全量实时数据
            realtime_data = self._get_realtime_data(symbol)
            if not realtime_data:
                print(f"[WATCH] ⚠️ 获取实时数据失败，跳过")
                return

            # 🔥 步骤3：Claude最终决策（全量数据）
            final_result = self._claude_final_decision(signal, realtime_data, elapsed_minutes)

            if not final_result:
                print(f"[WATCH] ⚠️ Claude决策失败，跳过")
                return

            decision = final_result.get("decision", "ABANDON")

            if decision == "EXECUTE_MARKET":
                print(f"[WATCH] ✅ Claude决策: 市价买入")
                final_result["order_type"] = "market"
                final_result["entry_price"] = realtime_data["current_price"]
                self._trigger_signal(signal, final_result, "claude", "hard_rules", cursor)

            elif decision == "EXECUTE_LIMIT":
                print(f"[WATCH] ✅ Claude决策: 限价买入 @${final_result.get('entry_price', 0):.6f}")
                final_result["order_type"] = "limit"
                self._trigger_signal(signal, final_result, "claude", "hard_rules", cursor)

            elif decision == "ABANDON":
                reason = final_result.get("reasoning", "市场条件变化")
                print(f"[WATCH] ❌ Claude决策: 放弃 | {reason}")
                self._handle_abandoned_signal(signal, f"claude_abandon: {reason}", cursor)

        elif timing_decision == "ABANDON":
            print(f"[WATCH] ❌ 硬规则: 放弃信号")
            self._handle_abandoned_signal(signal, "hard_rules_abandon", cursor)

        else:  # WAIT
            print(f"[WATCH] ⏳ 硬规则: 继续观察")

    def _hard_rules_timing_check(
        self,
        signal: Dict,
        current_price: float,
        current_rsi: float,
        elapsed_minutes: int
    ) -> str:
        """
        🔥 v5.0: 硬规则时机判断（动态阈值 + 分级观察期）
        
        根据信号类型和市场波动率动态调整阈值
        
        Returns:
            "YES" / "WAIT" / "ABANDON"
        """
        symbol = signal["symbol"]
        side = signal["side"]
        detected_price = signal["detected_price"]
        detected_rsi = signal["detected_rsi"]
        signal_type = signal.get("signal_type", "unknown")

        # 计算价格变化
        price_change_pct = ((current_price - detected_price) / detected_price) * 100
        rsi_change = current_rsi - detected_rsi
        
        # 🔥🔥🔥 获取动态阈值乘数
        volatility_multiplier = self._get_volatility_multiplier(signal)
        
        print(f"[HARD_RULES] 价格变化: {price_change_pct:+.2f}% | RSI: {detected_rsi:.1f} → {current_rsi:.1f} | 波动乘数: {volatility_multiplier:.1f}x")

        # ========== 🔥🔥🔥 趋势预判信号（更宽松）==========
        if signal_type == "trend_anticipation":
            # 8分钟观察期
            expire_min = self.trend_anti_expire_minutes
            if elapsed_minutes > expire_min:
                print(f"[HARD_RULES] ❌ 趋势预判超过{expire_min}分钟窗口")
                return "ABANDON"
            
            # 应用动态阈值
            price_abandon = self.trend_anti_price_abandon_pct * volatility_multiplier
            price_miss = self.trend_anti_price_miss_pct * volatility_multiplier
            
            if side == "long":
                # 继续下跌放弃（宽松）
                if price_change_pct < -price_abandon:
                    print(f"[HARD_RULES] ❌ 趋势预判LONG但继续下跌{price_change_pct:.2f}% > {price_abandon:.1f}%")
                    return "ABANDON"
                # 涨太多错过（宽松）
                if price_change_pct > price_miss:
                    print(f"[HARD_RULES] ❌ 趋势预判LONG已涨{price_change_pct:.2f}% > {price_miss:.1f}%，错过入场")
                    return "ABANDON"
                # 🔥 放宽RSI阈值：65->75
                if current_rsi > 75:
                    print(f"[HARD_RULES] ❌ 趋势预判LONG但RSI已到{current_rsi:.1f}，机会已过")
                    return "ABANDON"
            else:
                if price_change_pct > price_abandon:
                    print(f"[HARD_RULES] ❌ 趋势预判SHORT但继续上涨{price_change_pct:.2f}% > {price_abandon:.1f}%")
                    return "ABANDON"
                if price_change_pct < -price_miss:
                    print(f"[HARD_RULES] ❌ 趋势预判SHORT已跌{price_change_pct:.2f}% > {price_miss:.1f}%，错过入场")
                    return "ABANDON"
                # 🔥 放宽RSI阈值：35->25
                if current_rsi < 25:
                    print(f"[HARD_RULES] ❌ 趋势预判SHORT但RSI已到{current_rsi:.1f}，机会已过")
                    return "ABANDON"
            
            print(f"[HARD_RULES] ✅ 趋势预判信号通过")
            return "YES"
        
        # ========== 趋势延续信号 ==========
        if signal_type == "trend_continuation":
            expire_min = self.trend_cont_expire_minutes
            if elapsed_minutes > expire_min:
                print(f"[HARD_RULES] ❌ 趋势延续超过{expire_min}分钟窗口")
                return "ABANDON"
            
            price_abandon = self.trend_cont_price_abandon_pct * volatility_multiplier
            price_miss = self.trend_cont_price_miss_pct * volatility_multiplier
            
            if side == "long":
                if price_change_pct < -price_abandon:
                    print(f"[HARD_RULES] ❌ 趋势延续LONG但下跌{price_change_pct:.2f}%")
                    return "ABANDON"
                if price_change_pct > price_miss:
                    print(f"[HARD_RULES] ❌ 趋势延续LONG已涨{price_change_pct:.2f}%，错过")
                    return "ABANDON"
            else:
                if price_change_pct > price_abandon:
                    print(f"[HARD_RULES] ❌ 趋势延续SHORT但上涨{price_change_pct:.2f}%")
                    return "ABANDON"
                if price_change_pct < -price_miss:
                    print(f"[HARD_RULES] ❌ 趋势延续SHORT已跌{price_change_pct:.2f}%，错过")
                    return "ABANDON"
            
            print(f"[HARD_RULES] ✅ 趋势延续信号通过")
            return "YES"

        # ========== 反转信号（分级处理）==========
        # 判断是极端信号还是普通信号
        is_extreme = False
        if side == "long" and detected_rsi <= self.extreme_rsi_long_threshold:
            is_extreme = True
        elif side == "short" and detected_rsi >= self.extreme_rsi_short_threshold:
            is_extreme = True
        
        if is_extreme:
            # 极端信号：2分钟观察期，更宽松阈值
            expire_min = self.extreme_expire_minutes
            price_abandon = self.extreme_price_abandon_pct * volatility_multiplier
            price_miss = self.extreme_price_miss_pct * volatility_multiplier
            rsi_recover_long = self.extreme_rsi_recover_long
            rsi_recover_short = self.extreme_rsi_recover_short
            signal_level = "极端"
        else:
            # 普通信号：4分钟观察期，标准阈值
            expire_min = self.normal_expire_minutes
            price_abandon = self.normal_price_abandon_pct * volatility_multiplier
            price_miss = self.normal_price_miss_pct * volatility_multiplier
            rsi_recover_long = self.normal_rsi_recover_long
            rsi_recover_short = self.normal_rsi_recover_short
            signal_level = "普通"
        
        # 检查是否超过观察期
        if elapsed_minutes > expire_min:
            print(f"[HARD_RULES] ❌ {signal_level}反转信号超过{expire_min}分钟窗口")
            return "ABANDON"
        
        if side == "long":
            # 继续下跌放弃
            if price_change_pct < -price_abandon:
                print(f"[HARD_RULES] ❌ {signal_level}反转LONG但继续下跌{price_change_pct:.2f}% > {price_abandon:.1f}%")
                return "ABANDON"
            # RSI回升太多
            if current_rsi > rsi_recover_long:
                print(f"[HARD_RULES] ❌ {signal_level}反转LONG但RSI已回升到{current_rsi:.1f} > {rsi_recover_long}，反弹已发生")
                return "ABANDON"
            # 价格涨太多
            if price_change_pct > price_miss:
                print(f"[HARD_RULES] ❌ {signal_level}反转LONG已涨{price_change_pct:.2f}% > {price_miss:.1f}%，错过入场")
                return "ABANDON"
        else:
            # 继续上涨放弃
            if price_change_pct > price_abandon:
                print(f"[HARD_RULES] ❌ {signal_level}反转SHORT但继续上涨{price_change_pct:.2f}% > {price_abandon:.1f}%")
                return "ABANDON"
            # RSI回落太多
            if current_rsi < rsi_recover_short:
                print(f"[HARD_RULES] ❌ {signal_level}反转SHORT但RSI已回落到{current_rsi:.1f} < {rsi_recover_short}，回调已发生")
                return "ABANDON"
            # 价格跌太多
            if price_change_pct < -price_miss:
                print(f"[HARD_RULES] ❌ {signal_level}反转SHORT已跌{price_change_pct:.2f}% > {price_miss:.1f}%，错过入场")
                return "ABANDON"
        
        print(f"[HARD_RULES] ✅ {signal_level}反转信号通过 (阈值: 放弃±{price_abandon:.1f}% / 错过±{price_miss:.1f}%)")
        return "YES"
    
    def _get_volatility_multiplier(self, signal: Dict) -> float:
        """
        🔥 根据ATR计算动态阈值乘数
        """
        if not self.dynamic_enabled:
            return 1.0
        
        signal_type = signal.get("signal_type", "unknown")
        
        # 排除的信号类型不应用动态阈值
        if signal_type in self.dynamic_exclude_types:
            return 1.0
        
        # 获取ATR
        atr_pct = signal.get("atr_pct", 0)
        if atr_pct <= 0:
            # 尝试从metrics获取
            metrics = signal.get("metrics", {})
            atr = metrics.get("atr", 0)
            price = signal.get("detected_price", 0)
            if price > 0 and atr > 0:
                atr_pct = atr / price
            else:
                return 1.0
        
        # 根据ATR返回乘数
        if atr_pct < self.atr_low:
            return self.low_vol_multiplier
        elif atr_pct < self.atr_normal:
            return self.normal_vol_multiplier
        elif atr_pct < self.atr_high:
            return self.high_vol_multiplier
        else:
            return self.extreme_vol_multiplier

    def _get_realtime_data(self, symbol: str) -> Optional[Dict]:
        """
        🔥 v3.1: 获取全量实时数据供Claude决策
        
        Returns:
            实时数据字典，包含价格、RSI、成交量、订单簿、BTC状态等
        """
        try:
            # 获取K线数据
            ohlcv = self.exchange.fetch_ohlcv(symbol, "1m", limit=100)
            if not ohlcv or len(ohlcv) < 60:
                print(f"[REALTIME] ⚠️ K线数据不足")
                return None
            
            df = pd.DataFrame(ohlcv, columns=["ts", "open", "high", "low", "close", "volume"])
            
            # 当前价格
            current_price = float(df["close"].iloc[-1])
            
            # RSI(14)
            delta = df["close"].diff()
            gain = delta.where(delta > 0, 0).rolling(14).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
            rs = gain / loss
            rsi_series = 100 - (100 / (1 + rs))
            current_rsi = float(rsi_series.iloc[-1])
            if pd.isna(current_rsi):
                current_rsi = 50.0
            
            # 成交量倍数
            vol_ma = df["volume"].rolling(20).mean().iloc[-1]
            vol_last = df["volume"].iloc[-1]
            volume_ratio = float(vol_last / vol_ma) if vol_ma > 0 else 1.0
            
            # ATR(14)
            high = df["high"]
            low = df["low"]
            close = df["close"]
            tr = pd.concat([
                high - low,
                (high - close.shift()).abs(),
                (low - close.shift()).abs()
            ], axis=1).max(axis=1)
            atr = float(tr.rolling(14).mean().iloc[-1])
            atr_pct = (atr / current_price * 100) if current_price > 0 else 2.0
            
            # ADX(14) - 简化计算
            adx = 25.0  # 默认值
            try:
                plus_dm = (high - high.shift()).clip(lower=0)
                minus_dm = (low.shift() - low).clip(lower=0)
                plus_di = 100 * (plus_dm.rolling(14).mean() / tr.rolling(14).mean())
                minus_di = 100 * (minus_dm.rolling(14).mean() / tr.rolling(14).mean())
                dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
                adx = float(dx.rolling(14).mean().iloc[-1])
                if pd.isna(adx):
                    adx = 25.0
            except:
                pass
            
            # MACD
            ema12 = df["close"].ewm(span=12).mean()
            ema26 = df["close"].ewm(span=26).mean()
            macd_line = ema12 - ema26
            signal_line = macd_line.ewm(span=9).mean()
            macd_hist = macd_line - signal_line
            
            macd_cross = "none"
            if len(macd_hist) >= 2:
                if macd_hist.iloc[-1] > 0 and macd_hist.iloc[-2] <= 0:
                    macd_cross = "golden"
                elif macd_hist.iloc[-1] < 0 and macd_hist.iloc[-2] >= 0:
                    macd_cross = "death"
            
            # 订单簿深度（尝试获取）
            orderbook_score = 0.5
            try:
                orderbook = self.exchange.fetch_order_book(symbol, limit=10)
                if orderbook:
                    bid_vol = sum([b[1] for b in orderbook.get("bids", [])[:5]])
                    ask_vol = sum([a[1] for a in orderbook.get("asks", [])[:5]])
                    total = bid_vol + ask_vol
                    if total > 0:
                        orderbook_score = bid_vol / total  # 买盘占比
            except:
                pass
            
            # BTC状态
            btc_trend = "unknown"
            btc_change_pct = 0.0
            try:
                btc_ohlcv = self.exchange.fetch_ohlcv("BTC/USDT:USDT", "5m", limit=6)
                if btc_ohlcv and len(btc_ohlcv) >= 6:
                    btc_current = btc_ohlcv[-1][4]
                    btc_prev = btc_ohlcv[-6][4]  # 30分钟前
                    btc_change_pct = (btc_current - btc_prev) / btc_prev * 100
                    if btc_change_pct > 0.5:
                        btc_trend = "up"
                    elif btc_change_pct < -0.5:
                        btc_trend = "down"
                    else:
                        btc_trend = "sideways"
            except:
                pass
            
            # 资金费率（尝试获取）
            funding_rate = 0.0
            try:
                # 不同交易所获取方式不同，这里用通用方式
                pass
            except:
                pass
            
            return {
                "current_price": current_price,
                "rsi": current_rsi,
                "volume_ratio": volume_ratio,
                "atr_pct": atr_pct,
                "adx": adx,
                "macd_cross": macd_cross,
                "orderbook_score": orderbook_score,
                "btc_trend": btc_trend,
                "btc_change_pct": btc_change_pct,
                "funding_rate": funding_rate
            }
            
        except Exception as e:
            print(f"[REALTIME] ❌ 获取实时数据失败: {e}")
            import traceback
            traceback.print_exc()
            return None

    def _claude_final_decision(self, signal: Dict, realtime: Dict, elapsed_minutes: int) -> Optional[Dict]:
        """
        🔥 v3.1: Claude最终决策（全量实时数据）
        
        Claude拥有完整信息，可以决定：
        - EXECUTE_MARKET: 市价买入
        - EXECUTE_LIMIT: 限价买入（并给出价格）
        - ABANDON: 放弃信号
        """
        try:
            import anthropic
            
            # 构建prompt
            prompt = self._build_final_decision_prompt(signal, realtime, elapsed_minutes)
            
            client = anthropic.Anthropic(api_key=self.claude_api_key)
            message = client.messages.create(
                model=self.claude_model,
                max_tokens=1000,
                temperature=0.2,
                system="""你是加密货币交易执行专家。你的职责是决定如何执行已审核通过的信号。

重要：这个信号已经通过了初审和观察期硬规则检查，信号质量已被确认。
你的任务是根据当前市场状态决定最优执行方式，而不是重新审核信号质量。

倾向于执行，ABANDON仅用于市场已发生根本性变化的极端情况。""",
                messages=[{"role": "user", "content": prompt}]
            )
            
            content = message.content[0].text
            result = self._parse_json_response(content)
            
            if result:
                return result
            else:
                print(f"[CLAUDE_FINAL] ⚠️ 返回格式错误")
                return None
                
        except Exception as e:
            print(f"[CLAUDE_FINAL] ❌ 调用失败: {e}")
            return None

    def _build_final_decision_prompt(self, signal: Dict, realtime: Dict, elapsed_minutes: int) -> str:
        """
        🔥 v3.1: 构建Claude最终决策prompt（对比格式）
        """
        # 解析原始信号数据
        original_payload = json.loads(signal["original_payload"])
        metrics = original_payload.get("metrics", {})
        
        symbol = signal["symbol"]
        side = signal["side"]
        signal_type = signal.get("signal_type", "unknown")
        
        # 信号发现时的数据
        detected_price = signal["detected_price"]
        detected_rsi = signal["detected_rsi"]
        detected_volume = metrics.get("vol_spike_ratio", 1.0)
        detected_macd = metrics.get("macd_cross", "unknown")
        detected_adx = metrics.get("adx", 25)
        
        # 审核通过原因（从original_payload提取）
        pass_reason = original_payload.get("pass_reason", "")
        if not pass_reason:
            # 根据信号类型生成默认原因
            if signal_type == "trend_anticipation":
                pass_reason = "趋势预判信号，提前布局"
            elif signal_type == "trend_continuation":
                pass_reason = "趋势跟随信号，回调入场"
            elif detected_rsi < 30:
                pass_reason = f"RSI={detected_rsi:.1f}超卖 + 成交量{detected_volume:.1f}x"
            elif detected_rsi > 70:
                pass_reason = f"RSI={detected_rsi:.1f}超买 + 成交量{detected_volume:.1f}x"
            else:
                pass_reason = "技术指标达标"
        
        # 当前实时数据
        current_price = realtime["current_price"]
        current_rsi = realtime["rsi"]
        current_volume = realtime["volume_ratio"]
        current_macd = realtime["macd_cross"]
        current_adx = realtime["adx"]
        orderbook_score = realtime["orderbook_score"]
        btc_trend = realtime["btc_trend"]
        btc_change = realtime["btc_change_pct"]
        atr_pct = realtime["atr_pct"]
        
        # 计算变化
        price_change_pct = (current_price - detected_price) / detected_price * 100
        rsi_change = current_rsi - detected_rsi
        volume_change = current_volume - detected_volume
        
        # 订单簿描述
        if orderbook_score > 0.6:
            orderbook_desc = f"买盘强({orderbook_score:.0%})"
        elif orderbook_score < 0.4:
            orderbook_desc = f"卖盘强({1-orderbook_score:.0%})"
        else:
            orderbook_desc = "均衡"
        
        # 信号类型描述
        if signal_type == "trend_anticipation":
            type_desc = "🔮 趋势预判"
            strategy_hint = "预判信号建议分批建仓，可适当等回调；止损2%，止盈6-10%"
        elif signal_type == "trend_continuation":
            type_desc = "📈 趋势跟随"
            strategy_hint = "趋势跟随建议快速入场；止损2%，止盈6-8%"
        else:
            type_desc = "🔄 反转信号"
            strategy_hint = "反转信号可以等小回调，建议限价单；止损2-3%，止盈5-8%"
        
        # 根据变化给出分析
        changes_analysis = []
        if price_change_pct > 2:
            changes_analysis.append(f"⚠️ 价格已涨{price_change_pct:.1f}%，需谨慎追高")
        elif price_change_pct < -2:
            changes_analysis.append(f"⚠️ 价格已跌{price_change_pct:.1f}%，信号可能失效")
        
        if side == "long" and rsi_change > 10:
            changes_analysis.append(f"⚠️ RSI回升{rsi_change:.0f}点，超卖减弱")
        elif side == "short" and rsi_change < -10:
            changes_analysis.append(f"⚠️ RSI回落{abs(rsi_change):.0f}点，超买减弱")
        
        if btc_trend == "down" and side == "long":
            changes_analysis.append("⚠️ BTC下跌中，做多需谨慎")
        elif btc_trend == "up" and side == "short":
            changes_analysis.append("⚠️ BTC上涨中，做空需谨慎")
        
        changes_str = "\n".join(changes_analysis) if changes_analysis else "✅ 市场状态稳定，无明显异常"
        
        # 计算建议止损止盈（根据信号类型）
        if signal_type == "trend_anticipation":
            sl_pct = 2.0
            tp_pct = 6.0
        elif signal_type == "trend_continuation":
            sl_pct = 2.0
            tp_pct = 6.0
        else:  # reversal
            sl_pct = 2.5
            tp_pct = 6.0
        
        if side == "long":
            suggested_sl = current_price * (1 - sl_pct / 100)
            suggested_tp = current_price * (1 + tp_pct / 100)
        else:
            suggested_sl = current_price * (1 + sl_pct / 100)
            suggested_tp = current_price * (1 - tp_pct / 100)
        
        prompt = f"""## 🎯 你的角色
你是**入场定价专家**，负责确定最优入场价格。

⚠️ **重要背景**：这个信号已经通过了两轮严格审核：
1. ✅ DeepSeek初审（信号质量确认）
2. ✅ 硬规则观察期（{elapsed_minutes}分钟，市场状态确认）

**你的核心职责是定价**，不是重新审核信号。

---

## 📊 信号信息
- 币种: **{symbol}**
- 方向: **{side.upper()}**
- 类型: {type_desc}
- 已观察: {elapsed_minutes}分钟

---

## 📈 信号发现时（{elapsed_minutes}分钟前）
| 指标 | 数值 |
|-----|------|
| 价格 | ${detected_price:.6f} |
| RSI | {detected_rsi:.1f} |
| 成交量 | {detected_volume:.2f}x |
| MACD | {detected_macd} |
| ADX | {detected_adx:.1f} |

**通过原因**: {pass_reason}

---

## 📊 当前实时数据
| 指标 | 数值 | 变化 |
|-----|------|------|
| 价格 | ${current_price:.6f} | {price_change_pct:+.2f}% |
| RSI | {current_rsi:.1f} | {rsi_change:+.1f} |
| 成交量 | {current_volume:.2f}x | {volume_change:+.2f} |
| MACD | {current_macd} | - |
| ADX | {current_adx:.1f} | - |
| 订单簿 | {orderbook_desc} | - |
| BTC | {btc_trend} | {btc_change:+.2f}% |
| ATR% | {atr_pct:.2f}% | - |

---

## ⚠️ 变化分析
{changes_str}

---

## 💡 策略提示
{strategy_hint}

---

## 🤔 定价决策（🔥 核心原则：宁可错过，不可追高/追低）

### 选项1: EXECUTE_LIMIT（限价入场 - 首选！）
**这是默认选项**，除非有特殊理由，否则都应该用限价单等待更好价格。

🔥 **定价策略**：
- **做多时**：挂低于当前价 **0.3%-0.8%**
  - 如果刚涨过（价格变化>1%）：挂低 0.5%-0.8%，等回调
  - 如果横盘中（价格变化<0.5%）：挂低 0.3%-0.5%
  - 如果刚跌过（价格变化<-1%）：可以挂低 0.2%-0.3%，价格已较低
  
- **做空时**：挂高于当前价 **0.3%-0.8%**
  - 如果刚跌过（价格变化<-1%）：挂高 0.5%-0.8%，等反弹
  - 如果横盘中（价格变化<0.5%）：挂高 0.3%-0.5%
  - 如果刚涨过（价格变化>1%）：可以挂高 0.2%-0.3%，价格已较高

⚠️ **关键点**：
- 做多不追涨！如果价格已经涨了，要等回调
- 做空不追跌！如果价格已经跌了，要等反弹
- ATR%越大，偏移可以越大（高波动币更容易回调）

### 选项2: EXECUTE_MARKET（市价入场 - 谨慎使用）
**仅用于以下极端情况**：
- 价格正在快速突破关键位，错过就没机会
- 成交量暴增(>3x)，趋势极强，回调概率很低
- 订单簿极度不平衡(>70%)，价格可能快速单边移动

### 选项3: ABANDON（放弃）
**当市场已根本性变化时才放弃**：
- 价格已反向移动超过3%
- RSI已从超买/超卖区完全恢复（回到40-60区间）
- BTC出现剧烈反向波动（>2%且与信号方向相反）

---

## 📝 返回JSON格式
```json
{{
    "decision": "EXECUTE_LIMIT" / "EXECUTE_MARKET" / "ABANDON",
    "entry_price": 入场价(限价单：根据上述策略计算；市价单：填{current_price:.6f}),
    "stop_loss": 止损价(建议${suggested_sl:.6f}，即{sl_pct}%),
    "take_profit": 止盈价(建议${suggested_tp:.6f}，即{tp_pct}%),
    "reasoning": "20字以内理由"
}}
```

⚠️ **核心原则**：倾向于用限价单执行，等待更好价格入场！只返回JSON！
"""
        return prompt

    def _get_price_ai_source(self, signal_id: int) -> str:
        """
        根据信号ID决定使用哪个AI进行价格评估

        Args:
            signal_id: 信号ID

        Returns:
            "claude" 或 "deepseek"
        """
        if not self.price_ai_rotation or not self.deepseek_enabled:
            return "claude"

        # 奇数用Claude，偶数用DeepSeek
        if signal_id % 2 == 1:
            return "claude"
        else:
            return "deepseek"

    def _full_price_evaluation(self, signal: Dict, current_price: float, ai_source: str) -> Optional[Dict]:
        """
        🔥 v3.0 完整价格评估（Claude固定定价）
        
        - 反转信号：固定限价单
        - 突破信号：Claude决定订单类型

        Returns:
            价格评估结果 {
                "order_type": "market"/"limit",
                "entry_price": float,
                "sl_price": float,
                "tp_price": float
            }
        """
        # 解析原始payload
        original_payload = json.loads(signal["original_payload"])

        # 更新当前价格
        original_payload["price"] = current_price
        
        # 获取信号类型
        signal_type = signal.get("signal_type", "unknown")
        if signal_type == "unknown":
            signal_type = original_payload.get("signal_type", "reversal")
        
        side = signal.get("side", "long")
        
        # 🔥 v3.0: 根据信号类型决定订单处理方式
        if signal_type == "trend_anticipation":
            forced_order_type = "limit"  # 预判信号使用限价单
            default_sl_pct = 0.02
            default_tp_pct = 0.06
            type_desc = "趋势预判（限价单）"
        elif signal_type == "trend_continuation":
            forced_order_type = "limit"  # 跟随信号使用限价单
            default_sl_pct = 0.02
            default_tp_pct = 0.06
            type_desc = "趋势跟随（限价单）"
        else:  # reversal
            forced_order_type = "limit"  # 🔥 反转信号固定限价单
            default_sl_pct = 0.025
            default_tp_pct = 0.06
            type_desc = "反转信号（固定限价单）"

        print(f"[WATCH] 📊 Claude定价 | {type_desc}")

        # 🔥 固定使用Claude定价
        result = self._claude_price_review(original_payload, forced_order_type)

        if not result:
            print(f"[WATCH] ⚠️ Claude价格评估失败，使用默认值")
            return self._get_default_prices(signal, current_price, signal_type, 
                                           forced_order_type, default_sl_pct, default_tp_pct)

        # 提取价格信息
        order_type = result.get("order_type", "market")
        entry_price = result.get("entry_price", current_price)
        sl_price = result.get("stop_loss", 0)
        tp_price = result.get("take_profit", 0)
        reasoning = result.get("reasoning", "")
        
        # 🔥 反转信号强制限价单
        if forced_order_type:
            order_type = forced_order_type
        
        # 验证价格合理性
        if entry_price <= 0:
            entry_price = current_price
            
        if sl_price <= 0:
            # 使用默认止损
            if side == "long":
                sl_price = entry_price * (1 - default_sl_pct)
            else:
                sl_price = entry_price * (1 + default_sl_pct)
            print(f"[WATCH] ⚠️ AI未返回止损，使用默认{default_sl_pct*100:.1f}%")
                
        if tp_price <= 0:
            # 使用默认止盈
            if side == "long":
                tp_price = entry_price * (1 + default_tp_pct)
            else:
                tp_price = entry_price * (1 - default_tp_pct)
            print(f"[WATCH] ⚠️ AI未返回止盈，使用默认{default_tp_pct*100:.1f}%")

        # 计算实际止损止盈百分比（用于日志）
        if side == "long":
            actual_sl_pct = (entry_price - sl_price) / entry_price * 100
            actual_tp_pct = (tp_price - entry_price) / entry_price * 100
        else:
            actual_sl_pct = (sl_price - entry_price) / entry_price * 100
            actual_tp_pct = (entry_price - tp_price) / entry_price * 100

        print(f"[WATCH] ✅ {ai_source.upper()}: {order_type} @${entry_price:.6f}")
        print(f"[WATCH]    止损: ${sl_price:.6f} ({actual_sl_pct:.2f}%) | 止盈: ${tp_price:.6f} ({actual_tp_pct:.2f}%)")
        if reasoning:
            print(f"[WATCH]    理由: {reasoning}")

        return {
            "order_type": order_type,
            "entry_price": entry_price,
            "sl_price": sl_price,
            "tp_price": tp_price
        }
    
    def _get_default_prices(self, signal: Dict, current_price: float, signal_type: str,
                           forced_order_type: Optional[str], sl_pct: float, tp_pct: float) -> Dict:
        """获取默认价格（AI失败时的回退）"""
        side = signal.get("side", "long")
        
        if forced_order_type:
            order_type = forced_order_type
        else:
            order_type = "limit"  # 默认使用限价单
        
        entry_price = current_price
        
        # 🔥 v3.2优化：根据价格变化动态调整偏移
        if order_type == "limit":
            # 获取信号发现时的价格
            detected_price = signal.get("detected_price", current_price)
            price_change_pct = (current_price - detected_price) / detected_price * 100 if detected_price > 0 else 0
            
            if side == "long":
                # 做多：根据价格变化调整
                if price_change_pct > 1.0:
                    # 价格已经涨了>1%，等更大回调
                    offset = 0.006  # 0.6%
                elif price_change_pct < -1.0:
                    # 价格已经跌了>1%，可以少等
                    offset = 0.003  # 0.3%
                else:
                    # 横盘，正常等待
                    offset = 0.004  # 0.4%
                entry_price = current_price * (1 - offset)
            else:
                # 做空：根据价格变化调整
                if price_change_pct < -1.0:
                    # 价格已经跌了>1%，等更大反弹
                    offset = 0.006  # 0.6%
                elif price_change_pct > 1.0:
                    # 价格已经涨了>1%，可以少等
                    offset = 0.003  # 0.3%
                else:
                    # 横盘，正常等待
                    offset = 0.004  # 0.4%
                entry_price = current_price * (1 + offset)
        
        if side == "long":
            sl_price = entry_price * (1 - sl_pct)
            tp_price = entry_price * (1 + tp_pct)
        else:
            sl_price = entry_price * (1 + sl_pct)
            tp_price = entry_price * (1 - tp_pct)
        
        return {
            "order_type": order_type,
            "entry_price": entry_price,
            "sl_price": sl_price,
            "tp_price": tp_price
        }

    def _claude_price_review(self, payload: Dict, forced_order_type: Optional[str] = None) -> Optional[Dict]:
        """
        🔥 v3.0 Claude价格评估
        
        Args:
            payload: 信号数据
            forced_order_type: 强制订单类型（反转信号固定"limit"）
        """
        try:
            import anthropic
            
            prompt = self._build_price_evaluation_prompt(payload, forced_order_type)
            
            client = anthropic.Anthropic(api_key=self.claude_api_key)
            message = client.messages.create(
                model=self.claude_model,
                max_tokens=800,
                temperature=0.3,
                system="你是专业的加密货币交易入场价格评估专家。根据市场状况给出最优入场价、止损和止盈建议。",
                messages=[{"role": "user", "content": prompt}]
            )
            
            content = message.content[0].text
            result = self._parse_json_response(content)
            
            if result:
                result["approved"] = True  # 价格评估默认通过
                result["_source"] = "claude"
                return result
            else:
                print(f"[WATCH] ⚠️ Claude返回格式错误")
                return None
                
        except Exception as e:
            print(f"[WATCH] ⚠️ Claude价格评估失败: {e}")
            return None

    def _deepseek_price_review(self, payload: Dict) -> Optional[Dict]:
        """
        DeepSeek价格评估（使用专门的价格评估prompt）
        """
        if not self.deepseek_enabled:
            print(f"[WATCH] ⚠️ DeepSeek未启用")
            return None

        try:
            import requests
            
            prompt = self._build_price_evaluation_prompt(payload)
            
            headers = {
                "Authorization": f"Bearer {self.deepseek_api_key}",
                "Content-Type": "application/json"
            }
            
            data = {
                "model": self.deepseek_model,
                "messages": [
                    {"role": "system", "content": "你是专业的加密货币交易入场价格评估专家。根据市场状况给出最优入场价、止损和止盈建议。"},
                    {"role": "user", "content": prompt}
                ],
                "temperature": 0.3,
                "max_tokens": 800
            }
            
            response = requests.post(
                f"{self.deepseek_base_url}/chat/completions",
                headers=headers,
                json=data,
                timeout=30
            )
            
            response.raise_for_status()
            result_data = response.json()
            content = result_data["choices"][0]["message"]["content"]
            
            result = self._parse_json_response(content)
            
            if result:
                result["approved"] = True
                result["_source"] = "deepseek"
                return result
            else:
                print(f"[WATCH] ⚠️ DeepSeek返回格式错误")
                return None
                
        except Exception as e:
            print(f"[WATCH] ⚠️ DeepSeek价格评估失败: {e}")
            return None

    def _build_price_evaluation_prompt(self, payload: Dict, forced_order_type: Optional[str] = None) -> str:
        """
        🔥 v3.0 构建价格评估专用prompt
        
        Args:
            payload: 信号数据
            forced_order_type: 强制订单类型（反转信号固定"limit"）
        """
        m = payload.get("metrics", {}) or {}
        stops = payload.get("calculated_stops", {}) or {}
        
        symbol = payload.get("symbol", "UNKNOWN")
        side = payload.get("bias", "long")
        price = payload.get("price", 0)
        signal_type = payload.get("signal_type", "unknown")
        
        # 技术指标
        rsi = m.get("rsi", 50)
        adx = m.get("adx", 25)
        atr = m.get("atr", 0)
        atr_pct = (atr / price * 100) if price > 0 else 2.0
        vol_ratio = m.get("volume_spike", 1.0)
        
        # BTC状态
        btc_status = payload.get("btc_status", {})
        btc_trend = btc_status.get("trend", "unknown")
        
        # 🔥 根据信号类型构建提示
        if signal_type == "trend_anticipation":
            type_desc = "🔮 趋势预判（提前布局）"
            order_type_hint = f"""
### ⚠️ 订单类型：限价单（强制）
预判信号使用限价单等待更好入场点。
- 做{side.upper()}时，限价单应挂在当前价{'下方' if side == 'long' else '上方'}
- 建议偏移0.2%-0.5%等回调
"""
            suggested_sl_pct = 2.0
            suggested_tp_pct = 6.0
        elif signal_type == "trend_continuation":
            type_desc = "📈 趋势跟随（回调入场）"
            order_type_hint = f"""
### ⚠️ 订单类型：限价单（强制）
趋势跟随信号使用限价单在回调时入场。
- 做{side.upper()}时，限价单应挂在当前价{'下方' if side == 'long' else '上方'}
- 建议偏移0.2%-0.5%
"""
            suggested_sl_pct = 2.0
            suggested_tp_pct = 6.0
        else:  # reversal
            type_desc = "🔄 反转信号（抄底/摸顶）"
            order_type_hint = f"""
### ⚠️ 订单类型：限价单（强制）
反转信号必须用限价单等待回调入场。
- 做{side.upper()}时，限价单应挂在当前价{'下方' if side == 'long' else '上方'}
- 建议偏移0.2%-0.5%等回调
"""
            suggested_sl_pct = 2.5
            suggested_tp_pct = 6.0
        
        # 计算建议的止损止盈价格
        if side == "long":
            suggested_sl_price = price * (1 - suggested_sl_pct / 100)
            suggested_tp_price = price * (1 + suggested_tp_pct / 100)
        else:
            suggested_sl_price = price * (1 + suggested_sl_pct / 100)
            suggested_tp_price = price * (1 - suggested_tp_pct / 100)
        
        prompt = f"""## 入场价格评估

### 信号信息
- 币种: {symbol}
- 方向: **{side.upper()}**
- 当前价: ${price:.6f}
- 类型: {type_desc}

{order_type_hint}

### 技术指标
- RSI: {rsi:.1f} | ADX: {adx:.1f}
- ATR%: {atr_pct:.2f}% | 成交量: {vol_ratio:.2f}x
- BTC趋势: {btc_trend}

### 建议参数
- 止损: {suggested_sl_pct:.1f}% → ${suggested_sl_price:.6f}
- 止盈: {suggested_tp_pct:.1f}% → ${suggested_tp_price:.6f}

### 返回JSON:
```json
{{
    "order_type": "market"/"limit",
    "entry_price": 入场价(数字),
    "stop_loss": 止损价(数字),
    "take_profit": 止盈价(数字),
    "reasoning": "10字以内理由"
}}
```

只返回JSON！
"""
        return prompt

    def _parse_json_response(self, content: str) -> Optional[Dict]:
        """从AI响应中提取JSON"""
        import json
        import re
        
        try:
            return json.loads(content)
        except:
            pass
        
        # 尝试提取```json```块
        json_match = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', content)
        if json_match:
            try:
                return json.loads(json_match.group(1))
            except:
                pass
        
        # 尝试提取{...}
        brace_match = re.search(r'\{[\s\S]*\}', content)
        if brace_match:
            try:
                return json.loads(brace_match.group(0))
            except:
                pass
        
        return None

    def _trigger_signal(
        self,
        signal: Dict,
        price_result: Dict,
        entry_ai_source: str,
        timing_ai_source: str,
        cursor
    ):
        """
        🔥 修复版: 触发信号，写入pushed_signals表供AutoTrader执行
        
        v2.1 修复:
        1. 添加表结构检查和自动修复（解决entry_price列缺失问题）
        2. 添加15分钟内信号重复检查
        """
        symbol = signal["symbol"]
        side = signal["side"]
        signal_id = signal["id"]

        entry_price = price_result.get("entry_price", signal["detected_price"])
        order_type = price_result.get("order_type", "market")
        # 🔥🔥🔥 v3.2修复: Claude返回的是stop_loss/take_profit，需要兼容两种字段名
        sl_price = price_result.get("sl_price") or price_result.get("stop_loss", 0)
        tp_price = price_result.get("tp_price") or price_result.get("take_profit", 0)
        
        # 🔥 如果仍然没有止损止盈，使用默认值
        if not sl_price or sl_price <= 0:
            side = signal.get("side", "long")
            signal_type = signal.get("signal_type", "reversal")
            default_sl_pct = 0.025 if signal_type == "reversal" else 0.02
            if side == "long":
                sl_price = entry_price * (1 - default_sl_pct)
            else:
                sl_price = entry_price * (1 + default_sl_pct)
            print(f"[WATCH] ⚠️ 止损为空，使用默认{default_sl_pct*100:.1f}%: ${sl_price:.6f}")
        
        if not tp_price or tp_price <= 0:
            side = signal.get("side", "long")
            default_tp_pct = 0.06
            if side == "long":
                tp_price = entry_price * (1 + default_tp_pct)
            else:
                tp_price = entry_price * (1 - default_tp_pct)
            print(f"[WATCH] ⚠️ 止盈为空，使用默认{default_tp_pct*100:.1f}%: ${tp_price:.6f}")
        
        # 🔥🔥🔥 写入pushed_signals表（在signals.db中）
        try:
            # 获取signals.db路径
            signals_db = self.full_config.get("analytics", {}).get("storage", {}).get("path", "./signals.db")
            
            conn_signals = sqlite3.connect(signals_db, timeout=30)
            conn_signals.execute("PRAGMA journal_mode=WAL")
            cur_signals = conn_signals.cursor()
            
            # 🔥 步骤1: 确保表存在（完整结构）
            cur_signals.execute("""
                CREATE TABLE IF NOT EXISTS pushed_signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    entry_price_immediate REAL,
                    entry_price REAL,
                    sl_price REAL,
                    tp_price REAL,
                    rsi REAL,
                    adx REAL,
                    score REAL,
                    entry_ai_source TEXT,
                    timing_ai_source TEXT,
                    order_type TEXT,
                    ai_decision TEXT DEFAULT 'approved',
                    auto_traded INTEGER DEFAULT 0,
                    auto_trade_order_id TEXT,
                    auto_trade_time TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # 🔥 步骤2: 检查并添加缺失的列（关键修复！）
            cur_signals.execute("PRAGMA table_info(pushed_signals)")
            existing_columns = {row[1] for row in cur_signals.fetchall()}
            
            required_columns = [
                ('entry_price', 'REAL'),
                ('entry_price_immediate', 'REAL'),
                ('sl_price', 'REAL'),
                ('tp_price', 'REAL'),
                ('rsi', 'REAL'),
                ('adx', 'REAL'),
                ('score', 'REAL'),
                ('entry_ai_source', 'TEXT'),
                ('timing_ai_source', 'TEXT'),
                ('order_type', 'TEXT'),
                ('ai_decision', 'TEXT'),
                ('auto_traded', 'INTEGER'),
                ('auto_trade_order_id', 'TEXT'),
                ('auto_trade_time', 'TEXT'),
                ('created_at', 'TEXT')
            ]
            
            for col_name, col_type in required_columns:
                if col_name not in existing_columns:
                    try:
                        cur_signals.execute(f"ALTER TABLE pushed_signals ADD COLUMN {col_name} {col_type}")
                        print(f"[WATCH] 🔧 自动添加列: {col_name}")
                    except Exception as alter_err:
                        if "duplicate" not in str(alter_err).lower():
                            print(f"[WATCH] ⚠️ 添加列失败 {col_name}: {alter_err}")
            
            # 🔥 步骤3: 检查是否有近期重复信号（15分钟内同币种同方向）
            cur_signals.execute("""
                SELECT COUNT(*) FROM pushed_signals
                WHERE symbol = ? AND side = ?
                AND created_at >= datetime('now', '-15 minutes')
                AND (auto_traded = 0 OR auto_traded IS NULL)
            """, (symbol, side))
            
            duplicate_count = cur_signals.fetchone()[0]
            if duplicate_count > 0:
                print(f"[WATCH] ⏭️ 跳过重复信号: {symbol} {side} (15分钟内已有{duplicate_count}个待执行)")
                conn_signals.close()
                # 仍然更新观察队列状态为已处理
                cursor.execute("""
                    UPDATE watch_signals
                    SET status = 'duplicate_skipped',
                        trigger_reason = ?
                    WHERE id = ?
                """, (f"15min内重复x{duplicate_count}", signal_id))
                return
            
            # 🔥 步骤4: 写入信号
            cur_signals.execute("""
                INSERT INTO pushed_signals
                (symbol, side, entry_price_immediate, entry_price, sl_price, tp_price, 
                 rsi, adx, score, entry_ai_source, timing_ai_source, order_type, ai_decision, auto_traded)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'approved', 0)
            """, (
                symbol,
                side,
                entry_price,
                entry_price,
                sl_price,
                tp_price,
                signal.get("detected_rsi", 50),
                signal.get("detected_adx", 25),
                0.85,
                entry_ai_source,
                timing_ai_source,
                order_type
            ))
            
            conn_signals.commit()
            pushed_signal_id = cur_signals.lastrowid
            conn_signals.close()
            
            print(f"[WATCH] ✅ 信号已写入pushed_signals (ID={pushed_signal_id})")
            print(f"[WATCH]    等待AutoTrader执行下单...")
            
        except Exception as e:
            print(f"[WATCH] ❌ 写入pushed_signals失败: {e}")
            import traceback
            traceback.print_exc()
            return

        # 更新观察队列状态
        cursor.execute("""
            UPDATE watch_signals
            SET status = 'triggered',
                triggered_time = datetime('now'),
                triggered_price = ?,
                trigger_reason = ?
            WHERE id = ?
        """, (entry_price, f"{timing_ai_source}_timing_{entry_ai_source}_price", signal_id))

        print(f"[WATCH] ✅ {symbol} {side} 触发入场")
        print(f"[WATCH]    时机: {timing_ai_source} | 价格: {entry_ai_source} | 订单: {order_type} @${entry_price:.6f}")

        # 🔥🔥🔥 发送Telegram推送
        try:
            detected_price = signal["detected_price"]
            price_change = ((entry_price - detected_price) / detected_price) * 100
            
            msg_lines = [
                "",
                f"🎯 **AI确定入场价，等待AutoTrader下单**",
                "",
                f"💰 入场价: `${entry_price:.6f}`",
                f"📊 相比发现时: `{price_change:+.2f}%`",
                "",
                f"🛡 止损: `${sl_price:.6f}`",
                f"🎯 止盈: `${tp_price:.6f}`",
                "",
                f"⏱ 时机判断: {timing_ai_source.upper()}",
                f"💵 价格评估: {entry_ai_source.upper()}",
                f"📝 订单类型: {order_type.upper()}"
            ]
            
            title = f"✅ 触发入场 | {symbol} {side.upper()}"
            tg_send(self.full_config, title, msg_lines)
        except Exception as e:
            print(f"[WATCH] ⚠️ 推送失败: {e}")

    def _handle_expired_signal(self, signal: Dict, cursor):
        """处理过期信号"""
        symbol = signal["symbol"]
        side = signal["side"]
        signal_type = signal.get("signal_type", "unknown")
        detected_rsi = signal.get("detected_rsi", 50)

        cursor.execute("""
            UPDATE watch_signals
            SET status = 'expired'
            WHERE id = ?
        """, (signal["id"],))

        # 🔥🔥🔥 v5.1: 根据信号类型获取实际过期时间（分钟）
        actual_expire_minutes = self._get_effective_expire_minutes(signal_type, side, detected_rsi)
        
        print(f"[WATCH] ⏱️ {symbol} {side} 已过期（{actual_expire_minutes}分钟无合适时机）")

        # 🔥🔥🔥 发送Telegram推送
        try:
            detected_price = signal["detected_price"]
            
            msg_lines = [
                "",
                f"⏱️ **观察期已过期**",
                "",
                f"💰 发现时价格: `${detected_price:.6f}`",
                f"⏳ 观察时长: {actual_expire_minutes}分钟",
                "",
                f"💡 未找到合适入场时机，信号作废"
            ]
            
            title = f"⏰ 信号过期 | {symbol} {side.upper()}"
            tg_send(self.full_config, title, msg_lines)
        except Exception as e:
            print(f"[WATCH] ⚠️ 推送失败: {e}")
    
    def _get_effective_expire_minutes(self, signal_type: str, side: str, detected_rsi: float) -> int:
        """
        🔥 v5.1: 根据信号类型获取实际过期时间（分钟）
        
        修复显示bug：之前显示480分钟是因为用了秒数而不是分钟数
        """
        if signal_type == "trend_anticipation":
            return self.trend_anti_expire_minutes
        elif signal_type == "trend_continuation":
            return self.trend_cont_expire_minutes
        else:
            # 反转信号：根据RSI极端程度分级
            is_extreme = False
            if side == "long" and detected_rsi <= self.extreme_rsi_long_threshold:
                is_extreme = True
            elif side == "short" and detected_rsi >= self.extreme_rsi_short_threshold:
                is_extreme = True
            
            if is_extreme:
                return self.extreme_expire_minutes
            else:
                return self.normal_expire_minutes

    def _handle_abandoned_signal(self, signal: Dict, reason: str, cursor):
        """处理AI中途放弃的信号"""
        symbol = signal["symbol"]
        side = signal["side"]

        cursor.execute("""
            UPDATE watch_signals
            SET status = 'abandoned',
                trigger_reason = ?
            WHERE id = ?
        """, (reason, signal["id"]))

        print(f"[WATCH] ❌ {symbol} {side} AI放弃（{reason}）")

        # 🔥🔥🔥 发送Telegram推送
        try:
            detected_price = signal["detected_price"]
            
            msg_lines = [
                "",
                f"❌ **AI决定放弃**",
                "",
                f"💰 发现时价格: `${detected_price:.6f}`",
                f"📊 原因: 市场条件变化",
                "",
                f"💡 AI判断入场时机已过或风险增大"
            ]
            
            title = f"🚫 放弃信号 | {symbol} {side.upper()}"
            tg_send(self.full_config, title, msg_lines)
        except Exception as e:
            print(f"[WATCH] ⚠️ 推送失败: {e}")

    def _is_expired(self, signal: Dict) -> bool:
        """
        🔥 v5.0: 检查信号是否过期（分级观察期）
        """
        expire_time_str = signal.get("expire_time")
        signal_type = signal.get("signal_type", "unknown")
        detected_rsi = signal.get("detected_rsi", 50)
        side = signal.get("side", "long")
        
        # 🔥🔥🔥 根据信号类型确定观察期
        if signal_type == "trend_anticipation":
            effective_expire_minutes = self.trend_anti_expire_minutes
        elif signal_type == "trend_continuation":
            effective_expire_minutes = self.trend_cont_expire_minutes
        else:
            # 反转信号：根据RSI极端程度分级
            is_extreme = False
            if side == "long" and detected_rsi <= self.extreme_rsi_long_threshold:
                is_extreme = True
            elif side == "short" and detected_rsi >= self.extreme_rsi_short_threshold:
                is_extreme = True
            
            if is_extreme:
                effective_expire_minutes = self.extreme_expire_minutes
            else:
                effective_expire_minutes = self.normal_expire_minutes
        
        # 如果没有expire_time，用created_at计算
        if not expire_time_str:
            created_at_str = signal.get("created_at")
            if created_at_str:
                try:
                    created_at = datetime.fromisoformat(created_at_str)
                    # 🔥🔥 修复v5.1.1：SQLite CURRENT_TIMESTAMP是UTC，需要用UTC比较
                    now_utc = datetime.utcnow()
                    elapsed_minutes = (now_utc - created_at).total_seconds() / 60
                    if elapsed_minutes > effective_expire_minutes:
                        print(f"[WATCH] ⚠️ {signal_type}信号已超时{elapsed_minutes:.0f}分钟（限制{effective_expire_minutes}分钟）")
                        return True
                except:
                    pass
            return False

        try:
            expire_time = datetime.fromisoformat(expire_time_str)
            # 额外检查：即使没到expire_time，超过限制也过期
            created_at_str = signal.get("created_at")
            if created_at_str:
                created_at = datetime.fromisoformat(created_at_str)
                # 🔥🔥 修复v5.1.1：SQLite CURRENT_TIMESTAMP是UTC，需要用UTC比较
                now_utc = datetime.utcnow()
                elapsed_minutes = (now_utc - created_at).total_seconds() / 60
                if elapsed_minutes > effective_expire_minutes:
                    print(f"[WATCH] ⚠️ {signal_type}信号超过{effective_expire_minutes}分钟（{elapsed_minutes:.0f}分钟），强制过期")
                    return True
            # 🔥 expire_time也需要用UTC比较
            return datetime.utcnow() > expire_time
        except:
            return False

    def _get_current_price(self, symbol: str) -> float:
        """获取当前价格"""
        ticker = self.exchange.fetch_ticker(symbol)
        return float(ticker["last"])

    def _get_current_rsi(self, symbol: str, period: int = 14) -> float:
        """
        获取当前实时RSI

        Args:
            symbol: 交易对
            period: RSI周期（默认14）

        Returns:
            当前RSI值
        """
        try:
            # 获取K线数据（需要足够的数据计算RSI）
            ohlcv = self.exchange.fetch_ohlcv(symbol, '1m', limit=100)
            if not ohlcv or len(ohlcv) < 60:
                return 50.0  # 数据不足，返回默认值

            # 转换为DataFrame
            df = pd.DataFrame(ohlcv, columns=["ts", "open", "high", "low", "close", "volume"])
            df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)

            # 计算RSI
            rsi_val = float(rsi(df, period).iloc[-1])
            return rsi_val

        except Exception as e:
            print(f"[WATCHER] ⚠️ 获取实时RSI失败: {e}")
            return 50.0  # 失败时返回默认值

    def get_watching_signals(self) -> List[Dict]:
        """获取当前观察中的信号"""
        try:
            conn = sqlite3.connect(self.db_path, timeout=30); conn.execute("PRAGMA journal_mode=WAL")
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()

            cur.execute("""
                SELECT * FROM watch_signals
                WHERE status = 'watching'
                ORDER BY created_at DESC
            """)

            signals = [dict(row) for row in cur.fetchall()]
            conn.close()

            return signals

        except Exception as e:
            print(f"[WATCHER_ERR] 获取观察信号失败: {e}")
            return []

    def get_stats(self) -> Dict:
        """获取统计信息"""
        try:
            conn = sqlite3.connect(self.db_path, timeout=30); conn.execute("PRAGMA journal_mode=WAL")
            cur = conn.cursor()

            stats = {}

            # 观察中的信号数量
            cur.execute("SELECT COUNT(*) FROM watch_signals WHERE status = 'watching'")
            stats["watching"] = cur.fetchone()[0]

            # 已触发的信号数量
            cur.execute("SELECT COUNT(*) FROM watch_signals WHERE status = 'triggered'")
            stats["triggered"] = cur.fetchone()[0]

            # 已过期的信号数量
            cur.execute("SELECT COUNT(*) FROM watch_signals WHERE status = 'expired'")
            stats["expired"] = cur.fetchone()[0]

            # 已放弃的信号数量
            cur.execute("SELECT COUNT(*) FROM watch_signals WHERE status = 'abandoned'")
            stats["abandoned"] = cur.fetchone()[0]

            # 触发成功率
            total_finished = stats["triggered"] + stats["expired"] + stats["abandoned"]
            if total_finished > 0:
                stats["trigger_rate"] = stats["triggered"] / total_finished
            else:
                stats["trigger_rate"] = 0.0

            conn.close()

            return stats

        except Exception as e:
            print(f"[WATCHER_ERR] 获取统计失败: {e}")
            return {}