# core/btc_advanced_monitor.py
# -*- coding: utf-8 -*-
"""
btc_advanced_monitor.py - 增强版BTC市场监控 (网络容错版)
包含: RSI + 动量分析 + 支撑阻力位 + 反转预警 + BTC市场占比
修复: RSI阈值从40/60调整为25/75,减少误判
新增: 区分BTC自身和山寨币适用的过滤原因
新增: BTC Dominance监控（1小时更新一次）
🆕 新增: 网络重试机制 + 缓存降级，提高稳定性
"""
import pandas as pd
import numpy as np
import requests
import time
from typing import Dict, Any, List, Tuple, Optional

# 🆕 Dominance 缓存（1小时有效）
_DOMINANCE_CACHE = {
    "data": None,
    "timestamp": 0,
    "ttl": 3600  # 1小时 = 3600秒
}

# 🆕 BTC数据缓存（用于网络失败时的降级）
_BTC_DATA_CACHE = {
    "data": None,
    "timestamp": 0,
    "ttl": 300  # 缓存有效期5分钟，超过后标记为过期但仍可降级使用
}


def calculate_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """计算RSI指标"""
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    return rsi


def get_btc_dominance(coingecko_api_key: str = "") -> Dict[str, Any]:
    """
    🆕 获取BTC市场占比（Dominance）
    使用1小时缓存，减少API调用
    
    Returns:
        {
            "dominance": 58.5,           # BTC市场占比 (%)
            "dominance_change_24h": 0.3, # 24小时变化 (%)
            "market_cap": 1200000000000, # BTC市值 ($)
            "total_market_cap": 2050000000000,  # 总市值 ($)
            "cached": True/False         # 是否使用缓存
        }
    """
    global _DOMINANCE_CACHE
    
    now = time.time()
    
    # 检查缓存
    if (_DOMINANCE_CACHE["data"] is not None and 
        now - _DOMINANCE_CACHE["timestamp"] < _DOMINANCE_CACHE["ttl"]):
        data = _DOMINANCE_CACHE["data"].copy()
        data["cached"] = True
        data["cache_age_sec"] = int(now - _DOMINANCE_CACHE["timestamp"])
        return data
    
    # 调用 CoinGecko API
    try:
        url = "https://api.coingecko.com/api/v3/global"
        headers = {}
        if coingecko_api_key:
            headers["x-cg-demo-api-key"] = coingecko_api_key
        
        response = requests.get(url, headers=headers, timeout=10)
        
        if response.status_code == 429:
            print("[BTC_DOM] ⚠️ CoinGecko API限流，使用旧缓存")
            if _DOMINANCE_CACHE["data"]:
                return _DOMINANCE_CACHE["data"]
            return {"dominance": 0, "dominance_change_24h": 0, "cached": False}
        
        if response.status_code != 200:
            print(f"[BTC_DOM] ⚠️ API返回错误码: {response.status_code}")
            if _DOMINANCE_CACHE["data"]:
                return _DOMINANCE_CACHE["data"]
            return {"dominance": 0, "dominance_change_24h": 0, "cached": False}
        
        data = response.json()
        global_data = data.get("data", {})
        
        # 提取 BTC Dominance
        market_cap_percentage = global_data.get("market_cap_percentage", {})
        btc_dominance = market_cap_percentage.get("btc", 0)
        
        # 提取市值数据
        market_cap_btc = global_data.get("market_cap", {}).get("btc", 0)
        total_market_cap = global_data.get("total_market_cap", {}).get("usd", 0)
        
        # 计算24小时变化（通过市值变化推算）
        market_cap_change_24h = global_data.get("market_cap_change_percentage_24h_usd", 0)
        
        result = {
            "dominance": round(btc_dominance, 2),
            "dominance_change_24h": round(market_cap_change_24h * 0.1, 2),  # 粗略估算
            "market_cap": market_cap_btc,
            "total_market_cap": total_market_cap,
            "cached": False,
            "cache_age_sec": 0,
            "timestamp": now
        }
        
        # 更新缓存
        _DOMINANCE_CACHE["data"] = result
        _DOMINANCE_CACHE["timestamp"] = now
        
        print(f"[BTC_DOM] ✅ 更新成功: {btc_dominance:.2f}% (下次更新: 1小时后)")
        
        return result
        
    except Exception as e:
        print(f"[BTC_DOM] ⚠️ 获取失败: {e}")
        # 返回旧缓存或默认值
        if _DOMINANCE_CACHE["data"]:
            return _DOMINANCE_CACHE["data"]
        return {"dominance": 0, "dominance_change_24h": 0, "cached": False}


