# main.py - [v7.9.4 平衡优化版]
# 核心策略：反转(RSI 20/80 + 趋势减弱) + 趋势预判(蓄势确认) + 🔥高波动轨道(蓄势预判)
# 核心风控：30分钟去重 + 观察期二次探底容忍 + DeepSeek二审
# v7.9.4更新：放宽RSI阈值(20/80) + 放宽动能减弱判断(10根K线) + 成交量要求1.5x

import os
import json
import yaml
import ccxt
import sqlite3
import argparse
import datetime as dt
import time
import pandas as pd
import numpy as np
from typing import Dict, Any, List, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict

# ============ 核心工具库导入 ============
from core.utils import (
    ema, atr, realized_vol, wick_scores,
    funding_score, macro_score, orderbook_strength_fetch, oi_trend_score,
    cleanup_funding_oi_cache,
    adx, bollinger_bandwidth, rsi, macd
)

# ============ 组件库导入 ============
from core.notifier import tg_send
from core.state import ensure_db
from core.enhanced_reporting import report_daily_enhanced, report_weekly_enhanced
from core.enhanced_reporting import should_run_daily_report, should_run_weekly_report
from core.adaptive_stops import calculate_adaptive_stops
from core.signal_tracker import update_signal_tracking

from core.btc_advanced_monitor import check_btc_market_advanced, format_btc_status_message
from core.altcoin_correlation import get_cached_correlation, format_correlation_message

from core.claude_reviewer import ClaudeReviewer
from core.free_fingpt import FreeFinGPT
from core.xgboost_collector import XGBoostDataCollector
from core.auto_trader import AutoTrader
from core.signal_watcher import SignalWatcher

# 🔥🔥🔥 高波动轨道导入
try:
    from core.high_volatility_track import HighVolatilityTrack
    HIGH_VOL_TRACK_AVAILABLE = True
except ImportError:
    HIGH_VOL_TRACK_AVAILABLE = False
    print("[WARN] 高波动轨道模块(core/high_volatility_track.py)未找到，功能将禁用")

# ============ 模块可用性检测 ============
# 趋势预判模块
try:
    from core.trend_anticipation import (
        detect_trend_anticipation,
        detect_support_resistance,
        SignalDeduplicator,
        get_recent_trades,
        get_trade_statistics,
        add_trade_to_history
    )
    TREND_ANTICIPATION_AVAILABLE = True
except ImportError:
    print("[WARN] 趋势预判模块(core/trend_anticipation.py)未找到，相关功能将禁用")
    TREND_ANTICIPATION_AVAILABLE = False
    # 提供空实现以防止报错
    def detect_trend_anticipation(*args, **kwargs): return None
    def detect_support_resistance(*args, **kwargs): return {"bonus": 0, "nearest_level": 0}
    class SignalDeduplicator:
        def __init__(self, cfg): pass
        def should_emit(self, *args): return True, "模块未加载"
    def get_recent_trades(count=10): return []
    def get_trade_statistics(): return {}
    def add_trade_to_history(trade): pass

# ============ 全局变量与缓存 ============
_BTC_MARKET_CACHE = {"data": None, "ts": 0}
_CACHE_STATUS = {"fingpt_last_update": 0, "funding_oi_last_update": 0}
_SIGNAL_DEDUP_CACHE: Dict[str, Dict] = {} 
_MTF_KLINE_CACHE: Dict[str, Dict] = {}
_MTF_CACHE_TTL = 60
_TRADE_HISTORY_CACHE: List[Dict] = []
_FIRST_CYCLE_DONE = False

# 全局组件引用
_SIGNAL_WATCHER = None
_AUTO_TRADER = None
_HIGH_VOL_TRACK = None  # 🔥 高波动轨道

# 性能优化缓存
_DISCOVER_CACHE = {"symbols": [], "ts": 0, "ttl": 1800}
_HIGH_VOL_DISCOVER_CACHE = {"symbols": [], "ts": 0, "ttl": 300}  # 🔥 轨道2独立缓存（5分钟）
_ORDERBOOK_CACHE = {}  
_ORDERBOOK_TTL = 120   
_FUNDING_BATCH_CACHE = {} 
_FUNDING_BATCH_TTL = 300   

# ============ 辅助打印函数 ============
def print_btc_status_enhanced(btc_status: Dict[str, Any]):
    """打印详细的BTC市场状态"""
    print(f"\n{'='*60}")
    print(f"📊 BTC 市场状态监控")
    print(f"{'='*60}")
    
    price = btc_status.get('price', 0)
    print(f"💰 当前价格: ${price:,.2f}")
    
    # 涨跌幅
    change_1h = btc_status.get('price_change_1h', 0)
    change_4h = btc_status.get('price_change_4h', 0)
    emoji_1h = "📈" if change_1h > 0 else "📉"
    print(f"{emoji_1h} 1小时涨跌: {change_1h:+.2f}% | 4小时涨跌: {change_4h:+.2f}%")
    
    # 趋势与RSI
    trend = btc_status.get('trend', 'neutral')
    rsi_val = btc_status.get('rsi', 50)
    print(f"🌊 市场趋势: {trend.upper()} | 🔥 BTC RSI: {rsi_val:.1f}")
    
    # 波动率与动量
    volatility = btc_status.get('volatility', 0)
    momentum = btc_status.get('momentum_15m', 0)
    print(f"📉 波动率: {volatility:.2f}% | 💪 动量(15m): {momentum:+.2f}%")
    
    # 交易建议
    allow_long = btc_status.get('allow_long', True)
    allow_short = btc_status.get('allow_short', True)
    
    if allow_long and allow_short:
        print(f"✅ 山寨币策略: 双向可交易")
    elif allow_long:
        print(f"⚠️ 山寨币策略: 仅建议做多")
    elif allow_short:
        print(f"⚠️ 山寨币策略: 仅建议做空")
    else:
        reasons = btc_status.get('altcoin_reversal_reasons', [])
        print(f"🚫 山寨币策略: 暂停交易 ({', '.join(reasons)})")
    
    print(f"{'='*60}\n")

# ============ 基础工具函数 ============
def normalize_datetime(dt_obj):
    if dt_obj is None: return None
    if isinstance(dt_obj, str): dt_obj = dt.datetime.fromisoformat(dt_obj)
    if dt_obj.tzinfo is None: return dt_obj.replace(tzinfo=dt.timezone.utc)
    else: return dt_obj.astimezone(dt.timezone.utc)

def load_cfg(path="config.yaml")->Dict[str,Any]:
    with open(path,"r",encoding="utf-8") as f: cfg=yaml.safe_load(f)
    # 默认配置兜底
    cfg.setdefault("exchange", {"name":"binance","timeframe":"1m","limit":800})
    cfg.setdefault("push", {"master":"on","observe_only":False,"thresholds":{"majors":0.75}})
    cfg.setdefault("analytics", {"storage":{"path":"./signals.db"}})
    cfg.setdefault("performance", {"kline_workers": 5, "kline_limit": 800})
    return cfg

def get_exchange(cfg):
    klass = getattr(ccxt, cfg["exchange"]["name"])
    ex = klass({"enableRateLimit": True, "options": {"adjustForTimeDifference": True}})
    market_type = cfg.get("exchange", {}).get("market_type", "future")
    if market_type == "future": ex.options['defaultType'] = 'future'
    return ex

