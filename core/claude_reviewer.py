# core/claude_reviewer.py - [v10.0 CVD+Funding版] Claude + DeepSeek 并行审核
# -*- coding: utf-8 -*-
"""
Claude审核器 - CVD背离+Funding Z-Score版 v10.0

🔥🔥🔥 v10.0 重大更新 (反转质量识别版):
1. 新增CVD背离检测 - 识别真假反转，避免接飞刀
2. 新增Funding Z-Score - 识别拥挤交易，提高反转信号价值
3. 硬规则新增CVD快速过滤
4. AI审核新增反转质量指标
5. 趋势预判新增FDI检测

🔥 v9.3 更新 (配合v7.9.3):
1. RSI阈值大幅收紧: 做多≤15 / 做空≥85
2. BTC方向检查更严格: 1h跌>1%不做多 / 1h涨>1%不做空
3. 成交量要求提高: 2.0x
4. 趋势预判评分: 0.85
5. ADX阈值: 28
"""

import anthropic
import requests
import json
import math
import numpy as np
from typing import Dict, Optional, Tuple, List, Any
from datetime import datetime, timezone

# 🔥 v10.0: 导入新指标函数
try:
    from .utils import (
        cvd_divergence, 
        funding_zscore,
        reversal_quality_score
    )
    HAS_REVERSAL_INDICATORS = True
except ImportError:
    HAS_REVERSAL_INDICATORS = False
    print("[CLAUDE_REVIEWER] ⚠️ 新指标函数未找到，使用内置版本")

# 🔥 v10.0: Funding历史缓存
_FUNDING_HISTORY: Dict[str, List[float]] = {}


# ==================== 🔥 统一配置读取（v7.9.1极端收紧）====================

def get_rsi_thresholds(cfg: Dict) -> Dict:
    """
    🔥 从统一配置读取RSI阈值 (Single Source of Truth)
    
    v7.9.3: 大幅收紧阈值
    """
    reversal = cfg.get("reversal_strategy", {})
    
    return {
        "long_max": reversal.get("rsi_long_max", 15),         # 🔥 v7.9.3: 20->15
        "short_min": reversal.get("rsi_short_min", 85),       # 🔥 v7.9.3: 80->85
        "extreme_long": reversal.get("rsi_extreme_long", 12), # 🔥 v7.9.3: 15->12
        "extreme_short": reversal.get("rsi_extreme_short", 88), # 🔥 v7.9.3: 85->88
    }