def analyze_momentum(df: pd.DataFrame) -> Dict[str, Any]:
    """
    分析价格动量
    """
    if len(df) < 20:
        return {
            "momentum_15m": 0, "momentum_5m": 0, "momentum_1m": 0,
            "is_weakening": False, "acceleration": 0
        }
    
    price_now = float(df['close'].iloc[-1])
    price_15m = float(df['close'].iloc[-15])
    price_5m = float(df['close'].iloc[-5])
    price_1m = float(df['close'].iloc[-2]) if len(df) >= 2 else price_now
    
    momentum_15m = (price_now - price_15m) / price_15m * 100
    momentum_5m = (price_now - price_5m) / price_5m * 100
    momentum_1m = (price_now - price_1m) / price_1m * 100
    
    is_weakening = False
    if abs(momentum_15m) > 0.3:
        if momentum_15m > 0: is_weakening = momentum_5m < momentum_15m * 0.5
        else: is_weakening = momentum_5m > momentum_15m * 0.5
    
    acceleration = (momentum_5m - momentum_15m) / 10
    
    return {
        "momentum_15m": round(momentum_15m, 3), 
        "momentum_5m": round(momentum_5m, 3),
        "momentum_1m": round(momentum_1m, 3), 
        "is_weakening": is_weakening,
        "acceleration": round(acceleration, 3)
    }


def find_support_resistance(df: pd.DataFrame, lookback: int = 60) -> Dict[str, Any]:
    """
    识别支撑位和阻力位
    """
    recent_df = df.tail(lookback)
    support = float(recent_df['low'].quantile(0.2))
    resistance = float(recent_df['high'].quantile(0.8))
    price_now = float(df['close'].iloc[-1])
    
    distance_to_support_pct = (price_now - support) / price_now * 100
    distance_to_resistance_pct = (resistance - price_now) / price_now * 100
    
    near_threshold = 2.0
    near_support = distance_to_support_pct < near_threshold
    near_resistance = distance_to_resistance_pct < near_threshold
    
    return {
        "support": round(support, 2), 
        "resistance": round(resistance, 2),
        "distance_to_support_pct": round(distance_to_support_pct, 2),
        "distance_to_resistance_pct": round(distance_to_resistance_pct, 2),
        "near_support": near_support, 
        "near_resistance": near_resistance
    }


def detect_volume_spike(df: pd.DataFrame) -> Dict[str, Any]:
    """
    检测成交量异常
    """
    if len(df) < 20: 
        return {"volume_ratio": 1.0, "is_spike": False}
    
    volume_ma = float(df['volume'].rolling(20).mean().iloc[-1])
    current_volume = float(df['volume'].iloc[-1])
    
    volume_ratio = current_volume / volume_ma if volume_ma > 0 else 1.0
    return {
        "volume_ratio": round(volume_ratio, 2), 
        "is_spike": volume_ratio > 1.5
    }


