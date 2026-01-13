# core/hard_rules_engine.py - 硬规则引擎 v1.0
# 用途：将原来100+行的硬规则if嵌套重构为可维护的规则引擎

from typing import Dict, Any, Tuple, List, Callable, Optional
from dataclasses import dataclass
import math


@dataclass
class RuleResult:
    """规则检查结果"""
    passed: bool
    rule_name: str
    reason: str = ""
    details: Dict[str, Any] = None
    
    def __post_init__(self):
        if self.details is None:
            self.details = {}


class HardRule:
    """
    单条硬规则
    
    用法：
    ```python
    rule = HardRule(
        name="rsi_reversal",
        description="RSI反转条件检查",
        check_fn=lambda ctx: ctx['side'] == 'long' and ctx['rsi'] <= ctx['rsi_long_max'],
        reason_template="❌ RSI {rsi:.1f} 不符合做多条件(需≤{rsi_long_max})"
    )
    
    result = rule.check(context)
    if not result.passed:
        print(result.reason)
    ```
    """
    
    def __init__(
        self,
        name: str,
        check_fn: Callable[[Dict], bool],
        reason_template: str,
        description: str = "",
        category: str = "general",
        severity: str = "block",  # block=必须通过, warn=仅警告
    ):
        self.name = name
        self.check_fn = check_fn
        self.reason_template = reason_template
        self.description = description
        self.category = category
        self.severity = severity
    
    def check(self, ctx: Dict[str, Any]) -> RuleResult:
        """
        检查规则是否通过
        
        Args:
            ctx: 上下文字典，包含所有需要的数据
            
        Returns:
            RuleResult对象
        """
        try:
            passed = self.check_fn(ctx)
            
            if passed:
                return RuleResult(
                    passed=True,
                    rule_name=self.name,
                    reason="OK",
                    details={"category": self.category}
                )
            else:
                # 格式化拒绝原因
                try:
                    reason = self.reason_template.format(**ctx)
                except KeyError as e:
                    reason = f"{self.reason_template} (missing key: {e})"
                
                return RuleResult(
                    passed=False,
                    rule_name=self.name,
                    reason=reason,
                    details={"category": self.category, "severity": self.severity}
                )
                
        except Exception as e:
            return RuleResult(
                passed=False,
                rule_name=self.name,
                reason=f"❌ 规则检查异常: {str(e)[:100]}",
                details={"error": str(e)}
            )