# ============ 数据获取函数 (并发优化) ============
def fetch_df_single(ex, symbol: str, timeframe: str, limit: int) -> tuple:
    """获取单个交易对K线"""
    try:
        raw = ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        if not raw or len(raw) < 60:
            return (symbol, None)
        df = pd.DataFrame(raw, columns=["ts","open","high","low","close","volume"])
        df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        return (symbol, df)
    except Exception as e:
        # print(f"[FETCH_ERR] {symbol}: {e}")
        return (symbol, None)

def fetch_df(ex, symbol, timeframe, limit)->Optional[pd.DataFrame]:
    """兼容旧接口"""
    _, df = fetch_df_single(ex, symbol, timeframe, limit)
    return df

def fetch_klines_batch(ex, symbols: List[str], timeframe: str, limit: int, workers: int = 5) -> Dict[str, pd.DataFrame]:
    """并发批量获取K线"""
    results = {}
    print(f"[KLINE] 🚀 正在并发获取 {len(symbols)} 个交易对（{workers}线程）...")
    start_time = time.time()
    
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(fetch_df_single, ex, sym, timeframe, limit): sym 
            for sym in symbols
        }
        for future in as_completed(futures):
            try:
                symbol, df = future.result()
                if df is not None:
                    results[symbol] = df
            except Exception: pass
    
    elapsed = time.time() - start_time
    print(f"[KLINE] ✅ 完成：{len(results)}/{len(symbols)} 个，耗时 {elapsed:.1f}秒")
    return results

def batch_fetch_funding_rates(ex, symbols: List[str]) -> Dict[str, Dict]:
    """批量获取资金费率"""
    global _FUNDING_BATCH_CACHE
    now = time.time()
    results = {}
    symbols_to_fetch = []
    
    # 检查缓存
    for sym in symbols:
        cached = _FUNDING_BATCH_CACHE.get(sym)
        if cached and (now - cached["ts"]) < _FUNDING_BATCH_TTL:
            results[sym] = cached
        else:
            symbols_to_fetch.append(sym)
    
    if not symbols_to_fetch: return results
    
    try:
        # 尝试使用 Binance 的批量接口
        funding_data = ex.fapiPublicGetPremiumIndex()
        funding_map = {item["symbol"]: float(item.get("lastFundingRate", 0) or 0) for item in funding_data}
        
        for sym in symbols_to_fetch:
            clean_sym = sym.replace("/", "").replace(":USDT", "")
            rate = funding_map.get(clean_sym, 0)
            score = float(np.clip(0.5 + 0.5 * np.tanh(-rate * 200), 0.0, 1.0))
            cache_entry = {"rate": rate, "score": score, "ts": now}
            _FUNDING_BATCH_CACHE[sym] = cache_entry
            results[sym] = cache_entry
            
    except Exception:
        # 如果批量接口失败，使用默认值，不阻塞流程
        for sym in symbols_to_fetch:
            results[sym] = {"rate": 0, "score": 0.5, "ts": now}
            
    return results

def orderbook_strength_cached(ex, symbol: str, limit: int = 20) -> float:
    """带缓存的 Orderbook 深度获取"""
    global _ORDERBOOK_CACHE
    now = time.time()
    cached = _ORDERBOOK_CACHE.get(symbol)
    if cached and (now - cached["ts"]) < _ORDERBOOK_TTL: return cached["data"]
    try:
        ob = ex.fetch_order_book(symbol, limit=limit)
        bid_vol = sum([b[1] for b in ob.get("bids", [])])
        ask_vol = sum([a[1] for a in ob.get("asks", [])])
        
        if bid_vol + ask_vol <= 0:
            result = 0.5
        else:
            result = float(0.5 + 0.5 * np.tanh(np.log(bid_vol / max(1e-9, ask_vol) + 1e-9)))
            
        _ORDERBOOK_CACHE[symbol] = {"data": result, "ts": now}
        return result
    except: return 0.5

def last_price(df: pd.DataFrame)->float: return float(df["close"].iloc[-1])

