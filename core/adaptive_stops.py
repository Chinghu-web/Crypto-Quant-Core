# -*- coding: utf-8 -*-
"""
adaptive_stops.py - 自适应止盈止损计算模型

基于ATR和市场环境的动态止盈止损系统
完全替代固定百分比方案，针对不同币种波动特征差异化处理
"""
import math
from typing import Dict, Any, Optional, Tuple


def _safe_float(x, default: float = 0.0) -> float:
    """安全浮点数转换"""
    try:
        v = float(x)
        if math.isnan(v) or math.isinf(v):
            return default
        return v
    except Exception:
        return default


def calculate_adaptive_stops(
    symbol: str,
    price: float,
    atr: float,
    side: str,
    btc_status: Dict[str, Any],
    df: Optional[Any] = None
) -> Dict[str, Any]:
    """
    自适应止盈止损计算 - 核心算法
    
    Args:
        symbol: 交易对名称
        price: 当前价格
        atr: ATR值 (14周期)
        side: 方向 'long' 或 'short'
        btc_status: BTC市场状态 {'volatility_state': 'low/normal/high/extreme', 'trend': 'stable/crash/moon'}
        df: K线数据 (可选，用于支撑阻力校验)
    
    Returns:
        {
            'sl_pct': 止损百分比,
            'tp_pct': 止盈百分比,
            'sl_price': 止损价格,
            'tp_price': 止盈价格,
            'category': 币种分类,
            'atr_pct': ATR百分比,
            'max_leverage': 最大杠杆,
            'risk_reward_ratio': 盈亏比,
            'adjustments': 调整说明
        }
    """
    
    # ========== 第一步: 计算ATR百分比 ==========
    if price <= 0 or atr <= 0:
        # 异常情况返回保守默认值
        return _get_default_stops(side, price)
    
    atr_pct = (atr / price) * 100
    
    # ========== 第二步: 币种分类（基于ATR%） ==========
    if atr_pct < 1.5:
        # 超稳定币 (BTC, ETH在低波动期)
        category = "ultra_stable"
        base_sl_multiplier = 2.0
        base_tp_multiplier = 4.0
        max_leverage = 10
        
    elif atr_pct < 3.0:
        # 稳定币 (BTC, ETH正常, 主流L1)
        category = "stable"
        base_sl_multiplier = 2.5
        base_tp_multiplier = 5.0
        max_leverage = 8
        
    elif atr_pct < 5.0:
        # 正常币 (大多数altcoin)
        category = "normal"
        base_sl_multiplier = 3.0
        base_tp_multiplier = 6.0
        max_leverage = 5
        
    elif atr_pct < 8.0:
        # 波动币 (小市值币)
        category = "volatile"
        base_sl_multiplier = 3.5
        base_tp_multiplier = 7.0
        max_leverage = 3
        
    else:
        # 极端波动 (Meme币, 超小市值)
        category = "extreme"
        base_sl_multiplier = 4.0
        base_tp_multiplier = 8.0
        max_leverage = 2
    
    # ========== 第三步: 市场环境调整 ==========
    env_multiplier_sl = 1.0
    env_multiplier_tp = 1.0
    
    volatility_state = btc_status.get('volatility_state', 'normal')
    btc_trend = btc_status.get('trend', 'stable')
    
    # BTC波动率调整
    if volatility_state == 'extreme':
        # 极端波动: 止损放宽50%, 止盈收紧20%
        env_multiplier_sl = 1.5
        env_multiplier_tp = 0.8
        
    elif volatility_state == 'high':
        # 高波动: 止损放宽30%
        env_multiplier_sl = 1.3
        env_multiplier_tp = 0.9
        
    elif volatility_state == 'low':
        # 低波动: 可以用更紧的止损
        env_multiplier_sl = 0.8
        env_multiplier_tp = 1.0
    
    # BTC趋势调整
    if btc_trend in ['crash', 'moon']:
        # 单边市场: 进一步放宽止损，避免正常回调扫损
        env_multiplier_sl *= 1.2
    
    # ========== 第四步: 计算最终止盈止损百分比 ==========
    sl_pct = atr_pct * base_sl_multiplier * env_multiplier_sl
    tp_pct = atr_pct * base_tp_multiplier * env_multiplier_tp
    
    # ========== 第五步: 安全限制 ==========
    # 止损: 不能太小(容易扫)，也不能太大(亏太多)
    sl_pct = max(0.8, min(sl_pct, 20.0))
    
    # 止盈: 不能太小(不值得)，也不能太大(不现实)
    tp_pct = max(1.5, min(tp_pct, 50.0))
    
    # ========== 第六步: 确保盈亏比 ==========
    min_risk_reward = 1.8  # 最低盈亏比1.8:1
    if tp_pct < sl_pct * min_risk_reward:
        tp_pct = sl_pct * min_risk_reward
        # 重新检查上限
        if tp_pct > 50.0:
            tp_pct = 50.0
            sl_pct = tp_pct / min_risk_reward
    
    # ========== 第七步: 可选的支撑阻力校验 ==========
    support_adjusted = False
    if df is not None:
        try:
            import pandas as pd
            if len(df) > 100:
                lookback = min(100, len(df))
                support = float(df['low'].tail(lookback).quantile(0.2))
                resistance = float(df['high'].tail(lookback).quantile(0.8))
                
                if side.lower() == 'long':
                    # 做多：检查止损是否穿过支撑
                    sl_price_calculated = price * (1 - sl_pct/100)
                    if sl_price_calculated < support * 0.98:
                        # 止损穿过支撑，调整到支撑上方2%
                        new_sl_pct = ((price - support * 1.02) / price) * 100
                        if 0.8 <= new_sl_pct <= 20.0:  # 在合理范围内才调整
                            sl_pct = new_sl_pct
                            support_adjusted = True
                            # 重新调整止盈以保持盈亏比
                            tp_pct = max(tp_pct, sl_pct * min_risk_reward)
                else:
                    # 做空：检查止损是否穿过阻力
                    sl_price_calculated = price * (1 + sl_pct/100)
                    if sl_price_calculated > resistance * 1.02:
                        new_sl_pct = ((resistance * 0.98 - price) / price) * 100
                        if 0.8 <= new_sl_pct <= 20.0:
                            sl_pct = new_sl_pct
                            support_adjusted = True
                            tp_pct = max(tp_pct, sl_pct * min_risk_reward)
        except Exception as e:
            # 支撑阻力校验失败不影响主流程
            pass
    
    # ========== 第八步: 计算实际价格 ==========
    if side.lower() == 'long':
        sl_price = price * (1 - sl_pct/100)
        tp_price = price * (1 + tp_pct/100)
    else:
        sl_price = price * (1 + sl_pct/100)
        tp_price = price * (1 - tp_pct/100)
    
    # ========== 第九步: 返回完整结果 ==========
    return {
        'sl_pct': round(sl_pct, 2),
        'tp_pct': round(tp_pct, 2),
        'sl_price': round(sl_price, 8),
        'tp_price': round(tp_price, 8),
        'category': category,
        'atr_pct': round(atr_pct, 2),
        'max_leverage': max_leverage,
        'risk_reward_ratio': round(tp_pct / sl_pct, 2),
        'adjustments': {
            'base_multiplier': f"sl×{base_sl_multiplier} tp×{base_tp_multiplier}",
            'env_multiplier': f"sl×{env_multiplier_sl:.1f} tp×{env_multiplier_tp:.1f}",
            'volatility_state': volatility_state,
            'btc_trend': btc_trend,
            'support_adjusted': support_adjusted
        }
    }


