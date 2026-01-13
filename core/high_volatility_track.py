# core/high_volatility_track.py - 高波动轨道 v2.1 趋势融合版
# -*- coding: utf-8 -*-
"""
高波动轨道 - 蓄势预判自动交易系统

🔥🔥🔥 v2.1 重大更新（趋势融合版）:
1. 集成趋势预判分析 - FDI分形维数/聪明钱/效率比
2. AI审核前自动获取趋势上下文
3. 成为系统唯一信号入口（禁用majors/anomaly/accum）
4. AI prompt增强 - 加入趋势分析指标

🔥🔥🔥 v2.0 更新（真假突破识别版）:
1. 新增CVD背离检测 - 识别假突破，避免被套
2. 新增Efficiency Ratio - 评估趋势纯度
3. 新增Hurst指数 - 判断趋势持续性
4. AI审核新增三大核心指标，显著提高胜率
5. 硬规则新增假突破快速过滤

🔥 v1.4 更新（持仓同步版）:
1. 启动时自动同步OKX实际持仓
2. 清理本地记录中已不存在的持仓
3. 避免"显示持仓1个实际没有"的问题

核心理念：
- 扫描24h涨跌8-40%的高波动币种
- 识别蓄势特征，提前限价布局
- 全自动挂单、止损、止盈

流程：
扫描 → 硬规则筛选 → 观察池 → 就绪评分 → 趋势分析 → AI预判 → 自动挂限价单

版本：v2.1
"""

import sqlite3
import json
import time
import numpy as np
import pandas as pd
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Tuple, Optional, Any
from dataclasses import dataclass, field, asdict
from enum import Enum
import threading
import requests

# 🔥🔥🔥 v2.1: 导入趋势分析模块
try:
    from .trend_anticipation import analyze_trend_context, get_trend_context_for_ai
    HAS_TREND_ANALYSIS = True
    print("[HIGH_VOL] ✅ 趋势分析模块已加载 (v2.1)")
except ImportError:
    try:
        # 尝试直接导入（非包模式）
        from trend_anticipation import analyze_trend_context, get_trend_context_for_ai
        HAS_TREND_ANALYSIS = True
        print("[HIGH_VOL] ✅ 趋势分析模块已加载 (直接导入)")
    except ImportError:
        HAS_TREND_ANALYSIS = False
        print("[HIGH_VOL] ⚠️ 趋势分析模块未找到，AI审核将不包含趋势上下文")

# 🔥 v2.0: 导入新指标函数
try:
    from .utils import (
        calculate_cvd, cvd_divergence, 
        efficiency_ratio, efficiency_ratio_trend,
        hurst_exponent, hurst_analysis,
        breakout_quality_score
    )
    HAS_NEW_INDICATORS = True
except ImportError:
    HAS_NEW_INDICATORS = False
    print("[HIGH_VOL] ⚠️ 新指标函数未找到，使用内置版本")


# ==================== 常量与枚举 ====================

class SignalStatus(Enum):
    """信号状态"""
    WATCHING = "watching"          # 在观察池中
    READY = "ready"                # 就绪，等待AI决策
    LIMIT_PLACED = "limit_placed"  # 已挂限价单
    FILLED = "filled"              # 已成交
    EXPIRED = "expired"            # 过期未成交
    ABANDONED = "abandoned"        # 放弃
    STOPPED = "stopped"            # 止损出局
    PROFIT = "profit"              # 止盈出局
    TIMEOUT = "timeout"            # 超时平仓


class Track(Enum):
    """轨道标识"""
    NORMAL = 1       # 常规轨道（反转+趋势预判）
    HIGH_VOL = 2     # 高波动轨道


# ==================== 数据结构 ====================

@dataclass
class HighVolSignal:
    """高波动信号"""
    id: str
    symbol: str
    track: int = 2
    signal_type: str = "high_vol_accumulation"
    
    # 价格信息
    signal_price: float = 0.0
    entry_price: float = 0.0
    stop_loss: float = 0.0
    take_profit: float = 0.0
    side: str = "long"
    
    # 波动率信息
    change_24h: float = 0.0
    volume_24h: float = 0.0
    atr_pct: float = 0.0
    
    # 就绪评分
    readiness_score: int = 0
    readiness_details: List[str] = field(default_factory=list)
    
    # BTC相关性
    btc_correlation: float = 0.0
    btc_trend: str = "neutral"
    
    # 状态跟踪
    status: str = "watching"
    ai_reviews: int = 0
    limit_order_id: str = ""
    
    # 时间戳
    created_at: str = ""
    updated_at: str = ""
    filled_at: str = ""
    
    # 🔥 v3.3新增: 健康度追踪
    health_score: int = 100  # 健康度 0-100，低于阈值淘汰
    peak_readiness: int = 0  # 历史最高就绪分
    bb_trend: str = "neutral"  # 布林带趋势: squeezing/expanding/neutral
    vol_trend: str = "neutral"  # 成交量趋势: rising/falling/neutral
    momentum_trend: str = "neutral"  # 动量趋势: bullish/bearish/neutral
    warning_count: int = 0  # 警告次数
    last_warning: str = ""  # 最后警告原因
    
    # 🔥 v2.0新增: 突破质量指标
    cvd_divergence: str = "none"  # CVD背离: bullish/bearish/none
    cvd_score: float = 50.0  # CVD信号质量 0-100
    efficiency_ratio: float = 0.5  # 效率比 0-1
    hurst_value: float = 0.5  # Hurst指数 0-1
    breakout_quality: float = 50.0  # 综合突破质量 0-100
    is_fake_breakout: bool = False  # 是否假突破
    
    # 持仓信息（成交后）
    position_size: float = 0.0
    current_pnl: float = 0.0
    
    def to_dict(self) -> Dict:
        return asdict(self)


# ==================== 高波动轨道主类 ====================