def analyze_btc_reversal_risk(
    df: pd.DataFrame, change_1h: float, change_4h: float
) -> Dict[str, Any]:
    """
    综合分析BTC反转风险
    🔧 修复: RSI阈值优化 (25/30/70/75)
    """
    rsi_series = calculate_rsi(df['close'])
    current_rsi = float(rsi_series.iloc[-1])
    momentum = analyze_momentum(df)
    sr = find_support_resistance(df)
    volume = detect_volume_spike(df)
    
    reversal_risk = "none"
    reversal_reasons = []
    
    # 🔧 核心修改: 上涨时的RSI检查 (阈值调整)
    if change_1h > 0:
        # 只有真正极端的RSI才触发HIGH风险
        if current_rsi > 75:  # ✅ 从70改为75
            reversal_risk = "high"
            reversal_reasons.append(f"RSI超买({current_rsi:.1f})")
        elif current_rsi > 70:  # ✅ 从65改为70
            reversal_risk = "medium" if reversal_risk == "none" else reversal_risk
            reversal_reasons.append(f"RSI偏高({current_rsi:.1f})")
        
        # 动势衰竭检查
        if momentum.get("is_weakening"):
            reversal_risk = "medium" if reversal_risk == "none" else "high"
            reversal_reasons.append("涨势衰竭")
        
        # 阻力位检查
        if sr.get("near_resistance"):
            reversal_risk = "medium" if reversal_risk == "none" else "high"
            reversal_reasons.append(f"接近阻力位({sr['resistance']:.0f})")
        
        # 放量+超买组合(顶部信号) - 阈值也调整为70
        if volume.get("is_spike") and current_rsi > 70:  # ✅ 从65改为70
            reversal_risk = "high"
            reversal_reasons.append("放量+超买(疑似顶部)")
    
    # 🔧 核心修改: 下跌时的RSI检查 (阈值调整)
    elif change_1h < 0:
        # 只有真正极端的RSI才触发HIGH风险
        if current_rsi < 25:  # ✅ 从30改为25
            reversal_risk = "high"
            reversal_reasons.append(f"RSI超卖({current_rsi:.1f})")
        elif current_rsi < 30:  # ✅ 从35改为30
            reversal_risk = "medium" if reversal_risk == "none" else reversal_risk
            reversal_reasons.append(f"RSI偏低({current_rsi:.1f})")
        
        # 动势衰竭检查
        if momentum.get("is_weakening"):
            reversal_risk = "medium" if reversal_risk == "none" else "high"
            reversal_reasons.append("跌势衰竭")
        
        # 支撑位检查
        if sr.get("near_support"):
            reversal_risk = "medium" if reversal_risk == "none" else "high"
            reversal_reasons.append(f"接近支撑位({sr['support']:.0f})")
        
        # 放量+超卖组合(底部信号) - 阈值也调整为30
        if volume.get("is_spike") and current_rsi < 30:  # ✅ 从35改为30
            reversal_risk = "high"
            reversal_reasons.append("放量+超卖(疑似底部)")

    # 决定建议操作
    recommended_action = "ALLOW_ALL"
    if reversal_risk == "high":
        if change_1h > 0:
            recommended_action = "BLOCK_LONG"
        elif change_1h < 0:
            recommended_action = "BLOCK_SHORT"
        reversal_reasons.append(f"⛔ 暂停做{'多' if change_1h > 0 else '空'}")

    return {
        "reversal_risk": reversal_risk, 
        "reversal_reasons": reversal_reasons, 
        "rsi": round(current_rsi, 1),
        "momentum": momentum, 
        "support_resistance": sr, 
        "volume": volume,
        "recommended_action": recommended_action
    }


def _get_default_btc_status() -> Dict[str, Any]:
    """默认BTC状态(数据获取失败时)"""
    return {
        "allow_long": True, 
        "allow_short": True, 
        "trend": "unknown", 
        "price": 0,
        "price_change_1h": 0,
        "price_change_4h": 0,
        "dominance": 0,
        "dominance_change": 0,
        "volatility": 0,
        "volatility_state": "unknown",
        "reversal_risk": "unknown", 
        "reversal_reasons": ["数据获取失败"],
        "altcoin_reversal_reasons": ["数据获取失败"],
        "rsi": 50, 
        "momentum_15m": 0,
        "is_weakening": False, 
        "support": 0, 
        "resistance": 0, 
        "updated": False, 
        "cache_age_sec": 0
    }