def _get_default_stops(side: str, price: float) -> Dict[str, Any]:
    """异常情况的保守默认值"""
    if side.lower() == 'long':
        sl_pct, tp_pct = 3.0, 6.0
        sl_price = price * 0.97
        tp_price = price * 1.06
    else:
        sl_pct, tp_pct = 3.0, 6.0
        sl_price = price * 1.03
        tp_price = price * 0.94
    
    return {
        'sl_pct': sl_pct,
        'tp_pct': tp_pct,
        'sl_price': sl_price,
        'tp_price': tp_price,
        'category': 'unknown',
        'atr_pct': 0.0,
        'max_leverage': 5,
        'risk_reward_ratio': 2.0,
        'adjustments': {
            'base_multiplier': 'default',
            'env_multiplier': 'default',
            'volatility_state': 'unknown',
            'btc_trend': 'unknown',
            'support_adjusted': False
        }
    }


def calculate_safe_leverage(atr_pct: float, volatility_state: str, btc_trend: str) -> int:
    """
    根据波动率计算安全杠杆
    
    Args:
        atr_pct: ATR百分比
        volatility_state: 市场波动率状态
        btc_trend: BTC趋势状态
    
    Returns:
        推荐杠杆倍数 (1-20)
    """
    # 基础杠杆 (根据币种波动)
    if atr_pct < 1.5:
        base_leverage = 10
    elif atr_pct < 3.0:
        base_leverage = 8
    elif atr_pct < 5.0:
        base_leverage = 5
    elif atr_pct < 8.0:
        base_leverage = 3
    else:
        base_leverage = 2
    
    # 市场环境调整
    if volatility_state == 'extreme':
        base_leverage = min(base_leverage, 3)
    elif volatility_state == 'high':
        base_leverage = min(base_leverage, 5)
    
    # BTC趋势调整
    if btc_trend in ['crash', 'moon']:
        base_leverage = min(base_leverage, 3)
    
    return max(1, min(base_leverage, 20))


