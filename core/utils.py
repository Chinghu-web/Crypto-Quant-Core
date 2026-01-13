# core/utils.py - v2.0 升级版 (新增CVD + Efficiency Ratio + Hurst指标)
# -*- coding: utf-8 -*-
"""
🔥 v2.0 更新:
1. 新增 CVD (累积成交量差) - 识别真假突破
2. 新增 Efficiency Ratio - 趋势纯度
3. 新增 Hurst Exponent - 趋势持续性
4. 新增 真假突破综合检测器
"""

import os, json, time, math, subprocess, hashlib
from typing import List, Dict, Any, Optional, Tuple
import pandas as pd
import numpy as np
from datetime import datetime, timezone, timedelta

# ========== 技术指标 ==========
def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=int(period), adjust=False).mean()

def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([(h - l).abs(),
                    (h - c.shift(1)).abs(),
                    (l - c.shift(1)).abs()], axis=1).max(axis=1)
    out = tr.rolling(int(period)).mean()
    out = out.bfill()
    return out

def obv(df: pd.DataFrame) -> pd.Series:
    close = df["close"].values
    vol = df["volume"].values
    obv_vals = [0.0]
    for i in range(1, len(close)):
        if close[i] > close[i-1]:
            obv_vals.append(obv_vals[-1] + vol[i])
        elif close[i] < close[i-1]:
            obv_vals.append(obv_vals[-1] - vol[i])
        else:
            obv_vals.append(obv_vals[-1])
    return pd.Series(obv_vals, index=df.index)

def realized_vol(returns: pd.Series) -> float:
    if returns is None or len(returns) == 0:
        return 0.0
    return float(np.sqrt(np.sum(np.square(returns))))

def wick_scores(df: pd.DataFrame) -> Tuple[float, float]:
    body = (df["close"] - df["open"]).abs() + 1e-12
    up_wick = (df["high"] - df[["close","open"]].max(axis=1)) / body
    down_wick = (df[["close","open"]].min(axis=1) - df["low"]) / body
    return float(up_wick.tail(50).clip(0,10).mean()), float(down_wick.tail(50).clip(0,10).mean())


# ========== 🆕 CVD (累积成交量差) ==========
def calculate_cvd(df: pd.DataFrame) -> pd.Series:
    """
    计算CVD (Cumulative Volume Delta) - 累积成交量差
    
    原理: 价格上涨时视为主买，下跌时视为主卖
    - CVD上升 = 买盘力量占优
    - CVD下降 = 卖盘力量占优
    
    用途: 配合价格识别真假突破
    - 价格上涨 + CVD上涨 = 真突破 (买盘推动)
    - 价格上涨 + CVD下跌 = 假突破 (卖盘出货)
    
    Returns:
        CVD序列
    """
    # 方向判断: close > open 为买盘主导，反之为卖盘主导
    direction = np.sign(df['close'].values - df['open'].values)
    # 成交量乘以方向
    volume_delta = direction * df['volume'].values
    # 累积求和
    cvd = np.cumsum(volume_delta)
    return pd.Series(cvd, index=df.index)


def cvd_divergence(df: pd.DataFrame, lookback: int = 20) -> Dict[str, Any]:
    """
    检测CVD背离 - 核心的真假突破检测器
    
    Args:
        df: K线数据
        lookback: 回看周期
        
    Returns:
        {
            "cvd_delta": CVD变化百分比,
            "price_delta": 价格变化百分比,
            "divergence": "bullish"/"bearish"/"none",
            "divergence_strength": 背离强度 0-100,
            "is_fake_breakout": 是否假突破,
            "signal_quality": 信号质量评分
        }
    """
    if len(df) < lookback + 5:
        return {
            "cvd_delta": 0, "price_delta": 0,
            "divergence": "none", "divergence_strength": 0,
            "is_fake_breakout": False, "signal_quality": 50
        }
    
    cvd = calculate_cvd(df)
    
    # 计算近期变化
    cvd_now = cvd.iloc[-1]
    cvd_past = cvd.iloc[-lookback]
    price_now = df['close'].iloc[-1]
    price_past = df['close'].iloc[-lookback]
    
    # 防止除零
    cvd_range = max(abs(cvd.iloc[-lookback:].max() - cvd.iloc[-lookback:].min()), 1)
    price_past_safe = max(price_past, 1e-10)
    
    cvd_delta = (cvd_now - cvd_past) / cvd_range * 100  # 归一化
    price_delta = (price_now - price_past) / price_past_safe * 100
    
    # 判断背离类型
    divergence = "none"
    divergence_strength = 0
    is_fake_breakout = False
    
    # 价格上涨但CVD下跌 = 看跌背离 (假突破风险)
    if price_delta > 1 and cvd_delta < -5:
        divergence = "bearish"
        divergence_strength = min(100, abs(cvd_delta) * 2)
        if price_delta > 3 and cvd_delta < -10:
            is_fake_breakout = True
    
    # 价格下跌但CVD上涨 = 看涨背离 (假跌风险)
    elif price_delta < -1 and cvd_delta > 5:
        divergence = "bullish"
        divergence_strength = min(100, abs(cvd_delta) * 2)
        if price_delta < -3 and cvd_delta > 10:
            is_fake_breakout = True
    
    # 计算信号质量 (CVD和价格同向时质量高)
    if price_delta * cvd_delta > 0:  # 同向
        signal_quality = min(100, 50 + abs(cvd_delta) * 2)
    else:  # 背离
        signal_quality = max(0, 50 - divergence_strength * 0.5)
    
    return {
        "cvd_delta": round(cvd_delta, 2),
        "price_delta": round(price_delta, 2),
        "divergence": divergence,
        "divergence_strength": round(divergence_strength, 1),
        "is_fake_breakout": is_fake_breakout,
        "signal_quality": round(signal_quality, 1)
    }