class HighVolatilityTrack:
    """
    高波动轨道管理器
    
    职责：
    1. 扫描高波动币种
    2. 管理观察池
    3. 计算就绪评分
    4. 触发AI决策
    5. 管理限价单
    6. 持仓监控
    """
    
    def __init__(self, config: Dict, exchange, auto_trader, db_path: str = "data/high_vol_track.db"):
        """
        初始化高波动轨道
        
        Args:
            config: 配置字典
            exchange: ccxt交易所实例
            auto_trader: AutoTrader实例（用于下单）
            db_path: 数据库路径
        """
        self.config = config
        self.exchange = exchange
        self.auto_trader = auto_trader
        self.db_path = db_path
        
        # 轨道配置
        track_cfg = config.get("high_volatility_track", {})
        self.enabled = track_cfg.get("enabled", True)
        
        # 扫描配置
        scan_cfg = track_cfg.get("scan", {})
        self.scan_interval_sec = scan_cfg.get("interval_sec", 60)
        self.min_change_24h = scan_cfg.get("min_change_24h", 0.08)
        self.max_change_24h = scan_cfg.get("max_change_24h", 0.40)
        self.min_volume_24h = scan_cfg.get("min_volume_24h", 2_000_000)
        
        # 观察池配置
        pool_cfg = track_cfg.get("observation_pool", {})
        self.pool_capacity = pool_cfg.get("capacity", 10)
        self.pool_max_time_min = pool_cfg.get("max_time_min", 30)
        self.readiness_threshold = pool_cfg.get("readiness_threshold", 75)
        
        # 🔥 v3.3新增: 健康度淘汰机制
        self.health_threshold = pool_cfg.get("health_threshold", 40)  # 健康度低于40淘汰
        self.health_check_interval_min = pool_cfg.get("health_check_interval_min", 2)  # 每2分钟检查一次
        
        # 挂单配置
        order_cfg = track_cfg.get("limit_order", {})
        self.max_concurrent_orders = order_cfg.get("max_concurrent", 3)
        self.order_valid_sec = order_cfg.get("valid_sec", 300)  # 5分钟
        self.max_ai_reviews = order_cfg.get("max_ai_reviews", 3)
        
        # 止损配置
        sl_cfg = track_cfg.get("stop_loss", {})
        self.sl_atr_multipliers = {
            0.03: sl_cfg.get("atr_lt_3", 0.012),
            0.05: sl_cfg.get("atr_3_5", 0.015),
            0.08: sl_cfg.get("atr_5_8", 0.018),
            999: sl_cfg.get("atr_gt_8", 0.020),
        }
        self.sl_max = sl_cfg.get("max", 0.02)
        
        # 资金配置
        capital_cfg = track_cfg.get("capital", {})
        self.track_capital_pct = capital_cfg.get("track_pct", 0.30)
        self.single_position_pct = capital_cfg.get("single_pct", 0.10)
        self.high_vol_reduce = capital_cfg.get("high_vol_reduce", 0.5)  # 20-40%波动减仓
        
        # 持仓配置
        position_cfg = track_cfg.get("position", {})
        self.max_hold_hours = position_cfg.get("max_hold_hours", 2)
        
        # AI配置
        ai_cfg = config.get("deepseek", {})
        self.ai_api_key = ai_cfg.get("api_key", "")
        self.ai_base_url = ai_cfg.get("base_url", "https://api.deepseek.com")
        self.ai_model = ai_cfg.get("model", "deepseek-chat")
        self.ai_timeout = ai_cfg.get("timeout", 30)
        
        # Telegram配置
        tg_cfg = config.get("telegram", {})
        self.tg_bot_token = tg_cfg.get("bot_token", "")
        self.tg_chat_ids = tg_cfg.get("chat_id", [])
        
        # 观察池（内存）
        self.observation_pool: Dict[str, HighVolSignal] = {}
        
        # 活跃限价单（内存）
        self.active_orders: Dict[str, HighVolSignal] = {}
        
        # 活跃持仓（内存）
        self.active_positions: Dict[str, HighVolSignal] = {}
        
        # 锁
        self._lock = threading.Lock()
        
        # 初始化数据库
        self._init_database()
        
        # 加载未完成的信号
        self._load_pending_signals()
        
        print(f"[HIGH_VOL] 高波动轨道初始化完成")
        print(f"  扫描: 24h波动{self.min_change_24h*100:.0f}%-{self.max_change_24h*100:.0f}%, 成交量>{self.min_volume_24h/1e6:.0f}M")
        print(f"  观察池: 容量{self.pool_capacity}, 最长{self.pool_max_time_min}分钟, 就绪阈值{self.readiness_threshold}分")
        print(f"  挂单: 最多{self.max_concurrent_orders}个, 有效{self.order_valid_sec//60}分钟, AI重评{self.max_ai_reviews}次")
        print(f"  止损: 动态1.2-2%, 上限{self.sl_max*100:.1f}%")
        print(f"  资金: 轨道占比{self.track_capital_pct*100:.0f}%, 单笔{self.single_position_pct*100:.0f}%")
    
    # ==================== 数据库 ====================
    
    def _init_database(self):
        """初始化数据库"""
        import os
        os.makedirs(os.path.dirname(self.db_path) if os.path.dirname(self.db_path) else "data", exist_ok=True)
        
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        
        conn.execute("""
            CREATE TABLE IF NOT EXISTS high_vol_signals (
                id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                track INTEGER DEFAULT 2,
                signal_type TEXT DEFAULT 'high_vol_accumulation',
                
                signal_price REAL,
                entry_price REAL,
                stop_loss REAL,
                take_profit REAL,
                side TEXT,
                
                change_24h REAL,
                volume_24h REAL,
                atr_pct REAL,
                
                readiness_score INTEGER,
                readiness_details TEXT,
                
                btc_correlation REAL,
                btc_trend TEXT,
                
                status TEXT,
                ai_reviews INTEGER DEFAULT 0,
                limit_order_id TEXT,
                
                created_at TEXT,
                updated_at TEXT,
                filled_at TEXT,
                
                position_size REAL,
                current_pnl REAL,
                
                ai_reasoning TEXT,
                
                UNIQUE(symbol, created_at)
            )
        """)
        
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_high_vol_status ON high_vol_signals(status)
        """)
        
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_high_vol_symbol ON high_vol_signals(symbol)
        """)
        
        conn.commit()
        conn.close()
    
    def _save_signal(self, signal: HighVolSignal):
        """保存信号到数据库"""
        conn = sqlite3.connect(self.db_path, timeout=30)
        
        conn.execute("""
            INSERT OR REPLACE INTO high_vol_signals
            (id, symbol, track, signal_type, signal_price, entry_price, stop_loss, take_profit, side,
             change_24h, volume_24h, atr_pct, readiness_score, readiness_details, btc_correlation, btc_trend,
             status, ai_reviews, limit_order_id, created_at, updated_at, filled_at, position_size, current_pnl)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            signal.id, signal.symbol, signal.track, signal.signal_type,
            signal.signal_price, signal.entry_price, signal.stop_loss, signal.take_profit, signal.side,
            signal.change_24h, signal.volume_24h, signal.atr_pct,
            signal.readiness_score, json.dumps(signal.readiness_details),
            signal.btc_correlation, signal.btc_trend,
            signal.status, signal.ai_reviews, signal.limit_order_id,
            signal.created_at, signal.updated_at, signal.filled_at,
            signal.position_size, signal.current_pnl
        ))
        
        conn.commit()
        conn.close()
    
    def _load_pending_signals(self):
        """加载未完成的信号"""
        conn = sqlite3.connect(self.db_path, timeout=30)
        cursor = conn.execute("""
            SELECT * FROM high_vol_signals
            WHERE status IN ('watching', 'ready', 'limit_placed', 'filled')
        """)
        
        for row in cursor.fetchall():
            signal = self._row_to_signal(row)
            if signal.status == SignalStatus.WATCHING.value:
                self.observation_pool[signal.symbol] = signal
            elif signal.status == SignalStatus.LIMIT_PLACED.value:
                self.active_orders[signal.symbol] = signal
            elif signal.status == SignalStatus.FILLED.value:
                self.active_positions[signal.symbol] = signal
        
        conn.close()
        
        print(f"[HIGH_VOL] 加载: 观察池{len(self.observation_pool)}个, 挂单{len(self.active_orders)}个, 持仓{len(self.active_positions)}个")
        
        # 🔥🔥🔥 v1.4: 启动时同步OKX实际持仓，清理已不存在的记录
        self._sync_positions_with_okx()
    
    def _sync_positions_with_okx(self):
        """🔥 v1.4: 同步OKX实际持仓，清理已不存在的持仓记录"""
        if not self.active_positions:
            return
            
        try:
            # 获取OKX实际持仓
            okx_positions = self.auto_trader.get_current_positions() if self.auto_trader else []
            okx_symbols = set()
            for pos in okx_positions:
                symbol = pos.get('symbol', '')
                if symbol:
                    okx_symbols.add(symbol)
            
            # 检查本地记录的持仓是否还在OKX上
            to_remove = []
            for symbol, signal in self.active_positions.items():
                okx_symbol = symbol
                if not okx_symbol.endswith(':USDT'):
                    okx_symbol = symbol.replace('/USDT', '/USDT:USDT')
                
                if okx_symbol not in okx_symbols:
                    print(f"[HIGH_VOL] ⚠️ {symbol} 在OKX已无持仓，清理本地记录")
                    to_remove.append(symbol)
            
            # 从内存和数据库中清理
            if to_remove:
                conn = sqlite3.connect(self.db_path, timeout=30)
                for symbol in to_remove:
                    # 从内存删除
                    del self.active_positions[symbol]
                    # 从数据库更新状态
                    conn.execute("""
                        UPDATE high_vol_signals 
                        SET status = 'closed', updated_at = datetime('now')
                        WHERE symbol = ? AND status = 'filled'
                    """, (symbol,))
                conn.commit()
                conn.close()
                print(f"[HIGH_VOL] ✅ 同步完成: 清理了{len(to_remove)}个无效持仓记录")
            else:
                print(f"[HIGH_VOL] ✅ 持仓同步正常: {len(self.active_positions)}个持仓与OKX一致")
                
        except Exception as e:
            print(f"[HIGH_VOL] ⚠️ 同步OKX持仓失败: {e}")
    
    def _row_to_signal(self, row) -> HighVolSignal:
        """数据库行转信号对象"""
        return HighVolSignal(
            id=row[0],
            symbol=row[1],
            track=row[2],
            signal_type=row[3],
            signal_price=row[4] or 0,
            entry_price=row[5] or 0,
            stop_loss=row[6] or 0,
            take_profit=row[7] or 0,
            side=row[8] or "long",
            change_24h=row[9] or 0,
            volume_24h=row[10] or 0,
            atr_pct=row[11] or 0,
            readiness_score=row[12] or 0,
            readiness_details=json.loads(row[13]) if row[13] else [],
            btc_correlation=row[14] or 0,
            btc_trend=row[15] or "neutral",
            status=row[16] or "watching",
            ai_reviews=row[17] or 0,
            limit_order_id=row[18] or "",
            created_at=row[19] or "",
            updated_at=row[20] or "",
            filled_at=row[21] or "",
            position_size=row[22] or 0,
            current_pnl=row[23] or 0,
        )
    
    # ==================== 主循环 ====================
    
    def run_once(self, all_klines: Dict[str, pd.DataFrame], btc_df: pd.DataFrame, btc_status: Dict):
        """
        执行一次轮询
        
        Args:
            all_klines: 所有币种的K线数据 {symbol: DataFrame}
            btc_df: BTC的K线数据
            btc_status: BTC市场状态
        """
        # 🔥 调试日志
        print(f"[HIGH_VOL] 🔄 run_once() 开始 | enabled={self.enabled} | klines={len(all_klines)}个")
        
        if not self.enabled:
            print(f"[HIGH_VOL] ⚠️ 未启用，跳过")
            return
        
        now = datetime.now(timezone.utc)
        now_str = now.isoformat()
        
        with self._lock:
            # 1. 扫描新的高波动币种
            print(f"[HIGH_VOL] 📡 开始扫描高波动币种...")
            self._scan_high_volatility(all_klines, btc_df, btc_status, now_str)
            
            # 2. 更新观察池（计算就绪分数）
            self._update_observation_pool(all_klines, btc_df, btc_status, now_str)
            
            # 3. 检查挂单状态
            self._check_limit_orders(now_str)
            
            # 4. 监控持仓
            self._monitor_positions(all_klines, btc_status, now_str)
            
            # 5. 清理过期
            self._cleanup_expired(now_str)
            
            # 🔥 v3.3新增: 打印观察池详细状态
            self._print_pool_status()
    
    def _print_pool_status(self):
        """🔥 v3.3新增: 打印观察池详细状态"""
        pool_count = len(self.observation_pool)
        order_count = len(self.active_orders)
        pos_count = len(self.active_positions)
        
        # 基础状态行
        status_line = f"\n🔸 轨道2状态: 观察{pool_count}/{self.pool_capacity} | 挂单{order_count}/{self.max_concurrent_orders} | 持仓{pos_count}"
        
        # 如果观察池有内容，打印健康度摘要
        if self.observation_pool:
            healthy = sum(1 for s in self.observation_pool.values() if s.health_score >= 70)
            warning = sum(1 for s in self.observation_pool.values() if 40 <= s.health_score < 70)
            critical = sum(1 for s in self.observation_pool.values() if s.health_score < 40)
            
            status_line += f" | 健康:{healthy}🟢 {warning}🟡 {critical}🔴"
            
            # 打印前3个最高就绪分的币种详情
            top_signals = sorted(self.observation_pool.values(), 
                               key=lambda x: x.readiness_score, reverse=True)[:3]
            
            if top_signals:
                print(status_line)
                for sig in top_signals:
                    health_emoji = "🟢" if sig.health_score >= 70 else ("🟡" if sig.health_score >= 40 else "🔴")
                    age_min = 0
                    try:
                        created = datetime.fromisoformat(sig.created_at.replace('Z', '+00:00'))
                        age_min = (datetime.now(timezone.utc) - created).total_seconds() / 60
                    except:
                        pass
                    
                    print(f"   {sig.symbol[:15]:<15} | 就绪:{sig.readiness_score:>2} | 健康:{sig.health_score:>3}{health_emoji} | {age_min:.0f}分钟 | {sig.bb_trend}/{sig.vol_trend}")
            else:
                print(status_line)
        else:
            print(status_line)
    
    # ==================== 第一步：扫描 ====================
    
    def _scan_high_volatility(self, all_klines: Dict[str, pd.DataFrame], btc_df: pd.DataFrame, 
                               btc_status: Dict, now_str: str):
        """扫描高波动币种"""
        
        # 检查观察池是否已满
        if len(self.observation_pool) >= self.pool_capacity:
            print(f"[HIGH_VOL] 观察池已满 ({len(self.observation_pool)}/{self.pool_capacity})")
            return
        
        candidates = []
        rejected_reasons = {}  # 记录拒绝原因统计
        
        for symbol, df in all_klines.items():
            # 跳过BTC本身
            if "BTC" in symbol:
                continue
            
            # 跳过已在观察池、挂单、持仓中的
            if symbol in self.observation_pool or symbol in self.active_orders or symbol in self.active_positions:
                continue
            
            # 硬规则筛选
            passed, reason, metrics = self._hard_filter(symbol, df)
            if not passed:
                # 统计拒绝原因
                key = reason.split()[0] if reason else "未知"
                rejected_reasons[key] = rejected_reasons.get(key, 0) + 1
                continue
            
            candidates.append({
                "symbol": symbol,
                "metrics": metrics,
                "reason": reason
            })
        
        # 打印扫描结果
        print(f"[HIGH_VOL] 扫描: {len(all_klines)-1}个币 → {len(candidates)}个候选")
        if rejected_reasons:
            top_reasons = sorted(rejected_reasons.items(), key=lambda x: -x[1])[:3]
            print(f"[HIGH_VOL] 主要过滤原因: {', '.join([f'{k}({v})' for k,v in top_reasons])}")
        
        # 按24h涨跌幅排序，优先处理波动大的
        candidates.sort(key=lambda x: abs(x["metrics"]["change_24h"]), reverse=True)
        
        # 添加到观察池（不超过容量）
        added = 0
        skipped_okx = 0
        for c in candidates:
            if len(self.observation_pool) >= self.pool_capacity:
                break
            
            # 🔥🔥🔥 v3.4新增: 先验证OKX是否支持
            if not self._validate_okx_symbol(c["symbol"]):
                skipped_okx += 1
                continue
            
            signal = HighVolSignal(
                id=f"hv_{c['symbol'].replace('/', '_')}_{int(time.time())}",
                symbol=c["symbol"],
                signal_price=c["metrics"]["price"],
                change_24h=c["metrics"]["change_24h"],
                volume_24h=c["metrics"]["volume_24h"],
                atr_pct=c["metrics"]["atr_pct"],
                status=SignalStatus.WATCHING.value,
                created_at=now_str,
                updated_at=now_str,
            )
            
            self.observation_pool[c["symbol"]] = signal
            self._save_signal(signal)
            added += 1
            
            print(f"[HIGH_VOL] ➕ 进入观察池: {c['symbol']} | 24h:{c['metrics']['change_24h']*100:+.1f}% | 成交:{c['metrics']['volume_24h']/1e6:.1f}M")
        
        if skipped_okx > 0:
            print(f"[HIGH_VOL] ⚠️ 跳过{skipped_okx}个OKX不支持的交易对")
        if added > 0:
            print(f"[HIGH_VOL] 观察池: {len(self.observation_pool)}/{self.pool_capacity}")
    
    def _hard_filter(self, symbol: str, df: pd.DataFrame) -> Tuple[bool, str, Dict]:
        """
        硬规则筛选 - 🔥v2.0 新增假突破快速检测
        
        Returns:
            (是否通过, 原因, 指标数据)
        """
        metrics = {}
        
        if df is None or len(df) < 100:
            return False, "数据不足", metrics
        
        price = float(df['close'].iloc[-1])
        metrics["price"] = price
        
        # 1. 24h涨跌幅
        if len(df) >= 1440:
            price_24h = float(df['close'].iloc[-1440])
        else:
            price_24h = float(df['close'].iloc[0])
        
        change_24h = (price - price_24h) / price_24h
        metrics["change_24h"] = change_24h
        
        abs_change = abs(change_24h)
        if abs_change < self.min_change_24h:
            return False, f"24h涨跌{abs_change*100:.1f}% < {self.min_change_24h*100:.0f}%", metrics
        if abs_change > self.max_change_24h:
            return False, f"24h涨跌{abs_change*100:.1f}% > {self.max_change_24h*100:.0f}%", metrics
        
        # 2. 24h成交量
        volume_24h = float(df['volume'].tail(min(1440, len(df))).sum() * price)
        metrics["volume_24h"] = volume_24h
        
        if volume_24h < self.min_volume_24h:
            return False, f"成交量{volume_24h/1e6:.1f}M < {self.min_volume_24h/1e6:.0f}M", metrics
        
        # 3. 不在刚暴涨暴跌后（5分钟内波动>3%）
        if len(df) >= 5:
            change_5m = (price - float(df['close'].iloc[-5])) / float(df['close'].iloc[-5])
            if abs(change_5m) > 0.03:
                return False, f"5分钟内已波动{change_5m*100:.1f}%", metrics
        
        # 4. 计算ATR
        atr_pct = self._calculate_atr_pct(df)
        metrics["atr_pct"] = atr_pct
        
        # 5. 布林带宽度（检查是否在收缩）
        bb_width = self._calculate_bb_width(df)
        bb_width_ma = self._calculate_bb_width_ma(df, 20)
        metrics["bb_width"] = bb_width
        metrics["bb_width_ma"] = bb_width_ma
        
        if bb_width > bb_width_ma * 1.3:  # 布林带在明显扩张
            return False, "布林带扩张中，非蓄势状态", metrics
        
        # 🔥🔥🔥 v2.0新增: CVD快速假突破检测
        cvd_result = self._quick_cvd_check(df)
        metrics["cvd_divergence"] = cvd_result["divergence"]
        metrics["cvd_score"] = cvd_result["signal_quality"]
        metrics["is_fake_breakout"] = cvd_result["is_fake_breakout"]
        
        # 如果检测到明显假突破，直接拒绝
        if cvd_result["is_fake_breakout"] and cvd_result["divergence_strength"] > 60:
            return False, f"CVD检测到假突破(背离强度:{cvd_result['divergence_strength']:.0f})", metrics
        
        # 🔥 v2.0新增: 效率比检测 (过滤噪音市)
        er = self._quick_efficiency_ratio(df)
        metrics["efficiency_ratio"] = er
        
        if er < 0.2:  # 效率比太低，价格来回震荡
            return False, f"效率比过低({er:.2f})，价格震荡无方向", metrics
        
        return True, "通过硬规则", metrics
    
    def _quick_cvd_check(self, df: pd.DataFrame, lookback: int = 20) -> Dict:
        """
        🔥 v2.0新增: 快速CVD检测 (硬规则用)
        """
        try:
            if len(df) < lookback + 5:
                return {"divergence": "none", "divergence_strength": 0, 
                        "is_fake_breakout": False, "signal_quality": 50}
            
            # 计算CVD
            direction = np.sign(df['close'].values - df['open'].values)
            volume_delta = direction * df['volume'].values
            cvd = np.cumsum(volume_delta)
            
            # 计算变化
            cvd_now = cvd[-1]
            cvd_past = cvd[-lookback]
            price_now = df['close'].iloc[-1]
            price_past = df['close'].iloc[-lookback]
            
            cvd_range = max(abs(cvd[-lookback:].max() - cvd[-lookback:].min()), 1)
            price_past_safe = max(price_past, 1e-10)
            
            cvd_delta = (cvd_now - cvd_past) / cvd_range * 100
            price_delta = (price_now - price_past) / price_past_safe * 100
            
            divergence = "none"
            divergence_strength = 0
            is_fake_breakout = False
            
            # 价格上涨但CVD下跌 = 假突破风险
            if price_delta > 1 and cvd_delta < -5:
                divergence = "bearish"
                divergence_strength = min(100, abs(cvd_delta) * 2)
                if price_delta > 3 and cvd_delta < -10:
                    is_fake_breakout = True
            
            # 价格下跌但CVD上涨 = 假跌风险
            elif price_delta < -1 and cvd_delta > 5:
                divergence = "bullish"
                divergence_strength = min(100, abs(cvd_delta) * 2)
                if price_delta < -3 and cvd_delta > 10:
                    is_fake_breakout = True
            
            signal_quality = 50 + (cvd_delta * price_delta / 100) if price_delta * cvd_delta > 0 else 50 - divergence_strength * 0.3
            
            return {
                "divergence": divergence,
                "divergence_strength": round(divergence_strength, 1),
                "is_fake_breakout": is_fake_breakout,
                "signal_quality": round(max(0, min(100, signal_quality)), 1)
            }
        except Exception as e:
            return {"divergence": "none", "divergence_strength": 0, 
                    "is_fake_breakout": False, "signal_quality": 50}
    
    def _quick_efficiency_ratio(self, df: pd.DataFrame, period: int = 20) -> float:
        """
        🔥 v2.0新增: 快速效率比计算 (硬规则用)
        """
        try:
            if len(df) < period + 1:
                return 0.5
            
            close = df['close'].tail(period + 1)
            net_move = abs(close.iloc[-1] - close.iloc[0])
            total_move = close.diff().abs().sum()
            
            if total_move == 0:
                return 0.5
            
            return round(float(net_move / total_move), 4)
        except:
            return 0.5
    
    # ==================== 第二步：观察池更新 ====================
    
    def _update_observation_pool(self, all_klines: Dict[str, pd.DataFrame], btc_df: pd.DataFrame,
                                  btc_status: Dict, now_str: str):
        """
        🔥 v3.3重构: 智能观察池更新
        
        核心理念：
        1. 不是简单的超时淘汰，而是基于"健康度"的动态评估
        2. 健康度由多个维度组成：趋势、成交量、动量、布林带等
        3. 健康度下降到阈值才淘汰，而不是分数不涨就淘汰
        4. 增加"突破前兆"检测，提前发现机会
        """
        
        to_remove = []
        to_trigger = []
        
        for symbol, signal in self.observation_pool.items():
            df = all_klines.get(symbol)
            if df is None:
                continue
            
            # 检查是否超时（保底机制）
            created = datetime.fromisoformat(signal.created_at.replace('Z', '+00:00'))
            age_min = (datetime.now(timezone.utc) - created).total_seconds() / 60
            
            if age_min > self.pool_max_time_min:
                signal.status = SignalStatus.EXPIRED.value
                signal.updated_at = now_str
                self._save_signal(signal)
                to_remove.append(symbol)
                print(f"[HIGH_VOL] ⏰ 观察超时: {symbol} ({age_min:.0f}分钟)")
                continue
            
            # 🔥 计算就绪分数
            readiness = self._calculate_readiness_score(symbol, df, btc_df, btc_status)
            current_score = readiness["score"]
            signal.readiness_score = current_score
            signal.readiness_details = readiness["details"]
            signal.btc_correlation = readiness.get("btc_correlation", 0)
            signal.btc_trend = btc_status.get("trend", "neutral")
            signal.updated_at = now_str
            
            # 更新历史最高分
            if current_score > signal.peak_readiness:
                signal.peak_readiness = current_score
            
            # 🔥 v3.3核心: 计算健康度
            health_result = self._calculate_health_score(symbol, df, signal, readiness)
            signal.health_score = health_result["score"]
            signal.bb_trend = health_result.get("bb_trend", "neutral")
            signal.vol_trend = health_result.get("vol_trend", "neutral")
            signal.momentum_trend = health_result.get("momentum_trend", "neutral")
            
            # 健康度过低，淘汰
            if signal.health_score < self.health_threshold:
                signal.status = SignalStatus.EXPIRED.value
                signal.last_warning = health_result.get("warning", "健康度过低")
                signal.updated_at = now_str
                self._save_signal(signal)
                to_remove.append(symbol)
                print(f"[HIGH_VOL] 💔 健康度淘汰: {symbol} | 健康:{signal.health_score} | {signal.last_warning}")
                continue
            
            # 🔥 检测突破前兆
            breakout_signal = self._detect_breakout_precursor(symbol, df, signal, readiness)
            if breakout_signal:
                signal.readiness_score = max(current_score, self.readiness_threshold)  # 提升到触发阈值
                signal.readiness_details.insert(0, f"🚀{breakout_signal}")
            
            # 检查是否就绪
            if signal.readiness_score >= self.readiness_threshold:
                signal.status = SignalStatus.READY.value
                to_trigger.append(signal)
                print(f"[HIGH_VOL] 🎯 就绪触发: {symbol} | 分数:{signal.readiness_score} | 健康:{signal.health_score} | {', '.join(signal.readiness_details[:3])}")
            
            self._save_signal(signal)
        
        # 移除超时的
        for symbol in to_remove:
            del self.observation_pool[symbol]
        
        # 触发AI决策
        for signal in to_trigger:
            if len(self.active_orders) < self.max_concurrent_orders:
                self._trigger_ai_decision(signal, all_klines, btc_status, now_str)
            else:
                print(f"[HIGH_VOL] ⚠️ 挂单已满({self.max_concurrent_orders}个)，{signal.symbol}等待")
    
    def _calculate_readiness_score(self, symbol: str, df: pd.DataFrame, 
                                    btc_df: pd.DataFrame, btc_status: Dict) -> Dict:
        """
        计算就绪分数 (0-100)
        """
        score = 0
        details = []
        
        price = float(df['close'].iloc[-1])
        
        # 1. 布林带收缩程度 (0-25分)
        bb_width = self._calculate_bb_width(df)
        bb_percentile = self._get_percentile(df, bb_width, 'bb_width', 100)
        
        if bb_percentile < 10:
            score += 25
            details.append("布林带极度收窄")
        elif bb_percentile < 20:
            score += 20
            details.append("布林带较窄")
        elif bb_percentile < 35:
            score += 12
            details.append("布林带收窄")
        
        # 2. 成交量变化 (0-25分)
        vol_now = float(df['volume'].iloc[-5:].mean())
        vol_ma = float(df['volume'].iloc[-60:-5].mean()) if len(df) > 65 else vol_now
        vol_ratio = vol_now / vol_ma if vol_ma > 0 else 1
        
        if vol_ratio > 2.5:
            score += 25
            details.append(f"成交量放大{vol_ratio:.1f}x")
        elif vol_ratio > 1.8:
            score += 20
            details.append(f"成交量回升{vol_ratio:.1f}x")
        elif vol_ratio > 1.2:
            score += 12
            details.append(f"成交量温和{vol_ratio:.1f}x")
        elif vol_ratio < 0.4:
            score += 8
            details.append("极度缩量蓄势")
        
        # 3. 价格位置 (0-25分)
        support, resistance = self._find_key_levels(df)
        
        dist_to_support = (price - support) / price if support > 0 else 1
        dist_to_resistance = (resistance - price) / price if resistance > 0 else 1
        
        if dist_to_support < 0.008:
            score += 25
            details.append("贴近支撑位")
        elif dist_to_resistance < 0.008:
            score += 25
            details.append("贴近阻力位")
        elif min(dist_to_support, dist_to_resistance) < 0.015:
            score += 18
            details.append("接近关键位")
        elif min(dist_to_support, dist_to_resistance) < 0.025:
            score += 10
            details.append("靠近关键位")
        
        # 4. BTC状态 (0-25分)
        btc_trend = btc_status.get('trend', 'neutral')
        btc_volatility = btc_status.get('volatility', 0)
        
        # 计算与BTC相关性
        btc_corr = self._calculate_btc_correlation(df, btc_df)
        
        if btc_trend in ['neutral', 'sideways']:
            if btc_volatility < 0.008:
                score += 25
                details.append("BTC平稳，山寨独立机会")
            else:
                score += 15
                details.append("BTC震荡")
        elif btc_trend in ['pump', 'rally']:
            score += 18
            details.append("BTC上涨带动")
        elif btc_trend in ['dump', 'crash']:
            if btc_corr < 0.4:
                score += 15
                details.append(f"BTC下跌但独立(相关{btc_corr:.2f})")
            else:
                score += 5
                details.append(f"BTC下跌，高相关{btc_corr:.2f}")
        
        return {
            "score": score,
            "details": details,
            "btc_correlation": btc_corr,
            "bb_percentile": bb_percentile,
            "vol_ratio": vol_ratio,
            "support": support,
            "resistance": resistance,
        }
    
    def _calculate_health_score(self, symbol: str, df: pd.DataFrame, 
                                 signal: HighVolSignal, readiness: Dict) -> Dict:
        """
        🔥 v3.3新增: 计算信号健康度
        
        健康度评估维度：
        1. 布林带趋势（是否还在收缩或开始反向扩张）
        2. 成交量趋势（是否萎缩到危险水平）
        3. 动量趋势（是否明显反向）
        4. 价格位置（是否破位关键支撑/阻力）
        5. 相对于入池价格的表现
        
        Returns:
            {"score": 0-100, "bb_trend": str, "vol_trend": str, "momentum_trend": str, "warning": str}
        """
        health = 100
        warnings = []
        
        price = float(df['close'].iloc[-1])
        
        # ========== 1. 布林带趋势评估 (扣分项) ==========
        bb_width = self._calculate_bb_width(df)
        bb_width_5 = self._calculate_bb_width(df.iloc[-5:]) if len(df) >= 5 else bb_width
        bb_width_10 = self._calculate_bb_width(df.iloc[-10:]) if len(df) >= 10 else bb_width
        
        bb_trend = "neutral"
        if bb_width > bb_width_10 * 1.3:
            # 布林带明显扩张 - 可能是突破或者失败
            # 需要判断是突破还是失败扩张
            price_5 = float(df['close'].iloc[-5]) if len(df) >= 5 else price
            if abs(price - price_5) / price_5 > 0.015:
                # 价格也有明显变动 - 可能是有效突破，不扣分
                bb_trend = "breaking"
            else:
                # 价格没动但带宽扩张 - 不好的信号
                health -= 25
                bb_trend = "expanding"
                warnings.append("布林带扩张但价格未突破")
        elif bb_width < bb_width_10 * 0.85:
            # 布林带继续收缩 - 好信号，加分
            health = min(100, health + 10)
            bb_trend = "squeezing"
        
        # ========== 2. 成交量趋势评估 ==========
        vol_now = float(df['volume'].iloc[-3:].mean())
        vol_ma_20 = float(df['volume'].iloc[-20:].mean()) if len(df) >= 20 else vol_now
        vol_ratio = vol_now / vol_ma_20 if vol_ma_20 > 0 else 1
        
        vol_trend = "neutral"
        if vol_ratio < 0.3:
            # 成交量极度萎缩 - 可能是失去关注
            health -= 20
            vol_trend = "dying"
            warnings.append(f"成交量萎缩至{vol_ratio:.1f}x")
        elif vol_ratio < 0.5:
            health -= 10
            vol_trend = "falling"
        elif vol_ratio > 2.0:
            # 成交量放大 - 可能有行情
            health = min(100, health + 15)
            vol_trend = "surging"
        elif vol_ratio > 1.2:
            vol_trend = "rising"
        
        # ========== 3. 动量趋势评估 ==========
        momentum_trend = "neutral"
        
        # RSI变化
        rsi_now = self._calculate_rsi(df, 14)
        rsi_5 = self._calculate_rsi(df.iloc[:-5], 14) if len(df) > 19 else rsi_now
        
        # 根据24h涨跌幅判断预期方向
        expected_direction = "up" if signal.change_24h > 0 else "down"
        
        if expected_direction == "up":
            # 涨势币种，RSI下跌是警告
            if rsi_now < rsi_5 - 15:
                health -= 20
                momentum_trend = "reversing"
                warnings.append(f"RSI快速下跌({rsi_5:.0f}→{rsi_now:.0f})")
            elif rsi_now < 30:
                health -= 15
                momentum_trend = "weak"
                warnings.append("RSI进入超卖")
        else:
            # 跌势币种，RSI上涨是警告
            if rsi_now > rsi_5 + 15:
                health -= 20
                momentum_trend = "reversing"
                warnings.append(f"RSI快速上涨({rsi_5:.0f}→{rsi_now:.0f})")
            elif rsi_now > 70:
                health -= 15
                momentum_trend = "weak"
                warnings.append("RSI进入超买")
        
        # ========== 4. 价格位置评估 ==========
        support = readiness.get("support", 0)
        resistance = readiness.get("resistance", 0)
        
        if expected_direction == "up" and support > 0:
            # 涨势币种跌破支撑 - 严重警告
            if price < support * 0.995:
                health -= 30
                warnings.append(f"跌破支撑位${support:.4f}")
        elif expected_direction == "down" and resistance > 0:
            # 跌势币种突破阻力 - 严重警告
            if price > resistance * 1.005:
                health -= 30
                warnings.append(f"突破阻力位${resistance:.4f}")
        
        # ========== 5. 相对入池价格表现 ==========
        entry_price = signal.signal_price
        if entry_price > 0:
            price_change = (price - entry_price) / entry_price
            
            if expected_direction == "up" and price_change < -0.03:
                # 涨势币种入池后跌了3%+
                health -= 15
                warnings.append(f"入池后下跌{price_change*100:.1f}%")
            elif expected_direction == "down" and price_change > 0.03:
                # 跌势币种入池后涨了3%+
                health -= 15
                warnings.append(f"入池后上涨{price_change*100:+.1f}%")
        
        # 确保健康度在0-100范围
        health = max(0, min(100, health))
        
        return {
            "score": health,
            "bb_trend": bb_trend,
            "vol_trend": vol_trend,
            "momentum_trend": momentum_trend,
            "warning": "; ".join(warnings) if warnings else ""
        }
    
    def _detect_breakout_precursor(self, symbol: str, df: pd.DataFrame,
                                    signal: HighVolSignal, readiness: Dict) -> Optional[str]:
        """
        🔥 v3.3新增: 检测突破前兆
        
        突破前兆特征：
        1. 布林带极度收窄 + 成交量突然放大
        2. 价格触及关键位 + 成交量放大
        3. 连续收窄后的首根放量K线
        4. 多空力量出现明显失衡
        
        Returns:
            突破信号描述，None表示没有检测到
        """
        if len(df) < 20:
            return None
        
        price = float(df['close'].iloc[-1])
        
        # ========== 1. 布林带收窄 + 成交量放大 ==========
        bb_percentile = readiness.get("bb_percentile", 50)
        vol_ratio = readiness.get("vol_ratio", 1)
        
        if bb_percentile < 15 and vol_ratio > 1.8:
            return f"布林带极窄+放量{vol_ratio:.1f}x"
        
        # ========== 2. 价格突破布林带 ==========
        bb_upper = self._calculate_bb_upper(df)
        bb_lower = self._calculate_bb_lower(df)
        
        if price > bb_upper:
            # 突破上轨
            vol_now = float(df['volume'].iloc[-1])
            vol_ma = float(df['volume'].iloc[-20:].mean())
            if vol_now > vol_ma * 1.5:
                return f"突破上轨+放量"
        elif price < bb_lower:
            # 跌破下轨
            vol_now = float(df['volume'].iloc[-1])
            vol_ma = float(df['volume'].iloc[-20:].mean())
            if vol_now > vol_ma * 1.5:
                return f"跌破下轨+放量"
        
        # ========== 3. K线形态识别 ==========
        candle_pattern = self._detect_candle_pattern(df)
        if candle_pattern:
            return candle_pattern
        
        # ========== 4. 连续收窄后首次放量 ==========
        if bb_percentile < 20:
            # 检查是否是连续收窄后的首次放量
            vol_history = df['volume'].iloc[-10:-1]
            vol_now = float(df['volume'].iloc[-1])
            vol_avg = float(vol_history.mean())
            vol_max = float(vol_history.max())
            
            if vol_now > vol_max * 1.5 and vol_now > vol_avg * 2:
                return f"蓄势后首次放量{vol_now/vol_avg:.1f}x"
        
        return None
    
    def _detect_candle_pattern(self, df: pd.DataFrame) -> Optional[str]:
        """检测K线形态"""
        if len(df) < 3:
            return None
        
        # 最近3根K线
        c1, c2, c3 = df.iloc[-3], df.iloc[-2], df.iloc[-1]
        
        o3, h3, l3, c3_close = float(c3['open']), float(c3['high']), float(c3['low']), float(c3['close'])
        body3 = abs(c3_close - o3)
        range3 = h3 - l3 if h3 > l3 else 0.0001
        
        # 1. 锤子线/倒锤子（反转信号）
        upper_shadow = h3 - max(o3, c3_close)
        lower_shadow = min(o3, c3_close) - l3
        
        if lower_shadow > body3 * 2 and upper_shadow < body3 * 0.5:
            return "锤子线"
        if upper_shadow > body3 * 2 and lower_shadow < body3 * 0.5:
            return "倒锤子"
        
        # 2. 吞没形态
        o2, c2_close = float(c2['open']), float(c2['close'])
        body2 = abs(c2_close - o2)
        
        if body3 > body2 * 1.5:
            if c3_close > o3 and c2_close < o2:  # 阳吞阴
                return "看涨吞没"
            elif c3_close < o3 and c2_close > o2:  # 阴吞阳
                return "看跌吞没"
        
        # 3. 十字星（犹豫信号，可能反转）
        if body3 < range3 * 0.1:
            return "十字星"
        
        return None
    
    def _calculate_rsi(self, df: pd.DataFrame, period: int = 14) -> float:
        """计算RSI"""
        if len(df) < period + 1:
            return 50.0
        
        delta = df['close'].diff()
        gain = delta.where(delta > 0, 0).rolling(window=period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
        
        rs = gain / loss.replace(0, 0.0001)
        rsi = 100 - (100 / (1 + rs))
        
        return float(rsi.iloc[-1]) if not pd.isna(rsi.iloc[-1]) else 50.0
    
    def _calculate_bb_upper(self, df: pd.DataFrame, period: int = 20, std_mult: float = 2.0) -> float:
        """计算布林带上轨"""
        if len(df) < period:
            return float(df['close'].iloc[-1])
        
        ma = df['close'].rolling(window=period).mean()
        std = df['close'].rolling(window=period).std()
        upper = ma + std * std_mult
        
        return float(upper.iloc[-1])
    
    def _calculate_bb_lower(self, df: pd.DataFrame, period: int = 20, std_mult: float = 2.0) -> float:
        """计算布林带下轨"""
        if len(df) < period:
            return float(df['close'].iloc[-1])
        
        ma = df['close'].rolling(window=period).mean()
        std = df['close'].rolling(window=period).std()
        lower = ma - std * std_mult
        
        return float(lower.iloc[-1])
    
    # ==================== 第三步：AI决策 ====================
    
    def _trigger_ai_decision(self, signal: HighVolSignal, all_klines: Dict[str, pd.DataFrame],
                             btc_status: Dict, now_str: str):
        """触发AI决策 - 🔥v2.1 新增趋势分析集成"""
        
        df = all_klines.get(signal.symbol)
        if df is None:
            return
        
        # 🔥🔥🔥 v3.4新增: 先验证OKX是否支持该交易对
        if not self._validate_okx_symbol(signal.symbol):
            print(f"[HIGH_VOL] ⚠️ OKX不支持: {signal.symbol}，跳过")
            signal.status = SignalStatus.ABANDONED.value
            signal.updated_at = now_str
            self._save_signal(signal)
            if signal.symbol in self.observation_pool:
                del self.observation_pool[signal.symbol]
            return
        
        # 🔥🔥🔥 v2.0新增: 计算突破质量指标
        breakout_result = self._calculate_breakout_quality(df)
        signal.cvd_divergence = breakout_result.get("cvd_divergence", "none")
        signal.cvd_score = breakout_result.get("cvd_score", 50)
        signal.efficiency_ratio = breakout_result.get("efficiency_ratio", 0.5)
        signal.hurst_value = breakout_result.get("hurst_value", 0.5)
        signal.breakout_quality = breakout_result.get("overall_score", 50)
        signal.is_fake_breakout = breakout_result.get("is_fake_breakout", False)
        
        # 🔥 v2.0: 如果是明确假突破，直接放弃
        if signal.is_fake_breakout and signal.breakout_quality < 40:
            print(f"[HIGH_VOL] ⚠️ 假突破检测: {signal.symbol} | 质量:{signal.breakout_quality:.0f} | CVD:{signal.cvd_divergence}")
            signal.status = SignalStatus.ABANDONED.value
            signal.last_warning = f"假突破(质量{signal.breakout_quality:.0f})"
            signal.updated_at = now_str
            self._save_signal(signal)
            if signal.symbol in self.observation_pool:
                del self.observation_pool[signal.symbol]
            return
        
        # 🔥🔥🔥 v2.1新增: 获取趋势分析上下文
        trend_context = {}
        if HAS_TREND_ANALYSIS:
            try:
                # 尝试获取OI变化数据（如果有的话）
                oi_change = getattr(signal, 'oi_change', 0)
                volume_24h = signal.volume_24h if hasattr(signal, 'volume_24h') else 0
                
                trend_context = analyze_trend_context(df, signal.symbol, oi_change, volume_24h)
                
                # 🔥 v2.1: 根据FDI质量调整策略
                fdi_value = trend_context.get("fdi_value", 1.35)
                if fdi_value > 1.48:
                    print(f"[HIGH_VOL] ⚠️ FDI={fdi_value:.3f} 过高，趋势太嘈杂，放弃: {signal.symbol}")
                    signal.status = SignalStatus.ABANDONED.value
                    signal.last_warning = f"FDI过高({fdi_value:.2f})"
                    signal.updated_at = now_str
                    self._save_signal(signal)
                    if signal.symbol in self.observation_pool:
                        del self.observation_pool[signal.symbol]
                    return
                    
            except Exception as e:
                print(f"[HIGH_VOL] ⚠️ 趋势分析异常: {e}")
        
        signal.ai_reviews += 1
        
        # 构建AI prompt - 🔥 v2.1: 传入趋势上下文
        price = float(df['close'].iloc[-1])
        support, resistance = self._find_key_levels(df)
        
        prompt = self._build_ai_prompt(signal, df, btc_status, support, resistance, trend_context)
        
        # 调用AI
        ai_result = self._call_ai(prompt)
        
        if ai_result is None or ai_result.get("direction") == "unclear":
            print(f"[HIGH_VOL] 🤷 AI不确定: {signal.symbol} (第{signal.ai_reviews}次)")
            
            if signal.ai_reviews >= self.max_ai_reviews:
                signal.status = SignalStatus.ABANDONED.value
                signal.updated_at = now_str
                self._save_signal(signal)
                if signal.symbol in self.observation_pool:
                    del self.observation_pool[signal.symbol]
                print(f"[HIGH_VOL] ❌ 放弃: {signal.symbol} (AI{self.max_ai_reviews}次不确定)")
            return
        
        # 解析AI结果
        direction = ai_result.get("direction", "long")
        entry_offset = ai_result.get("entry_offset_pct", 0.01)
        tp_pct = ai_result.get("take_profit_pct", 0.06)
        confidence = ai_result.get("confidence", 0.5)
        reasoning = ai_result.get("reasoning", "")
        
        # 🔥 v2.1: 根据FDI调整entry_offset（趋势嘈杂时挂远单）
        fdi_value = trend_context.get("fdi_value", 1.35) if trend_context else 1.35
        if fdi_value > 1.40:
            # FDI高，挂远单接针
            entry_offset = max(entry_offset, 0.02)
            print(f"[HIGH_VOL] 📏 FDI={fdi_value:.2f} 偏高，调整挂单距离: {entry_offset*100:.1f}%")
        elif fdi_value < 1.25:
            # FDI低，可以挂近单
            entry_offset = min(entry_offset, 0.015)
            print(f"[HIGH_VOL] 📏 FDI={fdi_value:.2f} 优秀，挂近单: {entry_offset*100:.1f}%")
        
        # 计算入场价
        if direction == "long":
            entry_price = price * (1 - abs(entry_offset))
        else:
            entry_price = price * (1 + abs(entry_offset))
        
        # 计算止损（动态，上限2%）
        sl_pct = self._calculate_stop_loss_pct(signal.atr_pct)
        if direction == "long":
            stop_loss = entry_price * (1 - sl_pct)
        else:
            stop_loss = entry_price * (1 + sl_pct)
        
        # 计算止盈
        if direction == "long":
            take_profit = entry_price * (1 + tp_pct)
        else:
            take_profit = entry_price * (1 - tp_pct)
        
        # 更新信号
        signal.side = direction
        signal.entry_price = entry_price
        signal.stop_loss = stop_loss
        signal.take_profit = take_profit
        signal.updated_at = now_str
        
        # 计算仓位
        position_size = self._calculate_position_size(signal)
        signal.position_size = position_size
        
        # 挂限价单
        success = self._place_limit_order(signal)
        
        if success:
            signal.status = SignalStatus.LIMIT_PLACED.value
            self.active_orders[signal.symbol] = signal
            if signal.symbol in self.observation_pool:
                del self.observation_pool[signal.symbol]
            
            # 发送Telegram通知
            self._send_signal_notification(signal, confidence, reasoning)
            
            print(f"[HIGH_VOL] 📝 挂单成功: {signal.symbol} {direction.upper()} @ ${entry_price:.6f}")
        else:
            print(f"[HIGH_VOL] ⚠️ 挂单失败: {signal.symbol}")
        
        self._save_signal(signal)
    
    def _build_ai_prompt(self, signal: HighVolSignal, df: pd.DataFrame, 
                         btc_status: Dict, support: float, resistance: float,
                         trend_context: Dict = None) -> str:
        """构建AI prompt - 🔥v2.1 集成趋势分析上下文"""
        
        price = float(df['close'].iloc[-1])
        rsi = self._calculate_rsi(df)
        
        dist_support = (price - support) / price * 100 if support > 0 else 999
        dist_resist = (resistance - price) / price * 100 if resistance > 0 else 999
        
        # 🔥 v1.1: 获取BTC详细信息
        btc_change_1h = btc_status.get('price_change_1h', 0)
        btc_trend = btc_status.get('trend', 'neutral')
        
        # 🔥 v1.1: 计算成交量比率
        vol_ma = float(df['volume'].iloc[-20:].mean()) if len(df) >= 20 else 1
        vol_now = float(df['volume'].iloc[-1])
        vol_ratio = vol_now / vol_ma if vol_ma > 0 else 1
        
        # 🔥🔥🔥 v2.0: 获取突破质量指标
        cvd_div = signal.cvd_divergence
        cvd_score = signal.cvd_score
        er = signal.efficiency_ratio
        hurst = signal.hurst_value
        bq_score = signal.breakout_quality
        is_fake = signal.is_fake_breakout
        
        # 判断趋势状态
        hurst_status = "趋势持续" if hurst > 0.55 else "均值回归" if hurst < 0.45 else "随机游走"
        er_status = "趋势纯净" if er > 0.6 else "震荡市" if er < 0.3 else "趋势形成中"
        
        # 🔥🔥🔥 v2.1新增: 解析趋势上下文
        trend_section = ""
        if trend_context:
            fdi = trend_context.get("fdi_value", 1.35)
            fdi_quality = trend_context.get("fdi_quality", "moderate")
            is_smart_money = trend_context.get("is_smart_money", False)
            sm_type = trend_context.get("smart_money_type", "neutral")
            trend_bias = trend_context.get("trend_bias_score", 0)
            is_squeeze = trend_context.get("is_squeeze", False)
            recommendation = trend_context.get("recommendation", "neutral")
            
            fdi_desc = {
                "excellent": "趋势极纯净(噪音极少)",
                "good": "趋势良好(噪音较少)",
                "moderate": "趋势一般(有一定噪音)",
                "noisy": "趋势嘈杂(噪音大,易扫损)"
            }.get(fdi_quality, "未知")
            
            sm_desc = f"✅聪明钱在{sm_type}" if is_smart_money else "❌无明显主力痕迹"
            rec_desc = {"long_bias": "⬆️偏多", "short_bias": "⬇️偏空", "neutral": "↔️中性", "avoid": "⚠️回避"}.get(recommendation, "未知")
            
            trend_section = f"""
### 🔮 趋势深度分析 (v2.1新增)

| 指标 | 数值 | 解读 |
|------|------|------|
| **FDI分形维数** | {fdi:.3f} | {fdi_desc} |
| **聪明钱** | {sm_desc} | |
| **布林带** | {'🔥蓄势收窄中' if is_squeeze else '正常波动'} | |
| **综合偏向** | {trend_bias:+.2f} | {rec_desc} |

**🎯 FDI决策规则**:
- FDI < 1.25: 可积极入场，entry_offset=1-1.5%
- FDI 1.25-1.35: 正常入场，entry_offset=1.5-2%
- FDI 1.35-1.45: 谨慎入场，entry_offset=2-3% (挂远接针)
- FDI > 1.45: 建议unclear，走势太乱

**🎯 聪明钱规则**:
- 聪明钱吸筹(accumulation) + RSI<40 → 强烈看多
- 聪明钱出货(distribution) + RSI>60 → 强烈看空
- 无聪明钱迹象 → 依赖其他指标

---
"""
        
        prompt = f"""## 高波动币蓄势预判 - 🔥v2.1趋势融合审核

🚨🚨🚨 **核心指标检查** 🚨🚨🚨
{trend_section}
### 1️⃣ CVD背离检测 (假突破识别)
- CVD背离类型: {cvd_div} {'⚠️假突破风险!' if is_fake else '✅正常'}
- CVD信号质量: {cvd_score:.0f}/100 {'❌低质量' if cvd_score < 50 else '✅良好'}

### 2️⃣ 效率比 & Hurst
- 效率比(ER): {er:.2f} → {er_status}
- Hurst指数: {hurst:.2f} → {hurst_status}

### 📊 综合突破质量: {bq_score:.0f}/100 {'✅优质' if bq_score >= 60 else '⚠️一般' if bq_score >= 40 else '❌劣质'}

---

🚨🚨🚨 **必须检查的拒绝条件** 🚨🚨🚨

1. ❓ **假突破**: {'⚠️检测到!' if is_fake else '✅未检测到'}
2. ❓ **BTC方向**: {btc_change_1h:+.2f}% {'⚠️大跌中' if btc_change_1h < -1.5 else '⚠️大涨中' if btc_change_1h > 1.5 else '✅正常'}
3. ❓ **RSI位置**: {rsi:.1f} {'⚠️中性区' if 40 <= rsi <= 60 else '✅有方向'}
4. ❓ **追涨追跌**: {signal.change_24h*100:+.1f}% {'⚠️风险' if abs(signal.change_24h) > 0.15 else '✅正常'}
5. ❓ **成交量**: {vol_ratio:.1f}x {'✅放量' if vol_ratio >= 1.5 else '⚠️缩量'}

### 币种信息
- 交易对: {signal.symbol}
- 当前价: ${price:.8f}
- 24h涨跌: {signal.change_24h*100:+.1f}%
- ATR波动率: {signal.atr_pct*100:.2f}%

### 蓄势特征
- 就绪分数: {signal.readiness_score}/100
- 特征: {', '.join(signal.readiness_details)}

### 技术位置
- RSI: {rsi:.1f}
- 成交量: {vol_ratio:.1f}x均量
- 支撑: ${support:.8f} ({dist_support:+.1f}%)
- 阻力: ${resistance:.8f} ({dist_resist:+.1f}%)

### BTC状态
- 趋势: {btc_trend} | 1h变化: {btc_change_1h:+.2f}%
- 与该币相关性: {signal.btc_correlation:.2f}

---

### 🧠 AI决策矩阵

| 条件组合 | 决策 |
|---------|------|
| FDI<1.3 + 无假突破 + 聪明钱配合 | ✅ 高置信度，挂近单 |
| FDI<1.3 + 无假突破 + 无聪明钱 | ⚠️ 可入场，标准挂单 |
| FDI 1.3-1.45 + 其他条件好 | ⚠️ 谨慎，挂远单接针 |
| FDI>1.45 或 假突破 | ❌ unclear |
| BTC大跌+做多 或 BTC大涨+做空 | ❌ unclear |

请返回JSON:
```json
{{
    "direction": "long或short或unclear",
    "entry_offset_pct": 0.015,  // 根据FDI调整: FDI越小越近，FDI越大越远
    "take_profit_pct": 0.06,
    "confidence": 0.7,
    "reasoning": "需提及FDI/聪明钱/CVD分析"
}}
```

💡 优先给出明确方向，只有真正无法判断才返回unclear！
"""
        return prompt
    
    def _call_ai(self, prompt: str) -> Optional[Dict]:
        """调用DeepSeek AI - 🔥v1.3平衡版"""
        
        if not self.ai_api_key:
            print("[HIGH_VOL] ⚠️ 未配置AI API Key")
            return None
        
        try:
            headers = {
                "Authorization": f"Bearer {self.ai_api_key}",
                "Content-Type": "application/json"
            }
            
            # 🔥🔥🔥 v2.0: 升级版 - 加入CVD/ER/Hurst三大指标
            system_prompt = """你是加密货币交易分析专家，负责审核高波动币种的蓄势信号。

🎯 **核心原则**：在风险可控的前提下，积极捕捉机会

🔥🔥🔥 **v2.0新增: 三大核心指标** 🔥🔥🔥

1. **CVD背离检测 (权重40%)**
   - CVD背离 = 价格和成交量方向不一致
   - 价格涨+CVD跌 = 假突破，卖盘在出货
   - 价格跌+CVD涨 = 假跌，有买盘接货
   - ⚠️ CVD背离强度>50时必须谨慎

2. **效率比ER (权重30%)**
   - ER>0.6: 趋势纯净，可以跟随
   - ER<0.3: 震荡市，不适合趋势策略
   - ER 0.3-0.6: 趋势形成中

3. **Hurst指数 (权重30%)**
   - H>0.55: 趋势会延续
   - H<0.45: 价格会反转
   - H≈0.5: 随机游走

✅ **应该通过的情况**：
1. 突破质量≥60 + 无CVD背离
2. ER>0.5 + Hurst>0.5 (趋势确认)
3. 布林带收窄 + 接近支撑位做多或阻力位做空
4. BTC稳定（1h变化<1%）或方向配合

⛔ **必须拒绝的情况**：
1. CVD检测到假突破 (背离强度>50)
2. ER<0.25 (完全震荡市)
3. BTC大跌(1h跌>1.5%)时做多
4. BTC大涨(1h涨>1.5%)时做空
5. 追涨追跌：已涨>25%还做多，已跌>25%还做空

📊 **审核目标**：
- 通过率：30-40% (比之前更严格)
- 必须在reasoning中提及CVD/ER/Hurst分析
- 只有真正无法判断时才返回unclear

只返回JSON格式。"""
            
            data = {
                "model": self.ai_model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt}
                ],
                "temperature": 0.3,  # 🔥 v1.3: 0.15 -> 0.3 更灵活
                "max_tokens": 500
            }
            
            response = requests.post(
                f"{self.ai_base_url}/v1/chat/completions",
                headers=headers,
                json=data,
                timeout=self.ai_timeout
            )
            
            if response.status_code != 200:
                print(f"[HIGH_VOL] AI调用失败: {response.status_code}")
                return None
            
            result = response.json()
            content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
            
            # 解析JSON
            return self._parse_ai_response(content)
            
        except Exception as e:
            print(f"[HIGH_VOL] AI调用异常: {e}")
            return None
    
    def _parse_ai_response(self, content: str) -> Optional[Dict]:
        """解析AI响应中的JSON"""
        import re
        
        try:
            # 尝试直接解析
            return json.loads(content)
        except:
            pass
        
        try:
            # 尝试提取JSON块
            match = re.search(r'\{[^{}]*\}', content, re.DOTALL)
            if match:
                return json.loads(match.group())
        except:
            pass
        
        return None
    
    # 🔥🔥🔥 v3.4新增: OKX交易对验证
    _okx_symbols_cache = None  # 类级别缓存
    _okx_symbols_cache_time = None
    
    def _validate_okx_symbol(self, symbol: str) -> bool:
        """
        验证OKX是否支持该交易对
        
        使用缓存避免频繁API调用，缓存1小时
        """
        import time as time_module
        
        # 检查缓存是否有效（1小时）
        if (HighVolatilityTrack._okx_symbols_cache is not None and 
            HighVolatilityTrack._okx_symbols_cache_time is not None and
            time_module.time() - HighVolatilityTrack._okx_symbols_cache_time < 3600):
            return symbol in HighVolatilityTrack._okx_symbols_cache
        
        # 刷新缓存
        try:
            if self.auto_trader and self.auto_trader.exchange:
                markets = self.auto_trader.exchange.load_markets()
                HighVolatilityTrack._okx_symbols_cache = set(markets.keys())
                HighVolatilityTrack._okx_symbols_cache_time = time_module.time()
                print(f"[HIGH_VOL] 🔄 刷新OKX交易对缓存: {len(HighVolatilityTrack._okx_symbols_cache)}个")
                return symbol in HighVolatilityTrack._okx_symbols_cache
        except Exception as e:
            print(f"[HIGH_VOL] ⚠️ 获取OKX交易对失败: {e}")
        
        # 如果获取失败，默认允许（让后续挂单时报错）
        return True
    
    # ==================== 第四步：挂单管理 ====================
    
    def _place_limit_order(self, signal: HighVolSignal) -> bool:
        """挂限价单"""
        
        if self.auto_trader is None:
            print("[HIGH_VOL] ⚠️ AutoTrader未初始化")
            return False
        
        try:
            # 调用AutoTrader挂限价单
            order_result = self.auto_trader.place_limit_order(
                symbol=signal.symbol,
                side=signal.side,
                amount=signal.position_size,
                price=signal.entry_price,
                stop_loss=signal.stop_loss,
                take_profit=signal.take_profit,
                order_tag=f"hv_{signal.id}"
            )
            
            if order_result and order_result.get("success"):
                signal.limit_order_id = order_result.get("order_id", "")
                return True
            
            return False
            
        except Exception as e:
            print(f"[HIGH_VOL] 挂单异常: {e}")
            return False
    
    def _check_limit_orders(self, now_str: str):
        """检查限价单状态"""
        
        to_remove = []
        
        for symbol, signal in self.active_orders.items():
            # 检查是否超时
            updated = datetime.fromisoformat(signal.updated_at.replace('Z', '+00:00'))
            age_sec = (datetime.now(timezone.utc) - updated).total_seconds()
            
            if age_sec > self.order_valid_sec:
                # 超时，取消订单
                self._cancel_limit_order(signal)
                
                if signal.ai_reviews < self.max_ai_reviews:
                    # 还有重评机会，重新AI评估
                    signal.status = SignalStatus.READY.value
                    self.observation_pool[symbol] = signal
                    print(f"[HIGH_VOL] ⏰ 挂单超时，重新评估: {symbol} (第{signal.ai_reviews}/{self.max_ai_reviews}次)")
                else:
                    # 放弃
                    signal.status = SignalStatus.EXPIRED.value
                    print(f"[HIGH_VOL] ❌ 挂单超时放弃: {symbol}")
                
                signal.updated_at = now_str
                self._save_signal(signal)
                to_remove.append(symbol)
                continue
            
            # 检查是否成交
            if self.auto_trader:
                order_status = self.auto_trader.check_order_status(signal.limit_order_id)
                
                if order_status == "filled":
                    signal.status = SignalStatus.FILLED.value
                    signal.filled_at = now_str
                    signal.updated_at = now_str
                    self._save_signal(signal)
                    
                    self.active_positions[symbol] = signal
                    to_remove.append(symbol)
                    
                    print(f"[HIGH_VOL] ✅ 成交: {symbol} {signal.side.upper()} @ ${signal.entry_price:.6f}")
                    self._send_fill_notification(signal)
        
        for symbol in to_remove:
            if symbol in self.active_orders:
                del self.active_orders[symbol]
    
    def _cancel_limit_order(self, signal: HighVolSignal):
        """取消限价单"""
        if self.auto_trader and signal.limit_order_id:
            try:
                self.auto_trader.cancel_order(signal.limit_order_id, signal.symbol)
            except Exception as e:
                print(f"[HIGH_VOL] 取消订单异常: {e}")
    
    # ==================== 第五步：持仓监控 ====================
    
    def _monitor_positions(self, all_klines: Dict[str, pd.DataFrame], btc_status: Dict, now_str: str):
        """监控持仓"""
        
        to_close = []
        
        for symbol, signal in self.active_positions.items():
            df = all_klines.get(symbol)
            if df is None:
                continue
            
            current_price = float(df['close'].iloc[-1])
            
            # 计算当前盈亏
            if signal.side == "long":
                pnl_pct = (current_price - signal.entry_price) / signal.entry_price
            else:
                pnl_pct = (signal.entry_price - current_price) / signal.entry_price
            
            signal.current_pnl = pnl_pct
            
            # 检查持仓时间
            filled = datetime.fromisoformat(signal.filled_at.replace('Z', '+00:00'))
            hold_hours = (datetime.now(timezone.utc) - filled).total_seconds() / 3600
            
            if hold_hours > self.max_hold_hours:
                # 超时平仓
                to_close.append((symbol, "timeout", f"持仓超{self.max_hold_hours}小时"))
                continue
            
            # 检查是否需要调整止损
            action, reason, new_sl = self._check_position_health(signal, df, btc_status, current_price)
            
            if action == "move_sl" and new_sl:
                # 移动止损
                self._update_stop_loss(signal, new_sl)
                signal.stop_loss = new_sl
                print(f"[HIGH_VOL] 📍 移动止损: {symbol} → ${new_sl:.6f} ({reason})")
            
            elif action == "close":
                to_close.append((symbol, "ai_close", reason))
            
            signal.updated_at = now_str
            self._save_signal(signal)
        
        # 执行平仓
        for symbol, close_type, reason in to_close:
            signal = self.active_positions.get(symbol)
            if signal:
                self._close_position(signal, close_type, reason, now_str)
    
    def _check_position_health(self, signal: HighVolSignal, df: pd.DataFrame, 
                                btc_status: Dict, current_price: float) -> Tuple[str, str, Optional[float]]:
        """
        检查持仓健康度
        
        Returns:
            (动作, 原因, 新止损价)
            动作: hold/move_sl/close
        """
        entry_price = signal.entry_price
        side = signal.side
        
        # 计算盈亏
        if side == "long":
            pnl_pct = (current_price - entry_price) / entry_price
        else:
            pnl_pct = (entry_price - current_price) / entry_price
        
        # ===== 盈利中：移动止损 =====
        if pnl_pct > 0.05:
            # 盈利>5%，止损移到+3%/+1.5%
            if side == "long":
                new_sl = entry_price * 1.03
            else:
                new_sl = entry_price * 0.97  # 做空：止损在成本价下方3%
            return "move_sl", f"盈利{pnl_pct*100:.1f}%，止损→+3%", new_sl
        
        elif pnl_pct > 0.03:
            # 盈利>3%，止损移到+1%
            if side == "long":
                new_sl = entry_price * 1.01
            else:
                new_sl = entry_price * 0.99  # 做空：止损在成本价下方1%
            return "move_sl", f"盈利{pnl_pct*100:.1f}%，止损→+1%", new_sl
        
        elif pnl_pct > 0.004:  # 🔥 v1.3: 0.8% → 0.4%
            # 盈利>0.4%，止损移到保本（成本价+0.1%缓冲）
            if side == "long":
                new_sl = entry_price * 1.001  # 做多：止损在成本价上方0.1%
            else:
                new_sl = entry_price * 0.999  # 做空：止损在成本价下方0.1%
            return "move_sl", f"盈利{pnl_pct*100:.1f}%，止损→保本", new_sl
        
        # ===== 亏损中：检查异常 =====
        if pnl_pct < -0.005:
            # 检查BTC异动
            btc_change_5m = btc_status.get('change_5m', 0)
            if abs(btc_change_5m) > 0.015:  # BTC 5分钟波动>1.5%
                return "close", f"BTC异动{btc_change_5m*100:+.1f}%，提前平仓", None
            
            # 检查连续反向K线
            if len(df) >= 5:
                recent = df.tail(5)
                if side == "long":
                    all_red = all(recent['close'].iloc[i] < recent['open'].iloc[i] for i in range(len(recent)))
                    if all_red:
                        return "close", "连续5根阴线，提前平仓", None
                else:
                    all_green = all(recent['close'].iloc[i] > recent['open'].iloc[i] for i in range(len(recent)))
                    if all_green:
                        return "close", "连续5根阳线，提前平仓", None
        
        return "hold", f"盈亏{pnl_pct*100:+.1f}%", None
    
    def _update_stop_loss(self, signal: HighVolSignal, new_sl: float):
        """更新止损"""
        if self.auto_trader:
            try:
                self.auto_trader.update_stop_loss(signal.symbol, new_sl)
            except Exception as e:
                print(f"[HIGH_VOL] 更新止损异常: {e}")
    
    def _close_position(self, signal: HighVolSignal, close_type: str, reason: str, now_str: str):
        """平仓"""
        
        if self.auto_trader:
            try:
                # 限价平仓
                self.auto_trader.close_position_limit(signal.symbol, signal.side)
            except Exception as e:
                print(f"[HIGH_VOL] 平仓异常: {e}")
        
        if close_type == "timeout":
            signal.status = SignalStatus.TIMEOUT.value
        elif signal.current_pnl > 0:
            signal.status = SignalStatus.PROFIT.value
        else:
            signal.status = SignalStatus.STOPPED.value
        
        signal.updated_at = now_str
        self._save_signal(signal)
        
        if signal.symbol in self.active_positions:
            del self.active_positions[signal.symbol]
        
        emoji = "✅" if signal.current_pnl > 0 else "❌"
        print(f"[HIGH_VOL] {emoji} 平仓: {signal.symbol} | {signal.current_pnl*100:+.1f}% | {reason}")
        
        self._send_close_notification(signal, reason)
    
    # ==================== 清理 ====================
    
    def _cleanup_expired(self, now_str: str):
        """清理过期数据"""
        # 清理观察池中超时的
        to_remove = []
        for symbol, signal in self.observation_pool.items():
            created = datetime.fromisoformat(signal.created_at.replace('Z', '+00:00'))
            age_min = (datetime.now(timezone.utc) - created).total_seconds() / 60
            
            if age_min > self.pool_max_time_min + 5:  # 额外5分钟buffer
                signal.status = SignalStatus.EXPIRED.value
                signal.updated_at = now_str
                self._save_signal(signal)
                to_remove.append(symbol)
        
        for symbol in to_remove:
            del self.observation_pool[symbol]
    
    # ==================== 工具函数 ====================
    
    def _calculate_atr_pct(self, df: pd.DataFrame, period: int = 14) -> float:
        """计算ATR百分比"""
        high = df['high']
        low = df['low']
        close = df['close']
        
        tr1 = high - low
        tr2 = abs(high - close.shift(1))
        tr3 = abs(low - close.shift(1))
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr = tr.rolling(window=period).mean().iloc[-1]
        
        price = close.iloc[-1]
        return float(atr / price) if price > 0 else 0.02
    
    def _calculate_bb_width(self, df: pd.DataFrame, period: int = 20) -> float:
        """计算布林带宽度"""
        close = df['close']
        ma = close.rolling(window=period).mean()
        std = close.rolling(window=period).std()
        
        upper = ma + 2 * std
        lower = ma - 2 * std
        
        width = (upper.iloc[-1] - lower.iloc[-1]) / ma.iloc[-1]
        return float(width) if not np.isnan(width) else 0.05
    
    def _calculate_bb_width_ma(self, df: pd.DataFrame, lookback: int = 20) -> float:
        """计算布林带宽度均值"""
        widths = []
        for i in range(lookback):
            if len(df) > 20 + i:
                sub_df = df.iloc[-(20+i+1):-(i+1)] if i > 0 else df.iloc[-21:]
                if len(sub_df) >= 20:
                    w = self._calculate_bb_width(sub_df)
                    widths.append(w)
        
        return np.mean(widths) if widths else 0.05
    
    def _get_percentile(self, df: pd.DataFrame, value: float, metric: str, lookback: int = 100) -> float:
        """计算某指标在近期的百分位"""
        if metric == 'bb_width':
            values = []
            for i in range(min(lookback, len(df) - 20)):
                sub_df = df.iloc[-(20+i+1):-(i+1)] if i > 0 else df.iloc[-21:]
                if len(sub_df) >= 20:
                    w = self._calculate_bb_width(sub_df)
                    values.append(w)
            
            if not values:
                return 50
            
            values = sorted(values)
            position = sum(1 for v in values if v < value)
            return (position / len(values)) * 100
        
        return 50
    
    def _find_key_levels(self, df: pd.DataFrame) -> Tuple[float, float]:
        """找支撑位和阻力位"""
        if len(df) < 50:
            return 0, 0
        
        price = float(df['close'].iloc[-1])
        
        # 近期高低点
        recent_low = float(df['low'].tail(100).min())
        recent_high = float(df['high'].tail(100).max())
        
        # 简单支撑阻力
        support = recent_low
        resistance = recent_high
        
        # 如果价格离支撑太远，用近期低点
        if price - support > price * 0.05:
            support = float(df['low'].tail(20).min())
        
        # 如果价格离阻力太远，用近期高点
        if resistance - price > price * 0.05:
            resistance = float(df['high'].tail(20).max())
        
        return support, resistance
    
    # ==================== 🔥v2.0新增: 突破质量计算 ====================
    
    def _calculate_breakout_quality(self, df: pd.DataFrame, lookback: int = 20) -> Dict:
        """
        🔥 v2.0新增: 综合突破质量评估
        
        整合CVD、Efficiency Ratio、Hurst三个核心指标
        
        Returns:
            {
                "cvd_divergence": 背离类型,
                "cvd_score": CVD信号质量,
                "efficiency_ratio": 效率比,
                "hurst_value": Hurst指数,
                "overall_score": 综合评分,
                "is_fake_breakout": 是否假突破,
                "recommendation": 建议
            }
        """
        result = {
            "cvd_divergence": "none",
            "cvd_score": 50.0,
            "efficiency_ratio": 0.5,
            "hurst_value": 0.5,
            "overall_score": 50.0,
            "is_fake_breakout": False,
            "recommendation": ""
        }
        
        try:
            # 1. CVD背离检测
            cvd_result = self._quick_cvd_check(df, lookback)
            result["cvd_divergence"] = cvd_result["divergence"]
            result["cvd_score"] = cvd_result["signal_quality"]
            result["is_fake_breakout"] = cvd_result["is_fake_breakout"]
            
            # 2. 效率比
            result["efficiency_ratio"] = self._quick_efficiency_ratio(df, lookback)
            
            # 3. Hurst指数
            result["hurst_value"] = self._calculate_hurst(df, lookback * 3)
            
            # 4. 综合评分
            # CVD权重40% (假突破检测最重要)
            cvd_score = result["cvd_score"]
            if result["is_fake_breakout"]:
                cvd_score = max(0, cvd_score - 30)
            
            # ER权重30%
            er = result["efficiency_ratio"]
            er_score = er * 100  # 转为0-100
            
            # Hurst权重30%
            h = result["hurst_value"]
            # Hurst>0.5为趋势，<0.5为回归，都有用，关键是不要≈0.5
            hurst_score = abs(h - 0.5) * 200  # 距离0.5越远分数越高
            
            result["overall_score"] = round(cvd_score * 0.4 + er_score * 0.3 + hurst_score * 0.3, 1)
            
            # 5. 生成建议
            if result["is_fake_breakout"]:
                result["recommendation"] = f"⚠️假突破! CVD背离:{cvd_result['divergence']}"
            elif result["overall_score"] >= 60:
                result["recommendation"] = f"✅优质信号 CVD:{cvd_score:.0f} ER:{er:.2f} H:{h:.2f}"
            else:
                weak = []
                if cvd_score < 50: weak.append("CVD弱")
                if er < 0.4: weak.append("ER低")
                if abs(h - 0.5) < 0.1: weak.append("Hurst中性")
                result["recommendation"] = f"⚠️信号一般: {', '.join(weak)}"
            
        except Exception as e:
            print(f"[HIGH_VOL] 突破质量计算异常: {e}")
        
        return result
    
    def _calculate_hurst(self, df: pd.DataFrame, period: int = 60) -> float:
        """
        🔥 v2.0新增: 计算Hurst指数
        
        H > 0.5: 趋势持续
        H = 0.5: 随机游走
        H < 0.5: 均值回归
        """
        try:
            if len(df) < period:
                return 0.5
            
            close = df['close'].tail(period)
            max_lag = min(20, period // 3)
            lags = range(2, max_lag)
            
            tau = []
            for lag in lags:
                diff = close.values[lag:] - close.values[:-lag]
                if len(diff) > 0:
                    tau.append(np.std(diff))
                else:
                    tau.append(1e-10)
            
            if len(tau) < 3:
                return 0.5
            
            log_lags = np.log(list(lags))
            log_tau = np.log(np.array(tau) + 1e-10)
            
            slope, _ = np.polyfit(log_lags, log_tau, 1)
            hurst = max(0.0, min(1.0, slope))
            
            return round(float(hurst), 4)
        except:
            return 0.5
    
    def _calculate_btc_correlation(self, df: pd.DataFrame, btc_df: pd.DataFrame) -> float:
        """计算与BTC的相关性"""
        if btc_df is None or len(df) < 60 or len(btc_df) < 60:
            return 0.5
        
        min_len = min(len(df), len(btc_df), 60)
        
        coin_returns = df['close'].pct_change().tail(min_len).values
        btc_returns = btc_df['close'].pct_change().tail(min_len).values
        
        # 去除NaN
        mask = np.isfinite(coin_returns) & np.isfinite(btc_returns)
        coin_returns = coin_returns[mask]
        btc_returns = btc_returns[mask]
        
        if len(coin_returns) < 20:
            return 0.5
        
        corr = np.corrcoef(coin_returns, btc_returns)[0, 1]
        return float(corr) if not np.isnan(corr) else 0.5
    
    def _calculate_rsi(self, df: pd.DataFrame, period: int = 14) -> float:
        """计算RSI"""
        delta = df['close'].diff()
        gain = delta.where(delta > 0, 0).rolling(window=period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
        
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        
        return float(rsi.iloc[-1]) if not np.isnan(rsi.iloc[-1]) else 50
    
    def _calculate_stop_loss_pct(self, atr_pct: float) -> float:
        """根据ATR计算止损百分比，上限2%"""
        for threshold, sl in sorted(self.sl_atr_multipliers.items()):
            if atr_pct < threshold:
                return min(sl, self.sl_max)
        return self.sl_max
    
    def _calculate_position_size(self, signal: HighVolSignal) -> float:
        """计算仓位大小 - v1.3 修复杠杆计算 + 最小保证金检查"""
        if self.auto_trader is None:
            return 0
        
        # 获取可用资金
        available = self.auto_trader.get_available_balance()
        
        # 🔥 v1.3: 最小保证金检查 - 至少需要5U才能开仓
        MIN_MARGIN_USDT = 5.0
        if available < MIN_MARGIN_USDT:
            print(f"[HIGH_VOL] ⚠️ 可用资金${available:.2f} < 最小保证金${MIN_MARGIN_USDT}，跳过开仓")
            return 0
        
        # 轨道2资金池
        track_capital = available * self.track_capital_pct
        
        # 单笔保证金 (这是我们要投入的保证金金额)
        margin = track_capital * self.single_position_pct
        
        # 🔥 v1.3: 确保保证金至少5U
        if margin < MIN_MARGIN_USDT:
            margin = MIN_MARGIN_USDT
            print(f"[HIGH_VOL] 📊 保证金不足，使用最小保证金${MIN_MARGIN_USDT}")
        
        # 🔥 v1.3: 确保不超过可用资金
        if margin > available:
            margin = available * 0.9  # 留10%余量
            print(f"[HIGH_VOL] 📊 保证金超过可用资金，调整为${margin:.2f}")
        
        # 高波动减仓
        if abs(signal.change_24h) > 0.20:
            margin *= self.high_vol_reduce
            # 减仓后仍需满足最小保证金
            if margin < MIN_MARGIN_USDT:
                print(f"[HIGH_VOL] ⚠️ 高波动减仓后保证金${margin:.2f} < ${MIN_MARGIN_USDT}，跳过")
                return 0
        
        # 🔥 v1.3: 获取杠杆倍数
        leverage = getattr(self.auto_trader, 'default_leverage', 20) or 20
        
        # 🔥 v1.3: 计算名义仓位 = 保证金 × 杠杆
        position_value = margin * leverage
        
        # 计算数量
        if signal.entry_price > 0:
            size = position_value / signal.entry_price
            
            # 🔥 v1.4: 检查最小交易数量（避免OKX精度错误）
            try:
                if self.auto_trader and self.auto_trader.exchange:
                    market = self.auto_trader.exchange.market(signal.symbol)
                    min_amount = market.get('limits', {}).get('amount', {}).get('min', 1)
                    if min_amount and size < min_amount:
                        print(f"[HIGH_VOL] ⚠️ 计算数量{size:.2f} < 最小数量{min_amount}，跳过 {signal.symbol}")
                        return 0
            except Exception as e:
                # 如果获取市场信息失败，使用默认检查
                if size < 1:
                    print(f"[HIGH_VOL] ⚠️ 计算数量{size:.2f} < 1，跳过 {signal.symbol}")
                    return 0
            
            print(f"[HIGH_VOL] 📊 仓位计算: 保证金${margin:.2f} x {leverage}x = 名义${position_value:.2f} = {size:.6f} {signal.symbol}")
            return size
        
        return 0
    
    # ==================== Telegram通知 ====================
    
    def _send_signal_notification(self, signal: HighVolSignal, confidence: float, reasoning: str):
        """发送信号通知"""
        
        sl_pct = abs(signal.stop_loss - signal.entry_price) / signal.entry_price * 100
        tp_pct = abs(signal.take_profit - signal.entry_price) / signal.entry_price * 100
        
        emoji_side = "📈" if signal.side == "long" else "📉"
        
        msg = f"""⚡ 高波动信号 | {signal.symbol} {signal.side.upper()} {emoji_side}

🌪️ 类型: 蓄势预判
💰 当前价: ${signal.signal_price:.8f}
📊 24h波动: {signal.change_24h*100:+.1f}%
📦 24h成交: {signal.volume_24h/1e6:.1f}M USDT
🎯 就绪分数: {signal.readiness_score}/100

📍 限价买入: ${signal.entry_price:.8f}
🛑 止损: ${signal.stop_loss:.8f} (-{sl_pct:.1f}%)
✅ 止盈: ${signal.take_profit:.8f} (+{tp_pct:.1f}%)
⏱️ 挂单有效: 5分钟

📊 BTC相关性: {signal.btc_correlation:.2f}
🤖 AI置信度: {confidence:.0%}
💡 理由: {reasoning}

💡 蓄势特征: {', '.join(signal.readiness_details[:3])}"""
        
        self._send_telegram(msg)
    
    def _send_fill_notification(self, signal: HighVolSignal):
        """发送成交通知"""
        
        msg = f"""✅ 高波动成交 | {signal.symbol}

方向: {signal.side.upper()}
成交价: ${signal.entry_price:.8f}
止损: ${signal.stop_loss:.8f}
止盈: ${signal.take_profit:.8f}

⏱️ 最长持有: {self.max_hold_hours}小时"""
        
        self._send_telegram(msg)
    
    def _send_close_notification(self, signal: HighVolSignal, reason: str):
        """发送平仓通知"""
        
        emoji = "✅" if signal.current_pnl > 0 else "❌"
        
        msg = f"""{emoji} 高波动平仓 | {signal.symbol}

方向: {signal.side.upper()}
盈亏: {signal.current_pnl*100:+.1f}%
原因: {reason}"""
        
        self._send_telegram(msg)
    
    def _send_telegram(self, msg: str):
        """发送Telegram消息"""
        if not self.tg_bot_token or not self.tg_chat_ids:
            return
        
        try:
            for chat_id in self.tg_chat_ids:
                url = f"https://api.telegram.org/bot{self.tg_bot_token}/sendMessage"
                requests.post(url, json={
                    "chat_id": chat_id,
                    "text": msg,
                    "parse_mode": "HTML"
                }, timeout=10)
        except Exception as e:
            print(f"[HIGH_VOL] Telegram发送失败: {e}")
    
    # ==================== 状态查询 ====================
    
    def get_status(self) -> Dict:
        """获取轨道状态"""
        return {
            "enabled": self.enabled,
            "observation_pool": len(self.observation_pool),
            "active_orders": len(self.active_orders),
            "active_positions": len(self.active_positions),
            "pool_capacity": self.pool_capacity,
            "max_orders": self.max_concurrent_orders,
            "symbols_watching": list(self.observation_pool.keys()),
            "symbols_ordered": list(self.active_orders.keys()),
            "symbols_holding": list(self.active_positions.keys()),
        }