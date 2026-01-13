# core/signal_classifier.py - 信号类型分类器
# -*- coding: utf-8 -*-
"""
信号类型分类器 v1.0

功能: 根据技术指标和BTC相关性，将信号分为三类
- independent: 独立行情（最优先，不受BTC影响）
- trend: 趋势跟进（顺势而为）
- reversal: 反转信号（逆势操作，风险高）

分类逻辑:
优先级1: 独立行情（is_independent=True）
优先级2: 趋势跟进（在趋势中，无交叉）
优先级3: 反转信号（金叉/死叉，超买超卖）
"""

from typing import Dict, Any, Optional


def classify_signal_type(
    corr_analysis: Dict[str, Any],
    ema_cross: str,
    rsi: float,
    macd_hist: Optional[float] = None,
    config: Optional[Dict] = None
) -> str:
    """
    根据相关性分析和技术指标判断信号类型
    
    Args:
        corr_analysis: BTC相关性分析结果（从altcoin_correlation获得）
            必需字段: is_highly_independent, is_independent, is_stronger, is_resilient
        ema_cross: EMA交叉状态 ("golden"/"death"/"none")
        rsi: RSI指标值 (0-100)
        macd_hist: MACD柱状图值（可选）
        config: 配置字典（可选）
    
    Returns:
        信号类型: "independent" / "trend" / "reversal"
    """
    
    # 默认配置
    default_config = {
        'rsi_neutral_min': 40,
        'rsi_neutral_max': 70,
        'rsi_overbought': 70,
        'rsi_oversold': 30
    }
    cfg = config or default_config
    
    # 提取相关性分析字段
    is_highly_independent = corr_analysis.get("is_highly_independent", False)
    is_independent = corr_analysis.get("is_independent", False)
    is_stronger = corr_analysis.get("is_stronger", False)
    is_resilient = corr_analysis.get("is_resilient", False)
    correlation = corr_analysis.get("correlation", 0.0)
    
    # 获取成交量倍数（如果有）
    vol_spike_ratio = corr_analysis.get("vol_spike_ratio", 1.0)
    
    # ========================================
    # 优先级1: 独立行情（最优）
    # ========================================
    
    # 1.1 极度独立 (相关性<0.2)
    if is_highly_independent:
        return "independent"
    
    # 1.2 中度独立 + 强放量 (相关性0.2-0.3 + 成交量>2.5倍)
    if is_independent and vol_spike_ratio > 2.5:
        return "independent"
    
    # 1.3 低相关 + 极强放量 (相关性0.3-0.4 + 成交量>4倍)
    if abs(correlation) < 0.4 and vol_spike_ratio > 4.0:
        return "independent"
    
    # ========================================
    # 优先级2: 趋势跟进
    # ========================================
    
    # 2.1 相对BTC表现更强/更抗跌
    if is_stronger or is_resilient:
        return "trend"
    
    # 2.2 无EMA交叉 + RSI中性区域 = 趋势中段
    if ema_cross == "none":
        rsi_min = cfg.get('rsi_neutral_min', 40)
        rsi_max = cfg.get('rsi_neutral_max', 70)
        
        if rsi_min < rsi < rsi_max:
            # 在趋势中段，RSI既不超买也不超卖
            return "trend"
    
    # 2.3 刚金叉/死叉但RSI中性 = 趋势初期（也算趋势）
    if ema_cross in ["golden", "death"]:
        if 40 < rsi < 70:
            return "trend"
    
    # 2.4 MACD持续扩大（如果提供了MACD）
    if macd_hist is not None and ema_cross == "none":
        if abs(macd_hist) > 0:  # MACD有动能
            return "trend"
    
    # ========================================
    # 优先级3: 反转信号（默认归类）
    # ========================================
    
    # 3.1 明确的金叉/死叉
    if ema_cross in ["golden", "death"]:
        return "reversal"
    
    # 3.2 超买超卖区域
    if rsi > cfg.get('rsi_overbought', 70) or rsi < cfg.get('rsi_oversold', 30):
        return "reversal"
    
    # 3.3 默认（无法明确分类时，保守归为反转）
    return "reversal"


def get_signal_confidence(
    signal_type: str,
    corr_analysis: Dict[str, Any],
    rsi: float,
    vol_spike_ratio: float
) -> float:
    """
    计算信号的置信度 (0-1)
    
    Args:
        signal_type: 信号类型
        corr_analysis: 相关性分析
        rsi: RSI值
        vol_spike_ratio: 成交量倍数
    
    Returns:
        置信度 (0-1)
    """
    
    base_confidence = 0.5
    
    # === 独立行情: 置信度最高 ===
    if signal_type == "independent":
        base_confidence = 0.75
        
        # 极度独立 + 强放量 = 极高置信度
        if corr_analysis.get("is_highly_independent") and vol_spike_ratio > 3.0:
            base_confidence = 0.90
        
        # 只是低相关，置信度一般
        elif not corr_analysis.get("is_independent"):
            base_confidence = 0.65
    
    # === 趋势跟进: 置信度中等偏高 ===
    elif signal_type == "trend":
        base_confidence = 0.70
        
        # 真强势/真抗跌 = 高置信度
        if corr_analysis.get("is_stronger") or corr_analysis.get("is_resilient"):
            base_confidence = 0.80
        
        # RSI极端值降低置信度
        if rsi > 75 or rsi < 25:
            base_confidence *= 0.85
    
    # === 反转信号: 置信度中等偏低 ===
    elif signal_type == "reversal":
        base_confidence = 0.55
        
        # 极度超买超卖稍微提高置信度
        if rsi > 80 or rsi < 20:
            base_confidence = 0.60
        
        # 高度跟随BTC降低置信度
        if abs(corr_analysis.get("correlation", 0)) > 0.7:
            base_confidence *= 0.90
    
    # 限制在合理范围
    return max(0.3, min(base_confidence, 0.95))