# ========== 🆕 Efficiency Ratio (效率比) ==========
def efficiency_ratio(df: pd.DataFrame, period: int = 20) -> float:
    """
    计算Efficiency Ratio (效率比/ER)
    
    公式: ER = 净移动距离 / 总移动距离
    
    解释:
    - ER接近1: 价格趋势纯净，直线移动，噪音小
    - ER接近0: 价格震荡剧烈，来回波动，噪音大
    
    用途:
    - ER > 0.6: 趋势明确，适合趋势跟踪
    - ER < 0.3: 震荡市，不适合趋势策略
    - ER 0.3-0.6: 趋势形成中
    
    Args:
        df: K线数据
        period: 计算周期
        
    Returns:
        效率比 (0-1)
    """
    if len(df) < period + 1:
        return 0.5
    
    close = df['close'].tail(period + 1)
    
    # 净移动距离 (起点到终点的直线距离)
    net_move = abs(close.iloc[-1] - close.iloc[0])
    
    # 总移动距离 (每根K线变化的绝对值之和)
    total_move = close.diff().abs().sum()
    
    if total_move == 0:
        return 0.5
    
    er = net_move / total_move
    return round(float(er), 4)


def efficiency_ratio_trend(df: pd.DataFrame, period: int = 20) -> Dict[str, Any]:
    """
    效率比趋势分析
    
    Returns:
        {
            "er": 当前效率比,
            "er_5": 5周期前效率比,
            "trend": "trending"/"ranging"/"forming",
            "trend_quality": 趋势质量评分 0-100,
            "recommendation": 建议
        }
    """
    if len(df) < period + 10:
        return {
            "er": 0.5, "er_5": 0.5,
            "trend": "unknown", "trend_quality": 50,
            "recommendation": "数据不足"
        }
    
    er_now = efficiency_ratio(df, period)
    er_5 = efficiency_ratio(df.iloc[:-5], period)
    
    # 判断趋势状态
    if er_now >= 0.6:
        trend = "trending"
        trend_quality = min(100, er_now * 100 + 20)
        recommendation = "趋势明确，可以跟随"
    elif er_now <= 0.3:
        trend = "ranging"
        trend_quality = max(0, er_now * 100)
        recommendation = "震荡市，谨慎操作"
    else:
        trend = "forming"
        trend_quality = er_now * 100
        if er_now > er_5:
            recommendation = "趋势正在形成，等待确认"
        else:
            recommendation = "趋势减弱，注意风险"
    
    return {
        "er": er_now,
        "er_5": er_5,
        "trend": trend,
        "trend_quality": round(trend_quality, 1),
        "recommendation": recommendation
    }