class HardRulesEngine:
    """
    硬规则引擎
    
    设计理念：
    1. 每条规则独立、可测试
    2. 支持规则分类和优先级
    3. 支持规则的启用/禁用
    4. 提供详细的拒绝原因
    
    使用方式：
    ```python
    engine = HardRulesEngine(config)
    
    # 构建上下文
    ctx = engine.build_context(payload, metrics)
    
    # 检查所有规则
    passed, reason, details = engine.evaluate(ctx)
    
    if not passed:
        print(f"信号被拒绝: {reason}")
    ```
    """
    
    def __init__(self, config: Dict[str, Any]):
        """
        初始化规则引擎
        
        Args:
            config: 完整配置字典
        """
        self.config = config
        self.rules: List[HardRule] = []
        self.disabled_rules: set = set()
        
        # 加载配置
        self._load_config()
        
        # 构建规则
        self._build_rules()
        
        print(f"[HARD_RULES] 引擎初始化完成 | 规则数: {len(self.rules)}")
    
    def _load_config(self):
        """从配置加载参数"""
        # RSI阈值 - 从reversal_strategy读取
        reversal = self.config.get("reversal_strategy", {})
        self.rsi_long_max = reversal.get("rsi_long_max", 25)
        self.rsi_short_min = reversal.get("rsi_short_min", 75)
        self.rsi_extreme_long = reversal.get("rsi_extreme_long", 20)
        self.rsi_extreme_short = reversal.get("rsi_extreme_short", 80)
        self.min_volume_ratio = reversal.get("min_volume_ratio", 1.2)
        self.min_score = reversal.get("min_score", 0.55)
        
        # 硬规则配置
        hard = self.config.get("review", {}).get("hard_rules", {})
        self.max_price_change_extreme = hard.get("max_price_change_extreme", 0.80)
        self.max_price_change_high = hard.get("max_price_change_high", 0.50)
        self.price_change_high_min_score = hard.get("price_change_high_min_score", 0.86)
        self.bb_squeeze_threshold = hard.get("bb_squeeze_threshold", 0.01)
        self.bb_squeeze_vol_min = hard.get("bb_squeeze_vol_min", 1.5)
        self.min_adx_with_low_vol = hard.get("min_adx_with_low_vol", 18)
        self.adx_trend_end_threshold = hard.get("adx_trend_end_threshold", 40)
        self.max_funding_rate = hard.get("max_funding_rate", 0.0008)
        self.min_orderbook_score = hard.get("min_orderbook_score", 0.40)
    
    def _build_rules(self):
        """构建所有硬规则"""
        
        # ========== 1. RSI反转条件 ==========
        self.rules.append(HardRule(
            name="rsi_reversal_long",
            category="rsi",
            description="做多RSI必须处于超卖区域",
            check_fn=lambda ctx: (
                ctx['side'] != 'long' or 
                ctx['rsi'] <= ctx['rsi_long_max']
            ),
            reason_template="❌ RSI={rsi:.1f} > {rsi_long_max} | 做多需要超卖(RSI≤{rsi_long_max})"
        ))
        
        self.rules.append(HardRule(
            name="rsi_reversal_short",
            category="rsi",
            description="做空RSI必须处于超买区域",
            check_fn=lambda ctx: (
                ctx['side'] != 'short' or 
                ctx['rsi'] >= ctx['rsi_short_min']
            ),
            reason_template="❌ RSI={rsi:.1f} < {rsi_short_min} | 做空需要超买(RSI≥{rsi_short_min})"
        ))
        
        # ========== 2. 评分要求 ==========
        self.rules.append(HardRule(
            name="min_score",
            category="score",
            description="信号必须达到最低评分",
            check_fn=lambda ctx: ctx['score'] >= ctx['min_score'],
            reason_template="❌ 评分{score:.2f} < {min_score:.2f}"
        ))
        
        # ========== 3. 成交量要求 ==========
        self.rules.append(HardRule(
            name="min_volume",
            category="volume",
            description="成交量必须达到最低倍数",
            check_fn=lambda ctx: ctx['vol_spike'] >= ctx['min_vol'],
            reason_template="❌ 成交量{vol_spike:.1f}x < {min_vol:.1f}x"
        ))
        
        # ========== 4. 暴涨暴跌过滤 ==========
        self.rules.append(HardRule(
            name="extreme_price_change",
            category="price_change",
            description="过滤极端价格变动",
            check_fn=lambda ctx: abs(ctx['price_change_24h']) <= ctx['max_price_change_extreme'],
            reason_template="❌ 24h涨跌幅{price_change_24h:+.1%} 超过极端阈值({max_price_change_extreme:.0%})"
        ))
        
        self.rules.append(HardRule(
            name="high_price_change_score",
            category="price_change",
            description="高波动需要更高评分",
            check_fn=lambda ctx: (
                abs(ctx['price_change_24h']) <= ctx['max_price_change_high'] or
                ctx['score'] >= ctx['price_change_high_min_score']
            ),
            reason_template="❌ 24h涨跌幅{price_change_24h:+.1%}过高，需评分≥{price_change_high_min_score:.2f}(当前{score:.2f})"
        ))
        
        # ========== 5. 布林带挤压检测 ==========
        self.rules.append(HardRule(
            name="bb_squeeze",
            category="volatility",
            description="布林带挤压时需要更高成交量确认",
            check_fn=lambda ctx: (
                ctx['bb_width'] > ctx['bb_squeeze_threshold'] or
                ctx['vol_spike'] >= ctx['bb_squeeze_vol_min']
            ),
            reason_template="❌ 布林带挤压({bb_width:.3f}<{bb_squeeze_threshold}) + 成交量不足({vol_spike:.1f}x<{bb_squeeze_vol_min:.1f}x)"
        ))
        
        # ========== 6. ADX趋势检测 ==========
        self.rules.append(HardRule(
            name="adx_dead_zone",
            category="trend",
            description="ADX过低且成交量不足时拒绝",
            check_fn=lambda ctx: (
                ctx['adx'] >= ctx['min_adx_with_low_vol'] or
                ctx['vol_spike'] >= 1.5
            ),
            reason_template="❌ ADX死寂区({adx:.1f}<{min_adx_with_low_vol}) + 成交量不足"
        ))
        
        self.rules.append(HardRule(
            name="adx_trend_end",
            category="trend",
            description="ADX极高可能趋势末端",
            check_fn=lambda ctx: (
                ctx['adx'] < ctx['adx_trend_end_threshold'] or
                ctx['bb_width'] > 0.02 or
                ctx['vol_spike'] >= 1.0
            ),
            reason_template="❌ ADX极高({adx:.1f}≥{adx_trend_end_threshold})，可能趋势末端"
        ))
        
        # ========== 7. 资金费率检测 ==========
        self.rules.append(HardRule(
            name="funding_rate",
            category="funding",
            description="资金费率异常高",
            check_fn=lambda ctx: abs(ctx['funding_rate']) <= ctx['max_funding_rate'],
            reason_template="❌ 资金费率{funding_rate:.4f}过高(>{max_funding_rate:.4f})"
        ))
        
        # 做多时负资金费率警告（但不阻止）
        self.rules.append(HardRule(
            name="funding_direction_long",
            category="funding",
            description="做多方向资金费率不利",
            severity="warn",
            check_fn=lambda ctx: (
                ctx['side'] != 'long' or 
                ctx['funding_rate'] <= 0.0003
            ),
            reason_template="⚠️ 做多但资金费率为正({funding_rate:.4f})，需承担费用"
        ))
        
        # 做空时正资金费率警告（但不阻止）
        self.rules.append(HardRule(
            name="funding_direction_short",
            category="funding",
            description="做空方向资金费率不利",
            severity="warn",
            check_fn=lambda ctx: (
                ctx['side'] != 'short' or 
                ctx['funding_rate'] >= -0.0003
            ),
            reason_template="⚠️ 做空但资金费率为负({funding_rate:.4f})，需承担费用"
        ))
        
        # ========== 8. 订单簿深度 ==========
        self.rules.append(HardRule(
            name="orderbook_depth",
            category="liquidity",
            description="订单簿深度不足",
            check_fn=lambda ctx: ctx['orderbook_score'] >= ctx['min_orderbook_score'],
            reason_template="❌ 订单簿深度{orderbook_score:.2f} < {min_orderbook_score:.2f}"
        ))
        
        # ========== 9. MACD确认（反转信号） ==========
        self.rules.append(HardRule(
            name="macd_confirm_long",
            category="macd",
            description="做多需要MACD确认",
            check_fn=lambda ctx: (
                not ctx.get('require_macd_confirm', False) or
                ctx['side'] != 'long' or
                ctx['macd_cross'] in ['golden', 'bullish_divergence'] or
                (ctx['rsi'] <= ctx.get('rsi_extreme_long', 20) and ctx['vol_spike'] >= 2.0)
            ),
            reason_template="❌ 做多缺少MACD确认(需金叉/背离/极端RSI+巨量)"
        ))
        
        self.rules.append(HardRule(
            name="macd_confirm_short",
            category="macd",
            description="做空需要MACD确认",
            check_fn=lambda ctx: (
                not ctx.get('require_macd_confirm', False) or
                ctx['side'] != 'short' or
                ctx['macd_cross'] in ['death', 'bearish_divergence'] or
                (ctx['rsi'] >= ctx.get('rsi_extreme_short', 80) and ctx['vol_spike'] >= 2.0)
            ),
            reason_template="❌ 做空缺少MACD确认(需死叉/背离/极端RSI+巨量)"
        ))
        
        # ========== 10. BTC市场状态 ==========
        self.rules.append(HardRule(
            name="btc_crash_long",
            category="btc",
            description="BTC暴跌时不做多山寨币",
            check_fn=lambda ctx: (
                ctx['side'] != 'long' or
                ctx.get('btc_change_1h', 0) >= -0.03 or
                ctx.get('is_independent', False)
            ),
            reason_template="❌ BTC暴跌({btc_change_1h:+.1%})，山寨币做多风险极高"
        ))
        
        self.rules.append(HardRule(
            name="btc_moon_short",
            category="btc",
            description="BTC暴涨时不做空山寨币",
            check_fn=lambda ctx: (
                ctx['side'] != 'short' or
                ctx.get('btc_change_1h', 0) <= 0.03 or
                ctx.get('is_independent', False)
            ),
            reason_template="❌ BTC暴涨({btc_change_1h:+.1%})，山寨币做空风险极高"
        ))
    
    def build_context(self, payload: Dict[str, Any], metrics: Dict[str, Any] = None) -> Dict[str, Any]:
        """
        构建规则检查上下文
        
        Args:
            payload: 信号数据
            metrics: 可选的额外指标数据
            
        Returns:
            上下文字典
        """
        m = payload.get("metrics", {}) or {}
        if metrics:
            m.update(metrics)
        
        btc = payload.get("btc_status", {}) or {}
        corr = payload.get("correlation_analysis", {}) or {}
        subscores = payload.get("subscores", {}) or {}
        
        # 安全获取数值
        def safe_float(x, default=0.0):
            try:
                v = float(x) if x is not None else default
                return default if (math.isnan(v) or math.isinf(v)) else v
            except:
                return default
        
        ctx = {
            # 基础信息
            "symbol": payload.get("symbol", "UNKNOWN"),
            "side": (payload.get("side") or payload.get("bias", "long")).lower(),
            "score": safe_float(payload.get("score"), 0.5),
            
            # RSI阈值（从配置读取）
            "rsi_long_max": self.rsi_long_max,
            "rsi_short_min": self.rsi_short_min,
            "rsi_extreme_long": self.rsi_extreme_long,
            "rsi_extreme_short": self.rsi_extreme_short,
            
            # 技术指标
            "rsi": safe_float(m.get("rsi"), 50),
            "adx": safe_float(m.get("adx"), 25),
            "macd_histogram": safe_float(m.get("macd_histogram"), 0),
            "macd_cross": m.get("macd_cross", "none"),
            "bb_width": safe_float(m.get("bb_width"), 0.03),
            "bb_position": safe_float(m.get("bb_position"), 0),
            "vol_spike": safe_float(m.get("vol_spike_ratio", m.get("vol_spike")), 1.0),
            
            # 价格变动
            "price_change_24h": safe_float(m.get("price_change_24h"), 0),
            
            # 资金费率
            "funding_rate": safe_float(m.get("funding", m.get("funding_rate")), 0),
            
            # 订单簿
            "orderbook_score": safe_float(subscores.get("orderbook"), 0.5),
            
            # BTC状态
            "btc_change_1h": safe_float(btc.get("price_change_1h"), 0),
            "btc_trend": btc.get("trend", "stable"),
            
            # 相关性
            "is_independent": corr.get("is_independent", False),
            "btc_correlation": safe_float(corr.get("correlation"), 0),
            
            # 阈值配置
            "min_score": self.min_score,
            "min_vol": self.min_volume_ratio,
            "max_price_change_extreme": self.max_price_change_extreme,
            "max_price_change_high": self.max_price_change_high,
            "price_change_high_min_score": self.price_change_high_min_score,
            "bb_squeeze_threshold": self.bb_squeeze_threshold,
            "bb_squeeze_vol_min": self.bb_squeeze_vol_min,
            "min_adx_with_low_vol": self.min_adx_with_low_vol,
            "adx_trend_end_threshold": self.adx_trend_end_threshold,
            "max_funding_rate": self.max_funding_rate,
            "min_orderbook_score": self.min_orderbook_score,
            
            # MACD确认要求
            "require_macd_confirm": self.config.get("reversal_strategy", {}).get("require_macd_confirm", True),
        }
        
        return ctx
    
    def evaluate(self, ctx: Dict[str, Any]) -> Tuple[bool, str, Dict[str, Any]]:
        """
        评估所有规则
        
        Args:
            ctx: 上下文字典（通过build_context生成）
            
        Returns:
            (是否通过, 拒绝原因, 详细信息)
        """
        results = []
        warnings = []
        
        for rule in self.rules:
            # 跳过禁用的规则
            if rule.name in self.disabled_rules:
                continue
            
            result = rule.check(ctx)
            results.append(result)
            
            if not result.passed:
                if result.details.get("severity") == "warn":
                    warnings.append(result)
                else:
                    # 阻塞性规则未通过
                    details = {
                        "failed_rule": rule.name,
                        "category": rule.category,
                        "all_results": [r.__dict__ for r in results],
                        "warnings": [w.__dict__ for w in warnings],
                    }
                    return False, result.reason, details
        
        # 所有规则通过
        details = {
            "all_results": [r.__dict__ for r in results],
            "warnings": [w.__dict__ for w in warnings],
            "rules_checked": len(results),
        }
        
        # 构建警告消息
        warning_msg = ""
        if warnings:
            warning_msg = " | 警告: " + "; ".join([w.reason for w in warnings])
        
        return True, f"✅ 通过所有硬规则({len(results)}条){warning_msg}", details
    
    def evaluate_payload(self, payload: Dict[str, Any]) -> Tuple[bool, str, Dict[str, Any]]:
        """
        便捷方法：直接评估payload
        
        Args:
            payload: 信号数据
            
        Returns:
            (是否通过, 拒绝原因, 详细信息)
        """
        ctx = self.build_context(payload)
        return self.evaluate(ctx)
    
    def disable_rule(self, rule_name: str):
        """禁用指定规则"""
        self.disabled_rules.add(rule_name)
    
    def enable_rule(self, rule_name: str):
        """启用指定规则"""
        self.disabled_rules.discard(rule_name)
    
    def list_rules(self) -> List[Dict[str, str]]:
        """列出所有规则"""
        return [
            {
                "name": r.name,
                "category": r.category,
                "description": r.description,
                "severity": r.severity,
                "enabled": r.name not in self.disabled_rules,
            }
            for r in self.rules
        ]
    
    def get_rules_by_category(self, category: str) -> List[HardRule]:
        """获取指定分类的规则"""
        return [r for r in self.rules if r.category == category]


