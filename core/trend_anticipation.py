# core/trend_anticipation.py - 趋势预判支持模块 v2.1
# -*- coding: utf-8 -*-
"""
趋势预判支持模块 v2.1

🔥🔥🔥 v2.1 重大更新 (高波动轨道集成版):
1. 新增 analyze_trend_context() 独立分析函数
2. 供高波动轨道AI审核调用
3. 不再独立推送信号，作为分析工具使用

🔥🔥🔥 v2.0 重大更新 (智能趋势识别版):
1. 新增FDI分形维数 - 识别趋势纯度，过滤噪音趋势
2. 新增OI/Volume Ratio - 识别聪明钱，判断趋势真假
3. 新增Efficiency Ratio - 评估趋势效率
4. 新增trend_quality_score综合评估
5. 信号输出新增趋势质量指标

🔥 v1.3 更新 (v7.9.4 平衡版):
1. RSI区间放宽: [15,25] / [75,85]
2. ADX阈值: 28 -> 22
3. 成交量阈值: 1.0x -> 0.8x
4. 评分阈值: 0.85 -> 0.75
5. 去重冷却期: 45 -> 30分钟
6. 最低条件数: 4 -> 3

功能：
1. 支撑位/阻力位检测
2. K线形态识别
3. 成交量结构分析
4. 多时间框架分析
5. 信号去重
6. 趋势预判信号生成
7. 🆕 FDI趋势纯度检测
8. 🆕 聪明钱分析
9. 🆕 v2.1: analyze_trend_context() 供外部调用
"""

import time
import numpy as np
import pandas as pd
import datetime as dt
from typing import Dict, List, Optional, Tuple, Any

# 🔥 v2.0: 导入新指标函数
try:
    from .utils import (
        fractal_dimension, fdi_analysis,
        smart_money_analysis, oi_volume_ratio,
        efficiency_ratio_trend, hurst_analysis,
        trend_quality_score
    )
    HAS_TREND_INDICATORS = True
except ImportError:
    HAS_TREND_INDICATORS = False
    print("[TREND_ANTICIPATION] ⚠️ 新指标函数未找到，使用内置版本")

# ============ 全局缓存 ============
_SIGNAL_DEDUP_CACHE: Dict[str, Dict] = {}
_MTF_KLINE_CACHE: Dict[str, Dict] = {}
_MTF_CACHE_TTL = 60
_TRADE_HISTORY: List[Dict] = []