# ========== 🆕 Hurst Exponent (赫斯特指数) ==========
def hurst_exponent(series: pd.Series, max_lag: int = 20) -> float:
    """
    计算Hurst Exponent (赫斯特指数)
    
    使用R/S (Rescaled Range) 分析法
    
    解释:
    - H > 0.5: 趋势持续性 (趋势倾向于继续)
    - H = 0.5: 随机游走 (无法预测)
    - H < 0.5: 均值回归 (价格倾向于反转)
    
    用途:
    - H > 0.55: 适合趋势跟踪策略
    - H < 0.45: 适合均值回归策略
    - H ≈ 0.5: 市场随机，策略效果不佳
    
    Args:
        series: 价格序列
        max_lag: 最大滞后期
        
    Returns:
        Hurst指数 (0-1)
    """
    if len(series) < max_lag * 2:
        return 0.5
    
    series = series.dropna()
    if len(series) < max_lag * 2:
        return 0.5
    
    lags = range(2, min(max_lag, len(series) // 2))
    
    # 计算每个滞后期的标准差
    tau = []
    for lag in lags:
        # 计算滞后差分的标准差
        diff = series.values[lag:] - series.values[:-lag]
        if len(diff) > 0:
            tau.append(np.std(diff))
        else:
            tau.append(1e-10)
    
    if len(tau) < 3:
        return 0.5
    
    # 对数回归求斜率
    try:
        log_lags = np.log(list(lags))
        log_tau = np.log(np.array(tau) + 1e-10)
        
        # 线性回归
        slope, _ = np.polyfit(log_lags, log_tau, 1)
        
        # Hurst指数 = 斜率
        hurst = slope
        
        # 限制在合理范围内
        hurst = max(0.0, min(1.0, hurst))
        
        return round(float(hurst), 4)
    except:
        return 0.5


def hurst_analysis(df: pd.DataFrame, period: int = 60) -> Dict[str, Any]:
    """
    Hurst指数综合分析
    
    Returns:
        {
            "hurst": 当前Hurst指数,
            "regime": "trending"/"mean_reverting"/"random",
            "persistence": 持续性评分 0-100,
            "strategy_fit": 适合的策略类型,
            "recommendation": 建议
        }
    """
    if len(df) < period:
        return {
            "hurst": 0.5,
            "regime": "unknown",
            "persistence": 50,
            "strategy_fit": "unknown",
            "recommendation": "数据不足"
        }
    
    close = df['close'].tail(period)
    h = hurst_exponent(close, max_lag=min(20, period // 3))
    
    # 判断市场状态
    if h > 0.55:
        regime = "trending"
        persistence = min(100, (h - 0.5) * 200 + 50)
        strategy_fit = "趋势跟踪"
        recommendation = "趋势持续性强，适合顺势交易"
    elif h < 0.45:
        regime = "mean_reverting"
        persistence = max(0, (0.5 - h) * 200)
        strategy_fit = "均值回归"
        recommendation = "价格倾向回归，适合逆势交易"
    else:
        regime = "random"
        persistence = 50
        strategy_fit = "观望"
        recommendation = "市场随机性强，建议观望"
    
    return {
        "hurst": h,
        "regime": regime,
        "persistence": round(persistence, 1),
        "strategy_fit": strategy_fit,
        "recommendation": recommendation
    }


# ========== 🆕 综合突破质量评估 ==========
def breakout_quality_score(df: pd.DataFrame, lookback: int = 20) -> Dict[str, Any]:
    """
    综合突破质量评估 - 整合CVD、ER、Hurst三个指标
    
    这是用于高波动轨道AI审核的核心函数
    
    Args:
        df: K线数据
        lookback: 回看周期
        
    Returns:
        {
            "cvd_analysis": CVD分析结果,
            "er_analysis": 效率比分析结果,
            "hurst_analysis": Hurst分析结果,
            "overall_score": 综合评分 0-100,
            "is_quality_signal": 是否优质信号,
            "risk_level": "low"/"medium"/"high",
            "recommendation": 交易建议
        }
    """
    # 获取三个指标分析
    cvd_result = cvd_divergence(df, lookback)
    er_result = efficiency_ratio_trend(df, lookback)
    hurst_result = hurst_analysis(df, lookback * 3)
    
    # 计算综合评分
    # CVD权重40% (假突破检测最重要)
    cvd_score = cvd_result["signal_quality"]
    if cvd_result["is_fake_breakout"]:
        cvd_score = max(0, cvd_score - 30)  # 假突破严重扣分
    
    # ER权重30% (趋势纯度)
    er_score = er_result["trend_quality"]
    
    # Hurst权重30% (趋势持续性)
    hurst_score = hurst_result["persistence"]
    
    overall_score = cvd_score * 0.4 + er_score * 0.3 + hurst_score * 0.3
    
    # 判断风险等级
    if cvd_result["is_fake_breakout"]:
        risk_level = "high"
    elif overall_score >= 70:
        risk_level = "low"
    elif overall_score >= 50:
        risk_level = "medium"
    else:
        risk_level = "high"
    
    # 综合建议
    is_quality_signal = overall_score >= 60 and not cvd_result["is_fake_breakout"]
    
    if cvd_result["is_fake_breakout"]:
        recommendation = f"⚠️ 检测到假突破信号! CVD背离强度:{cvd_result['divergence_strength']:.0f}"
    elif is_quality_signal:
        recommendation = f"✅ 信号质量良好 (CVD:{cvd_score:.0f} ER:{er_score:.0f} H:{hurst_score:.0f})"
    else:
        weak_points = []
        if cvd_score < 50:
            weak_points.append("CVD背离")
        if er_score < 50:
            weak_points.append("趋势不纯")
        if hurst_score < 50:
            weak_points.append("持续性差")
        recommendation = f"⚠️ 信号质量一般，风险点: {', '.join(weak_points)}"
    
    return {
        "cvd_analysis": cvd_result,
        "er_analysis": er_result,
        "hurst_analysis": hurst_result,
        "overall_score": round(overall_score, 1),
        "is_quality_signal": is_quality_signal,
        "risk_level": risk_level,
        "recommendation": recommendation
    }


# ========== 🆕 RSI (相对强弱指标) ==========
def rsi(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """
    计算RSI指标 (Relative Strength Index)
    用于判断超买超卖:
    - RSI > 70: 超买区域
    - RSI > 80: 极度超买
    - RSI < 30: 超卖区域
    - RSI < 20: 极度超卖
    """
    close = df['close']
    delta = close.diff()
    
    # 分离涨跌
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    
    # 计算平均涨跌幅
    avg_gain = gain.rolling(window=period, min_periods=1).mean()
    avg_loss = loss.rolling(window=period, min_periods=1).mean()
    
    # 避免除以0
    avg_loss = avg_loss.replace(0, 1e-10)
    
    # 计算RS和RSI
    rs = avg_gain / avg_loss
    rsi_val = 100 - (100 / (1 + rs))
    
    # 填充NaN为50(中性)
    rsi_val = rsi_val.fillna(50)
    
    return rsi_val


# ========== 🆕 MACD (指数平滑异同移动平均线) ==========
def macd(df: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """
    计算MACD指标
    返回: (MACD线, 信号线, 柱状图)
    
    用于判断趋势和动量:
    - MACD > Signal: 看涨
    - MACD < Signal: 看跌
    - 柱状图 > 0: 多头力量增强
    - 柱状图 < 0: 空头力量增强
    """
    close = df['close']
    
    # 计算快慢EMA
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    
    # MACD线 = 快线 - 慢线
    macd_line = ema_fast - ema_slow
    
    # 信号线 = MACD线的EMA
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    
    # 柱状图 = MACD线 - 信号线
    histogram = macd_line - signal_line
    
    return macd_line, signal_line, histogram


# ========== 🆕 ADX (平均趋向指数) ==========
def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """
    计算平均趋向指数 ADX (Average Directional Index)
    用于判断趋势强度:
    - ADX < 20: 震荡市/无趋势
    - ADX 20-25: 趋势形成中
    - ADX 25-40: 明确趋势
    - ADX > 40: 强趋势
    """
    high, low, close = df['high'], df['low'], df['close']
    
    # 计算True Range
    tr1 = high - low
    tr2 = (high - close.shift()).abs()
    tr3 = (low - close.shift()).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    
    # 计算方向移动 +DM 和 -DM
    plus_dm = high.diff()
    minus_dm = -low.diff()
    
    # 只保留正值
    plus_dm = plus_dm.where(plus_dm > 0, 0)
    minus_dm = minus_dm.where(minus_dm > 0, 0)
    
    # 过滤:只有当+DM > -DM时才计入+DM,反之亦然
    plus_dm = plus_dm.where(plus_dm > minus_dm, 0)
    minus_dm = minus_dm.where(minus_dm > plus_dm, 0)
    
    # 计算ATR (添加小值避免除0)
    atr_val = tr.rolling(period).mean()
    atr_val = atr_val.replace(0, 1e-10)
    
    # 计算+DI和-DI
    plus_di = 100 * (plus_dm.rolling(period).mean() / atr_val)
    minus_di = 100 * (minus_dm.rolling(period).mean() / atr_val)
    
    # 计算DX (Directional Index) - 添加小值避免除0
    di_sum = plus_di + minus_di
    di_sum = di_sum.replace(0, 1e-10)
    dx = 100 * ((plus_di - minus_di).abs() / di_sum)
    dx = dx.fillna(0)
    
    # ADX是DX的移动平均
    adx_val = dx.rolling(period).mean()
    
    # 确保返回值有效 (填充NaN为0)
    adx_val = adx_val.fillna(0)
    
    return adx_val


# ========== 🆕 布林带宽度 ==========
def bollinger_bandwidth(df: pd.DataFrame, period: int = 20, std_dev: float = 2.0) -> pd.Series:
    """
    计算布林带宽度 (Bollinger Bandwidth)
    用于判断波动率状态:
    - 宽度收缩 (< 0.02): 盘整期,可能即将突破
    - 宽度正常 (0.02-0.05): 正常波动
    - 宽度扩张 (> 0.05): 高波动,趋势进行中
    
    返回: 归一化宽度 (上轨-下轨)/中轨
    """
    close = df['close']
    sma = close.rolling(period).mean()
    std = close.rolling(period).std()
    
    upper = sma + (std_dev * std)
    lower = sma - (std_dev * std)
    
    # 归一化宽度 = (上轨-下轨) / 中轨
    # 添加小值避免除0
    sma_safe = sma.replace(0, 1e-10)
    bandwidth = (upper - lower) / sma_safe
    
    # 填充NaN
    bandwidth = bandwidth.fillna(0.03)
    
    return bandwidth


# ========== 🆕 分形维数 FDI (Fractal Dimension Index) ==========
def fractal_dimension(df: pd.DataFrame, period: int = 30) -> float:
    """
    计算分形维数 FDI (Fractal Dimension Index)
    
    使用Higuchi方法计算价格曲线的"平滑度"
    
    解释:
    - FDI ≈ 1.0: 价格像直线移动（强趋势，确定性高）
    - FDI ≈ 1.5: 价格像布朗运动（随机震荡，无趋势）
    
    用途:
    - FDI < 1.3: 趋势清晰，可以跟随
    - FDI > 1.4: 充满噪音，不适合趋势策略
    
    Args:
        df: K线数据
        period: 计算周期
        
    Returns:
        分形维数 (1.0-1.5)
    """
    if len(df) < period:
        return 1.35  # 默认中性值
    
    try:
        prices = df['close'].tail(period).values
        n = len(prices)
        
        # Higuchi方法
        k_max = min(10, n // 4)
        lk = []
        
        for k in range(1, k_max + 1):
            lm_sum = 0
            for m in range(1, k + 1):
                # 构建子序列
                indices = np.arange(m - 1, n, k)
                if len(indices) < 2:
                    continue
                sub_series = prices[indices]
                
                # 计算路径长度
                length = np.sum(np.abs(np.diff(sub_series)))
                norm_factor = (n - 1) / (k * ((n - m) // k) * k)
                lm_sum += length * norm_factor
            
            if lm_sum > 0:
                lk.append(lm_sum / k)
        
        if len(lk) < 3:
            return 1.35
        
        # 对数回归求斜率
        x = np.log(np.arange(1, len(lk) + 1))
        y = np.log(np.array(lk) + 1e-10)
        
        slope, _ = np.polyfit(x, y, 1)
        
        # FDI = 2 - slope (理论上)
        fdi = 2 + slope  # slope通常为负
        
        # 限制在合理范围
        fdi = max(1.0, min(1.5, fdi))
        
        return round(float(fdi), 4)
    except:
        return 1.35


def fdi_analysis(df: pd.DataFrame, period: int = 30) -> Dict[str, Any]:
    """
    FDI综合分析
    
    Returns:
        {
            "fdi": 当前FDI值,
            "regime": "trending"/"noisy"/"neutral",
            "trend_quality": 趋势质量 0-100,
            "recommendation": 建议
        }
    """
    fdi = fractal_dimension(df, period)
    
    if fdi < 1.25:
        regime = "trending"
        trend_quality = min(100, (1.5 - fdi) * 200)
        recommendation = "趋势非常清晰，适合趋势跟踪"
    elif fdi < 1.35:
        regime = "neutral"
        trend_quality = 50
        recommendation = "趋势一般，需要其他指标确认"
    else:
        regime = "noisy"
        trend_quality = max(0, (1.5 - fdi) * 100)
        recommendation = "市场噪音大，不适合趋势策略"
    
    return {
        "fdi": fdi,
        "regime": regime,
        "trend_quality": round(trend_quality, 1),
        "recommendation": recommendation
    }


# ========== 🆕 OI/Volume Ratio (聪明钱指标) ==========
def oi_volume_ratio(oi_change: float, volume: float) -> float:
    """
    计算OI/Volume比率 - 判断趋势真假
    
    原理:
    - 成交量大可能是刷量
    - 但OI增加意味着有资金留宿，是真实押注
    
    解释:
    - ratio高 + 价格涨 = 增量资金推动（真趋势）
    - ratio低/负 + 价格涨 = 空头回补（假趋势）
    
    Args:
        oi_change: OI变化量
        volume: 成交量
        
    Returns:
        OI/Volume比率
    """
    if volume <= 0:
        return 0
    
    return round(oi_change / volume, 6)


def smart_money_analysis(oi_change: float, volume: float, price_change_pct: float) -> Dict[str, Any]:
    """
    聪明钱分析 - 基于OI/Volume判断趋势真假
    
    Returns:
        {
            "oi_vol_ratio": 比率,
            "is_real_trend": 是否真趋势,
            "money_flow": "smart_buy"/"smart_sell"/"retail"/"mixed",
            "confidence": 置信度 0-100,
            "recommendation": 建议
        }
    """
    ratio = oi_volume_ratio(oi_change, volume)
    
    # 判断资金流向
    is_real_trend = False
    money_flow = "mixed"
    confidence = 50
    
    if price_change_pct > 0.5:  # 价格上涨
        if ratio > 0.01:  # OI增加
            is_real_trend = True
            money_flow = "smart_buy"
            confidence = min(100, 50 + ratio * 2000)
            recommendation = "✅ 真趋势：增量资金推动上涨"
        elif ratio < -0.005:  # OI减少
            is_real_trend = False
            money_flow = "retail"
            confidence = max(0, 50 - abs(ratio) * 2000)
            recommendation = "⚠️ 假趋势：空头回补，非增量资金"
        else:
            recommendation = "观察中：资金流向不明确"
    
    elif price_change_pct < -0.5:  # 价格下跌
        if ratio > 0.01:  # OI增加
            is_real_trend = True
            money_flow = "smart_sell"
            confidence = min(100, 50 + ratio * 2000)
            recommendation = "✅ 真下跌：增量资金做空"
        elif ratio < -0.005:  # OI减少
            is_real_trend = False
            money_flow = "retail"
            confidence = max(0, 50 - abs(ratio) * 2000)
            recommendation = "⚠️ 假下跌：多头平仓，可能反弹"
        else:
            recommendation = "观察中：资金流向不明确"
    else:
        recommendation = "价格波动小，无明确信号"
    
    return {
        "oi_vol_ratio": ratio,
        "is_real_trend": is_real_trend,
        "money_flow": money_flow,
        "confidence": round(confidence, 1),
        "recommendation": recommendation
    }


# ========== 🆕 分形维数 FDI (Fractal Dimension Index) ==========
def fractal_dimension(df: pd.DataFrame, period: int = 30) -> float:
    """
    计算分形维数 FDI (Fractal Dimension Index)
    
    使用Higuchi方法计算价格曲线的"混乱度"
    
    解释:
    - FDI接近1.0: 价格运动像直线（强趋势，确定性高）
    - FDI接近1.5: 价格运动像布朗运动（随机震荡，无趋势）
    
    用途:
    - FDI < 1.3: 趋势明确，可跟随
    - FDI > 1.4: 震荡剧烈，趋势不可靠
    - FDI 1.3-1.4: 趋势形成中
    
    Args:
        df: K线数据
        period: 计算周期
        
    Returns:
        FDI值 (1.0-1.5)
    """
    if len(df) < period:
        return 1.25  # 默认中性
    
    try:
        prices = df['close'].tail(period).values
        n = len(prices)
        
        # Higuchi方法计算
        k_max = min(10, n // 4)
        L = []
        
        for k in range(1, k_max + 1):
            Lk = []
            for m in range(1, k + 1):
                # 构建子序列
                indices = np.arange(m - 1, n, k)
                if len(indices) < 2:
                    continue
                sub_prices = prices[indices]
                
                # 计算长度
                length = np.sum(np.abs(np.diff(sub_prices))) * (n - 1) / (k * len(indices))
                if length > 0:
                    Lk.append(length)
            
            if Lk:
                L.append((k, np.mean(Lk)))
        
        if len(L) < 3:
            return 1.25
        
        # 对数回归求斜率（斜率就是分形维数）
        log_k = np.log([x[0] for x in L])
        log_L = np.log([x[1] for x in L])
        
        slope, _ = np.polyfit(log_k, log_L, 1)
        fdi = -slope  # 取负值
        
        # 限制在合理范围
        fdi = max(1.0, min(1.5, fdi))
        
        return round(float(fdi), 4)
    except:
        return 1.25


def fdi_analysis(df: pd.DataFrame, period: int = 30) -> Dict[str, Any]:
    """
    分形维数综合分析
    
    Returns:
        {
            "fdi": 当前FDI值,
            "trend_quality": "strong"/"weak"/"noise",
            "quality_score": 质量评分 0-100,
            "recommendation": 建议
        }
    """
    fdi = fractal_dimension(df, period)
    
    if fdi < 1.25:
        trend_quality = "strong"
        quality_score = min(100, (1.5 - fdi) * 200)
        recommendation = "趋势纯净，可跟随"
    elif fdi < 1.35:
        trend_quality = "moderate"
        quality_score = 50 + (1.35 - fdi) * 100
        recommendation = "趋势一般，谨慎跟随"
    elif fdi < 1.45:
        trend_quality = "weak"
        quality_score = max(0, (1.45 - fdi) * 200)
        recommendation = "趋势较弱，不建议跟随"
    else:
        trend_quality = "noise"
        quality_score = 0
        recommendation = "纯噪音，避免交易"
    
    return {
        "fdi": fdi,
        "trend_quality": trend_quality,
        "quality_score": round(quality_score, 1),
        "recommendation": recommendation
    }


# ========== 🆕 OI/Volume Ratio (聪明钱指标) ==========
def oi_volume_ratio(oi_change: float, volume: float) -> float:
    """
    计算OI/Volume比率 - 判断趋势真假
    
    原理:
    - 成交量大可能是刷量
    - OI增加意味着有资金留宿（真实押注）
    
    解释:
    - 价格涨 + ratio高 = 增量资金推动 ✅ 真趋势
    - 价格涨 + ratio低/负 = 空头回补 ⚠️ 假趋势
    - 价格跌 + ratio高 = 增量做空 ✅ 真趋势
    - 价格跌 + ratio低/负 = 多头平仓 ⚠️ 假趋势
    
    Args:
        oi_change: OI变化量
        volume: 成交量
        
    Returns:
        比率值
    """
    if volume <= 0:
        return 0
    return oi_change / volume


def smart_money_analysis(price_change: float, oi_change: float, volume: float) -> Dict[str, Any]:
    """
    聪明钱分析 - 判断趋势是否由真实资金推动
    
    Returns:
        {
            "oi_vol_ratio": OI/Volume比率,
            "is_smart_money": 是否聪明钱推动,
            "trend_type": "accumulation"/"distribution"/"short_squeeze"/"long_liquidation",
            "quality_score": 质量评分 0-100,
            "recommendation": 建议
        }
    """
    ratio = oi_volume_ratio(oi_change, volume)
    
    # 判断趋势类型
    if price_change > 0:  # 价格上涨
        if oi_change > 0 and ratio > 0.3:
            trend_type = "accumulation"  # 吸筹
            is_smart_money = True
            quality_score = min(100, 50 + ratio * 100)
            recommendation = "增量资金推动，真趋势"
        elif oi_change < 0:
            trend_type = "short_squeeze"  # 空头回补
            is_smart_money = False
            quality_score = max(0, 50 - abs(ratio) * 50)
            recommendation = "空头回补，趋势不可持续"
        else:
            trend_type = "neutral"
            is_smart_money = False
            quality_score = 50
            recommendation = "资金中性"
    else:  # 价格下跌
        if oi_change > 0 and ratio > 0.3:
            trend_type = "distribution"  # 出货
            is_smart_money = True
            quality_score = min(100, 50 + ratio * 100)
            recommendation = "增量做空，真趋势"
        elif oi_change < 0:
            trend_type = "long_liquidation"  # 多头平仓
            is_smart_money = False
            quality_score = max(0, 50 - abs(ratio) * 50)
            recommendation = "多头平仓，趋势不可持续"
        else:
            trend_type = "neutral"
            is_smart_money = False
            quality_score = 50
            recommendation = "资金中性"
    
    return {
        "oi_vol_ratio": round(ratio, 4),
        "is_smart_money": is_smart_money,
        "trend_type": trend_type,
        "quality_score": round(quality_score, 1),
        "recommendation": recommendation
    }


# ========== 🆕 Funding Rate Z-Score ==========
_FUNDING_HISTORY: Dict[str, List[float]] = {}

def funding_zscore(symbol: str, current_rate: float, history_days: int = 30) -> Dict[str, Any]:
    """
    计算Funding Rate的Z-Score
    
    原理:
    - 简单看费率正负没用，要看相对偏差
    - Z-Score > 2: 极度拥挤，反向信号价值高
    - Z-Score < -2: 极度恐慌，反向信号价值高
    
    Args:
        symbol: 交易对
        current_rate: 当前费率
        history_days: 历史天数（用于估算）
        
    Returns:
        {
            "zscore": Z-Score值,
            "percentile": 百分位,
            "crowding": "extreme_long"/"extreme_short"/"moderate"/"neutral",
            "reversal_value": 反转信号价值 0-100,
            "recommendation": 建议
        }
    """
    global _FUNDING_HISTORY
    
    # 更新历史
    if symbol not in _FUNDING_HISTORY:
        _FUNDING_HISTORY[symbol] = []
    
    _FUNDING_HISTORY[symbol].append(current_rate)
    
    # 只保留最近N个数据点（假设每8小时一个费率，30天约90个）
    max_points = history_days * 3
    if len(_FUNDING_HISTORY[symbol]) > max_points:
        _FUNDING_HISTORY[symbol] = _FUNDING_HISTORY[symbol][-max_points:]
    
    history = _FUNDING_HISTORY[symbol]
    
    # 需要足够的历史数据
    if len(history) < 10:
        return {
            "zscore": 0,
            "percentile": 50,
            "crowding": "neutral",
            "reversal_value": 50,
            "recommendation": "历史数据不足"
        }
    
    # 计算Z-Score
    mean_rate = np.mean(history)
    std_rate = np.std(history)
    
    if std_rate < 1e-10:
        zscore = 0
    else:
        zscore = (current_rate - mean_rate) / std_rate
    
    # 计算百分位
    percentile = (np.sum(np.array(history) < current_rate) / len(history)) * 100
    
    # 判断拥挤程度
    if zscore > 2.5:
        crowding = "extreme_long"
        reversal_value = min(100, 50 + zscore * 15)
        recommendation = "极度多头拥挤，做空信号价值极高"
    elif zscore > 1.5:
        crowding = "long_crowded"
        reversal_value = min(100, 50 + zscore * 10)
        recommendation = "多头拥挤，做空信号价值高"
    elif zscore < -2.5:
        crowding = "extreme_short"
        reversal_value = min(100, 50 + abs(zscore) * 15)
        recommendation = "极度空头拥挤，做多信号价值极高"
    elif zscore < -1.5:
        crowding = "short_crowded"
        reversal_value = min(100, 50 + abs(zscore) * 10)
        recommendation = "空头拥挤，做多信号价值高"
    else:
        crowding = "neutral"
        reversal_value = 50
        recommendation = "费率中性"
    
    return {
        "zscore": round(zscore, 2),
        "percentile": round(percentile, 1),
        "crowding": crowding,
        "reversal_value": round(reversal_value, 1),
        "recommendation": recommendation
    }


# ========== 🆕 反转信号综合质量评估 ==========
def reversal_quality_score(df: pd.DataFrame, side: str, 
                           funding_rate: float = 0, 
                           symbol: str = "UNKNOWN") -> Dict[str, Any]:
    """
    反转信号综合质量评估 - 整合CVD背离 + Funding Z-Score
    
    这是用于claude_reviewer的核心函数
    
    Args:
        df: K线数据
        side: 交易方向 "long"/"short"
        funding_rate: 当前费率
        symbol: 交易对
        
    Returns:
        {
            "cvd_analysis": CVD分析结果,
            "funding_analysis": Funding分析结果,
            "overall_score": 综合评分 0-100,
            "is_quality_reversal": 是否优质反转信号,
            "risk_level": "low"/"medium"/"high",
            "recommendation": 交易建议
        }
    """
    # CVD分析
    cvd_result = cvd_divergence(df, lookback=20)
    
    # Funding分析
    funding_result = funding_zscore(symbol, funding_rate)
    
    # CVD评分 (权重50%)
    cvd_score = cvd_result["signal_quality"]
    
    # 检查CVD是否支持反转方向
    if side == "long" and cvd_result["divergence"] == "bullish":
        cvd_score += 20  # 看涨背离支持做多
    elif side == "short" and cvd_result["divergence"] == "bearish":
        cvd_score += 20  # 看跌背离支持做空
    elif cvd_result["is_fake_breakout"]:
        cvd_score -= 20  # 假突破扣分
    
    cvd_score = max(0, min(100, cvd_score))
    
    # Funding评分 (权重50%)
    funding_score = funding_result["reversal_value"]
    
    # 检查Funding是否支持反转方向
    if side == "long" and funding_result["crowding"] in ["extreme_short", "short_crowded"]:
        funding_score += 20  # 空头拥挤支持做多
    elif side == "short" and funding_result["crowding"] in ["extreme_long", "long_crowded"]:
        funding_score += 20  # 多头拥挤支持做空
    
    funding_score = max(0, min(100, funding_score))
    
    # 综合评分
    overall_score = cvd_score * 0.5 + funding_score * 0.5
    
    # 判断是否优质反转
    is_quality_reversal = overall_score >= 65
    
    # 风险等级
    if overall_score >= 75:
        risk_level = "low"
    elif overall_score >= 55:
        risk_level = "medium"
    else:
        risk_level = "high"
    
    # 综合建议
    if is_quality_reversal:
        if cvd_result["divergence"] != "none" and funding_result["crowding"] != "neutral":
            recommendation = f"✅ 优质反转: CVD{cvd_result['divergence']}背离 + {funding_result['crowding']}"
        elif cvd_result["divergence"] != "none":
            recommendation = f"✅ CVD{cvd_result['divergence']}背离确认反转"
        else:
            recommendation = f"✅ 费率支持反转"
    else:
        weak_points = []
        if cvd_score < 50:
            weak_points.append("CVD不支持")
        if funding_score < 50:
            weak_points.append("费率不支持")
        recommendation = f"⚠️ 反转信号弱: {', '.join(weak_points)}"
    
    return {
        "cvd_analysis": cvd_result,
        "funding_analysis": funding_result,
        "cvd_score": round(cvd_score, 1),
        "funding_score": round(funding_score, 1),
        "overall_score": round(overall_score, 1),
        "is_quality_reversal": is_quality_reversal,
        "risk_level": risk_level,
        "recommendation": recommendation
    }


# ========== 🆕 趋势预判综合质量评估 ==========
def trend_quality_score(df: pd.DataFrame, side: str,
                        oi_change: float = 0,
                        volume: float = 1) -> Dict[str, Any]:
    """
    趋势预判综合质量评估 - 整合FDI + OI/Vol + ER + Hurst
    
    这是用于trend_anticipation的核心函数
    
    Args:
        df: K线数据
        side: 交易方向
        oi_change: OI变化量
        volume: 成交量
        
    Returns:
        {
            "fdi_analysis": FDI分析结果,
            "smart_money_analysis": 聪明钱分析结果,
            "er_analysis": 效率比分析结果,
            "hurst_analysis": Hurst分析结果,
            "overall_score": 综合评分 0-100,
            "is_quality_trend": 是否优质趋势信号,
            "risk_level": "low"/"medium"/"high",
            "recommendation": 交易建议
        }
    """
    # 计算价格变化
    if len(df) >= 20:
        price_change = (df['close'].iloc[-1] - df['close'].iloc[-20]) / df['close'].iloc[-20]
    else:
        price_change = 0
    
    # 各项分析
    fdi_result = fdi_analysis(df)
    smart_money_result = smart_money_analysis(price_change, oi_change, volume)
    er_result = efficiency_ratio_trend(df)
    hurst_result = hurst_analysis(df)
    
    # 评分权重:
    # FDI 30% (趋势纯度最重要)
    # 聪明钱 25% (资金真假)
    # ER 25% (趋势效率)
    # Hurst 20% (持续性)
    
    fdi_score = fdi_result["quality_score"]
    sm_score = smart_money_result["quality_score"]
    er_score = er_result["trend_quality"]
    hurst_score = hurst_result["persistence"]
    
    overall_score = fdi_score * 0.30 + sm_score * 0.25 + er_score * 0.25 + hurst_score * 0.20
    
    # 判断是否优质趋势
    is_quality_trend = overall_score >= 60 and fdi_result["fdi"] < 1.4
    
    # 风险等级
    if overall_score >= 70 and fdi_result["fdi"] < 1.3:
        risk_level = "low"
    elif overall_score >= 50:
        risk_level = "medium"
    else:
        risk_level = "high"
    
    # 综合建议
    if is_quality_trend:
        if smart_money_result["is_smart_money"]:
            recommendation = f"✅ 优质趋势: FDI{fdi_result['fdi']:.2f} + 聪明钱{smart_money_result['trend_type']}"
        else:
            recommendation = f"✅ 趋势良好: FDI{fdi_result['fdi']:.2f}"
    else:
        weak_points = []
        if fdi_result["fdi"] >= 1.4:
            weak_points.append(f"FDI高({fdi_result['fdi']:.2f})")
        if not smart_money_result["is_smart_money"]:
            weak_points.append("非聪明钱")
        if er_score < 50:
            weak_points.append("ER低")
        recommendation = f"⚠️ 趋势质量差: {', '.join(weak_points)}"
    
    return {
        "fdi_analysis": fdi_result,
        "smart_money_analysis": smart_money_result,
        "er_analysis": er_result,
        "hurst_analysis": hurst_result,
        "overall_score": round(overall_score, 1),
        "is_quality_trend": is_quality_trend,
        "risk_level": risk_level,
        "recommendation": recommendation
    }


# ========== 资金费率缓存 ==========
_FUNDING_CACHE: Dict[str, Dict[str, Any]] = {}
_FUNDING_HISTORY: Dict[str, List[Tuple[float, float]]] = {}  # 🆕 历史记录用于Z-Score

def funding_score(symbol: str, cfg: Dict[str, Any]) -> float:
    fcfg = cfg.get("funding") or {}
    if not fcfg.get("enabled", False): return 0.5
    now = time.time()
    cached = _FUNDING_CACHE.get(symbol)
    if cached and (now - cached["ts"] < 300): return cached["score"]
    
    try:
        import ccxt
        ex = getattr(ccxt, cfg.get("exchange", {}).get("name", "binance"))()
        contract_symbol = (fcfg.get("symbol_map") or {}).get(symbol, f"{symbol}:USDT")
        
        funding_rate = ex.fetch_funding_rate(contract_symbol).get("fundingRate")
        if funding_rate is None: return 0.5

        clip_val = float(fcfg.get("clip", 0.01))
        funding_rate = np.clip(funding_rate, -clip_val, clip_val)
        score = float(np.clip(0.5 + 0.5 * np.tanh(-funding_rate * 200), 0.0, 1.0))
        
        # 🆕 记录历史用于Z-Score
        if symbol not in _FUNDING_HISTORY:
            _FUNDING_HISTORY[symbol] = []
        _FUNDING_HISTORY[symbol].append((now, funding_rate))
        # 保留30天数据（每8小时一次，约90条）
        _FUNDING_HISTORY[symbol] = [(t, r) for t, r in _FUNDING_HISTORY[symbol] if now - t < 30 * 86400][-100:]
        
        _FUNDING_CACHE[symbol] = {"ts": now, "score": score, "rate": funding_rate}
        return score
    except Exception as e:
        # print(f"[FUNDING_ERR] {symbol}: {e}")
        return 0.5


# ========== 🆕 Funding Rate Z-Score ==========
def funding_zscore(symbol: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """
    计算资金费率的Z-Score - 识别极端拥挤
    
    原理:
    - 简单的费率正负没用，要看相对偏差
    - Z-Score > 2: 极度拥挤，反向信号价值高
    - Z-Score < -2: 极度恐慌，反向信号价值高
    
    Returns:
        {
            "current_rate": 当前费率,
            "zscore": Z-Score值,
            "is_extreme": 是否极端,
            "crowd_direction": "long"/"short"/"neutral",
            "contrarian_value": 反向交易价值 0-100,
            "recommendation": 建议
        }
    """
    result = {
        "current_rate": 0,
        "zscore": 0,
        "is_extreme": False,
        "crowd_direction": "neutral",
        "contrarian_value": 50,
        "recommendation": "数据不足"
    }
    
    # 先调用funding_score确保有最新数据
    funding_score(symbol, cfg)
    
    cached = _FUNDING_CACHE.get(symbol)
    history = _FUNDING_HISTORY.get(symbol, [])
    
    if not cached or len(history) < 10:
        return result
    
    current_rate = cached.get("rate", 0)
    result["current_rate"] = current_rate
    
    # 计算历史均值和标准差
    rates = [r for _, r in history]
    mean_rate = np.mean(rates)
    std_rate = np.std(rates)
    
    if std_rate < 1e-10:
        return result
    
    # 计算Z-Score
    zscore = (current_rate - mean_rate) / std_rate
    result["zscore"] = round(zscore, 2)
    
    # 判断极端程度
    if zscore > 2:
        result["is_extreme"] = True
        result["crowd_direction"] = "long"
        result["contrarian_value"] = min(100, 50 + zscore * 15)
        result["recommendation"] = f"🔥 极度正费率(Z={zscore:.1f})，全网做多拥挤，做空反转价值高"
    elif zscore < -2:
        result["is_extreme"] = True
        result["crowd_direction"] = "short"
        result["contrarian_value"] = min(100, 50 + abs(zscore) * 15)
        result["recommendation"] = f"🔥 极度负费率(Z={zscore:.1f})，全网做空拥挤，做多反转价值高"
    elif zscore > 1:
        result["crowd_direction"] = "long"
        result["contrarian_value"] = 50 + zscore * 10
        result["recommendation"] = f"费率偏高(Z={zscore:.1f})，多头略拥挤"
    elif zscore < -1:
        result["crowd_direction"] = "short"
        result["contrarian_value"] = 50 + abs(zscore) * 10
        result["recommendation"] = f"费率偏低(Z={zscore:.1f})，空头略拥挤"
    else:
        result["recommendation"] = "费率正常，无极端拥挤"
    
    return result


# ========== OI(未平仓量)缓存 ==========
_OI_HISTORY: Dict[str, list] = {}

def oi_trend_score(symbol: str, cfg: Dict[str, Any]) -> float:
    oicfg = cfg.get("oi") or {}
    if not oicfg.get("enabled", False): return 0.5
    try:
        import ccxt
        ex = getattr(ccxt, cfg.get("exchange", {}).get("name", "binance"))()
        contract_symbol = (oicfg.get("symbol_map") or {}).get(symbol, symbol.replace("/",""))
        
        oi_value = float(ex.fapiPublicGetOpenInterest({"symbol": contract_symbol}).get("openInterest", 0))
        if oi_value == 0: return 0.5
        
        history = _OI_HISTORY.setdefault(symbol, [])
        history.append((time.time(), oi_value))
        
        lookback_n = int(oicfg.get("lookback_n", 24))
        history = history[-lookback_n:]
        _OI_HISTORY[symbol] = history
        
        if len(history) < 3: return 0.5
        
        timestamps = np.array([x[0] for x in history])
        oi_array = np.array([x[1] for x in history])
        slope, _ = np.polyfit(timestamps - timestamps.min(), oi_array, 1)
        
        score = float(np.clip(0.5 + 0.5 * np.tanh(slope / max(1, np.mean(oi_array)) * 50), 0.0, 1.0))
        return score
    except Exception as e:
        # print(f"[OI_ERR] {symbol}: {e}")
        return 0.5


# ========== 定时清理缓存 ==========
def cleanup_funding_oi_cache():
    global _FUNDING_CACHE, _OI_HISTORY
    now = time.time()
    for k in [k for k, v in _FUNDING_CACHE.items() if now - v["ts"] > 3600]: del _FUNDING_CACHE[k]
    for k in list(_OI_HISTORY.keys()):
        _OI_HISTORY[k] = [item for item in _OI_HISTORY[k] if now - item[0] < 86400]
        if not _OI_HISTORY[k]: del _OI_HISTORY[k]


# ========== 宏观、盘口 ==========
def macro_score(cfg: Dict[str,Any]) -> float:
    return 0.5

def orderbook_strength_fetch(ex, symbol: str, limit: int = 50) -> float:
    try:
        ob = ex.fetch_order_book(symbol, limit=limit)
        bid_vol = sum([b[1] for b in ob.get("bids", [])])
        ask_vol = sum([a[1] for a in ob.get("asks", [])])
        if bid_vol + ask_vol <= 0: return 0.5
        return float(0.5 + 0.5*np.tanh(np.log(bid_vol / max(1e-9, ask_vol) + 1e-9)))
    except Exception:
        return 0.5