class ClaudeReviewer:
    """
    Claude审核器 - 双AI版
    
    功能:
    1. 硬规则预过滤（必须通过）
    2. Claude深度审核
    3. DeepSeek深度审核（可选）
    4. 返回整合结果
    """
    
    def __init__(self, config: Dict):
        """初始化审核器"""
        self.config = config
        
        # Claude配置
        self.claude_api_key = config.get("claude", {}).get("api_key")
        self.claude_model = config.get("claude", {}).get("model", "claude-sonnet-4-5-20250929")
        
        if not self.claude_api_key:
            raise ValueError("⛔ 缺少Claude API Key")
        
        # DeepSeek配置（可选）
        deepseek_cfg = config.get("deepseek", {})
        self.deepseek_enabled = deepseek_cfg.get("enabled", False)
        self.deepseek_api_key = deepseek_cfg.get("api_key")
        self.deepseek_model = deepseek_cfg.get("model", "deepseek-chat")
        self.deepseek_base_url = deepseek_cfg.get("base_url", "https://api.deepseek.com/v1")
        self.deepseek_timeout = deepseek_cfg.get("timeout", 60)
        
        # 🔥 预加载统一RSI阈值
        self.rsi_thresholds = get_rsi_thresholds(config)
        print(f"[CLAUDE_REVIEWER] RSI阈值: 做多≤{self.rsi_thresholds['long_max']} | 做空≥{self.rsi_thresholds['short_min']}")
        
        if self.deepseek_enabled:
            if not self.deepseek_api_key:
                print("[WARN] DeepSeek启用但缺少API Key，将仅使用Claude")
                self.deepseek_enabled = False
            else:
                print(f"[CLAUDE_REVIEWER] 双AI模式 | Claude + DeepSeek")
        else:
            print(f"[CLAUDE_REVIEWER] 单AI模式 | 仅Claude")
    
    def review_signal(self, payload: Dict) -> Dict:
        """
        全面审核交易信号
        
        流程:
        1. 硬规则预过滤
        2. Claude审核
        3. DeepSeek审核（如启用）
        4. 返回整合结果
        """
        symbol = payload.get("symbol", "UNKNOWN")
        
        # ========== 第一关：硬规则预过滤 ==========
        print(f"\n[REVIEW] 🔍 开始审核 {symbol}...")
        print(f"[REVIEW] 第一关：硬规则过滤")
        
        passed, reason = self._hard_rules_filter(payload)
        
        if not passed:
            print(f"[REVIEW] ⛔ 硬规则拒绝 | {reason}")
            return self._build_reject_result("hard_rules", reason, payload)
        
        print(f"[REVIEW] ✅ 硬规则通过")
        
        # ========== 第二关：DeepSeek初审（v8.0 更宽松）==========
        # 🔥 v8.0: 初审改用DeepSeek，更宽松，成本更低
        # 🔥 v9.0: DeepSeek失败时回退到Claude
        ai_result = None
        ai_name = "DEEPSEEK"
        
        if self.deepseek_enabled and self.deepseek_api_key:
            print(f"[REVIEW] 第二关：DeepSeek初审（更宽松）")
            ai_result = self._deepseek_review(payload)
            
            # 🔥🔥🔥 检查是否是API错误导致的拒绝
            if not ai_result.get("approved", False):
                reasoning = ai_result.get("reasoning", "")
                if "调用失败" in reasoning or "连接" in reasoning or "timeout" in reasoning.lower():
                    print(f"[REVIEW] ⚠️ DeepSeek连接失败，回退到Claude")
                    ai_result = self._claude_review(payload)
                    ai_name = "CLAUDE"
        else:
            print(f"[REVIEW] 第二关：Claude审核")
            ai_result = self._claude_review(payload)
            ai_name = "CLAUDE"
        
        ai_approved = ai_result.get("approved", False)
        ai_status = "✅通过" if ai_approved else "⛔拒绝"
        ai_reason = ai_result.get('reasoning', 'N/A')[:80]
        print(f"[{ai_name}] {ai_status} | {ai_reason}")
        
        # ========== 汇总决策 ==========
        if ai_approved:
            print(f"[REVIEW] ✅ 通过 → 进入观察期")
        else:
            print(f"[REVIEW] ⛔ 拒绝")
        
        # ========== 构建返回结果 ==========
        return self._build_unified_result(
            ai_result,
            None,
            f"{ai_name.lower()}_approved" if ai_approved else "rejected",
            ai_approved,
            payload
        )
    
    # ========== 🔥 硬规则过滤（使用统一配置）==========
    
    def _hard_rules_filter(self, payload: Dict) -> Tuple[bool, str]:
        """硬规则过滤 - 🔥使用统一RSI配置"""
        
        m = payload.get("metrics", {}) or {}
        subs = payload.get("subscores", {}) or {}
        stops = payload.get("calculated_stops", {}) or {}
        
        symbol = payload.get("symbol", "UNKNOWN")
        score = self._safe_float(payload.get("score"), 0.0)
        price = self._safe_float(payload.get("price"), 0.0)
        side = payload.get("bias", "long").lower()
        
        rsi = self._safe_float(m.get("rsi"), 50.0)
        adx = self._safe_float(m.get("adx"), 0.0)
        vol_spike = self._safe_float(m.get("vol_spike_ratio"), 1.0)
        bb_width = self._safe_float(m.get("bb_width"), 0.03)
        atr = self._safe_float(m.get("atr"), 0.0)
        atr_pct = (atr / price * 100) if price > 0 else 2.0
        
        sl_pct = self._safe_float(stops.get("sl_pct"), 3.0)
        orderbook = self._safe_float(subs.get("orderbook"), 0.5)
        
        funding = payload.get("funding", {})
        raw_funding_rate = self._safe_float(funding.get("rate"), 0.0)
        
        cfg = payload.get("cfg", {})
        
        # ========== 🔥 从统一配置读取RSI阈值 ==========
        # 优先使用实例变量（初始化时已加载），其次从payload的cfg读取
        rsi_cfg = get_rsi_thresholds(cfg) if cfg else self.rsi_thresholds
        reversal_long_max = rsi_cfg["long_max"]
        reversal_short_min = rsi_cfg["short_min"]
        extreme_rsi_long = rsi_cfg["extreme_long"]
        extreme_rsi_short = rsi_cfg["extreme_short"]
        
        print(f"[HARD_RULES] {symbol} | RSI:{rsi:.1f} 方向:{side} | 阈值:做多≤{reversal_long_max}/做空≥{reversal_short_min}")
        
        # 🔥🔥🔥 读取信号类型（可能在顶层或signal_info里）
        signal_type = payload.get("signal_type", "unknown")
        if signal_type == "unknown":
            signal_info = payload.get("signal_info", {})
            signal_type = signal_info.get("signal_type", "unknown")
        
        # 🔥🔥🔥 趋势预判信号的硬规则检查（v9.2大幅加强）
        if signal_type == "trend_anticipation":
            print(f"[TREND_ANTICIPATION] ✅ 趋势预判信号，使用专用硬规则")
            
            # 🔥🔥🔥 v9.2: 从配置读取加强后的阈值
            ta_cfg = cfg.get("trend_anticipation", {})
            hard_filter = ta_cfg.get("hard_filter", {})
            
            # 1. 评分检查（🔥 v7.9.3提高到0.85）
            ta_min_score = ta_cfg.get("scoring", {}).get("min_score_to_emit", 0.85)  # 🔥 v7.9.3: 0.80->0.85
            if score < ta_min_score:
                return False, f"❌ 趋势预判评分{score:.2f}<{ta_min_score:.2f}"
            print(f"[TREND_ANTICIPATION] ✅ 评分{score:.2f}≥{ta_min_score:.2f}")
            
            # 2. ADX趋势检查（🔥🔥 v7.9.3: 提高到28）
            min_adx = hard_filter.get("min_adx", 28)  # 🔥 v7.9.3: 25->28
            if adx < min_adx:
                return False, f"❌ ADX{adx:.1f}<{min_adx} 趋势不明确"
            print(f"[TREND_ANTICIPATION] ✅ ADX{adx:.1f}≥{min_adx}")
            
            # 3. RSI检查（🔥 v7.9.3大幅收窄：12-20/80-88）
            if side == "long":
                rsi_range = ta_cfg.get("long_conditions", {}).get("rsi_range", [12, 20])  # 🔥 v7.9.3: [18,28]->[12,20]
                if not (rsi_range[0] <= rsi <= rsi_range[1]):
                    return False, f"❌ 趋势预判做多RSI{rsi:.1f}不在{rsi_range[0]}-{rsi_range[1]}区间"
                print(f"[TREND_ANTICIPATION] ✅ RSI{rsi:.1f}在做多预判区间")
            else:
                rsi_range = ta_cfg.get("short_conditions", {}).get("rsi_range", [80, 88])  # 🔥 v7.9.3: [72,82]->[80,88]
                if not (rsi_range[0] <= rsi <= rsi_range[1]):
                    return False, f"❌ 趋势预判做空RSI{rsi:.1f}不在{rsi_range[0]}-{rsi_range[1]}区间"
                print(f"[TREND_ANTICIPATION] ✅ RSI{rsi:.1f}在做空预判区间")
            
            # 🔥🔥 4. 蓄势确认（布林带宽度收紧：🔥 v7.9.3: 2.5%->2.2%）
            max_bb_width = hard_filter.get("max_bb_width", 0.022)  # 🔥 v7.9.3: 0.025->0.022
            if bb_width > max_bb_width:
                return False, f"❌ 布林带{bb_width*100:.1f}%>{max_bb_width*100:.1f}% 未蓄势"
            print(f"[TREND_ANTICIPATION] ✅ 布林带{bb_width*100:.1f}%≤{max_bb_width*100:.1f}% 蓄势中")
            
            # 🔥🔥 5. 成交量检查（🔥 v7.9.3: 0.5x->1.0x）
            min_vol = hard_filter.get("min_volume_ratio", 1.0)  # 🔥 v7.9.3: 0.5->1.0
            if vol_spike < min_vol:
                return False, f"❌ 成交量{vol_spike:.1f}x<{min_vol:.1f}x 太低"
            print(f"[TREND_ANTICIPATION] ✅ 成交量{vol_spike:.1f}x≥{min_vol:.1f}x")
            
            # 6. 资金费率检查（🔥 v7.9.3收紧：0.15%->0.12%）
            if abs(raw_funding_rate) > 0.0012:  # 🔥 v7.9.3: 0.0015->0.0012
                if side == "long" and raw_funding_rate > 0.0012:
                    return False, f"❌ 做多但资金费率{raw_funding_rate:.4f}>0.12%"
                if side == "short" and raw_funding_rate < -0.0012:
                    return False, f"❌ 做空但资金费率{raw_funding_rate:.4f}<-0.12%"
            print(f"[TREND_ANTICIPATION] ✅ 资金费率{raw_funding_rate:.4f}正常")
            
            # 🔥🔥 7. 订单簿深度（🔥 v7.9.3: 0.40->0.45）
            if orderbook < 0.45:  # 🔥 v7.9.3: 0.40->0.45
                return False, f"❌ 订单簿{orderbook:.2f}<0.45 深度不足"
            print(f"[TREND_ANTICIPATION] ✅ 订单簿{orderbook:.2f}≥0.45")
            
            # 🔥🔥🔥 8. 新增：动能减弱确认
            momentum_weakening = m.get("momentum_weakening", False)
            if not momentum_weakening:
                # 如果没有动能减弱，需要更高的评分才能通过
                if score < 0.90:
                    return False, f"❌ 动能未减弱且评分{score:.2f}<0.90"
                print(f"[TREND_ANTICIPATION] ⚠️ 动能未减弱但评分{score:.2f}足够高")
            else:
                print(f"[TREND_ANTICIPATION] ✅ 动能减弱确认")
            
            # 🔥🔥🔥 9. v7.9.3加强：BTC方向一致性检查（更严格）
            btc_status = payload.get("btc_status", {})
            btc_trend = btc_status.get("trend", "unknown")
            btc_change_1h = self._safe_float(btc_status.get("price_change_1h"), 0.0)
            
            # 🔥🔥🔥 v7.9.3: btc_change_1h 已经是百分比形式
            # 做多时BTC不能下跌，做空时BTC不能上涨（阈值从2%收紧到1%）
            if side == "long":
                if btc_change_1h < -1.0:  # 🔥🔥 v7.9.3: -2.0 -> -1.0 (更严格)
                    return False, f"❌ 做多但BTC 1h跌{btc_change_1h:.1f}%，方向冲突"
                if btc_trend == "CRASH":
                    return False, f"❌ 做多但BTC暴跌中，方向冲突"
                if btc_trend == "DOWN" and btc_change_1h < -0.5:  # 🔥 v7.9.3新增
                    return False, f"❌ 做多但BTC下跌中({btc_change_1h:.1f}%)，方向冲突"
            else:  # short
                if btc_change_1h > 1.0:  # 🔥🔥 v7.9.3: 2.0 -> 1.0 (更严格)
                    return False, f"❌ 做空但BTC 1h涨{btc_change_1h:.1f}%，方向冲突"
                if btc_trend == "MOON":
                    # 🔥 BTC强势上涨时，做空需要更严格条件
                    if score < 0.93:  # 🔥 v7.9.3: 0.92 -> 0.93
                        return False, f"❌ BTC强势上涨，做空需评分≥0.93 | 当前:{score:.2f}"
                    print(f"[TREND_ANTICIPATION] ⚠️ BTC强势但做空评分{score:.2f}足够高")
                if btc_trend == "UP" and btc_change_1h > 0.5:  # 🔥 v7.9.3新增
                    if score < 0.90:
                        return False, f"❌ BTC上涨中({btc_change_1h:.1f}%)，做空需评分≥0.90 | 当前:{score:.2f}"
            print(f"[TREND_ANTICIPATION] ✅ BTC方向检查通过 | 趋势:{btc_trend} 1h:{btc_change_1h:+.1f}%")
            
            print(f"[TREND_ANTICIPATION] ✅ 硬规则通过 → 交给AI审核")
            return True, "趋势预判信号硬规则通过"
        
        # 🔥🔥🔥 新增：趋势延续信号的硬规则检查
        if signal_type == "trend_continuation":
            print(f"[TREND_CONT] ✅ 趋势延续信号，使用专用硬规则")
            
            tc_min_score = cfg.get("trend_continuation", {}).get("scoring", {}).get("base_score", 0.65)
            if score < tc_min_score:
                return False, f"❌ 趋势延续评分{score:.2f}<{tc_min_score:.2f}"
            
            tc_adx_min = cfg.get("trend_continuation", {}).get("signal", {}).get("adx_min", 20)
            if adx < tc_adx_min:
                return False, f"❌ ADX{adx:.1f}<{tc_adx_min} 趋势不明确"
            
            if abs(raw_funding_rate) > 0.0015:
                if side == "long" and raw_funding_rate > 0.0015:
                    return False, f"❌ 做多但资金费率{raw_funding_rate:.4f}>0.15%"
                if side == "short" and raw_funding_rate < -0.0015:
                    return False, f"❌ 做空但资金费率{raw_funding_rate:.4f}<-0.15%"
            
            if orderbook < 0.25:
                return False, f"❌ 订单簿{orderbook:.2f}<0.25"
            
            print(f"[TREND_CONT] ✅ 硬规则通过 → 交给AI审核")
            return True, "趋势延续信号硬规则通过"
        
        # ========== 以下是反转信号的完整硬规则检查 ==========
        
        # 🔥🔥🔥 v10.0新增: CVD背离检测
        cvd_result = None
        funding_result = None
        reversal_quality = None
        
        try:
            # 获取K线数据（如果有）
            klines = payload.get("klines")
            if klines is not None and len(klines) > 0:
                import pandas as pd
                if isinstance(klines, pd.DataFrame):
                    df = klines
                else:
                    df = pd.DataFrame(klines)
                
                # CVD背离检测
                cvd_result = self._quick_cvd_check(df)
                
                # 🔥 如果是明显的假突破，直接拒绝
                if cvd_result.get("is_fake_breakout", False) and cvd_result.get("divergence_strength", 0) > 70:
                    print(f"[REVERSAL] ⚠️ CVD检测到假突破 | 背离强度:{cvd_result['divergence_strength']:.0f}")
                    return False, f"❌ CVD检测到假突破(背离强度{cvd_result['divergence_strength']:.0f})"
                
                # Funding Z-Score检测
                funding_result = self._quick_funding_zscore(symbol, raw_funding_rate)
                
                print(f"[REVERSAL] 🔥v10.0: CVD背离={cvd_result.get('divergence', 'none')} | Funding Z={funding_result.get('zscore', 0):.1f}")
        except Exception as e:
            print(f"[REVERSAL] CVD/Funding检测异常: {e}")
        
        # 判断是否符合反转信号
        is_reversal_long = (side == "long" and rsi <= reversal_long_max)
        is_reversal_short = (side == "short" and rsi >= reversal_short_min)
        
        if is_reversal_long:
            print(f"[REVERSAL] ✅ 超卖做多 | RSI={rsi:.1f}≤{reversal_long_max}")
        elif is_reversal_short:
            print(f"[REVERSAL] ✅ 超买做空 | RSI={rsi:.1f}≥{reversal_short_min}")
        else:
            # 不是反转信号，直接拒绝
            if side == "long":
                return False, f"❌ 做多要求RSI≤{reversal_long_max}(超卖) | 当前:{rsi:.1f}"
            else:
                return False, f"❌ 做空要求RSI≥{reversal_short_min}(超买) | 当前:{rsi:.1f}"
        
        # 🔥🔥🔥 v7.9新增：检查趋势减弱确认
        momentum_weakening = m.get("momentum_weakening", False)
        still_trending = m.get("still_trending", False)
        bullish_div = m.get("bullish_divergence", False)
        bearish_div = m.get("bearish_divergence", False)
        
        # 如果还在创新高/新低且没有背离，需要更高评分
        if still_trending:
            if side == "long" and not bullish_div:
                if score < 0.80:
                    return False, f"❌ 还在创新低(趋势中)无背离，要求评分≥0.80 | 当前:{score:.2f}"
                print(f"[REVERSAL] ⚠️ 还在创新低但评分足够，允许通过")
            elif side == "short" and not bearish_div:
                if score < 0.80:
                    return False, f"❌ 还在创新高(趋势中)无背离，要求评分≥0.80 | 当前:{score:.2f}"
                print(f"[REVERSAL] ⚠️ 还在创新高但评分足够，允许通过")
        
        # 动能减弱是加分项，记录日志
        if momentum_weakening:
            print(f"[REVERSAL] ✅ 检测到动能减弱，信号质量+")
        
        # 评分阈值 - 🔥 使用yaml配置，优先级: review.hard_rules.min_score > push.thresholds.majors
        score_threshold = cfg.get("review", {}).get("hard_rules", {}).get("min_score",
                            cfg.get("push", {}).get("thresholds", {}).get("majors", 0.55))
        if score < score_threshold:
            return False, f"❌ 评分{score:.2f}<{score_threshold:.2f}"
        
        # 成交量要求（🔥🔥🔥 v7.9.3从统一配置读取，提高到2.0x）
        min_vol = cfg.get("reversal_strategy", {}).get("min_volume_ratio", 
                    cfg.get("review", {}).get("hard_rules", {}).get("min_volume_ratio", 2.0))  # 🔥 v7.9.3: 1.8->2.0
        if vol_spike < min_vol:
            return False, f"❌ 成交量{vol_spike:.2f}x<{min_vol:.1f}x"
        
        # 🔥 暴涨暴跌过滤
        price_change_24h = self._safe_float(m.get("price_change_24h_pct"), 0.0)
        price_change_pct = abs(price_change_24h * 100)
        
        max_extreme = cfg.get("review", {}).get("hard_rules", {}).get("max_price_change_extreme", 0.60)
        max_high = cfg.get("review", {}).get("hard_rules", {}).get("max_price_change_high", 0.40)
        high_min_score = cfg.get("review", {}).get("hard_rules", {}).get("price_change_high_min_score", 0.86)
        high_min_vol = cfg.get("review", {}).get("hard_rules", {}).get("price_change_high_min_vol", 1.0)
        
        if abs(price_change_24h) > max_extreme:
            return False, f"❌ 24h涨跌幅{price_change_pct:.1f}%>{max_extreme*100:.0f}%过于极端"
        
        if abs(price_change_24h) > max_high:
            print(f"[HARD_RULES] ⚠️ 暴涨暴跌 | 24h涨跌幅:{price_change_pct:.1f}% | 提高要求")
            if score < high_min_score:
                return False, f"❌ 暴涨暴跌({price_change_pct:.1f}%)要求评分>={high_min_score:.2f} | 当前:{score:.2f}"
            if vol_spike < high_min_vol:
                return False, f"❌ 暴涨暴跌({price_change_pct:.1f}%)要求成交量>={high_min_vol:.1f}x | 当前:{vol_spike:.2f}x"
        
        # ADX震荡检测
        if adx < 15 and vol_spike < 2.0:
            return False, f"ADX{adx:.1f}<15且Vol{vol_spike:.2f}x<2.0死寂震荡"
        
        # 布林带陷阱检测
        if bb_width < 0.01:
            if vol_spike >= 0.8:
                if bb_width < 0.005:
                    return False, f"布林带极度挤压{bb_width:.4f}即使Vol{vol_spike:.2f}x高仍不足"
            else:
                if vol_spike < 0.8:
                    return False, f"布林带极度挤压{bb_width:.4f}+Vol{vol_spike:.2f}x不足"
        
        if bb_width < 0.008 and 45 < rsi < 55:
            return False, f"布林带极挤压{bb_width:.4f}+RSI{rsi:.1f}中性,方向不明"
        
        if adx > 35 and vol_spike < 0.5:
            return False, f"ADX{adx:.1f}强趋势但Vol{vol_spike:.2f}x不支持"
        
        macd_cross = m.get("macd_cross", "none")
        bullish_div = m.get("bullish_divergence", False)
        bearish_div = m.get("bearish_divergence", False)

        # 🔥 反转确认检查（使用统一的极端RSI阈值）
        if side == "long":
            has_reversal_confirm = (
                macd_cross == "golden" or
                bullish_div == True or
                (rsi <= extreme_rsi_long and vol_spike >= 3.0)
            )
            if not has_reversal_confirm:
                return False, f"❌ 缺少反转确认 | RSI={rsi:.0f}超卖但MACD未金叉且无底背离"
        else:
            has_reversal_confirm = (
                macd_cross == "death" or
                bearish_div == True or
                (rsi >= extreme_rsi_short and vol_spike >= 3.0)
            )
            if not has_reversal_confirm:
                return False, f"❌ 缺少反转确认 | RSI={rsi:.0f}超买但MACD未死叉且无顶背离"

        print(f"[REVERSAL_CONFIRM] ✅ 反转确认 | MACD:{macd_cross} 背离:{'底' if bullish_div else '顶' if bearish_div else '无'}")

        if bb_width < 0.01 and macd_cross in ("golden", "death") and vol_spike < 1.0:
            return False, f"布林带挤压{bb_width:.4f}时MACD交叉为噪音"
        
        if adx > 40 and bb_width < 0.02 and vol_spike < 1.0:
            return False, f"ADX{adx:.1f}高+BB{bb_width:.4f}缩+Vol{vol_spike:.2f}x衰=趋势末端"
        
        min_sl_by_atr = atr_pct * 1.5
        if sl_pct < min_sl_by_atr:
            return False, f"止损{sl_pct:.2f}%过紧(<{min_sl_by_atr:.2f}%)"
        
        if bb_width < 0.015:
            min_sl_squeeze = max(atr_pct * 1.5, 1.0) if vol_spike >= 2.0 else max(atr_pct * 2.0, 1.5)
            if sl_pct < min_sl_squeeze:
                return False, f"布林带挤压期止损需≥{min_sl_squeeze:.2f}%"
        
        if vol_spike < 1.0:
            min_sl_low_vol = atr_pct * 2.5
            if sl_pct < min_sl_low_vol:
                return False, f"低流动性Vol{vol_spike:.2f}x时止损需≥{min_sl_low_vol:.2f}%"
        
        # 资金费率检查
        if abs(raw_funding_rate) > 0.001:
            if side == "long" and raw_funding_rate > 0.001:
                return False, f"做多资金费率{raw_funding_rate:.4f}>0.001过高"
            if side == "short" and raw_funding_rate < -0.001:
                return False, f"做空资金费率{raw_funding_rate:.4f}<-0.001过负"
        
        if orderbook < 0.30:
            return False, f"订单簿{orderbook:.2f}<0.30深度不足"
        
        estimated_slip = self._estimate_slippage(vol_spike, orderbook)
        if estimated_slip > sl_pct * 0.6:
            return False, f"预估滑点{estimated_slip:.2f}%>止损{sl_pct:.2f}%×0.6"
        
        return True, "硬规则全部通过"
    
    # ========== Claude审核 ==========
    
    def _claude_review(self, payload: Dict) -> Dict:
        """Claude深度审核 - 包含3档入场价"""
        try:
            client = anthropic.Anthropic(api_key=self.claude_api_key)
            prompt = self._build_review_prompt(payload, "Claude")
            
            message = client.messages.create(
                model=self.claude_model,
                max_tokens=2500,
                temperature=0.3,
                system="你是专业的加密货币交易审核专家。严格分析信号质量，给出明确决策。",
                messages=[{"role": "user", "content": prompt}]
            )
            
            content = message.content[0].text
            result = self._parse_json_response(content)
            
            if result:
                result["_source"] = "claude"
                return result
            else:
                return self._build_ai_error_result("Claude", "返回格式错误", payload)
                
        except Exception as e:
            print(f"[CLAUDE_ERR] {e}")
            return self._build_ai_error_result("Claude", str(e), payload)
    
    # ========== DeepSeek审核 ==========
    
    def _deepseek_review(self, payload: Dict) -> Optional[Dict]:
        """DeepSeek深度审核"""
        try:
            prompt = self._build_review_prompt(payload, "DeepSeek")
            
            headers = {
                "Authorization": f"Bearer {self.deepseek_api_key}",
                "Content-Type": "application/json"
            }
            
            data = {
                "model": self.deepseek_model,
                "messages": [
                    {"role": "system", "content": """你是加密货币交易分析专家，负责审核交易信号。

🎯 **审核原则**：
- 趋势预判信号特点是"提前布局"，RSI在15-30做多、70-85做空是合理区间
- 反转信号需要更极端的RSI（<15做多，>85做空）
- 重点关注：趋势方向、支撑阻力、BTC配合

⛔ **必须拒绝**：
1. BTC明显下跌（1h跌>1.0%）时做多
2. BTC明显上涨（1h涨>1.0%）时做空
3. 完全没有支撑/阻力确认

✅ **可以通过**：
1. 预判信号：RSI 15-30做多或70-85做空 + 布林带收窄
2. 反转信号：RSI <15做多或>85做空 + 成交量放大
3. BTC稳定（变化<0.5%）或方向配合

📊 审核通过率目标：40-50%"""},
                    {"role": "user", "content": prompt}
                ],
                "temperature": 0.3,  # 🔥 v8.2: 更灵活
                "max_tokens": 2500
            }
            
            response = requests.post(
                f"{self.deepseek_base_url}/chat/completions",
                headers=headers,
                json=data,
                timeout=self.deepseek_timeout
            )
            
            response.raise_for_status()
            result_data = response.json()
            content = result_data["choices"][0]["message"]["content"]
            
            result = self._parse_json_response(content)
            
            if result:
                result["_source"] = "deepseek"
                return result
            else:
                return self._build_ai_error_result("DeepSeek", "返回格式错误", payload)
                
        except Exception as e:
            print(f"[DEEPSEEK_ERR] {e}")
            return self._build_ai_error_result("DeepSeek", str(e), payload)
    
    # ========== 提示词构建 ==========
    
    def _build_review_prompt(self, payload: Dict, ai_name: str) -> str:
        """构建审核提示词 - 根据信号类型使用不同prompt"""
        
        m = payload.get("metrics", {}) or {}
        subs = payload.get("subscores", {}) or {}
        stops = payload.get("calculated_stops", {}) or {}
        
        symbol = payload.get("symbol", "UNKNOWN")
        side = payload.get("bias", "long")
        price = self._safe_float(payload.get("price"), 0.0)
        score = self._safe_float(payload.get("score"), 0.0)
        
        rsi = self._safe_float(m.get("rsi"), 50.0)
        adx = self._safe_float(m.get("adx"), 0.0)
        vol_ratio = self._safe_float(m.get("vol_spike_ratio"), 1.0)
        bb_width = self._safe_float(m.get("bb_width"), 0.03)
        
        price_change_24h = self._safe_float(m.get("price_change_24h_pct"), 0.0)
        
        sl_pct = self._safe_float(stops.get("sl_pct"), 3.0)
        tp_pct = self._safe_float(stops.get("tp_pct"), 6.0)
        
        sentiment = self._safe_float(m.get("sentiment"), 0.5)
        orderbook = self._safe_float(subs.get("orderbook"), 0.5)
        
        bb_upper = self._safe_float(m.get("bb_upper"), 0.0)
        bb_lower = self._safe_float(m.get("bb_lower"), 0.0)
        
        if bb_upper > 0 and bb_lower > 0 and price > 0:
            if price > bb_upper:
                bb_position_desc = "突破上轨(超买)"
            elif price < bb_lower:
                bb_position_desc = "跌破下轨(超卖)"
            else:
                bb_position_desc = "中轨附近"
        else:
            bb_position_desc = "数据不足"
        
        macd_cross = m.get("macd_cross", "none")
        if macd_cross == "golden":
            macd_status = "✅ 金叉(看涨)"
        elif macd_cross == "death":
            macd_status = "⚠️ 死叉(看跌)"
        else:
            macd_status = "震荡无明确信号"

        bullish_div = m.get("bullish_divergence", False)
        bearish_div = m.get("bearish_divergence", False)
        div_strength = self._safe_float(m.get("divergence_strength"), 0.0)

        if bullish_div:
            divergence_desc = f"✅ 底背离(看涨) 强度:{div_strength:.2f}"
        elif bearish_div:
            divergence_desc = f"⚠️ 顶背离(看跌) 强度:{div_strength:.2f}"
        else:
            divergence_desc = "无背离"
        
        btc_status = payload.get("btc_status", {})
        btc_trend = btc_status.get("trend", "unknown")
        btc_change_1h = self._safe_float(btc_status.get("price_change_1h"), 0.0)
        
        correlation = payload.get("correlation_analysis", {})
        if correlation:
            corr_level = correlation.get("correlation_level", "unknown")
            corr_value = self._safe_float(correlation.get("correlation_value"), 0.0)
            btc_corr_text = f"{corr_level} (系数:{corr_value:.2f})"
        else:
            btc_corr_text = "未知"
        
        # 🔥🔥🔥 根据信号类型选择不同的prompt
        signal_type = payload.get("signal_type", "unknown")
        if signal_type == "unknown":
            signal_info = payload.get("signal_info", {})
            signal_type = signal_info.get("signal_type", "unknown")
        
        # ========== 🔥🔥🔥 趋势预判信号的专用prompt ==========
        if signal_type == "trend_anticipation":
            # 获取趋势预判特有的信息
            support_analysis = payload.get("support_analysis", {})
            pattern_analysis = payload.get("pattern_analysis", {})
            volume_analysis = payload.get("volume_analysis", {})
            mtf_analysis = payload.get("mtf_analysis", {})
            
            nearest_support = support_analysis.get("nearest_level", 0)
            support_type = support_analysis.get("level_type", "unknown")
            support_distance = support_analysis.get("distance_pct", 0) * 100
            
            patterns = pattern_analysis.get("patterns", [])
            volume_structure = volume_analysis.get("structure", "unknown")
            mtf_confirm = mtf_analysis.get("confirm_count", 0)
            
            # 获取历史交易记录（用于AI学习）
            history_text = self._build_history_text(payload.get("cfg", {}))
            
            prompt = f"""
## 🔮 趋势预判信号审核 - 严格模式

🚨🚨🚨 **极其重要的风控铁律** 🚨🚨🚨
1. BTC下跌时（1h跌>0.5%）→ 必须拒绝做多
2. BTC上涨时（1h涨>0.5%）→ 必须拒绝做空  
3. 成交量<1.5x → 必须拒绝
4. 没有明显支撑/阻力确认 → 必须拒绝
5. 多时间框架确认<2个 → 必须拒绝
6. 任何疑虑 → 拒绝（宁可错过，不可做错）

### 基础信息
- 币种: {symbol}
- 方向: {side.upper()}
- 当前价: ${price:.6f}
- 综合评分: {score:.2f}

### 🔥 预判信号核心指标
- 最近支撑位: ${nearest_support:.6f} ({support_type})
- 距支撑位: {support_distance:.2f}%
- K线形态: {', '.join(patterns) if patterns else '无明显形态'}
- 成交量结构: {volume_structure}
- 多时间框架确认数: {mtf_confirm}个

### 技术指标
- RSI: {rsi:.1f} （预判区间，非极值）
- ADX: {adx:.1f} （趋势强度）
- 成交量: {vol_ratio:.2f}x均量
- MACD: {macd_status}

### BTC背景 ⚠️关键判断依据
- BTC趋势: {btc_trend}
- BTC 1h变化: {btc_change_1h:+.2f}%
- 相关性: {btc_corr_text}
{history_text}

### 🚨 必须检查的拒绝条件
1. ❓ BTC方向是否与信号方向冲突？（做多时BTC跌/做空时BTC涨）
2. ❓ 成交量是否足够？（至少1.5x）
3. ❓ 是否有有效支撑/阻力位确认？
4. ❓ 动能是否真的在减弱？

### 请返回JSON格式:
```json
{{
    "approved": true/false,
    "confidence": 0.0-1.0,
    "side": "long"/"short",
    "reasoning": "20字以内简短理由"
}}
```

⚠️ 记住：你的任务是保护资金安全！有任何疑虑就拒绝。只返回JSON。
"""
            return prompt
        
        # ========== 趋势延续信号的专用prompt ==========
        if signal_type == "trend_continuation":
            correlation = payload.get("correlation_analysis", {})
            corr_value = self._safe_float(correlation.get("correlation_value"), 0.0)
            pullback_pct = self._safe_float(payload.get("pullback_pct"), 0.0)
            
            prompt = f"""
## 📈 趋势延续信号审核

⚠️ **这是趋势延续信号，跟随BTC方向！**
- 不要求RSI极值
- 重点看：BTC方向 + 相关性 + 回调入场

### 基础信息
- 币种: {symbol}
- 方向: {side.upper()}
- 当前价: ${price:.6f}
- 综合评分: {score:.2f}

### 趋势延续核心指标
- BTC 1h变化: {btc_change_1h*100:+.2f}%
- 与BTC相关性: {corr_value:.2f}
- 回调幅度: {pullback_pct*100:+.2f}%

### 技术指标
- RSI: {rsi:.1f} | ADX: {adx:.1f}
- 成交量: {vol_ratio:.2f}x均量

### 请返回JSON格式:
```json
{{
    "approved": true/false,
    "confidence": 0.0-1.0,
    "side": "long"/"short",
    "reasoning": "20字以内简短理由"
}}
```

⚠️ 只判断信号质量！只返回JSON。
"""
            return prompt
        # 🔥 使用统一的RSI阈值
        rsi_thresholds = self.rsi_thresholds
        
        # 🔥🔥🔥 v7.9.3: 加入动能减弱信息
        momentum_weakening = m.get("momentum_weakening", False)
        momentum_weakening_count = m.get("momentum_weakening_count", 0)
        still_trending = m.get("still_trending", False)
        
        momentum_status = "✅ 确认减弱" if momentum_weakening else "⚠️ 未确认"
        if momentum_weakening:
            momentum_status += f" ({momentum_weakening_count}根K线)"
        trending_status = "⚠️ 还在创新高/低" if still_trending else "✅ 趋势放缓"
        
        # 🔥🔥🔥 v10.0: 获取CVD和Funding信息
        cvd_info = payload.get("cvd_analysis", {})
        funding_info = payload.get("funding_analysis", {})
        
        cvd_divergence = cvd_info.get("divergence", "none")
        cvd_strength = cvd_info.get("divergence_strength", 0)
        cvd_status = "无背离"
        if cvd_divergence == "bullish":
            cvd_status = f"🟢看涨背离(强度{cvd_strength:.0f})"
        elif cvd_divergence == "bearish":
            cvd_status = f"🔴看跌背离(强度{cvd_strength:.0f})"
        
        funding_zscore = funding_info.get("zscore", 0)
        funding_crowding = funding_info.get("crowding", "neutral")
        funding_status = "中性"
        if funding_crowding == "extreme_long":
            funding_status = f"🔴极度多头拥挤(Z={funding_zscore:.1f})"
        elif funding_crowding == "extreme_short":
            funding_status = f"🟢极度空头拥挤(Z={funding_zscore:.1f})"
        elif funding_crowding == "long_crowded":
            funding_status = f"🟡多头拥挤(Z={funding_zscore:.1f})"
        elif funding_crowding == "short_crowded":
            funding_status = f"🟡空头拥挤(Z={funding_zscore:.1f})"
        
        prompt = f"""
## 🔄 反转信号审核 - 🔥v10.0 CVD+Funding增强版

🚨🚨🚨 **核心风控铁律** 🚨🚨🚨
1. RSI没到极值（做多>15，做空<85）→ 必须拒绝
2. 价格还在创新高/新低（趋势进行中）→ 必须拒绝
3. 动能没有明显减弱 → 必须拒绝
4. 成交量<2x均量 → 必须拒绝
5. BTC方向与信号冲突 → 必须拒绝
6. 🆕 CVD背离不支持反转方向 → 谨慎
7. 任何疑虑 → 拒绝（宁可错过，不可做错）

### 🔥🔥🔥 v10.0新增：反转质量指标
- **CVD背离**: {cvd_status}
  - 做多时看涨背离(价格跌+CVD涨)= ✅支持
  - 做空时看跌背离(价格涨+CVD跌)= ✅支持
- **Funding拥挤**: {funding_status}
  - 做多时空头拥挤 = ✅做多价值高
  - 做空时多头拥挤 = ✅做空价值高

### 基础信息
- 币种: {symbol}
- 方向: {side.upper()}
- 当前价: ${price:.6f}
- 综合评分: {score:.2f}

### 技术指标
- RSI: {rsi:.1f} {'🔥极端超卖' if rsi <= 15 else '🔥超卖' if rsi <= 20 else '❄️极端超买' if rsi >= 85 else '❄️超买' if rsi >= 80 else '⚠️中性区'}
- ADX: {adx:.1f}
- 成交量: {vol_ratio:.2f}x均量 {'✅放量' if vol_ratio >= 2.0 else '⚠️量能不足'}
- MACD: {macd_status}
- 背离: {divergence_desc}

### 🚨 关键判断 - 动能状态
- 动能减弱: {momentum_status}
- 趋势状态: {trending_status}

### BTC背景 ⚠️关键判断依据
- BTC趋势: {btc_trend}
- BTC 1h变化: {btc_change_1h:+.2f}%
- 相关性: {btc_corr_text}

### 🚨 必须检查的拒绝条件
1. ❓ RSI是否真的到了极值区域？（做多≤15/做空≥85）
2. ❓ 价格是否还在创新高/新低？（还在趋势中=危险）
3. ❓ 动能是否真的在减弱？（至少4根K线确认）
4. ❓ BTC方向是否支持？（做多时BTC不能跌/做空时BTC不能涨）
5. ❓ 成交量是否足够？（至少2x）
6. 🆕 CVD是否支持反转？（做多要看涨背离/做空要看跌背离）

### 请返回JSON格式:
```json
{{
    "approved": true/false,
    "confidence": 0.0-1.0,
    "side": "long"/"short",
    "reasoning": "20字以内简短理由，需提及CVD/Funding"
}}
```

⚠️ 记住：反转交易是逆势交易，风险极高！有任何疑虑就拒绝。只返回JSON。
"""
        return prompt
    
    # ========== 结果整合 ==========
    
    def _build_unified_result(
        self,
        claude_result: Dict,
        deepseek_result: Optional[Dict],
        decision: str,
        approved: bool,
        payload: Dict
    ) -> Dict:
        """构建统一返回结果 - 🔥简化版：只返回审核结果"""
        
        primary = claude_result or {}
        
        result = {
            "approved": approved,
            "side": primary.get("side", payload.get("bias", "long")),
            "confidence": primary.get("confidence", 0.0),
            "reasoning": primary.get("reasoning", ""),
            "decision": decision,
            "stage": "ai_reviewed"
        }
        
        return result
    
    def _build_reject_result(self, stage: str, reason: str, payload: Dict) -> Dict:
        """构建拒绝结果 - 🔥简化版"""
        return {
            "approved": False,
            "stage": stage,
            "reasoning": reason,
            "confidence": 1.0,
            "side": payload.get("bias", "long"),
            "decision": "rejected"
        }
    
    def _build_ai_error_result(self, ai_name: str, error: str, payload: Dict) -> Dict:
        """构建AI错误结果 - 🔥简化版"""
        return {
            "approved": False,
            "reasoning": f"{ai_name}调用失败: {error[:50]}",
            "confidence": 0.0,
            "side": payload.get("bias", "long"),
            "stage": "ai_error"
        }
    
    # ========== 工具函数 ==========
    
    @staticmethod
    def _parse_json_response(content: str) -> Optional[Dict]:
        """从AI响应中提取JSON"""
        try:
            return json.loads(content)
        except:
            pass
        
        try:
            start = content.find('{')
            end = content.rfind('}')
            if start != -1 and end != -1:
                json_str = content[start:end+1]
                return json.loads(json_str)
        except:
            pass
        
        return None
    
    @staticmethod
    def _safe_float(x, default: float = 0.0) -> float:
        """安全转换为浮点数"""
        try:
            v = float(x)
            if math.isnan(v) or math.isinf(v):
                return default
            return v
        except Exception:
            return default
    
    # ========== 🔥v10.0新增: CVD和Funding检测 ==========
    
    def _quick_cvd_check(self, df, lookback: int = 20) -> Dict:
        """
        🔥 v10.0新增: 快速CVD检测
        
        检测价格与成交量的背离，识别假突破
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
            price_now = float(df['close'].iloc[-1])
            price_past = float(df['close'].iloc[-lookback])
            
            cvd_range = max(abs(cvd[-lookback:].max() - cvd[-lookback:].min()), 1)
            price_past_safe = max(price_past, 1e-10)
            
            cvd_delta = (cvd_now - cvd_past) / cvd_range * 100
            price_delta = (price_now - price_past) / price_past_safe * 100
            
            divergence = "none"
            divergence_strength = 0
            is_fake_breakout = False
            
            # 价格上涨但CVD下跌 = 看跌背离（假突破风险）
            if price_delta > 1 and cvd_delta < -5:
                divergence = "bearish"
                divergence_strength = min(100, abs(cvd_delta) * 2)
                if price_delta > 3 and cvd_delta < -10:
                    is_fake_breakout = True
            
            # 价格下跌但CVD上涨 = 看涨背离（假跌风险，做多机会）
            elif price_delta < -1 and cvd_delta > 5:
                divergence = "bullish"
                divergence_strength = min(100, abs(cvd_delta) * 2)
                if price_delta < -3 and cvd_delta > 10:
                    is_fake_breakout = True
            
            # 计算信号质量
            if price_delta * cvd_delta > 0:  # 同向
                signal_quality = min(100, 50 + abs(cvd_delta) * 2)
            else:  # 背离
                signal_quality = max(0, 50 - divergence_strength * 0.3)
            
            return {
                "divergence": divergence,
                "divergence_strength": round(divergence_strength, 1),
                "is_fake_breakout": is_fake_breakout,
                "signal_quality": round(signal_quality, 1),
                "cvd_delta": round(cvd_delta, 2),
                "price_delta": round(price_delta, 2)
            }
        except Exception as e:
            return {"divergence": "none", "divergence_strength": 0, 
                    "is_fake_breakout": False, "signal_quality": 50}
    
    def _quick_funding_zscore(self, symbol: str, current_rate: float) -> Dict:
        """
        🔥 v10.0新增: 快速Funding Z-Score计算
        
        识别拥挤交易，提高反转信号价值
        """
        global _FUNDING_HISTORY
        
        try:
            # 更新历史
            if symbol not in _FUNDING_HISTORY:
                _FUNDING_HISTORY[symbol] = []
            
            _FUNDING_HISTORY[symbol].append(current_rate)
            
            # 只保留最近90个数据点
            if len(_FUNDING_HISTORY[symbol]) > 90:
                _FUNDING_HISTORY[symbol] = _FUNDING_HISTORY[symbol][-90:]
            
            history = _FUNDING_HISTORY[symbol]
            
            if len(history) < 10:
                return {"zscore": 0, "crowding": "neutral", "reversal_value": 50}
            
            mean_rate = np.mean(history)
            std_rate = np.std(history)
            
            if std_rate < 1e-10:
                zscore = 0
            else:
                zscore = (current_rate - mean_rate) / std_rate
            
            # 判断拥挤程度
            if zscore > 2.5:
                crowding = "extreme_long"
                reversal_value = min(100, 50 + zscore * 15)
            elif zscore > 1.5:
                crowding = "long_crowded"
                reversal_value = min(100, 50 + zscore * 10)
            elif zscore < -2.5:
                crowding = "extreme_short"
                reversal_value = min(100, 50 + abs(zscore) * 15)
            elif zscore < -1.5:
                crowding = "short_crowded"
                reversal_value = min(100, 50 + abs(zscore) * 10)
            else:
                crowding = "neutral"
                reversal_value = 50
            
            return {
                "zscore": round(zscore, 2),
                "crowding": crowding,
                "reversal_value": round(reversal_value, 1)
            }
        except:
            return {"zscore": 0, "crowding": "neutral", "reversal_value": 50}
    
    @staticmethod
    def _estimate_slippage(vol_spike: float, obk_score: float) -> float:
        """估算预期滑点"""
        if vol_spike < 0.5:
            base_slip = 0.8
        elif vol_spike < 1.0:
            base_slip = 0.5
        elif vol_spike < 2.0:
            base_slip = 0.3
        elif vol_spike < 5.0:
            base_slip = 0.15
        else:
            base_slip = 0.08
        
        if obk_score < 0.3:
            multiplier = 2.0
        elif obk_score < 0.5:
            multiplier = 1.5
        elif obk_score < 0.7:
            multiplier = 1.2
        else:
            multiplier = 1.0
        
        return base_slip * multiplier
    
    def _build_history_text(self, cfg: Dict) -> str:
        """
        🔥 构建历史交易记录文本（用于AI学习）
        """
        ai_learning = cfg.get("ai_learning", {})
        if not ai_learning.get("enabled", False):
            return ""
        
        inject_cfg = ai_learning.get("inject_history", {})
        if not inject_cfg.get("enabled", False):
            return ""
        
        # 尝试获取历史记录
        try:
            from core.trend_anticipation import get_recent_trades, get_trade_statistics
            
            recent_trades = get_recent_trades(inject_cfg.get("recent_trades_count", 10))
            stats = get_trade_statistics()
            
            if not recent_trades and stats.get("total", 0) == 0:
                return ""
            
            lines = ["\n### 📊 历史交易参考"]
            
            # 统计信息
            if stats.get("total", 0) > 0:
                win_rate = stats.get("win_rate", 0) * 100
                lines.append(f"- 近期胜率: {win_rate:.0f}% ({stats['wins']}胜/{stats['losses']}负)")
                
                # 按信号类型统计
                by_type = stats.get("by_signal_type", {})
                if by_type:
                    best_type = max(by_type.items(), key=lambda x: x[1].get("win_rate", 0), default=(None, {}))
                    if best_type[0]:
                        lines.append(f"- 最佳信号类型: {best_type[0]} ({best_type[1].get('win_rate', 0)*100:.0f}%)")
            
            # 最近几笔交易
            if recent_trades and inject_cfg.get("include_win_loss", True):
                lines.append("- 最近交易:")
                for trade in recent_trades[-3:]:
                    result = "✅胜" if trade.get("result") == "win" else "❌败"
                    symbol = trade.get("symbol", "?")
                    reason = trade.get("reason", "")[:20] if inject_cfg.get("include_reason", True) else ""
                    lines.append(f"  {result} {symbol} {reason}")
            
            return "\n".join(lines)
            
        except Exception as e:
            print(f"[AI_LEARNING] 获取历史记录失败: {e}")
            return ""
    
    def get_stats(self) -> Dict:
        """获取审核统计"""
        return {
            "claude_model": self.claude_model,
            "deepseek_enabled": self.deepseek_enabled,
            "deepseek_model": self.deepseek_model if self.deepseek_enabled else None,
            "rsi_thresholds": self.rsi_thresholds,
            "status": "ready"
        }