# ============ FinGPT & Metrics ============
def get_fingpt_sentiment(symbol: str, fingpt: FreeFinGPT, tech_indicators: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
    """调用 FinGPT 获取情绪分数"""
    fingpt_cfg = cfg.get("fingpt", {})
    if not fingpt_cfg.get("enabled", True):
        return {"sentiment_score": 0.0, "fear_greed": 50, "_cached": False}
    try:
        result = fingpt.analyze(symbol, tech_indicators)
        sentiment_data = result.get("sentiment", {})
        return {
            "sentiment_score": sentiment_data.get("score", 0.0),
            "fear_greed": sentiment_data.get("fear_greed", 50),
            "_cached": False
        }
    except:
        return {"sentiment_score": 0.0, "fear_greed": 50, "_cached": False}

def compute_common_subscores(cfg: Dict, symbol: str, df: pd.DataFrame, ex, fingpt, tech_indicators, funding_cache: Dict = None)->Dict:
    wick_bull, wick_bear = wick_scores(df)
    fingpt_data = get_fingpt_sentiment(symbol, fingpt, tech_indicators, cfg)
    sentiment_score = (fingpt_data["sentiment_score"] + 1) / 2
    
    if funding_cache and symbol in funding_cache:
        fscore = funding_cache[symbol].get("score", 0.5)
    else:
        fscore = funding_score(symbol, cfg)
        
    mscore = macro_score(cfg)
    obk = orderbook_strength_cached(ex, symbol)
    
    return {
        "fingpt_sentiment": sentiment_score,
        "wick_bull": float(wick_bull), "wick_bear": float(wick_bear), 
        "funding": float(fscore), "macro": float(mscore), "orderbook": float(obk), "oi": 0.5,
        "_fingpt_fear_greed": fingpt_data["fear_greed"]
    }

def weighted_score(base: float, subs: Dict[str,float], weights_cfg: Dict[str,float]):
    candidates = {
        "fingpt_sentiment": subs.get("fingpt_sentiment", 0.5),
        "funding": subs.get("funding", 0.5), 
        "macro": subs.get("macro", 0.5), 
        "orderbook": subs.get("orderbook", 0.5), 
        "oi": subs.get("oi", 0.5)
    }
    
    # 权重计算
    w = {k:max(0.0,float(v)) for k,v in (weights_cfg or {}).items() if k in candidates}
    s = sum(w.values())
    if s<=0: n = max(1,len(candidates)); weights = {k: 1.0/n for k in candidates}
    else: weights = {k: v/s for k,v in w.items()}
    
    adj = sum(float(weights[k]) * (float(candidates[k]) - 0.5) for k in weights)
    return max(0.0, min(1.0, base + adj)), weights

def detect_macd_divergence(df: pd.DataFrame, lookback: int = 50) -> Dict[str, Any]:
    """检测 MACD 背离"""
    if len(df) < lookback + 26: return {"bullish_divergence": False, "bearish_divergence": False, "divergence_strength": 0.0}
    
    macd_line, signal_line, histogram = macd(df, 12, 26, 9)
    recent_macd = macd_line.tail(lookback).values
    recent_prices = df["close"].tail(lookback).values
    
    price_lows, macd_lows = [], []
    price_highs, macd_highs = [], []
    
    # 寻找波峰波谷
    for i in range(5, len(recent_prices) - 5):
        if recent_prices[i] == min(recent_prices[i-5:i+6]):
            price_lows.append((i, recent_prices[i]))
        if recent_macd[i] == min(recent_macd[i-5:i+6]):
            macd_lows.append((i, recent_macd[i]))
        if recent_prices[i] == max(recent_prices[i-5:i+6]):
            price_highs.append((i, recent_prices[i]))
        if recent_macd[i] == max(recent_macd[i-5:i+6]):
            macd_highs.append((i, recent_macd[i]))

    bullish_div = False
    div_strength = 0.0
    
    # 判断底背离
    if len(price_lows) >= 2 and len(macd_lows) >= 2:
        last_p, prev_p = price_lows[-1], price_lows[-2]
        # 简单取最近的 macd 值对比 (近似处理)
        m_last = recent_macd[last_p[0]]
        m_prev = recent_macd[prev_p[0]]
        
        if last_p[1] < prev_p[1] and m_last > m_prev:
            bullish_div = True
            div_strength = 0.8
    
    # 判断顶背离
    bearish_div = False
    if len(price_highs) >= 2 and len(macd_highs) >= 2:
        last_p, prev_p = price_highs[-1], price_highs[-2]
        m_last = recent_macd[last_p[0]]
        m_prev = recent_macd[prev_p[0]]
        
        if last_p[1] > prev_p[1] and m_last < m_prev:
            bearish_div = True
            div_strength = 0.8
            
    return {"bullish_divergence": bullish_div, "bearish_divergence": bearish_div, "divergence_strength": div_strength}

def build_enhanced_metrics(df: pd.DataFrame, cfg: Dict[str,Any])->Dict[str,Any]:
    """构建增强版技术指标"""
    ema12_v = float(ema(df["close"], 12).iloc[-1])
    ema26_v = float(ema(df["close"], 26).iloc[-1])
    ema_cross = "golden" if ema12_v > ema26_v else ("death" if ema12_v < ema26_v else "none")
    
    atr_v = float(atr(df, 14).iloc[-1])
    
    vol_ma = float(df["volume"].rolling(20).mean().iloc[-1])
    vol_last = float(df["volume"].iloc[-1])
    vol_spike_ratio = (vol_last / vol_ma) if vol_ma > 0 else 1.0
    
    # 🔥 修复：计算wick scores
    wick_bull, wick_bear = wick_scores(df)
    
    adx_val = float(adx(df, 14).iloc[-1])
    bb_width = float(bollinger_bandwidth(df, 20, 2.0).iloc[-1])
    
    obs_cfg = cfg.get("overbought_oversold", {})
    rsi_val = float(rsi(df, obs_cfg.get("rsi_period", 14)).iloc[-1])
    
    macd_line, signal_line, histogram = macd(df, 12, 26, 9)
    macd_hist = float(histogram.iloc[-1])
    divergence_data = detect_macd_divergence(df, 50)
    
    # RSI 状态
    rsi_state = "neutral"
    if rsi_val >= 80: rsi_state = "extreme_overbought"
    elif rsi_val >= 70: rsi_state = "overbought"
    elif rsi_val <= 20: rsi_state = "extreme_oversold"
    elif rsi_val <= 30: rsi_state = "oversold"
    
    # 布林带数据
    sma = df["close"].rolling(20).mean()
    std = df["close"].rolling(20).std()
    bb_middle = float(sma.iloc[-1]) if len(sma) > 0 else 0.0
    bb_upper = float(sma.iloc[-1] + 2 * std.iloc[-1]) if len(sma) > 0 else 0.0
    bb_lower = float(sma.iloc[-1] - 2 * std.iloc[-1]) if len(sma) > 0 else 0.0
    bb_position = (df["close"].iloc[-1] - sma.iloc[-1]) / std.iloc[-1] if std.iloc[-1] > 0 else 0.0
    
    # 24h 涨跌幅
    price_now = float(df["close"].iloc[-1])
    price_change_24h_pct = 0.0
    try:
        if len(df) >= 1440:
            price_24h = float(df["close"].iloc[-1440])
            price_change_24h_pct = (price_now - price_24h) / price_24h
        elif len(df) > 0:
            price_24h = float(df["close"].iloc[0])
            price_change_24h_pct = (price_now - price_24h) / price_24h
    except: pass

    return {
        "ema12": ema12_v, "ema26": ema26_v, "ema_cross": ema_cross, "atr": atr_v,
        "vol_ma": vol_ma, "vol_last": vol_last, "vol_spike_ratio": vol_spike_ratio,
        "wick_absorb_score": float(max(wick_bull, wick_bear)),
        "adx": adx_val, "bb_width": bb_width, "bb_upper": bb_upper, "bb_lower": bb_lower, "bb_position": bb_position,
        "rsi": rsi_val, "rsi_state": rsi_state,
        "macd": float(macd_line.iloc[-1]), "macd_signal": float(signal_line.iloc[-1]), "macd_histogram": macd_hist, "macd_cross": "none",
        "bullish_divergence": divergence_data["bullish_divergence"],
        "bearish_divergence": divergence_data["bearish_divergence"],
        "divergence_strength": divergence_data["divergence_strength"],
        "price_change_24h_pct": float(price_change_24h_pct)
    }

# ============ 🔥 [已删除] 趋势跟随策略 ============
# v7.9: 趋势跟随已删除，其合理特点已融入趋势预判
# 原因：1. 和趋势预判重叠 2. 信号稀少 3. 入场时机差

# ============ 🔥 策略1: 反转 (Reversal) - v7.9.4平衡版 ============
def majors_signal_with_obs(cfg, ex, symbol, df, btc_status, fingpt, correlation_analysis=None, funding_cache=None):
    """
    反转策略：RSI过滤 + 🔥v7.9.4平衡版趋势减弱确认
    """
    m = build_enhanced_metrics(df, cfg)
    
    adx_val = m.get("adx", 0)
    vol_spike = m.get("vol_spike_ratio", 1.0)
    rsi_val = m.get("rsi", 50.0)
    
    # 1. 基础过滤：ADX太低且无量
    if adx_val < 15 and vol_spike < 1.5: return None  # 🔥 v7.9.4: 2.0 -> 1.5
    
    # 2. 🔥 v7.9.4: 平衡版RSI阈值
    # 极端阈值：RSI≤15做多，RSI≥85做空（直接放行）
    # 普通阈值：RSI≤20做多，RSI≥80做空（需要背离或巨量）
    rsi_extreme_oversold = 15   # 🔥 v7.9.4: 12 -> 15 放宽
    rsi_extreme_overbought = 85 # 🔥 v7.9.4: 88 -> 85 放宽
    rsi_normal_oversold = 20    # 🔥 v7.9.4: 15 -> 20 放宽
    rsi_normal_overbought = 80  # 🔥 v7.9.4: 85 -> 80 放宽
    
    bullish_div = m.get("bullish_divergence", False)
    bearish_div = m.get("bearish_divergence", False)
    div_strength = m.get("divergence_strength", 0.0)
    
    valid_signal = False
    bias = "neutral"
    signal_hint = "none"
    
    # 🔥🔥🔥 v7.9.4: 趋势减弱确认（放宽版）
    momentum_weakening = False
    still_trending = False
    momentum_weakening_count = 0
    
    if len(df) >= 10:  # 🔥 v7.9.4: 15 -> 10 放宽
        # 计算最近的价格动量
        prices = df['close'].values
        lows = df['low'].values
        highs = df['high'].values
        
        # 🔥 v7.9.4: 检查最近10根K线的价格变化
        recent_changes = [prices[-i] - prices[-i-1] for i in range(1, 10)]
        
        # 检查是否还在创新低/新高（趋势仍在进行）
        recent_low_5 = min(lows[-5:])
        recent_high_5 = max(highs[-5:])
        current_low = lows[-1]
        current_high = highs[-1]
        prev_low_10 = min(lows[-10:-5]) if len(lows) >= 10 else recent_low_5  # 🔥 v7.9.4: 放宽
        prev_high_10 = max(highs[-10:-5]) if len(highs) >= 10 else recent_high_5
        
        # 做多：检查下跌是否减弱
        if rsi_val <= rsi_normal_oversold:
            # 🔥 v7.9.4: 还在创新低 = 趋势仍在进行，不宜做多
            if current_low < prev_low_10:
                still_trending = True
            
            # 🔥 v7.9.4: 检查最近8根K线中有多少根是动能减弱的
            if len(recent_changes) >= 8:
                for i in range(0, 6):  # 检查最近6根
                    if recent_changes[i] > recent_changes[i+1]:  # 跌幅减小
                        momentum_weakening_count += 1
                
                # 🔥 v7.9.4: 至少3根K线显示动能减弱才算确认
                if momentum_weakening_count >= 3:
                    momentum_weakening = True
                
                # 🔥 v7.9.4: 放宽 - 最近3根有1根减弱即可
                recent_3_weakening = sum(1 for i in range(0, 2) if recent_changes[i] > recent_changes[i+1])
                if recent_3_weakening < 1:
                    momentum_weakening = False
        
        # 做空：检查上涨是否减弱
        elif rsi_val >= rsi_normal_overbought:
            # 🔥 v7.9.4: 还在创新高 = 趋势仍在进行，不宜做空
            if current_high > prev_high_10:
                still_trending = True
            
            # 🔥 v7.9.4: 检查最近8根K线中有多少根是动能减弱的
            if len(recent_changes) >= 8:
                for i in range(0, 6):  # 检查最近6根
                    if recent_changes[i] < recent_changes[i+1]:  # 涨幅减小
                        momentum_weakening_count += 1
                
                # 🔥 v7.9.4: 至少3根K线显示动能减弱才算确认
                if momentum_weakening_count >= 3:
                    momentum_weakening = True
                
                # 🔥 v7.9.4: 放宽 - 最近3根有1根减弱即可
                recent_3_weakening = sum(1 for i in range(0, 2) if recent_changes[i] < recent_changes[i+1])
                if recent_3_weakening < 1:
                    momentum_weakening = False
    
    # --- 逻辑 A: 做多检查 (RSI≤20) ---
    if rsi_val <= rsi_normal_oversold: 
        # 🔥 如果还在创新低且没有背离，拒绝信号
        if still_trending and not bullish_div:
            return None
        
        # 情况1: 极端超卖 (RSI <= 15) + 动能减弱 -> 放行
        if rsi_val <= rsi_extreme_oversold:
            if momentum_weakening or bullish_div or vol_spike > 1.5:  # 🔥 v7.9.4: 2.0 -> 1.5
                valid_signal = True
                bias = "long"
                signal_hint = "extreme_oversold" + ("_weakening" if momentum_weakening else "")
        # 情况2: 普通超卖 (15 < RSI <= 20) -> 必须有背离 或 巨量(>2.0x) + 动能减弱
        elif bullish_div and div_strength > 0.4:
            valid_signal = True
            bias = "long"
            signal_hint = "oversold_with_div"
        elif vol_spike > 2.0 and momentum_weakening:  # 🔥 v7.9.4: 2.5 -> 2.0
            valid_signal = True
            bias = "long"
            signal_hint = "panic_selling_weakening"
            
    # --- 逻辑 B: 做空检查 (RSI≥80) ---
    elif rsi_val >= rsi_normal_overbought: 
        # 🔥 如果还在创新高且没有背离，拒绝信号
        if still_trending and not bearish_div:
            return None
        
        # 情况1: 极端超买 (RSI >= 85) + 动能减弱 -> 放行
        if rsi_val >= rsi_extreme_overbought:
            if momentum_weakening or bearish_div or vol_spike > 1.5:  # 🔥 v7.9.4: 2.0 -> 1.5
                valid_signal = True
                bias = "short"
                signal_hint = "extreme_overbought" + ("_weakening" if momentum_weakening else "")
        # 情况2: 普通超买 (80 <= RSI < 85) -> 必须有背离 或 巨量(>2.0x) + 动能减弱
        elif bearish_div and div_strength > 0.4:
            valid_signal = True
            bias = "short"
            signal_hint = "overbought_with_div"
        elif vol_spike > 2.0 and momentum_weakening:  # 🔥 v7.9.4: 2.5 -> 2.0
            valid_signal = True
            bias = "short"
            signal_hint = "panic_buying_weakening"

    if not valid_signal: return None
    
    # 🔥 记录趋势减弱状态到metrics
    m["momentum_weakening"] = momentum_weakening
    m["still_trending"] = still_trending
    m["momentum_weakening_count"] = momentum_weakening_count
    
    # 3. 计算分数
    tech_indicators = {'rsi': rsi_val, 'vol_spike_ratio': vol_spike, 'adx': adx_val}
    subs = compute_common_subscores(cfg, symbol, df, ex, fingpt, tech_indicators, funding_cache)
    
    base_score = 0.75 # 起步分给高点
    wcfg = cfg.get("weights", {})
    score_long, _ = weighted_score(base_score, subs, wcfg)
    score_short, _ = weighted_score(base_score, subs, wcfg)
    
    score = score_long if bias == "long" else score_short
    side = bias
    
    # 相关性调整
    if correlation_analysis:
        score += correlation_analysis.get("score_adjustment", 0.0)
    
    px = last_price(df)
    entry_price = px # Entry简化为当前价，后续AI定
    
    stops = calculate_adaptive_stops(symbol=symbol, price=entry_price, atr=m["atr"], side=side, btc_status=btc_status, df=df)
    
    # 计算市场惯性
    try:
        if len(df) >= 15:
            m["momentum_5m"] = ((df['close'].iloc[-1] - df['close'].iloc[-5]) / df['close'].iloc[-5]) * 100
            m["momentum_15m"] = ((df['close'].iloc[-1] - df['close'].iloc[-15]) / df['close'].iloc[-15]) * 100
        else:
            m["momentum_5m"] = 0.0
            m["momentum_15m"] = 0.0
    except Exception:
        m["momentum_5m"] = 0.0
        m["momentum_15m"] = 0.0
    
    m["fingpt_sentiment"] = subs.get("fingpt_sentiment", 0.5)
    m["funding"] = subs.get("funding", 0.5)
    m["signal_hint"] = signal_hint
    
    return {
        "ts": dt.datetime.utcnow().isoformat(), 
        "category": "majors", "symbol": symbol, "price": px, "entry": entry_price, 
        "score": float(score), "bias": side, "subscores": subs, "metrics": m,
        "calculated_stops": stops, "btc_status": btc_status, 
        "correlation_analysis": correlation_analysis,
        "obs_signals": [signal_hint], "obs_adjustment": 0,
        "signal_type": "reversal", "pullback_pct": 0.0
    }

# ============ Claude审核推送 ============
def claude_review_and_push(cfg, cur, s, reviewer, collector):
    """优化版: 审核只判断信号质量，入场价由观察期后AI评估"""
    push_cfg = cfg.get("push", {})
    master_on = (str(push_cfg.get("master","on")).lower() == "on")
    observe_only = bool(push_cfg.get("observe_only", False))
    th = push_cfg.get("thresholds", {}).get("majors", 0.75)
    
    symbol = s["symbol"]
    print(f"\n[SIGNAL] {symbol} {s['bias'].upper()} | 评分:{s['score']:.2f} | 价格:${s['price']:.4f}")
    
    if s["score"] < th:
        print(f"[SKIP] {symbol} - 评分{s['score']:.2f}<{th:.2f}")
        return None
    
    calculated_stops = s.get("calculated_stops", {})
    if not calculated_stops:
        calculated_stops = {'sl_pct': 3.0, 'tp_pct': 6.0, 'max_leverage': 10, 'category': 'normal'}
    
    # 🆕 准备传给AI的信息
    signal_info = {
        "current_price": s["price"],
        "signal_type": s.get("signal_type", "unknown")
    }
    
    payload = {
        "cfg": cfg, "symbol": s["symbol"], "category": s["category"], 
        "price": s["price"], "score": s["score"], "bias": s["bias"],
        "subscores": s.get("subscores", {}), "metrics": s.get("metrics", {}),
        "calculated_stops": calculated_stops, "btc_status": s.get("btc_status", {}),
        "obs_signals": s.get("obs_signals", []),
        "funding": {"rate": 0.0, "score": 0.5}, "oi_data": {"change_24h": 0.0, "score": 0.5},
        "signal_info": signal_info,
        "signal_type": s.get("signal_type", "unknown"),
        "correlation_analysis": s.get("correlation_analysis", {}),
        "support_analysis": s.get("support_analysis", {}),
        "pattern_analysis": s.get("pattern_analysis", {}),
        "volume_analysis": s.get("volume_analysis", {}),
        "mtf_analysis": s.get("mtf_analysis", {}),
        "pullback_pct": s.get("pullback_pct", 0)
    }
    
    print(f"\n[AI_REVIEW] 正在审核 {s['symbol']} {s['bias']} ({s.get('signal_type')})...")
    result = reviewer.review_signal(payload)
    
    if not result.get("approved", False):
        print(f"❌ 拒绝: {result.get('reasoning', '')}")
        return None
    
    # 🔥 审核通过
    reasoning = result.get("reasoning", "无")
    
    # 检测方向是否反转
    original_side = s["bias"]
    final_side = result.get("side", original_side)
    
    if final_side != original_side:
        print(f"[AI] 🔄 方向反转: {original_side.upper()} → {final_side.upper()}")
    
    print(f"✅ 审核通过: {reasoning}")
    
    llm_json_str = json.dumps(result, ensure_ascii=False)
    
    cur.execute("""INSERT INTO signals(ts, category, symbol, price, entry, tp, sl, score, rationale, bias, llm_json, policy_version, ab_bucket)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (s["ts"], s["category"], s["symbol"], s["price"], 0, 0, 0, s["score"], 
         result.get("reasoning", "")[:50], final_side, llm_json_str, "v7.9.4", "A"))
    
    sid = cur.lastrowid
    
    # XGBoost
    if collector and cfg.get("xgboost", {}).get("enabled", True):
        try:
            collector.record_signal(payload, {"approved": True, "entry_price": s["price"]})
        except Exception as e:
            # print(f"[XGBOOST_ERR] {e}")
            pass
    
    # 🆕 推送
    if master_on and not observe_only:
        m = s.get("metrics", {})
        rsi_val = m.get("rsi", 50)
        momentum_5m = m.get("momentum_5m", 0)
        
        signal_type_text = s.get("signal_type", "unknown")
        signal_type_emoji = "🔄" if signal_type_text == "reversal" else "📈"
        
        title = f"🔔 新信号 | {s['symbol']} {final_side.upper()} {signal_type_emoji}"
        
        msg_lines = [
            "",
            f"💰 当前价: `${s['price']:.6f}`",
            f"📊 RSI: `{rsi_val:.1f}` | 动量: `{momentum_5m:+.2f}%`",
            ""
        ]
        
        # 🔥 观察系统提示
        watch_enabled = cfg.get("watch", {}).get("enabled", False)
        if watch_enabled:
            expire_min = cfg.get("watch", {}).get("expire_minutes", 4)
            msg_lines.append(f"🏃 **进入{expire_min}分钟观察期**")
            msg_lines.append(f"  AI将评估最佳入场时机和价格")
            msg_lines.append("")
        
        if reasoning:
            msg_lines.append(f"💡 {reasoning[:70]}")
            msg_lines.append("")
        
        if final_side != original_side:
            msg_lines.append(f"⚠️ 方向反转: {original_side.upper()} → {final_side.upper()}")
        
        tg_send(cfg, title, msg_lines)
        print(f"[PUSH] {s['symbol']} {final_side} | 审核通过，进入观察期")

        # 🔥🔥 加入观察队列
        global _SIGNAL_WATCHER
        if _SIGNAL_WATCHER:
            try:
                original_signal_type = s.get("signal_type", "unknown")
                
                # 根据原始信号类型确定观察类型（v7.9: 已删除trend_continuation）
                if original_signal_type == "trend_anticipation":
                    signal_type = "trend_anticipation"
                else:
                    # 反转信号：根据RSI判断（v7.9.4平衡版：20/80）
                    is_reversal = (final_side == "long" and rsi_val <= 20) or \
                                 (final_side == "short" and rsi_val >= 80)
                    signal_type = "reversal" if is_reversal else "trend"

                _SIGNAL_WATCHER.add_signal_to_watch(
                    symbol=s["symbol"],
                    side=final_side,
                    signal_type=signal_type,
                    price=s["price"],
                    rsi=rsi_val,
                    adx=m.get("adx", 0),
                    sl_price=0, tp_price=0, metrics=m, original_payload=payload
                )

            except Exception as e:
                print(f"[WATCHER_ERR] 添加观察信号失败: {e}")

    else:
        print(f"[OBSERVE] {s['symbol']} {final_side}")

    return sid

def _get_fallback_majors(quote: str = "USDT") -> list:
    majors = ["BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "AVAX", "LINK", "DOT", "LTC", "BCH", "UNI", "AAVE", "FIL", "INJ", "SUI", "APT", "ARB", "OP", "NEAR", "HBAR", "ETC", "XLM", "TON", "TRX", "DASH", "ZEC", "WAVES", "CRV", "ICP", "BNB", "BONK", "WIF", "CHZ"]
    return [f"{coin}/{quote}:{quote}" for coin in majors]

# ============ Discover Symbols (优化版) ============
def discover_symbols(cfg, ex):
    """30分钟缓存，避免每周期调用CoinGecko"""
    global _DISCOVER_CACHE
    now = time.time()
    perf_cfg = cfg.get("performance", {})
    cache_ttl = perf_cfg.get("discover_cache_ttl", 1800)
    
    if _DISCOVER_CACHE["symbols"] and (now - _DISCOVER_CACHE["ts"]) < cache_ttl:
        cache_age_min = (now - _DISCOVER_CACHE["ts"]) / 60
        print(f"[DISCOVER] 📦 使用缓存（{cache_age_min:.1f}分钟前）: {len(_DISCOVER_CACHE['symbols'])} 个交易对")
        return _DISCOVER_CACHE["symbols"]
    
    dynamic_cfg = cfg.get("majors", {}).get("dynamic", {})
    static_symbols = cfg.get("majors", {}).get("symbols", [])

    if not dynamic_cfg.get("enable", False):
        return static_symbols if static_symbols else []

    try:
        quote = dynamic_cfg.get("quote", "USDT")
        top_n_volume = dynamic_cfg.get("top_n_volume", 45)
        max_market_cap_rank = dynamic_cfg.get("max_market_cap_rank", 100)
        min_volume_24h = dynamic_cfg.get("min_volume_24h_usdt", 10000000)
        stablecoin_blacklist = {"USDT", "USDC", "BUSD", "TUSD", "DAI", "FDUSD", "USDP", "USDE"}

        print(f"[DISCOVER] 开始动态发现 | 交易额前{top_n_volume} + 市值前{max_market_cap_rank}...")
        tickers = ex.fetch_tickers()
        pairs = []

        for symbol, ticker in tickers.items():
            if ":" not in symbol: continue
            if f"/{quote}:USDT" not in symbol: continue
            base = symbol.split("/")[0].upper()
            if base in stablecoin_blacklist: continue
            volume_usd = ticker.get('quoteVolume', 0)
            if volume_usd and volume_usd >= min_volume_24h:
                pairs.append({'symbol': symbol, 'base': base, 'volume': volume_usd})

        pairs.sort(key=lambda x: x['volume'], reverse=True)
        top_volume_pairs = pairs[:top_n_volume]

        # CoinGecko获取市值数据
        from core.free_fingpt import FreeFinGPT
        coingecko_id_map = FreeFinGPT.SYMBOL_TO_COINGECKO_ID
        try:
            import requests
            cg_api_key = dynamic_cfg.get("coingecko_api_key", "")
            coin_ids = []
            base_to_symbol = {}

            for pair in top_volume_pairs:
                base = pair['base']
                cg_id = coingecko_id_map.get(base)
                if cg_id:
                    coin_ids.append(cg_id)
                    base_to_symbol[cg_id] = pair['symbol']

            if not coin_ids:
                final_symbols = [p['symbol'] for p in top_volume_pairs]
            else:
                url = "https://api.coingecko.com/api/v3/coins/markets"
                params = {"vs_currency": "usd", "ids": ",".join(coin_ids), "order": "market_cap_desc", "per_page": len(coin_ids), "page": 1}
                headers = {}
                if cg_api_key: headers["x-cg-demo-api-key"] = cg_api_key
                
                resp = None
                for retry in range(3):
                    try:
                        resp = requests.get(url, params=params, headers=headers, timeout=20)
                        if resp.status_code == 200: break
                    except Exception: time.sleep(2)

                if resp and resp.status_code == 200:
                    cg_data = resp.json()
                    qualified_symbols = []
                    for coin in cg_data:
                        coin_id = coin.get("id", "")
                        market_cap_rank = coin.get("market_cap_rank", 9999)
                        symbol = base_to_symbol.get(coin_id)
                        if symbol and market_cap_rank and market_cap_rank <= max_market_cap_rank:
                            qualified_symbols.append(symbol)
                    final_symbols = qualified_symbols
                else:
                    final_symbols = static_symbols if static_symbols else _get_fallback_majors(quote)

        except Exception as e:
            final_symbols = static_symbols if static_symbols else _get_fallback_majors(quote)

        all_symbols_raw = list(set(static_symbols + final_symbols))
        seen_bases = set()
        unique_symbols = []
        for symbol in all_symbols_raw:
            base = symbol.split("/")[0].split(":")[0].upper()
            if base not in seen_bases:
                seen_bases.add(base)
                unique_symbols.append(symbol)

        print(f"[DISCOVER] ✅ 最终选择: {len(unique_symbols)} 个交易对")
        _DISCOVER_CACHE["symbols"] = unique_symbols
        _DISCOVER_CACHE["ts"] = now
        return unique_symbols

    except Exception as e:
        print(f"[DISCOVER_ERR] {e}")
        return static_symbols if static_symbols else []

# ============ 🔥🔥🔥 轨道2专用：全市场币种发现 ============
def discover_high_vol_symbols(cfg, ex):
    """
    🔥 轨道2专用：获取全市场100+高成交量币种
    独立于轨道1，扫描范围更广
    """
    global _HIGH_VOL_DISCOVER_CACHE
    now = time.time()
    
    # 5分钟缓存
    if _HIGH_VOL_DISCOVER_CACHE["symbols"] and (now - _HIGH_VOL_DISCOVER_CACHE["ts"]) < 300:
        cache_age = (now - _HIGH_VOL_DISCOVER_CACHE["ts"]) / 60
        print(f"[HIGH_VOL_DISCOVER] 📦 使用缓存（{cache_age:.1f}分钟前）: {len(_HIGH_VOL_DISCOVER_CACHE['symbols'])} 个币种")
        return _HIGH_VOL_DISCOVER_CACHE["symbols"]
    
    try:
        hv_cfg = cfg.get("high_volatility_track", {}).get("scan", {})
        min_volume_24h = hv_cfg.get("min_volume_24h", 2000000)  # 默认2M
        
        stablecoin_blacklist = {"USDT", "USDC", "BUSD", "TUSD", "DAI", "FDUSD", "USDP", "USDE"}
        
        print(f"[HIGH_VOL_DISCOVER] 🔍 扫描全市场币种 (成交量>{min_volume_24h/1e6:.0f}M)...")
        tickers = ex.fetch_tickers()
        pairs = []
        
        for symbol, ticker in tickers.items():
            if ":" not in symbol: 
                continue
            if "/USDT:USDT" not in symbol: 
                continue
            base = symbol.split("/")[0].upper()
            if base in stablecoin_blacklist: 
                continue
            
            volume_usd = ticker.get('quoteVolume', 0)
            change_24h = ticker.get('percentage', 0) or 0  # 24h涨跌幅
            
            if volume_usd and volume_usd >= min_volume_24h:
                pairs.append({
                    'symbol': symbol, 
                    'base': base, 
                    'volume': volume_usd,
                    'change_24h': change_24h
                })
        
        # 按成交量排序
        pairs.sort(key=lambda x: x['volume'], reverse=True)
        
        # 取前150个
        top_pairs = pairs[:150]
        symbols = [p['symbol'] for p in top_pairs]
        
        # 统计波动情况
        high_vol_count = sum(1 for p in top_pairs if abs(p['change_24h']) >= 8)
        print(f"[HIGH_VOL_DISCOVER] ✅ 发现 {len(symbols)} 个币种 | 其中24h波动≥8%: {high_vol_count}个")
        
        _HIGH_VOL_DISCOVER_CACHE["symbols"] = symbols
        _HIGH_VOL_DISCOVER_CACHE["ts"] = now
        return symbols
        
    except Exception as e:
        print(f"[HIGH_VOL_DISCOVER] ❌ 错误: {e}")
        return []

def notify_startup(cfg):
    if not cfg.get("runtime", {}).get("start_notify", True): return
    perf_cfg = cfg.get("performance", {})
    msg_lines = [
        "✅ v7.9.4 平衡优化版启动",
        f"交易所: {cfg['exchange']['name']}",
        f"时间框架: {cfg['exchange']['timeframe']}", "",
        "🚀 性能优化:",
        f"  ⚡ K线并发: {perf_cfg.get('kline_workers', 5)}线程",
        f"  📊 K线数量: {perf_cfg.get('kline_limit', 800)}根", "",
        "🆕 信号类型 (v7.9.4平衡版):",
        "  🔮 趋势预判 (RSI 15-25/75-85 + 蓄势确认)",
        "  🔄 反转信号 (RSI ≤20/≥80 + 趋势减弱)", "",
        "🔥 核心优化:",
        "  🎯 30分钟去重",
        "  📊 观察期容忍二次探底",
        "  🛡️ 趋势减弱确认防接飞刀"
    ]
    tg_send(cfg, "启动", msg_lines)

# ============ 🚀 优化版运行策略 ============
def run_majors(cfg, ex, cur, btc_status, fingpt, reviewer, collector):
    """🚀 v7.9.4 平衡版：反转 + 预判"""
    global _FIRST_CYCLE_DONE
    
    tf = cfg["exchange"]["timeframe"]
    perf_cfg = cfg.get("performance", {})
    limit = perf_cfg.get("kline_limit", 800)
    workers = perf_cfg.get("kline_workers", 5)
    
    symbols = discover_symbols(cfg, ex)
    
    # 🎯 方案2: 第一个周期只注册币种到FinGPT预加载列表
    if not _FIRST_CYCLE_DONE:
        print("[FINGPT] 第一个周期:注册币种到预加载列表...")
        for sym in symbols: fingpt.register_symbol(sym)
        print(f"[FINGPT] 等待30秒让FinGPT完成首次更新...")
        time.sleep(30)
        _FIRST_CYCLE_DONE = True
        return
    
    # 🚀🚀🚀 并发获取所有K线数据
    all_symbols = ["BTC/USDT:USDT"] + [s for s in symbols if s != "BTC/USDT:USDT"]
    kline_data = fetch_klines_batch(ex, all_symbols, tf, limit, workers)
    btc_df = kline_data.get("BTC/USDT:USDT")
    
    # 🚀🚀🚀 批量获取funding数据
    funding_cache = batch_fetch_funding_rates(ex, symbols)

    for sym in symbols:
        df = kline_data.get(sym)
        if df is None: continue
        
        m = build_enhanced_metrics(df, cfg)
        vol_spike_ratio = m.get("vol_spike_ratio", 1.0)
        
        # 1. 趋势预判信号检测 (Trend Anticipation)
        correlation_analysis = None
        clean_sym = sym.split(':')[0].upper()
        if clean_sym != "BTC/USDT" and btc_df is not None:
            correlation_analysis = get_cached_correlation(sym, df, btc_df, btc_status, vol_spike_ratio=vol_spike_ratio)
        
        if cfg.get("trend_anticipation", {}).get("enabled", False) and TREND_ANTICIPATION_AVAILABLE:
            try:
                anti_sig = detect_trend_anticipation(cfg, ex, sym, df, btc_status, m, correlation_analysis)
                if anti_sig:
                    dedup = SignalDeduplicator(cfg)
                    should_emit, dedup_reason = dedup.should_emit(sym, "trend_anticipation", anti_sig["score"], anti_sig["bias"])
                    if should_emit:
                        if anti_sig["score"] >= cfg.get("push", {}).get("thresholds", {}).get("majors", 0.50):
                            claude_review_and_push(cfg, cur, anti_sig, reviewer, collector)
                    else:
                        print(f"[DEDUP] {sym} 预判去重: {dedup_reason}")
            except Exception as e:
                # print(f"  ⚠️ {sym} 趋势预判异常: {e}")
                pass
        
        # 2. 🔥 [已删除] 趋势跟随 - v7.9已移除，其特点已融入趋势预判

        # 3. 反转信号检测 (Reversal)
        signal = majors_signal_with_obs(cfg, ex, sym, df, btc_status, fingpt, correlation_analysis, funding_cache)
        if not signal: continue
        
        # 信号去重
        if TREND_ANTICIPATION_AVAILABLE:
            dedup = SignalDeduplicator(cfg)
            # 🔥 30分钟去重 (Config设置)
            should_emit, dedup_reason = dedup.should_emit(sym, "reversal", signal["score"], signal["bias"])
            if not should_emit:
                print(f"[DEDUP] 🔄 {sym} 反转去重: {dedup_reason}")
                continue
        
        if signal["score"] < cfg.get("push", {}).get("thresholds", {}).get("majors", 0.85):
            print(f"[SKIP] {sym} - 评分{signal['score']:.2f}低")
            continue
        
        # BTC过滤
        if clean_sym != "BTC/USDT":
            gate_cfg = cfg.get("push", {}).get("llm_gate", {})
            reversal_mode = gate_cfg.get("reversal_only", False)
            if not reversal_mode:
                should_skip = False
                skip_reasons = []
                if signal["bias"] == "long" and not btc_status.get("allow_long", True):
                    should_skip, skip_reasons = True, btc_status.get('altcoin_reversal_reasons', [])
                if signal["bias"] == "short" and not btc_status.get("allow_short", True):
                    should_skip, skip_reasons = True, btc_status.get('altcoin_reversal_reasons', [])
                if should_skip:
                    print(f"[BTC_FILTER] 跳过 {sym} ({', '.join(skip_reasons)})")
                    continue
        
        claude_review_and_push(cfg, cur, signal, reviewer, collector)

# ============ 主函数 ============
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-loop", action="store_true")
    ap.add_argument("--interval", type=int, default=60)
    ap.add_argument("--observe-only", action="store_true")
    ap.add_argument("--daily-report", action="store_true")
    ap.add_argument("--weekly-report", action="store_true")
    args = ap.parse_args()
    
    cfg = load_cfg()
    if args.observe_only:
        cfg.setdefault("push", {}).update({"observe_only": True})
    
    db = cfg["analytics"]["storage"]["path"]
    ensure_db(db)
    ex = get_exchange(cfg)
    
    if args.daily_report:
        report_daily_enhanced(cfg)
        return
    if args.weekly_report:
        report_weekly_enhanced(cfg, ex)
        return
    
    print("[INIT] 初始化组件...")
    cg_key = cfg.get("coingecko", {}).get("api_key", "")
    if not cg_key: cg_key = os.getenv("COINGECKO_API_KEY", "")
    fingpt = FreeFinGPT(coingecko_api_key=cg_key, config=cfg)
    fingpt.start_background_update()
    
    reviewer = ClaudeReviewer(cfg)
    print("  ✅ AI审核器 (Claude/DeepSeek)")
    
    collector = None
    if cfg.get("xgboost", {}).get("enabled", True):
        try:
            collector = XGBoostDataCollector(cfg, ex)
            print("  ✅ XGBoost收集器")
        except Exception: pass

    global _AUTO_TRADER, _SIGNAL_WATCHER
    _AUTO_TRADER = None
    if cfg.get("auto_trading", {}).get("enabled", False):
        try:
            _AUTO_TRADER = AutoTrader(cfg.get("auto_trading", {}), db, full_config=cfg)
            print("  ✅ OKX自动交易器")
        except Exception as e: print(f"  ⚠️ 自动交易器初始化失败: {e}")

    _SIGNAL_WATCHER = None
    if cfg.get("watch", {}).get("enabled", False):
        try:
            _SIGNAL_WATCHER = SignalWatcher(
                config=cfg.get("watch", {}),
                db_path="data/watch_signals.db",
                exchange=ex,
                claude_api_key=cfg.get("claude", {}).get("api_key", ""),
                deepseek_config=cfg.get("deepseek", {}),
                full_config=cfg
            )
            print("  ✅ 信号观察器 (v5.2)")
        except Exception as e: print(f"  ⚠️ 信号观察器初始化失败: {e}")
    
    # 🔥🔥🔥 高波动轨道初始化 (Track 2)
    global _HIGH_VOL_TRACK
    _HIGH_VOL_TRACK = None
    if cfg.get("high_volatility_track", {}).get("enabled", False) and HIGH_VOL_TRACK_AVAILABLE:
        try:
            _HIGH_VOL_TRACK = HighVolatilityTrack(
                config=cfg,
                exchange=ex,
                auto_trader=_AUTO_TRADER,
                db_path="data/high_vol_track.db"
            )
            hv_cfg = cfg.get("high_volatility_track", {})
            print("  ✅ 高波动轨道 (Track 2)")
            print(f"     └─ 扫描: 24h波动{hv_cfg.get('scan',{}).get('min_change_24h',0.08)*100:.0f}%-{hv_cfg.get('scan',{}).get('max_change_24h',0.40)*100:.0f}%")
            print(f"     └─ 观察池: {hv_cfg.get('observation_pool',{}).get('capacity',10)}个 | 就绪阈值: {hv_cfg.get('observation_pool',{}).get('readiness_threshold',75)}分")
            print(f"     └─ 资金占比: {hv_cfg.get('capital',{}).get('track_pct',0.30)*100:.0f}%")
        except Exception as e:
            print(f"  ⚠️ 高波动轨道初始化失败: {e}")
            import traceback
            traceback.print_exc()
    
    notify_startup(cfg)
    
    def one_cycle():
        global _BTC_MARKET_CACHE, _FIRST_CYCLE_DONE, _HIGH_VOL_TRACK
        conn = sqlite3.connect(db, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        cur = conn.cursor()
        try:
            print(f"\n{'='*60}\n[CYCLE] {dt.datetime.now().strftime('%H:%M:%S')}\n{'='*60}")
            if _FIRST_CYCLE_DONE: fingpt.clear_old_registrations()
            
            now = time.time()
            if now - _BTC_MARKET_CACHE.get("ts", 0) > 60:
                btc_status = check_btc_market_advanced(ex, cfg)
                _BTC_MARKET_CACHE["data"] = btc_status
                _BTC_MARKET_CACHE["ts"] = now
                print_btc_status_enhanced(btc_status)
            else:
                btc_status = _BTC_MARKET_CACHE["data"]
            
            # 🔹 轨道1：常规信号处理
            run_majors(cfg, ex, cur, btc_status, fingpt, reviewer, collector)
            
            # 🔸🔸🔸 轨道2：高波动信号处理（独立扫描全市场）
            if _HIGH_VOL_TRACK and _HIGH_VOL_TRACK.enabled:
                try:
                    # 🔥 独立获取全市场币种（不复用轨道1的symbols）
                    hv_symbols = discover_high_vol_symbols(cfg, ex)
                    
                    if hv_symbols:
                        tf = cfg.get("exchange", {}).get("timeframe", "1m")
                        limit = cfg.get("exchange", {}).get("limit", 2000)
                        workers = cfg.get("performance", {}).get("fetch_workers", 8)
                        
                        # 确保包含BTC
                        all_symbols = ["BTC/USDT:USDT"] + [s for s in hv_symbols if s != "BTC/USDT:USDT"]
                        
                        print(f"[HIGH_VOL] 📊 获取 {len(all_symbols)} 个币种K线...")
                        kline_data = fetch_klines_batch(ex, all_symbols, tf, limit, workers)
                        btc_df = kline_data.get("BTC/USDT:USDT")
                        
                        _HIGH_VOL_TRACK.run_once(
                            all_klines=kline_data,
                            btc_df=btc_df,
                            btc_status=btc_status
                        )
                        
                        # 打印高波动轨道状态
                        hv_status = _HIGH_VOL_TRACK.get_status()
                        if hv_status['observation_pool'] > 0 or hv_status['active_orders'] > 0 or hv_status['active_positions'] > 0:
                            print(f"\n🔸 轨道2状态: 观察{hv_status['observation_pool']}/{hv_status['pool_capacity']} | "
                                  f"挂单{hv_status['active_orders']}/{hv_status['max_orders']} | "
                                  f"持仓{hv_status['active_positions']}")
                        
                except Exception as e:
                    print(f"[HIGH_VOL] ❌ 轨道2异常: {e}")
                    import traceback
                    traceback.print_exc()
            
            if _SIGNAL_WATCHER: _SIGNAL_WATCHER.monitor()
            if _AUTO_TRADER: _AUTO_TRADER.run_once()
            if collector: 
                try: collector.check_pending_signals() 
                except: pass
                
            conn.commit()
        finally:
            conn.close()
    
    if args.run_loop:
        print("[MAIN] 进入主循环...")
        print(f"间隔: {args.interval}秒\n")
        last_daily_check = None
        last_weekly_check = None
        last_tracking_check = None
        cycle_count = 0
        try:
            while True:
                now = dt.datetime.now()
                cycle_count += 1
                if last_tracking_check is None or (now - last_tracking_check).total_seconds() > 14400:
                    update_signal_tracking(cfg, db)
                    last_tracking_check = now
                
                if should_run_daily_report(cfg):
                    if last_daily_check is None or (now - last_daily_check).total_seconds() > 3600:
                        report_daily_enhanced(cfg)
                        last_daily_check = now
                        
                if should_run_weekly_report(cfg):
                    if last_weekly_check is None or (now - last_weekly_check).total_seconds() > 3600:
                        report_weekly_enhanced(cfg, ex)
                        last_weekly_check = now
                        
                if cycle_count % 10 == 0: cleanup_funding_oi_cache()
                
                one_cycle()
                time.sleep(max(10, int(args.interval)))
        except KeyboardInterrupt:
            print("\n[MAIN] 正在停止...")
            fingpt.stop()
            tg_send(cfg, "系统", ["已停止"])
            print("[MAIN] 退出")
    else:
        one_cycle()

if __name__ == "__main__":
    main()