def _fetch_btc_ohlcv_with_retry(ex, symbol: str, max_retries: int = 3) -> Optional[list]:
    """
    🆕 带重试机制的K线数据获取
    
    Args:
        ex: ccxt交易所实例
        symbol: 交易对
        max_retries: 最大重试次数
        
    Returns:
        K线数据列表，失败返回None
    """
    last_error = None
    
    for attempt in range(max_retries):
        try:
            ohlcv = ex.fetch_ohlcv(symbol, '1m', limit=300)
            return ohlcv
        except Exception as e:
            last_error = e
            error_type = type(e).__name__
            
            # 判断是否值得重试的错误类型
            retryable_errors = [
                'NetworkError', 'RequestTimeout', 'ExchangeNotAvailable',
                'ConnectionError', 'RemoteDisconnected', 'ProtocolError'
            ]
            
            is_retryable = any(err in error_type or err in str(e) for err in retryable_errors)
            
            if attempt < max_retries - 1 and is_retryable:
                wait_time = 2 ** attempt  # 指数退避: 1s, 2s, 4s
                print(f"[BTC_ADV] ⚠️ 网络错误({error_type})，{wait_time}秒后重试 ({attempt+1}/{max_retries})...")
                time.sleep(wait_time)
            else:
                # 最后一次失败或不可重试的错误
                if attempt == max_retries - 1:
                    print(f"[BTC_ADV] ❌ {max_retries}次重试后仍失败: {error_type}")
                else:
                    print(f"[BTC_ADV] ❌ 不可重试的错误: {error_type} - {str(e)[:100]}")
                break
    
    return None


def _get_cached_btc_data() -> Optional[Dict[str, Any]]:
    """
    🆕 获取缓存的BTC数据（用于降级）
    """
    global _BTC_DATA_CACHE
    
    if _BTC_DATA_CACHE["data"] is None:
        return None
    
    now = time.time()
    cache_age = int(now - _BTC_DATA_CACHE["timestamp"])
    
    # 复制缓存数据并更新缓存年龄
    cached = _BTC_DATA_CACHE["data"].copy()
    cached["cache_age_sec"] = cache_age
    cached["updated"] = False  # 标记为非实时数据
    
    # 缓存超过5分钟，添加警告标记
    if cache_age > _BTC_DATA_CACHE["ttl"]:
        cached["reversal_reasons"] = cached.get("reversal_reasons", []) + [f"⚠️ 数据延迟{cache_age}秒"]
    
    return cached


def _update_btc_cache(data: Dict[str, Any]) -> None:
    """
    🆕 更新BTC数据缓存
    """
    global _BTC_DATA_CACHE
    _BTC_DATA_CACHE["data"] = data.copy()
    _BTC_DATA_CACHE["timestamp"] = time.time()