# ============ 🔥v2.0新增: 内置FDI计算 ============
def _calculate_fdi(df: pd.DataFrame, period: int = 30) -> float:
    """
    计算分形维数 FDI (Fractal Dimension Index) - 内置版本
    
    FDI接近1.0 = 强趋势（直线）
    FDI接近1.5 = 震荡（布朗运动）
    """
    if len(df) < period:
        return 1.25  # 默认中性
    
    try:
        prices = df['close'].tail(period).values
        n = len(prices)
        
        # 简化Higuchi方法
        k_max = min(8, n // 4)
        L = []
        
        for k in range(1, k_max + 1):
            Lk = []
            for m in range(1, k + 1):
                indices = np.arange(m - 1, n, k)
                if len(indices) < 2:
                    continue
                sub_prices = prices[indices]
                length = np.sum(np.abs(np.diff(sub_prices))) * (n - 1) / (k * len(indices))
                if length > 0:
                    Lk.append(length)
            
            if Lk:
                L.append((k, np.mean(Lk)))
        
        if len(L) < 3:
            return 1.25
        
        log_k = np.log([x[0] for x in L])
        log_L = np.log([x[1] for x in L])
        
        slope, _ = np.polyfit(log_k, log_L, 1)
        fdi = -slope
        
        return max(1.0, min(1.5, float(fdi)))
    except:
        return 1.25


def _analyze_smart_money(price_change: float, oi_change: float, volume: float) -> Dict:
    """
    🔥v2.0新增: 聪明钱分析 - 内置版本
    
    判断趋势是否由真实资金推动
    """
    if volume <= 0:
        return {"is_smart_money": False, "trend_type": "unknown", "quality_score": 50}
    
    ratio = oi_change / volume
    
    if price_change > 0:  # 价格上涨
        if oi_change > 0 and ratio > 0.3:
            return {
                "is_smart_money": True,
                "trend_type": "accumulation",
                "quality_score": min(100, 50 + ratio * 100)
            }
        elif oi_change < 0:
            return {
                "is_smart_money": False,
                "trend_type": "short_squeeze",
                "quality_score": max(0, 50 - abs(ratio) * 50)
            }
    else:  # 价格下跌
        if oi_change > 0 and ratio > 0.3:
            return {
                "is_smart_money": True,
                "trend_type": "distribution",
                "quality_score": min(100, 50 + ratio * 100)
            }
        elif oi_change < 0:
            return {
                "is_smart_money": False,
                "trend_type": "long_liquidation",
                "quality_score": max(0, 50 - abs(ratio) * 50)
            }
    
    return {"is_smart_money": False, "trend_type": "neutral", "quality_score": 50}


# ============ 支撑位/阻力位检测 ============
def detect_support_resistance(df: pd.DataFrame, cfg: Dict, side: str, current_price: float = None) -> Dict:
    """
    检测支撑位和阻力位
    """
    sr_cfg = cfg.get("support_resistance", {})
    if not sr_cfg.get("enabled", True):
        return {"nearest_level": 0, "distance_pct": 999, "level_type": "none", "bonus": 0, "all_levels": []}
    
    if current_price is None:
        current_price = float(df["close"].iloc[-1])
    
    levels = []
    
    if side == "long":
        sources = sr_cfg.get("support_sources", {})
        
        # 24h最低价
        if sources.get("recent_low_24h", True):
            lookback = min(1440, len(df))
            if lookback > 0:
                low_val = float(df["low"].tail(lookback).min())
                levels.append({"price": low_val, "type": "recent_low"})
        
        # 布林带下轨
        if sources.get("bollinger_lower", True) and len(df) >= 20:
            sma = df["close"].rolling(window=20).mean()
            std = df["close"].rolling(window=20).std()
            bb_lower = float(sma.iloc[-1] - 2 * std.iloc[-1])
            if bb_lower > 0:
                levels.append({"price": bb_lower, "type": "bb_lower"})
        
        # EMA200
        if sources.get("ema_200", True) and len(df) >= 200:
            ema200 = float(df["close"].ewm(span=200, adjust=False).mean().iloc[-1])
            if ema200 < current_price:
                levels.append({"price": ema200, "type": "ema_200"})
        
        # 局部低点
        if sources.get("local_lows", True):
            local_lows = _find_local_pivots(df, 5, "low")
            for ll in local_lows[-3:]:
                if ll < current_price:
                    levels.append({"price": ll, "type": "local_low"})
        
        # 整数关口
        if sources.get("round_numbers", True):
            round_levels = _find_round_numbers(current_price, side="long")
            for rl in round_levels:
                levels.append({"price": rl, "type": "round_number"})
    
    else:  # side == "short"
        sources = sr_cfg.get("resistance_sources", {})
        
        # 24h最高价
        if sources.get("recent_high_24h", True):
            lookback = min(1440, len(df))
            if lookback > 0:
                high_val = float(df["high"].tail(lookback).max())
                levels.append({"price": high_val, "type": "recent_high"})
        
        # 布林带上轨
        if sources.get("bollinger_upper", True) and len(df) >= 20:
            sma = df["close"].rolling(window=20).mean()
            std = df["close"].rolling(window=20).std()
            bb_upper = float(sma.iloc[-1] + 2 * std.iloc[-1])
            if bb_upper > 0:
                levels.append({"price": bb_upper, "type": "bb_upper"})
        
        # EMA200
        if sources.get("ema_200", True) and len(df) >= 200:
            ema200 = float(df["close"].ewm(span=200, adjust=False).mean().iloc[-1])
            if ema200 > current_price:
                levels.append({"price": ema200, "type": "ema_200"})
        
        # 局部高点
        if sources.get("local_highs", True):
            local_highs = _find_local_pivots(df, 5, "high")
            for lh in local_highs[-3:]:
                if lh > current_price:
                    levels.append({"price": lh, "type": "local_high"})
        
        # 整数关口
        if sources.get("round_numbers", True):
            round_levels = _find_round_numbers(current_price, side="short")
            for rl in round_levels:
                levels.append({"price": rl, "type": "round_number"})
    
    if not levels:
        return {"nearest_level": 0, "distance_pct": 999, "level_type": "none", "bonus": 0, "all_levels": []}
    
    # 找最近的支撑/阻力位
    if side == "long":
        valid_levels = [l for l in levels if l["price"] < current_price]
        if valid_levels:
            nearest = max(valid_levels, key=lambda x: x["price"])
        else:
            nearest = min(levels, key=lambda x: abs(x["price"] - current_price))
    else:
        valid_levels = [l for l in levels if l["price"] > current_price]
        if valid_levels:
            nearest = min(valid_levels, key=lambda x: x["price"])
        else:
            nearest = min(levels, key=lambda x: abs(x["price"] - current_price))
    
    distance_pct = abs(current_price - nearest["price"]) / current_price if current_price > 0 else 999
    
    # 计算评分加成
    scoring = sr_cfg.get("scoring", {})
    if distance_pct <= scoring.get("distance_very_close", 0.005):
        bonus = scoring.get("bonus_very_close", 0.15)
    elif distance_pct <= scoring.get("distance_close", 0.01):
        bonus = scoring.get("bonus_close", 0.10)
    elif distance_pct <= scoring.get("distance_near", 0.02):
        bonus = scoring.get("bonus_near", 0.05)
    else:
        bonus = 0
    
    # 多重支撑加成
    cluster_threshold = sr_cfg.get("detection", {}).get("cluster_threshold", 0.01)
    nearby_count = sum(1 for l in levels if abs(l["price"] - nearest["price"]) / current_price < cluster_threshold)
    if nearby_count >= 2:
        bonus += scoring.get("multi_support_bonus", 0.05)
    
    return {
        "nearest_level": nearest["price"],
        "distance_pct": distance_pct,
        "level_type": nearest["type"],
        "bonus": min(bonus, 0.20),
        "all_levels": levels
    }


def _find_local_pivots(df: pd.DataFrame, periods: int, pivot_type: str) -> List[float]:
    """找局部高点或低点"""
    pivots = []
    col = "high" if pivot_type == "high" else "low"
    values = df[col].values
    
    for i in range(periods, len(values) - periods):
        if pivot_type == "high":
            if all(values[i] >= values[i-j] for j in range(1, periods+1)) and \
               all(values[i] >= values[i+j] for j in range(1, min(periods+1, len(values)-i))):
                pivots.append(float(values[i]))
        else:
            if all(values[i] <= values[i-j] for j in range(1, periods+1)) and \
               all(values[i] <= values[i+j] for j in range(1, min(periods+1, len(values)-i))):
                pivots.append(float(values[i]))
    
    return pivots


def _find_round_numbers(price: float, side: str) -> List[float]:
    """找整数关口"""
    levels = []
    
    if price >= 1000:
        step = 100
    elif price >= 100:
        step = 10
    elif price >= 10:
        step = 1
    elif price >= 1:
        step = 0.1
    else:
        step = 0.01
    
    base = round(price / step) * step
    
    if side == "long":
        for i in range(1, 4):
            level = base - i * step
            if level > 0 and level < price:
                levels.append(level)
    else:
        for i in range(1, 4):
            level = base + i * step
            if level > price:
                levels.append(level)
    
    return levels


# ============ K线形态识别 ============
def detect_candlestick_patterns(df: pd.DataFrame, side: str) -> Dict:
    """识别K线形态"""
    patterns = []
    
    if len(df) < 5:
        return {"patterns": [], "bonus": 0}
    
    o = df["open"].values[-5:]
    h = df["high"].values[-5:]
    l = df["low"].values[-5:]
    c = df["close"].values[-5:]
    
    body = np.abs(c - o)
    upper_shadow = h - np.maximum(o, c)
    lower_shadow = np.minimum(o, c) - l
    total_range = h - l
    
    # 避免除零
    total_range = np.where(total_range == 0, 0.0001, total_range)
    body = np.where(body == 0, 0.0001, body)
    
    if side == "long":
        # 锤子线
        if body[-1] < total_range[-1] * 0.3 and lower_shadow[-1] > body[-1] * 2:
            patterns.append("hammer")
        
        # 看涨吞没
        if c[-2] < o[-2] and c[-1] > o[-1]:
            if c[-1] > o[-2] and o[-1] < c[-2]:
                patterns.append("bullish_engulfing")
        
        # 早晨之星
        if c[-3] < o[-3] and body[-2] < body[-3] * 0.5:
            if c[-1] > o[-1] and c[-1] > (o[-3] + c[-3]) / 2:
                patterns.append("morning_star")
    
    else:  # side == "short"
        # 射击之星
        if body[-1] < total_range[-1] * 0.3 and upper_shadow[-1] > body[-1] * 2:
            patterns.append("shooting_star")
        
        # 看跌吞没
        if c[-2] > o[-2] and c[-1] < o[-1]:
            if o[-1] > c[-2] and c[-1] < o[-2]:
                patterns.append("bearish_engulfing")
        
        # 黄昏之星
        if c[-3] > o[-3] and body[-2] < body[-3] * 0.5:
            if c[-1] < o[-1] and c[-1] < (o[-3] + c[-3]) / 2:
                patterns.append("evening_star")
    
    bonus = min(len(patterns) * 0.06, 0.12)
    
    return {"patterns": patterns, "bonus": bonus}


# ============ 成交量结构分析 ============
def analyze_volume_structure(df: pd.DataFrame, side: str) -> Dict:
    """分析成交量结构"""
    if len(df) < 30:
        return {"structure": "unknown", "bonus": 0, "details": {}}
    
    vol = df["volume"].values
    close = df["close"].values
    
    vol_ma = np.mean(vol[-20:])
    
    recent_vol = vol[-10:]
    recent_close = close[-10:]
    
    up_bars = []
    down_bars = []
    for i in range(1, len(recent_close)):
        if recent_close[i] > recent_close[i-1]:
            up_bars.append(recent_vol[i])
        else:
            down_bars.append(recent_vol[i])
    
    avg_up_vol = np.mean(up_bars) if up_bars else 0
    avg_down_vol = np.mean(down_bars) if down_bars else 0
    
    is_dry_volume = vol[-1] < vol_ma * 0.5
    is_volume_spike = vol[-1] > vol_ma * 1.5
    
    bonus = 0
    structure = "neutral"
    
    if side == "long":
        if avg_down_vol > 0 and avg_up_vol > avg_down_vol * 1.2:
            structure = "bullish_accumulation"
            bonus = 0.06
        
        if is_dry_volume:
            structure = "dry_volume"
            bonus = 0.04
        elif is_volume_spike and recent_close[-1] > recent_close[-2]:
            structure = "bullish_breakout"
            bonus = 0.08
        
        if avg_down_vol > avg_up_vol * 1.5 and recent_close[-1] < recent_close[0]:
            structure = "panic_selling"
            bonus = -0.05
    
    else:
        if avg_up_vol > 0 and avg_down_vol > avg_up_vol * 1.2:
            structure = "bearish_distribution"
            bonus = 0.06
        
        if is_volume_spike and recent_close[-1] < recent_close[-2]:
            structure = "bearish_breakout"
            bonus = 0.08
        
        if avg_up_vol > avg_down_vol * 1.5 and recent_close[-1] > recent_close[0]:
            structure = "strong_buying"
            bonus = -0.05
    
    return {
        "structure": structure,
        "bonus": bonus,
        "details": {
            "avg_up_vol": avg_up_vol,
            "avg_down_vol": avg_down_vol,
            "is_dry_volume": is_dry_volume,
            "is_volume_spike": is_volume_spike
        }
    }


# ============ 多时间框架分析 ============
def fetch_multi_timeframe_data(ex, symbol: str, timeframes: List[str] = None) -> Dict[str, pd.DataFrame]:
    """获取多时间框架K线数据"""
    global _MTF_KLINE_CACHE
    
    if timeframes is None:
        timeframes = ["5m", "15m", "1h"]
    
    now = time.time()
    result = {}
    
    for tf in timeframes:
        cache_key = f"{symbol}_{tf}"
        
        if cache_key in _MTF_KLINE_CACHE:
            cached = _MTF_KLINE_CACHE[cache_key]
            if now - cached["ts"] < _MTF_CACHE_TTL:
                result[tf] = cached["data"]
                continue
        
        try:
            limit = 100
            raw = ex.fetch_ohlcv(symbol, timeframe=tf, limit=limit)
            if raw and len(raw) > 20:
                df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
                df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
                result[tf] = df
                _MTF_KLINE_CACHE[cache_key] = {"data": df, "ts": now}
        except Exception as e:
            print(f"[MTF] {symbol} {tf} 获取失败: {e}")
    
    return result


def analyze_multi_timeframe(ex, symbol: str, df_1m: pd.DataFrame, side: str, cfg: Dict) -> Dict:
    """多时间框架分析"""
    mtf_cfg = cfg.get("trend_anticipation", {}).get("multi_timeframe", {})
    if not mtf_cfg.get("enabled", True):
        return {"confirm_count": 0, "bonus": 0, "details": {}}
    
    timeframes = mtf_cfg.get("timeframes", ["5m", "15m", "1h"])
    timeframes = [tf for tf in timeframes if tf != "1m"]  # 排除1m，用传入的df_1m
    
    mtf_data = fetch_multi_timeframe_data(ex, symbol, timeframes)
    
    confirm_count = 0
    details = {}
    
    # 先分析1m
    if len(df_1m) >= 30:
        rsi_1m = _calc_rsi(df_1m, 14)
        confirmed_1m = (side == "long" and rsi_1m < 45) or (side == "short" and rsi_1m > 55)
        details["1m"] = {"rsi": rsi_1m, "confirmed": confirmed_1m}
        if confirmed_1m:
            confirm_count += 1
    
    for tf in timeframes:
        if tf not in mtf_data or mtf_data[tf] is None:
            continue
        
        df_tf = mtf_data[tf]
        if len(df_tf) < 30:
            continue
        
        rsi_val = _calc_rsi(df_tf, 14)
        
        confirmed = False
        if side == "long" and rsi_val < 45:
            confirmed = True
        elif side == "short" and rsi_val > 55:
            confirmed = True
        
        details[tf] = {"rsi": rsi_val, "confirmed": confirmed}
        
        if confirmed:
            confirm_count += 1
    
    weights = mtf_cfg.get("weights", {"1m": 0.15, "5m": 0.25, "15m": 0.30, "1h": 0.30})
    weighted_bonus = sum(weights.get(tf, 0) for tf, d in details.items() if d.get("confirmed", False))
    
    max_bonus = cfg.get("trend_anticipation", {}).get("scoring", {}).get("max_mtf_bonus", 0.15)
    bonus = min(weighted_bonus * 0.5, max_bonus)
    
    return {
        "confirm_count": confirm_count,
        "bonus": bonus,
        "details": details
    }


def _calc_rsi(df: pd.DataFrame, period: int = 14) -> float:
    """计算RSI"""
    delta = df["close"].diff()
    gain = delta.where(delta > 0, 0).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    val = float(rsi.iloc[-1])
    return val if not np.isnan(val) else 50.0


# ============ 信号去重器 ============
class SignalDeduplicator:
    """信号去重器"""
    
    def __init__(self, cfg: Dict):
        dedup_cfg = cfg.get("signal_dedup", {})
        self.enabled = dedup_cfg.get("enabled", True)
        self.cooldown_minutes = dedup_cfg.get("cooldown_minutes", 30)  # 🔥 v7.9.4: 45 -> 30分钟 放宽
        self.priority = dedup_cfg.get("priority", {
            "trend_anticipation": 4,
            "reversal": 3,
            "trend_explosion": 2,
            "trend_continuation": 1
        })
        self.replace_rules = dedup_cfg.get("replace_rules", {})
    
    def should_emit(self, symbol: str, signal_type: str, score: float, side: str) -> Tuple[bool, str]:
        """判断是否应该发出信号"""
        global _SIGNAL_DEDUP_CACHE
        
        if not self.enabled:
            return True, "去重禁用"
        
        now = dt.datetime.now()
        
        if symbol not in _SIGNAL_DEDUP_CACHE:
            self._record_signal(symbol, signal_type, score, side)
            return True, "首个信号"
        
        existing = _SIGNAL_DEDUP_CACHE[symbol]
        time_since = (now - existing["timestamp"]).total_seconds() / 60
        
        # 冷却期已过
        if time_since >= self.cooldown_minutes:
            self._record_signal(symbol, signal_type, score, side)
            return True, f"冷却期已过({time_since:.0f}分钟)"
        
        # 允许相反方向
        if self.replace_rules.get("allow_opposite_side", True):
            if side != existing["side"]:
                self._record_signal(symbol, signal_type, score, side)
                return True, f"相反方向({existing['side']}→{side})"
        
        # 检查优先级
        new_priority = self.priority.get(signal_type, 0)
        existing_priority = self.priority.get(existing["signal_type"], 0)
        
        if self.replace_rules.get("higher_priority_always", True):
            if new_priority > existing_priority:
                self._record_signal(symbol, signal_type, score, side)
                return True, f"更高优先级({existing['signal_type']}→{signal_type})"
        
        # 同优先级检查评分
        score_diff = self.replace_rules.get("same_priority_score_diff", 0.05)
        if new_priority == existing_priority and score > existing["score"] + score_diff:
            self._record_signal(symbol, signal_type, score, side)
            return True, f"更高评分({existing['score']:.2f}→{score:.2f})"
        
        return False, f"冷却中({time_since:.0f}/{self.cooldown_minutes}分钟)"
    
    def _record_signal(self, symbol: str, signal_type: str, score: float, side: str):
        """记录信号"""
        global _SIGNAL_DEDUP_CACHE
        _SIGNAL_DEDUP_CACHE[symbol] = {
            "signal_type": signal_type,
            "score": score,
            "side": side,
            "timestamp": dt.datetime.now()
        }
    
    def clear_expired(self):
        """清理过期缓存"""
        global _SIGNAL_DEDUP_CACHE
        now = dt.datetime.now()
        expired = [
            sym for sym, data in _SIGNAL_DEDUP_CACHE.items()
            if (now - data["timestamp"]).total_seconds() / 60 > self.cooldown_minutes * 2
        ]
        for sym in expired:
            del _SIGNAL_DEDUP_CACHE[sym]


# ============ 趋势预判信号检测 ============
def detect_trend_anticipation(
    cfg: Dict,
    ex,
    symbol: str,
    df: pd.DataFrame,
    btc_status: Dict,
    metrics: Dict,
    correlation_analysis: Optional[Dict] = None
) -> Optional[Dict]:
    """
    趋势预判信号检测
    
    🔥 v7.9: 收紧条件，增加BTC暴跌保护
    
    Returns:
        信号字典或None
    """
    ta_cfg = cfg.get("trend_anticipation", {})
    if not ta_cfg.get("enabled", False):
        return None
    
    rsi_val = metrics.get("rsi", 50)
    adx_val = metrics.get("adx", 0)
    macd_hist = metrics.get("macd_histogram", 0)
    vol_spike = metrics.get("vol_spike_ratio", 1.0)
    current_price = float(df["close"].iloc[-1])
    
    # 🔥 v7.9: 提前获取BTC状态用于早期过滤
    btc_change_1h = btc_status.get("price_change_1h", 0) if btc_status else 0
    
    # ========== 第一步：判断方向（基于RSI区间）==========
    # 🔥 v7.9.3: 大幅收窄RSI区间
    # 反转做多: RSI ≤ 15  |  趋势预判做多: RSI 12-20
    # 反转做空: RSI ≥ 85  |  趋势预判做空: RSI 80-88
    side = None
    
    long_cfg = ta_cfg.get("long_conditions", {})
    long_rsi_range = long_cfg.get("rsi_range", [15, 25])  # 🔥🔥🔥 v7.9.4: [12,20] -> [15,25] 放宽
    if long_rsi_range[0] <= rsi_val <= long_rsi_range[1]:
        side = "long"
    
    short_cfg = ta_cfg.get("short_conditions", {})
    short_rsi_range = short_cfg.get("rsi_range", [75, 85])  # 🔥🔥🔥 v7.9.4: [80,88] -> [75,85] 放宽
    if short_rsi_range[0] <= rsi_val <= short_rsi_range[1]:
        side = "short"
    
    if side is None:
        return None
    
    # ========== 第二步：检查多个条件（至少满足4个）==========
    conditions_met = []
    conditions_failed = []
    
    # 条件1: RSI在预判区间（已满足，因为side不是None）
    conditions_met.append("RSI预判区间")
    
    # 条件2: MACD柱状图缩短（趋势减速）
    macd_shrinking = False
    if len(df) >= 5:
        # 获取最近5根K线的MACD柱状图
        close_vals = df["close"].values[-6:]
        price_changes = [close_vals[i+1] - close_vals[i] for i in range(len(close_vals)-1)]
        
        if side == "long":
            # 做多：下跌动能减弱（负变化在变小，即绝对值减小或变正）
            if len(price_changes) >= 3:
                # 检查最近3根是否有改善趋势
                if price_changes[-1] > price_changes[-2] or price_changes[-1] > price_changes[-3]:
                    macd_shrinking = True
        else:
            # 做空：上涨动能减弱（正变化在变小，即绝对值减小或变负）
            if len(price_changes) >= 3:
                if price_changes[-1] < price_changes[-2] or price_changes[-1] < price_changes[-3]:
                    macd_shrinking = True
    
    if macd_shrinking:
        conditions_met.append("动能减速")
    else:
        # 🔥🔥 v1.1: 动能未减弱是严重问题，需要更多其他条件补偿
        conditions_failed.append("动能未减速⚠️")
    
    # 条件3: 价格接近支撑位/阻力位（<2%）
    sr_result = detect_support_resistance(df, cfg, side, current_price)
    near_support = sr_result.get("distance_pct", 999) < 0.02  # 2%以内
    
    if near_support:
        conditions_met.append(f"接近支撑({sr_result.get('distance_pct', 0)*100:.1f}%)")
    else:
        conditions_failed.append(f"远离支撑({sr_result.get('distance_pct', 0)*100:.1f}%)")
    
    # 条件4: BTC企稳或同向
    btc_ok = False
    btc_crashing = False  # 🔥 新增：BTC是否在暴跌
    if btc_status:
        btc_change_1h = btc_status.get("price_change_1h", 0)
        btc_cfg = ta_cfg.get("btc_analysis", {})
        # 🔥 注意：price_change_1h 已经是百分比形式，如 -0.18 表示 -0.18%
        stabilizing_threshold = btc_cfg.get("btc_stabilizing_threshold", 0.3)  # 🔥 0.3%
        
        # 🔥 检查BTC是否在暴跌（1h跌幅>1%）
        if btc_change_1h < -1.0:  # 🔥 修复：-1.0 表示 -1%
            btc_crashing = True
        
        # BTC企稳（波动<0.3%）
        if abs(btc_change_1h) < stabilizing_threshold:
            btc_ok = True
        # 或者BTC同向
        elif (side == "long" and btc_change_1h > 0) or (side == "short" and btc_change_1h < 0):
            btc_ok = True
        # BTC暴跌时不做多预判
        if btc_cfg.get("require_btc_not_crashing", True):
            if side == "long" and btc_change_1h < -2.0:  # 🔥 修复：-2.0 表示 -2%
                btc_ok = False
    else:
        btc_ok = True  # 无BTC数据时默认通过
    
    # 🔥🔥🔥 v7.9: BTC暴跌时直接拒绝做多信号
    if side == "long" and btc_crashing:
        print(f"[TREND_ANTICIPATION] ❌ {symbol} BTC暴跌中(1h:{btc_change_1h:.2f}%)，拒绝做多预判")
        return None
    
    if btc_ok:
        conditions_met.append("BTC支持")
    else:
        conditions_failed.append("BTC不支持")
    
    # 条件5: 成交量（降低要求到1.0x，但缩量太严重要扣分）
    vol_ok = vol_spike >= 1.0
    if vol_ok:
        conditions_met.append(f"量能({vol_spike:.1f}x)")
    else:
        conditions_failed.append(f"缩量({vol_spike:.1f}x)")
    
    # 🔥🔥🔥 v7.9.4: 成交量太低直接拒绝（放宽到0.8x）
    min_vol = ta_cfg.get("hard_filter", {}).get("min_volume_ratio", 0.8)  # 🔥 v7.9.4: 1.0->0.8 放宽
    if vol_spike < min_vol:
        print(f"[TREND_ANTICIPATION] ❌ {symbol} 成交量太低({vol_spike:.1f}x<{min_vol}x)，拒绝")
        return None
    
    # 条件6: ADX显示有趋势（🔥🔥 v7.9.4放宽到22）
    min_adx = ta_cfg.get("hard_filter", {}).get("min_adx", 22)  # 🔥 v7.9.4: 28->22 放宽
    adx_ok = adx_val >= min_adx
    if adx_ok:
        conditions_met.append(f"有趋势(ADX{adx_val:.0f})")
    else:
        conditions_failed.append(f"无趋势(ADX{adx_val:.0f}<{min_adx})")
    
    # 🔥🔥🔥 条件7: 蓄势确认（布林带收窄 - squeeze）
    bb_width = metrics.get("bb_width", 0.03)
    bb_squeeze = bb_width < 0.025  # 布林带宽度小于2.5%表示蓄势
    if bb_squeeze:
        conditions_met.append(f"蓄势中(BB{bb_width*100:.1f}%)")
    else:
        conditions_failed.append(f"未蓄势(BB{bb_width*100:.1f}%)")
    
    # 🔥🔥🔥 条件8: 启动信号检测（价格突破+放量）
    startup_confirmed = False
    startup_details = []
    
    if len(df) >= 10:
        prices = df['close'].values
        volumes = df['volume'].values
        highs = df['high'].values
        lows = df['low'].values
        
        # 最近5根K线的高低点
        recent_high_5 = max(highs[-6:-1])
        recent_low_5 = min(lows[-6:-1])
        current_close = prices[-1]
        
        # 成交量对比
        vol_now = volumes[-1]
        vol_recent_avg = np.mean(volumes[-6:-1])
        vol_spike_sudden = vol_now > vol_recent_avg * 1.5
        
        if side == "long":
            # 做多：价格突破前5根高点
            price_breakout = current_close > recent_high_5
            if price_breakout:
                startup_details.append("突破前高")
            if vol_spike_sudden:
                startup_details.append("放量启动")
        else:
            # 做空：价格跌破前5根低点
            price_breakout = current_close < recent_low_5
            if price_breakout:
                startup_details.append("突破前低")
            if vol_spike_sudden:
                startup_details.append("放量启动")
        
        # 满足突破+放量 = 启动确认
        if price_breakout and vol_spike_sudden:
            startup_confirmed = True
    
    if startup_confirmed:
        conditions_met.append(f"启动确认({','.join(startup_details)})")
    # 启动不是必要条件，只是加分项
    
    # ========== 🔥v2.0新增：趋势质量检测 ==========
    trend_quality_result = None
    fdi_value = 1.25  # 默认中性
    is_smart_money = False
    
    try:
        # 计算FDI分形维数
        fdi_value = _calculate_fdi(df)
        
        # 🔥🔥🔥 FDI过滤：趋势太嘈杂直接拒绝
        if fdi_value >= 1.45:
            print(f"[TREND_ANTICIPATION] ⚠️ {symbol} FDI={fdi_value:.3f} 过高，趋势太嘈杂，跳过")
            return None
        
        # 计算聪明钱指标（如果有OI数据）
        oi_change = metrics.get("oi_change", 0)
        volume_24h = metrics.get("volume_24h", 1)
        
        if oi_change != 0:
            price_change = (current_price - float(df['close'].iloc[-20])) / float(df['close'].iloc[-20]) if len(df) >= 20 else 0
            sm_result = _analyze_smart_money(price_change, oi_change, volume_24h)
            is_smart_money = sm_result.get("is_smart_money", False)
            
            # 如果不是聪明钱推动且FDI偏高，降低信号质量
            if not is_smart_money and fdi_value >= 1.35:
                print(f"[TREND_ANTICIPATION] ⚠️ {symbol} 非聪明钱+FDI偏高，信号质量降低")
        
        # 计算综合趋势质量
        trend_quality_result = {
            "fdi": fdi_value,
            "is_smart_money": is_smart_money,
            "trend_quality": "strong" if fdi_value < 1.25 else "moderate" if fdi_value < 1.35 else "weak"
        }
        
    except Exception as e:
        print(f"[TREND_ANTICIPATION] 趋势质量计算异常: {e}")
    
    # ========== 第三步：检查是否满足至少3个条件（🔥v7.9.4放宽）==========
    min_conditions = 3  # 🔥 v7.9.4: 4->3 放宽
    if len(conditions_met) < min_conditions:
        # 不满足最低条件数，不发信号
        return None
    
    # ========== 第四步：计算评分 ==========
    scoring_cfg = ta_cfg.get("scoring", {})
    base_score = scoring_cfg.get("base_score", 0.55)
    
    # 1. 支撑位加成
    support_bonus = sr_result.get("bonus", 0)
    
    # 2. K线形态加成
    pattern_result = detect_candlestick_patterns(df, side)
    pattern_bonus = pattern_result.get("bonus", 0)
    
    # 3. 成交量结构加成
    volume_result = analyze_volume_structure(df, side)
    volume_bonus = volume_result.get("bonus", 0)
    
    # 4. 多时间框架加成
    mtf_result = analyze_multi_timeframe(ex, symbol, df, side, cfg)
    mtf_bonus = mtf_result.get("bonus", 0)
    
    # 5. BTC联动加成
    btc_bonus = 0
    if btc_ok and btc_status:
        btc_change_1h = btc_status.get("price_change_1h", 0)
        if abs(btc_change_1h) < 0.003:
            btc_bonus += 0.03
        if (side == "long" and btc_change_1h > 0) or (side == "short" and btc_change_1h < 0):
            btc_bonus += 0.05
    btc_bonus = min(btc_bonus, scoring_cfg.get("max_btc_bonus", 0.10))
    
    # 6. 条件满足数加成（满足越多分越高，但设置上限）
    condition_bonus = (len(conditions_met) - min_conditions) * 0.03
    condition_bonus = min(condition_bonus, 0.06)
    
    # 🔥 7. 蓄势加成
    squeeze_bonus = 0.05 if bb_squeeze else 0
    
    # 🔥 8. 启动确认加成
    startup_bonus = 0.08 if startup_confirmed else 0
    
    # 🔥🔥🔥 v2.0新增: 9. FDI趋势纯度加成/扣分
    fdi_bonus = 0
    if fdi_value < 1.20:
        fdi_bonus = 0.08  # 非常纯净的趋势
        conditions_met.append(f"FDI优秀({fdi_value:.2f})")
    elif fdi_value < 1.30:
        fdi_bonus = 0.04  # 良好趋势
        conditions_met.append(f"FDI良好({fdi_value:.2f})")
    elif fdi_value >= 1.40:
        fdi_bonus = -0.05  # 嘈杂趋势扣分
        conditions_failed.append(f"FDI偏高({fdi_value:.2f})")
    
    # 🔥🔥🔥 v2.0新增: 10. 聪明钱加成
    smart_money_bonus = 0
    if is_smart_money:
        smart_money_bonus = 0.06
        conditions_met.append("聪明钱推动")
    
    # 计算总分
    total_score = (
        base_score +
        min(support_bonus, scoring_cfg.get("max_support_bonus", 0.15)) +
        min(pattern_bonus, scoring_cfg.get("max_pattern_bonus", 0.12)) +
        min(volume_bonus, scoring_cfg.get("max_volume_bonus", 0.10)) +
        min(mtf_bonus, scoring_cfg.get("max_mtf_bonus", 0.15)) +
        btc_bonus +
        condition_bonus +
        squeeze_bonus +
        startup_bonus +
        fdi_bonus +
        smart_money_bonus
    )
    
    total_score = min(total_score, 1.0)
    
    # 检查是否达到发出信号的门槛（🔥🔥🔥 v7.9.4放宽到0.75）
    min_score = scoring_cfg.get("min_score_to_emit", 0.75)  # 🔥 v7.9.4: 0.85->0.75 放宽
    if total_score < min_score:
        return None
    
    # ========== 第五步：计算止损止盈 ==========
    risk_cfg = ta_cfg.get("risk", {})
    sl_pct = risk_cfg.get("sl_pct", 0.02)
    tp_pct = risk_cfg.get("tp_pct", 0.06)
    
    # 使用支撑位作为止损参考
    if risk_cfg.get("use_support_as_sl", True) and sr_result.get("nearest_level", 0) > 0:
        support_price = sr_result["nearest_level"]
        buffer = risk_cfg.get("sl_buffer_below_support", 0.005)
        
        if side == "long":
            sl_price = support_price * (1 - buffer)
            max_sl_price = current_price * (1 - sl_pct)
            sl_price = max(sl_price, max_sl_price)
        else:
            sl_price = support_price * (1 + buffer)
            min_sl_price = current_price * (1 + sl_pct)
            sl_price = min(sl_price, min_sl_price)
    else:
        if side == "long":
            sl_price = current_price * (1 - sl_pct)
        else:
            sl_price = current_price * (1 + sl_pct)
    
    if side == "long":
        tp_price = current_price * (1 + tp_pct)
    else:
        tp_price = current_price * (1 - tp_pct)
    
    # ========== 输出日志 ==========
    # 使用实际计算时的限制值
    actual_support_bonus = min(support_bonus, scoring_cfg.get("max_support_bonus", 0.15))
    actual_pattern_bonus = min(pattern_bonus, scoring_cfg.get("max_pattern_bonus", 0.12))
    actual_volume_bonus = min(volume_bonus, scoring_cfg.get("max_volume_bonus", 0.10))
    actual_mtf_bonus = min(mtf_bonus, scoring_cfg.get("max_mtf_bonus", 0.15))
    
    print(f"[TREND_ANTICIPATION] 🔮 {symbol} 预判信号: {side.upper()}")
    print(f"[TREND_ANTICIPATION]    评分: {total_score:.2f} | RSI: {rsi_val:.1f} | ADX: {adx_val:.1f}")
    # 🔥 v2.0: 添加FDI和聪明钱日志
    fdi_status = "优秀" if fdi_value < 1.25 else "良好" if fdi_value < 1.35 else "一般" if fdi_value < 1.45 else "差"
    sm_status = "✅" if is_smart_money else "❌"
    print(f"[TREND_ANTICIPATION]    🔥v2.0: FDI={fdi_value:.3f}({fdi_status}) | 聪明钱:{sm_status}")
    print(f"[TREND_ANTICIPATION]    ✅ 满足条件({len(conditions_met)}): {', '.join(conditions_met)}")
    if conditions_failed:
        print(f"[TREND_ANTICIPATION]    ❌ 未满足: {', '.join(conditions_failed)}")
    print(f"[TREND_ANTICIPATION]    加成: 支撑{actual_support_bonus:.2f} 形态{actual_pattern_bonus:.2f} 量能{actual_volume_bonus:.2f} MTF{actual_mtf_bonus:.2f} BTC{btc_bonus:.2f} FDI{fdi_bonus:+.2f} SM{smart_money_bonus:.2f}")
    print(f"[TREND_ANTICIPATION]    支撑位: ${sr_result.get('nearest_level', 0):.4f} ({sr_result.get('level_type', 'none')})")
    if pattern_result.get("patterns"):
        print(f"[TREND_ANTICIPATION]    K线形态: {', '.join(pattern_result['patterns'])}")
    
    return {
        "ts": dt.datetime.utcnow().isoformat(),
        "category": "majors",
        "symbol": symbol,
        "price": current_price,
        "entry": current_price,
        "score": float(total_score),
        "bias": side,
        "signal_type": "trend_anticipation",
        "subscores": {
            "support_bonus": support_bonus,
            "pattern_bonus": pattern_bonus,
            "volume_bonus": volume_bonus,
            "mtf_bonus": mtf_bonus,
            "btc_bonus": btc_bonus,
            "fdi_bonus": fdi_bonus,  # 🔥 v2.0新增
            "smart_money_bonus": smart_money_bonus,  # 🔥 v2.0新增
            "conditions_met": len(conditions_met)
        },
        "metrics": metrics,
        # 🔥🔥🔥 v2.0新增: 趋势质量指标
        "trend_quality": {
            "fdi": fdi_value,
            "fdi_status": "优秀" if fdi_value < 1.25 else "良好" if fdi_value < 1.35 else "一般" if fdi_value < 1.45 else "差",
            "is_smart_money": is_smart_money,
            "trend_purity": round((1.5 - fdi_value) * 200, 1),  # 0-100分
        },
        "calculated_stops": {
            "sl_price": sl_price,
            "tp_price": tp_price,
            "sl_pct": abs(current_price - sl_price) / current_price * 100,
            "tp_pct": abs(tp_price - current_price) / current_price * 100,
            "max_leverage": 15,
            "category": "trend_anticipation"
        },
        "btc_status": btc_status,
        "correlation_analysis": correlation_analysis or {},
        "support_analysis": sr_result,
        "pattern_analysis": pattern_result,
        "volume_analysis": volume_result,
        "mtf_analysis": mtf_result,
        "obs_signals": [
            f"RSI{rsi_val:.0f}预判",
            sr_result.get("level_type", ""),
            volume_result.get("structure", ""),
            f"条件{len(conditions_met)}/{len(conditions_met)+len(conditions_failed)}",
            f"FDI{fdi_value:.2f}"  # 🔥 v2.0新增
        ],
        "conditions_met": conditions_met,
        "conditions_failed": conditions_failed,
        "obs_adjustment": 0,
        "pullback_pct": 0
    }


# ============ 交易历史管理（用于AI学习）============
def add_trade_to_history(trade: Dict):
    """添加交易到历史"""
    global _TRADE_HISTORY
    _TRADE_HISTORY.append(trade)
    # 只保留最近100条
    if len(_TRADE_HISTORY) > 100:
        _TRADE_HISTORY = _TRADE_HISTORY[-100:]


def get_recent_trades(count: int = 10) -> List[Dict]:
    """获取最近的交易记录"""
    global _TRADE_HISTORY
    return _TRADE_HISTORY[-count:]


def get_trade_statistics() -> Dict:
    """获取交易统计"""
    global _TRADE_HISTORY
    
    if not _TRADE_HISTORY:
        return {"total": 0, "wins": 0, "losses": 0, "win_rate": 0}
    
    wins = sum(1 for t in _TRADE_HISTORY if t.get("result") == "win")
    losses = sum(1 for t in _TRADE_HISTORY if t.get("result") == "loss")
    total = wins + losses
    
    return {
        "total": total,
        "wins": wins,
        "losses": losses,
        "win_rate": wins / total if total > 0 else 0,
        "by_signal_type": _get_stats_by_signal_type()
    }


def _get_stats_by_signal_type() -> Dict:
    """按信号类型统计"""
    global _TRADE_HISTORY
    
    stats = {}
    for trade in _TRADE_HISTORY:
        st = trade.get("signal_type", "unknown")
        if st not in stats:
            stats[st] = {"wins": 0, "losses": 0}
        
        if trade.get("result") == "win":
            stats[st]["wins"] += 1
        elif trade.get("result") == "loss":
            stats[st]["losses"] += 1
    
    # 计算胜率
    for st in stats:
        total = stats[st]["wins"] + stats[st]["losses"]
        stats[st]["win_rate"] = stats[st]["wins"] / total if total > 0 else 0
    
    return stats


# ============ 🔥🔥🔥 v2.1新增: 独立分析函数 - 供高波动轨道调用 ============

def analyze_trend_context(df: pd.DataFrame, symbol: str, 
                          oi_change: float = 0, volume_24h: float = 0) -> Dict:
    """
    🔥🔥🔥 v2.1新增: 独立趋势分析函数 - 供高波动轨道AI审核调用
    
    不生成信号，只返回趋势分析结果，供AI决策参考
    
    Args:
        df: K线数据 DataFrame
        symbol: 交易对
        oi_change: 持仓量变化（可选）
        volume_24h: 24小时成交量（可选）
    
    Returns:
        Dict: 趋势分析上下文
        {
            "fdi_value": float,           # FDI分形维数 (1.0-1.5)
            "fdi_quality": str,           # "excellent"/"good"/"moderate"/"noisy"
            "is_smart_money": bool,       # 是否有聪明钱推动
            "smart_money_type": str,      # "accumulation"/"distribution"/"squeeze"/"liquidation"/"neutral"
            "efficiency_ratio": float,    # 效率比 (0-1)
            "trend_bias_score": float,    # 趋势偏向评分 (-1到1)
            "trend_strength": str,        # "strong"/"moderate"/"weak"/"choppy"
            "momentum_direction": str,    # "bullish"/"bearish"/"neutral"
            "adx_value": float,           # ADX值
            "rsi_value": float,           # RSI值
            "bb_width": float,            # 布林带宽度
            "is_squeeze": bool,           # 是否布林带收窄（蓄势）
            "recommendation": str,        # "long_bias"/"short_bias"/"neutral"/"avoid"
        }
    """
    result = {
        "fdi_value": 1.35,
        "fdi_quality": "moderate",
        "is_smart_money": False,
        "smart_money_type": "neutral",
        "efficiency_ratio": 0.5,
        "trend_bias_score": 0,
        "trend_strength": "moderate",
        "momentum_direction": "neutral",
        "adx_value": 25,
        "rsi_value": 50,
        "bb_width": 0.03,
        "is_squeeze": False,
        "recommendation": "neutral"
    }
    
    if df is None or len(df) < 30:
        return result
    
    try:
        # ========== 1. 计算FDI分形维数 ==========
        fdi_value = _calculate_fdi(df)
        result["fdi_value"] = fdi_value
        
        if fdi_value < 1.20:
            result["fdi_quality"] = "excellent"
        elif fdi_value < 1.30:
            result["fdi_quality"] = "good"
        elif fdi_value < 1.40:
            result["fdi_quality"] = "moderate"
        else:
            result["fdi_quality"] = "noisy"
        
        # ========== 2. 计算聪明钱分析 ==========
        if oi_change != 0 and volume_24h > 0:
            # 计算价格变化
            price_change = 0
            if len(df) >= 20:
                price_change = (float(df['close'].iloc[-1]) - float(df['close'].iloc[-20])) / float(df['close'].iloc[-20])
            
            sm_result = _analyze_smart_money(price_change, oi_change, volume_24h)
            result["is_smart_money"] = sm_result.get("is_smart_money", False)
            result["smart_money_type"] = sm_result.get("trend_type", "neutral")
        
        # ========== 3. 计算效率比 (Efficiency Ratio) ==========
        if len(df) >= 14:
            prices = df['close'].values[-14:]
            net_change = abs(prices[-1] - prices[0])
            total_change = sum(abs(prices[i+1] - prices[i]) for i in range(len(prices)-1))
            
            if total_change > 0:
                result["efficiency_ratio"] = net_change / total_change
        
        # ========== 4. 计算ADX ==========
        if len(df) >= 28:
            try:
                high = df['high'].values
                low = df['low'].values
                close = df['close'].values
                
                # 简化ADX计算
                tr = np.maximum(high[1:] - low[1:], 
                               np.abs(high[1:] - close[:-1]),
                               np.abs(low[1:] - close[:-1]))
                
                plus_dm = np.where((high[1:] - high[:-1]) > (low[:-1] - low[1:]),
                                  np.maximum(high[1:] - high[:-1], 0), 0)
                minus_dm = np.where((low[:-1] - low[1:]) > (high[1:] - high[:-1]),
                                   np.maximum(low[:-1] - low[1:], 0), 0)
                
                # 14期平滑
                period = 14
                atr_14 = pd.Series(tr).rolling(window=period).mean().iloc[-1]
                plus_di = 100 * pd.Series(plus_dm).rolling(window=period).mean().iloc[-1] / atr_14 if atr_14 > 0 else 0
                minus_di = 100 * pd.Series(minus_dm).rolling(window=period).mean().iloc[-1] / atr_14 if atr_14 > 0 else 0
                
                dx = 100 * abs(plus_di - minus_di) / (plus_di + minus_di) if (plus_di + minus_di) > 0 else 0
                result["adx_value"] = dx
            except:
                pass
        
        # ========== 5. 计算RSI ==========
        if len(df) >= 15:
            deltas = df['close'].diff()
            gains = deltas.where(deltas > 0, 0)
            losses = (-deltas).where(deltas < 0, 0)
            
            avg_gain = gains.rolling(window=14).mean().iloc[-1]
            avg_loss = losses.rolling(window=14).mean().iloc[-1]
            
            if avg_loss > 0:
                rs = avg_gain / avg_loss
                result["rsi_value"] = 100 - (100 / (1 + rs))
            else:
                result["rsi_value"] = 100
        
        # ========== 6. 计算布林带宽度 ==========
        if len(df) >= 20:
            sma = df['close'].rolling(window=20).mean()
            std = df['close'].rolling(window=20).std()
            bb_upper = sma + 2 * std
            bb_lower = sma - 2 * std
            
            current_price = float(df['close'].iloc[-1])
            bb_width = (float(bb_upper.iloc[-1]) - float(bb_lower.iloc[-1])) / current_price
            result["bb_width"] = bb_width
            result["is_squeeze"] = bb_width < 0.025  # 布林带宽度<2.5%为蓄势
        
        # ========== 7. 判断趋势强度 ==========
        er = result["efficiency_ratio"]
        adx = result["adx_value"]
        fdi = result["fdi_value"]
        
        # 综合评估趋势强度
        if er > 0.6 and adx > 30 and fdi < 1.30:
            result["trend_strength"] = "strong"
        elif er > 0.4 and adx > 22 and fdi < 1.40:
            result["trend_strength"] = "moderate"
        elif er < 0.25 or fdi > 1.45:
            result["trend_strength"] = "choppy"
        else:
            result["trend_strength"] = "weak"
        
        # ========== 8. 判断动量方向 ==========
        rsi = result["rsi_value"]
        
        if rsi < 30:
            result["momentum_direction"] = "oversold"
        elif rsi > 70:
            result["momentum_direction"] = "overbought"
        elif rsi < 45:
            result["momentum_direction"] = "bearish"
        elif rsi > 55:
            result["momentum_direction"] = "bullish"
        else:
            result["momentum_direction"] = "neutral"
        
        # ========== 9. 计算趋势偏向评分 ==========
        # -1 = 强看空, 0 = 中性, +1 = 强看多
        bias_score = 0
        
        # RSI贡献
        if rsi < 25:
            bias_score += 0.3  # 超卖看多
        elif rsi > 75:
            bias_score -= 0.3  # 超买看空
        elif rsi < 40:
            bias_score -= 0.1
        elif rsi > 60:
            bias_score += 0.1
        
        # 聪明钱贡献
        if result["is_smart_money"]:
            if result["smart_money_type"] == "accumulation":
                bias_score += 0.3
            elif result["smart_money_type"] == "distribution":
                bias_score -= 0.3
        
        # FDI贡献 (趋势清晰度)
        if fdi < 1.25:
            bias_score *= 1.2  # 趋势清晰，放大偏向
        elif fdi > 1.40:
            bias_score *= 0.5  # 趋势嘈杂，减弱偏向
        
        result["trend_bias_score"] = max(-1, min(1, bias_score))
        
        # ========== 10. 给出建议 ==========
        if fdi > 1.45:
            result["recommendation"] = "avoid"  # 太嘈杂，不建议
        elif bias_score > 0.3:
            result["recommendation"] = "long_bias"
        elif bias_score < -0.3:
            result["recommendation"] = "short_bias"
        else:
            result["recommendation"] = "neutral"
        
        print(f"[TREND_CONTEXT] {symbol}: FDI={fdi:.3f}({result['fdi_quality']}) | "
              f"ER={er:.2f} | RSI={rsi:.1f} | ADX={adx:.1f} | "
              f"SmartMoney={result['is_smart_money']}({result['smart_money_type']}) | "
              f"Bias={bias_score:+.2f} → {result['recommendation']}")
        
    except Exception as e:
        print(f"[TREND_CONTEXT] ⚠️ 分析异常 {symbol}: {e}")
    
    return result


def get_trend_context_for_ai(df: pd.DataFrame, symbol: str,
                             oi_change: float = 0, volume_24h: float = 0) -> str:
    """
    🔥🔥🔥 v2.1新增: 获取趋势上下文的文本描述 - 直接用于AI prompt
    
    Args:
        df: K线数据
        symbol: 交易对
        oi_change: 持仓量变化
        volume_24h: 24h成交量
    
    Returns:
        str: 格式化的趋势分析文本，可直接插入AI prompt
    """
    ctx = analyze_trend_context(df, symbol, oi_change, volume_24h)
    
    # 构建描述文本
    fdi_desc = {
        "excellent": "趋势极纯净(噪音极少)",
        "good": "趋势良好(噪音较少)",
        "moderate": "趋势一般(有一定噪音)",
        "noisy": "趋势嘈杂(噪音大,易扫损)"
    }.get(ctx["fdi_quality"], "未知")
    
    sm_desc = ""
    if ctx["is_smart_money"]:
        sm_type_desc = {
            "accumulation": "聪明钱在吸筹(看多)",
            "distribution": "聪明钱在出货(看空)",
            "squeeze": "空头挤压",
            "liquidation": "多头清算"
        }.get(ctx["smart_money_type"], "聪明钱活跃")
        sm_desc = f"✅ {sm_type_desc}"
    else:
        sm_desc = "❌ 无明显主力痕迹"
    
    strength_desc = {
        "strong": "强势趋势",
        "moderate": "中等趋势",
        "weak": "弱势趋势",
        "choppy": "震荡无趋势"
    }.get(ctx["trend_strength"], "未知")
    
    rec_desc = {
        "long_bias": "⬆️ 偏多",
        "short_bias": "⬇️ 偏空",
        "neutral": "↔️ 中性",
        "avoid": "⚠️ 建议回避"
    }.get(ctx["recommendation"], "未知")
    
    text = f"""### 🔮 趋势上下文分析 (v2.1)

| 指标 | 数值 | 解读 |
|------|------|------|
| FDI分形维数 | {ctx['fdi_value']:.3f} | {fdi_desc} |
| 效率比(ER) | {ctx['efficiency_ratio']:.2f} | {'趋势纯净' if ctx['efficiency_ratio'] > 0.5 else '震荡市'} |
| 趋势强度 | {strength_desc} | ADX={ctx['adx_value']:.1f} |
| 布林带宽度 | {ctx['bb_width']*100:.1f}% | {'🔥蓄势收窄中' if ctx['is_squeeze'] else '正常波动'} |
| 聪明钱 | {sm_desc} | |
| 偏向评分 | {ctx['trend_bias_score']:+.2f} | {rec_desc} |

**FDI规则**:
- FDI < 1.25: 可积极入场，挂近单(1-1.5%)
- FDI 1.25-1.35: 正常入场，标准挂单(1.5-2%)
- FDI 1.35-1.45: 谨慎入场，挂远单接针(2-3%)
- FDI > 1.45: 建议回避，走势太乱
"""
    
    return text