def format_stops_summary(stops: Dict[str, Any], symbol: str) -> str:
    """
    格式化止盈止损信息为可读文本
    
    Args:
        stops: calculate_adaptive_stops的返回值
        symbol: 交易对名称
    
    Returns:
        格式化的文本说明
    """
    lines = [
        f"📊 {symbol} 自适应止盈止损",
        f"",
        f"币种分类: {stops['category'].upper()} (ATR: {stops['atr_pct']:.2f}%)",
        f"",
        f"📈 止盈: {stops['tp_pct']:.2f}% → 价格 {stops['tp_price']:.6f}",
        f"📉 止损: {stops['sl_pct']:.2f}% → 价格 {stops['sl_price']:.6f}",
        f"",
        f"⚖️ 盈亏比: {stops['risk_reward_ratio']:.2f}:1",
        f"⚡ 建议杠杆: ≤{stops['max_leverage']}x",
        f"",
        f"🔧 调整说明:",
        f"  基础倍数: {stops['adjustments']['base_multiplier']}",
        f"  环境调整: {stops['adjustments']['env_multiplier']}",
        f"  市场状态: {stops['adjustments']['volatility_state']} / {stops['adjustments']['btc_trend']}"
    ]
    
    if stops['adjustments'].get('support_adjusted'):
        lines.append(f"  ✅ 已根据支撑/阻力位调整")
    
    return "\n".join(lines)


# 示例用法
if __name__ == "__main__":
    # 测试案例1: BTC正常市场
    print("="*60)
    print("测试1: BTC/USDT (正常市场)")
    print("="*60)
    stops = calculate_adaptive_stops(
        symbol="BTC/USDT",
        price=100000,
        atr=2000,
        side="long",
        btc_status={'volatility_state': 'normal', 'trend': 'stable'}
    )
    print(format_stops_summary(stops, "BTC/USDT"))
    
    # 测试案例2: PEPE极端波动
    print("\n" + "="*60)
    print("测试2: PEPE/USDT (Meme币)")
    print("="*60)
    stops = calculate_adaptive_stops(
        symbol="PEPE/USDT",
        price=0.00002,
        atr=0.000004,
        side="long",
        btc_status={'volatility_state': 'high', 'trend': 'stable'}
    )
    print(format_stops_summary(stops, "PEPE/USDT"))
    
    # 测试案例3: SOL BTC crash期
    print("\n" + "="*60)
    print("测试3: SOL/USDT (BTC crash)")
    print("="*60)
    stops = calculate_adaptive_stops(
        symbol="SOL/USDT",
        price=200,
        atr=8,
        side="long",
        btc_status={'volatility_state': 'extreme', 'trend': 'crash'}
    )
    print(format_stops_summary(stops, "SOL/USDT"))