def check_btc_market_advanced(ex, cfg) -> Dict[str, Any]:
    """
    增强版BTC市场监控 - 主入口函数
    🔧 新增: 区分BTC自身和山寨币适用的过滤原因
    🆕 新增: BTC Dominance 监控
    🆕 新增: 网络重试机制 + 缓存降级
    """
    symbol = "BTC/USDT:USDT"
    
    # 🆕 使用带重试的数据获取
    ohlcv = _fetch_btc_ohlcv_with_retry(ex, symbol, max_retries=3)
    
    # 🆕 数据获取失败，尝试使用缓存降级
    if ohlcv is None:
        cached = _get_cached_btc_data()
        if cached:
            print(f"[BTC_ADV] 📦 使用缓存数据降级 (age: {cached['cache_age_sec']}s)")
            return cached
        else:
            print("[BTC_ADV] ❌ 无可用缓存，返回默认状态")
            return _get_default_btc_status()
    
    try:
        # 数据长度检查
        if len(ohlcv) < 240:
            print("[BTC_ADV_WARN] 获取的BTC K线数据不足240根,部分指标可能不准。")
            if len(ohlcv) < 100:
                cached = _get_cached_btc_data()
                if cached:
                    return cached
                return _get_default_btc_status()

        df = pd.DataFrame(ohlcv, columns=["ts", "open", "high", "low", "close", "volume"])
        
        price_now = float(df['close'].iloc[-1])
        price_1h = float(df['close'].iloc[-60])
        price_4h = float(df['close'].iloc[-240])
        
        change_1h = (price_now - price_1h) / price_1h
        change_4h = (price_now - price_4h) / price_4h
        
        # 🆕 获取 BTC Dominance（1小时缓存）
        coingecko_key = cfg.get("coingecko", {}).get("api_key", "")
        dominance_data = get_btc_dominance(coingecko_key)
        
        # 调用修复后的反转风险分析
        reversal_analysis = analyze_btc_reversal_risk(df, change_1h, change_4h)
        
        allow_long, allow_short, trend = True, True, "stable"
        
        # 价格阈值检查
        th_1h = cfg.get("btc_monitor", {}).get("price_threshold_1h", 0.01)
        th_4h = cfg.get("btc_monitor", {}).get("price_threshold_4h", 0.015)
        
        if change_1h < -th_1h or change_4h < -th_4h: 
            trend, allow_long = "crash", False
        elif change_1h > th_1h or change_4h > th_4h: 
            trend, allow_short = "moon", False
        
        # 应用反转风险分析的建议
        if reversal_analysis["recommended_action"] == "BLOCK_LONG": 
            allow_long = False
        elif reversal_analysis["recommended_action"] == "BLOCK_SHORT": 
            allow_short = False
        
        # 🔧 新增: 生成适用于山寨币的过滤原因 (排除价格位置相关)
        altcoin_reversal_reasons = []
        for reason in reversal_analysis["reversal_reasons"]:
            # 只保留山寨币需要关注的原因:
            # 1. RSI极端情况
            # 2. 动势衰竭
            # 3. 放量+极端RSI组合
            # 排除: 支撑位、阻力位、⛔标记
            if ("支撑位" not in reason and 
                "阻力位" not in reason and 
                "⛔" not in reason):
                altcoin_reversal_reasons.append(reason)
        
        # 波动率状态
        vol_cfg = cfg.get("btc_monitor", {})
        returns_1h = df["close"].pct_change().tail(60)
        vol_1h = float(returns_1h.std())
        volatility = vol_1h * 100  # 转为百分比
        volatility_state = "normal"
        if vol_1h > vol_cfg.get("volatility_extreme", 0.04)/60: 
            volatility_state = "extreme"
        elif vol_1h > vol_cfg.get("volatility_high", 0.02)/60: 
            volatility_state = "high"
        elif vol_1h < vol_cfg.get("volatility_low", 0.005)/60:
            volatility_state = "low"

        result = {
            "allow_long": allow_long, 
            "allow_short": allow_short, 
            "trend": trend,
            "price": price_now,
            "price_change_1h": round(change_1h * 100, 2),  # 转为百分比
            "price_change_4h": round(change_4h * 100, 2),  # 转为百分比
            
            # 🆕 Dominance 数据
            "dominance": dominance_data.get("dominance", 0),
            "dominance_change": dominance_data.get("dominance_change_24h", 0),
            "dominance_cached": dominance_data.get("cached", False),
            
            # 波动率
            "volatility": round(volatility, 2),
            "volatility_state": volatility_state,
            
            # 反转风险
            "reversal_risk": reversal_analysis["reversal_risk"],
            "reversal_reasons": reversal_analysis["reversal_reasons"],  # BTC自己用的完整原因
            "altcoin_reversal_reasons": altcoin_reversal_reasons,  # 🔧 山寨币用的过滤原因
            
            # 技术指标
            "rsi": reversal_analysis["rsi"],
            "momentum_15m": reversal_analysis["momentum"].get("momentum_15m", 0),
            "support": reversal_analysis["support_resistance"].get("support", 0),
            "resistance": reversal_analysis["support_resistance"].get("resistance", 0),
            
            "updated": True, 
            "cache_age_sec": 0
        }
        
        # 🆕 更新缓存
        _update_btc_cache(result)
        
        return result
        
    except Exception as e:
        # 处理过程中出错，尝试缓存降级
        print(f"[BTC_ADV_ERR] 分析过程失败: {type(e).__name__}: {str(e)[:100]}")
        
        cached = _get_cached_btc_data()
        if cached:
            print(f"[BTC_ADV] 📦 分析失败，使用缓存降级 (age: {cached['cache_age_sec']}s)")
            return cached
        
        return _get_default_btc_status()