def format_signal_classification(
    signal_type: str,
    confidence: float,
    corr_analysis: Dict[str, Any]
) -> str:
    """
    格式化信号分类信息
    
    Args:
        signal_type: 信号类型
        confidence: 置信度
        corr_analysis: 相关性分析
    
    Returns:
        格式化的文本
    """
    
    # 信号类型emoji
    type_emoji = {
        "independent": "⭐",
        "trend": "📈",
        "reversal": "🔄"
    }
    
    # 信号类型中文
    type_name = {
        "independent": "独立行情",
        "trend": "趋势跟进",
        "reversal": "反转信号"
    }
    
    # 置信度星级
    stars = "★" * int(confidence * 5)
    
    lines = [
        f"{type_emoji.get(signal_type, '❓')} 信号类型: {type_name.get(signal_type, '未知')}",
        f"   置信度: {stars} ({confidence:.2f})"
    ]
    
    # 添加相关性信息
    correlation = corr_analysis.get("correlation", 0)
    if corr_analysis.get("is_highly_independent"):
        lines.append(f"   🟢 极度独立 (corr={correlation:+.2f})")
    elif corr_analysis.get("is_independent"):
        lines.append(f"   🟡 中度独立 (corr={correlation:+.2f})")
    else:
        lines.append(f"   🔴 跟随BTC (corr={correlation:+.2f})")
    
    # 添加特殊标签
    if corr_analysis.get("is_stronger"):
        lines.append(f"   💪 相对BTC强势")
    if corr_analysis.get("is_resilient"):
        lines.append(f"   🛡️ 相对BTC抗跌")
    
    return "\n".join(lines)


# 批量分类（用于回测）
def classify_signals_batch(
    signals: list,
    corr_analyses: list,
    config: Optional[Dict] = None
) -> list:
    """
    批量分类多个信号
    
    Args:
        signals: 信号列表，每个信号包含 ema_cross, rsi
        corr_analyses: 对应的相关性分析列表
        config: 配置字典
    
    Returns:
        分类结果列表
    """
    results = []
    
    for signal, corr in zip(signals, corr_analyses):
        signal_type = classify_signal_type(
            corr_analysis=corr,
            ema_cross=signal.get("ema_cross", "none"),
            rsi=signal.get("rsi", 50),
            macd_hist=signal.get("macd_hist"),
            config=config
        )
        
        confidence = get_signal_confidence(
            signal_type=signal_type,
            corr_analysis=corr,
            rsi=signal.get("rsi", 50),
            vol_spike_ratio=signal.get("vol_spike_ratio", 1.0)
        )
        
        results.append({
            'signal_type': signal_type,
            'confidence': confidence,
            'symbol': signal.get('symbol', 'UNKNOWN')
        })
    
    return results


# 测试用例
if __name__ == "__main__":
    print("=" * 70)
    print("信号分类器测试")
    print("=" * 70)
    
    # 测试案例1: 独立行情
    print("\n[案例1] ETH 独立上涨")
    corr1 = {
        "correlation": 0.18,
        "is_highly_independent": True,
        "is_independent": True,
        "is_stronger": False,
        "is_resilient": False,
        "vol_spike_ratio": 3.5
    }
    type1 = classify_signal_type(corr1, "none", 58)
    conf1 = get_signal_confidence(type1, corr1, 58, 3.5)
    print(format_signal_classification(type1, conf1, corr1))
    
    # 测试案例2: 趋势跟进
    print("\n" + "=" * 70)
    print("[案例2] BTC 趋势中段")
    corr2 = {
        "correlation": 0.55,
        "is_highly_independent": False,
        "is_independent": False,
        "is_stronger": False,
        "is_resilient": False,
        "vol_spike_ratio": 1.2
    }
    type2 = classify_signal_type(corr2, "none", 62)
    conf2 = get_signal_confidence(type2, corr2, 62, 1.2)
    print(format_signal_classification(type2, conf2, corr2))
    
    # 测试案例3: 反转信号
    print("\n" + "=" * 70)
    print("[案例3] SOL 金叉+超买")
    corr3 = {
        "correlation": 0.75,
        "is_highly_independent": False,
        "is_independent": False,
        "is_stronger": False,
        "is_resilient": False,
        "vol_spike_ratio": 1.8
    }
    type3 = classify_signal_type(corr3, "golden", 76)
    conf3 = get_signal_confidence(type3, corr3, 76, 1.8)
    print(format_signal_classification(type3, conf3, corr3))