# ==================== 工厂函数 ====================

def create_hard_rules_engine(config: Dict[str, Any] = None) -> HardRulesEngine:
    """
    创建硬规则引擎实例
    
    Args:
        config: 配置字典，如果为None则从config.yaml加载
    """
    if config is None:
        import yaml
        with open("config.yaml", "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
    
    return HardRulesEngine(config)


# ==================== 测试代码 ====================

if __name__ == "__main__":
    import yaml
    
    print("硬规则引擎测试")
    print("=" * 60)
    
    # 加载配置
    try:
        with open("config.yaml", "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
    except FileNotFoundError:
        print("使用默认配置")
        config = {
            "reversal_strategy": {
                "rsi_long_max": 25,
                "rsi_short_min": 75,
                "min_score": 0.55,
                "min_volume_ratio": 1.2,
            }
        }
    
    # 创建引擎
    engine = HardRulesEngine(config)
    
    # 列出所有规则
    print("\n📋 规则列表:")
    for rule in engine.list_rules():
        status = "✅" if rule["enabled"] else "❌"
        print(f"  {status} [{rule['category']}] {rule['name']}: {rule['description']}")
    
    # 测试用例1: 正常做多信号
    print("\n" + "=" * 60)
    print("测试1: 正常做多信号 (RSI=22, Score=0.78)")
    payload1 = {
        "symbol": "ETH/USDT:USDT",
        "side": "long",
        "score": 0.78,
        "metrics": {
            "rsi": 22,
            "adx": 28,
            "macd_cross": "golden",
            "bb_width": 0.025,
            "vol_spike_ratio": 1.8,
            "price_change_24h": 0.05,
            "funding": 0.0001,
        },
        "subscores": {"orderbook": 0.65},
        "btc_status": {"price_change_1h": 0.005},
    }
    passed, reason, details = engine.evaluate_payload(payload1)
    print(f"结果: {'✅ 通过' if passed else '❌ 拒绝'}")
    print(f"原因: {reason}")
    
    # 测试用例2: RSI不符合
    print("\n" + "=" * 60)
    print("测试2: RSI不符合 (RSI=45, 做多)")
    payload2 = {
        "symbol": "BTC/USDT:USDT",
        "side": "long",
        "score": 0.82,
        "metrics": {
            "rsi": 45,  # 不符合做多条件
            "adx": 30,
            "macd_cross": "golden",
            "vol_spike_ratio": 2.0,
        },
        "subscores": {"orderbook": 0.7},
    }
    passed, reason, details = engine.evaluate_payload(payload2)
    print(f"结果: {'✅ 通过' if passed else '❌ 拒绝'}")
    print(f"原因: {reason}")
    
    # 测试用例3: 评分不足
    print("\n" + "=" * 60)
    print("测试3: 评分不足 (Score=0.45)")
    payload3 = {
        "symbol": "SOL/USDT:USDT",
        "side": "long",
        "score": 0.45,  # 评分不足
        "metrics": {
            "rsi": 20,
            "adx": 25,
            "vol_spike_ratio": 1.5,
        },
        "subscores": {"orderbook": 0.6},
    }
    passed, reason, details = engine.evaluate_payload(payload3)
    print(f"结果: {'✅ 通过' if passed else '❌ 拒绝'}")
    print(f"原因: {reason}")
    
    # 测试用例4: BTC暴跌做多
    print("\n" + "=" * 60)
    print("测试4: BTC暴跌时做多 (BTC -4%)")
    payload4 = {
        "symbol": "DOGE/USDT:USDT",
        "side": "long",
        "score": 0.85,
        "metrics": {
            "rsi": 18,
            "adx": 35,
            "macd_cross": "golden",
            "vol_spike_ratio": 2.5,
        },
        "subscores": {"orderbook": 0.7},
        "btc_status": {"price_change_1h": -0.04},  # BTC暴跌
        "correlation_analysis": {"is_independent": False},
    }
    passed, reason, details = engine.evaluate_payload(payload4)
    print(f"结果: {'✅ 通过' if passed else '❌ 拒绝'}")
    print(f"原因: {reason}")
    
    print("\n" + "=" * 60)
    print("测试完成！")