def format_btc_status_message(btc_status: Dict[str, Any]) -> List[str]:
    """
    格式化BTC状态消息
    🆕 新增: 显示缓存状态
    """
    trend_emoji = {"stable": "🟢", "moon": "🚀", "crash": "💥", "unknown": "❓"}
    trend_name = {"stable": "稳定", "moon": "急涨", "crash": "急跌", "unknown": "未知"}
    vol_emoji = {"low": "😴", "normal": "➡️", "high": "⚡", "extreme": "🔥", "unknown": "❓"}
    
    emoji = trend_emoji.get(btc_status["trend"], "❓")
    name = trend_name.get(btc_status["trend"], "未知")
    vol_e = vol_emoji.get(btc_status.get("volatility_state", "unknown"), "❓")
    
    # 🆕 缓存状态标识
    cache_indicator = ""
    cache_age = btc_status.get("cache_age_sec", 0)
    if not btc_status.get("updated", True) and cache_age > 0:
        if cache_age > 300:
            cache_indicator = f" ⚠️[缓存{cache_age//60}分钟]"
        else:
            cache_indicator = f" 📦[缓存{cache_age}秒]"
    
    messages = [
        f"💰 当前价格: ${btc_status['price']:,.2f}{cache_indicator}",
        f"➡️ 1小时涨跌: {btc_status['price_change_1h']:+.2f}%",
        f"➡️ 4小时涨跌: {btc_status['price_change_4h']:+.2f}%",
        f"{vol_e} 波动率: {btc_status.get('volatility', 0):.2f}% ({btc_status.get('volatility_state', 'UNKNOWN').upper()})",
        f"{emoji} 趋势: {name.upper()}",
        f"⚖️ RSI: {btc_status['rsi']:.1f}",
        f"➡️ 动量(15分钟): {btc_status['momentum_15m']:+.2f}%",
    ]
    
    # 交易方向建议
    if btc_status["allow_long"] and btc_status["allow_short"]:
        messages.append("✅ 山寨币: 双向可交易")
    elif btc_status["allow_long"]:
        messages.append("⚠️ 山寨币: 仅可做多")
    elif btc_status["allow_short"]:
        messages.append("⚠️ 山寨币: 仅可做空")
    else:
        messages.append("🚫 山寨币: 暂停交易")
    
    return messages


# 🆕 新增: 获取缓存状态的工具函数
def get_btc_cache_status() -> Dict[str, Any]:
    """
    获取BTC数据缓存状态（用于诊断）
    """
    global _BTC_DATA_CACHE
    
    if _BTC_DATA_CACHE["data"] is None:
        return {"has_cache": False, "cache_age_sec": 0}
    
    now = time.time()
    cache_age = int(now - _BTC_DATA_CACHE["timestamp"])
    
    return {
        "has_cache": True,
        "cache_age_sec": cache_age,
        "cache_valid": cache_age <= _BTC_DATA_CACHE["ttl"],
        "cached_price": _BTC_DATA_CACHE["data"].get("price", 0),
        "cached_trend": _BTC_DATA_CACHE["data"].get("trend", "unknown")
    }