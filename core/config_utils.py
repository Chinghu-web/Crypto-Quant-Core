# core/config_utils.py - 统一配置工具模块 v1.0
# 用途：提供配置的单一来源，避免多处定义导致不一致

from typing import Dict, Any, Optional
import os
import yaml


class ConfigManager:
    """
    配置管理器 - 单例模式
    
    解决问题：
    1. RSI阈值等配置在多处定义，容易不同步
    2. API密钥明文存储在配置文件中
    
    使用方式：
    ```python
    from core.config_utils import get_config, get_rsi_thresholds
    
    cfg = get_config()
    rsi = get_rsi_thresholds(cfg)
    print(rsi["long_max"])  # 25
    ```
    """
    
    _instance = None
    _config = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    def load(self, path: str = "config.yaml") -> Dict[str, Any]:
        """加载配置文件"""
        if self._config is not None:
            return self._config
        
        with open(path, "r", encoding="utf-8") as f:
            self._config = yaml.safe_load(f)
        
        # 处理环境变量替换
        self._config = self._resolve_env_vars(self._config)
        
        return self._config
    
    def _resolve_env_vars(self, obj: Any) -> Any:
        """递归替换 ${ENV_VAR} 为环境变量值"""
        if isinstance(obj, str):
            if obj.startswith("${") and obj.endswith("}"):
                env_key = obj[2:-1]
                return os.getenv(env_key, obj)
            return obj
        elif isinstance(obj, dict):
            return {k: self._resolve_env_vars(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [self._resolve_env_vars(item) for item in obj]
        return obj
    
    def reload(self, path: str = "config.yaml") -> Dict[str, Any]:
        """强制重新加载配置"""
        self._config = None
        return self.load(path)
    
    @property
    def config(self) -> Dict[str, Any]:
        if self._config is None:
            self.load()
        return self._config


# ==================== 便捷函数 ====================

def get_config(path: str = "config.yaml") -> Dict[str, Any]:
    """获取配置（单例）"""
    return ConfigManager().load(path)


def get_rsi_thresholds(cfg: Dict[str, Any] = None) -> Dict[str, float]:
    """
    🔥 获取统一的RSI阈值配置
    
    这是RSI阈值的唯一来源！所有模块应该从这里读取，
    而不是各自在代码中硬编码。
    
    Returns:
        {
            "long_max": 25,        # 做多RSI上限 (超卖)
            "short_min": 75,       # 做空RSI下限 (超买)
            "extreme_long": 20,    # 极端超卖
            "extreme_short": 80,   # 极端超买
            "overbought": 70,      # 一般超买
            "oversold": 30,        # 一般超卖
        }
    """
    if cfg is None:
        cfg = get_config()
    
    # 优先从reversal_strategy读取
    reversal = cfg.get("reversal_strategy", {})
    
    # 备用：从overbought_oversold读取
    obs = cfg.get("overbought_oversold", {})
    
    return {
        # 反转策略阈值（主要）
        "long_max": reversal.get("rsi_long_max", 25),
        "short_min": reversal.get("rsi_short_min", 75),
        "extreme_long": reversal.get("rsi_extreme_long", 20),
        "extreme_short": reversal.get("rsi_extreme_short", 80),
        
        # 一般阈值（辅助）
        "overbought": obs.get("rsi_overbought", 70),
        "oversold": obs.get("rsi_oversold", 30),
        
        # 兼容旧配置
        "reversal_long_rsi_max": reversal.get("rsi_long_max", 25),
        "reversal_short_rsi_min": reversal.get("rsi_short_min", 75),
    }


def get_reversal_config(cfg: Dict[str, Any] = None) -> Dict[str, Any]:
    """
    获取完整的反转策略配置
    
    Returns:
        {
            "rsi_long_max": 25,
            "rsi_short_min": 75,
            "rsi_extreme_long": 20,
            "rsi_extreme_short": 80,
            "require_macd_confirm": True,
            "require_volume_confirm": True,
            "min_volume_ratio": 1.2,
            "min_score": 0.55,
        }
    """
    if cfg is None:
        cfg = get_config()
    
    reversal = cfg.get("reversal_strategy", {})
    
    return {
        "rsi_long_max": reversal.get("rsi_long_max", 25),
        "rsi_short_min": reversal.get("rsi_short_min", 75),
        "rsi_extreme_long": reversal.get("rsi_extreme_long", 20),
        "rsi_extreme_short": reversal.get("rsi_extreme_short", 80),
        "require_macd_confirm": reversal.get("require_macd_confirm", True),
        "require_volume_confirm": reversal.get("require_volume_confirm", True),
        "min_volume_ratio": reversal.get("min_volume_ratio", 1.2),
        "min_score": reversal.get("min_score", 0.55),
    }


def get_hard_rules_config(cfg: Dict[str, Any] = None) -> Dict[str, Any]:
    """
    获取硬规则配置
    
    合并reversal_strategy和review.hard_rules的配置
    """
    if cfg is None:
        cfg = get_config()
    
    reversal = cfg.get("reversal_strategy", {})
    hard_rules = cfg.get("review", {}).get("hard_rules", {})
    
    # RSI阈值从reversal_strategy读取
    rsi = get_rsi_thresholds(cfg)
    
    return {
        # RSI阈值
        "rsi_long_max": rsi["long_max"],
        "rsi_short_min": rsi["short_min"],
        "rsi_extreme_long": rsi["extreme_long"],
        "rsi_extreme_short": rsi["extreme_short"],
        
        # 评分要求
        "min_score": hard_rules.get("min_score", reversal.get("min_score", 0.55)),
        
        # 成交量要求
        "min_volume_ratio": hard_rules.get("min_volume_ratio", reversal.get("min_volume_ratio", 1.2)),
        
        # 暴涨暴跌过滤
        "max_price_change_extreme": hard_rules.get("max_price_change_extreme", 0.80),
        "max_price_change_high": hard_rules.get("max_price_change_high", 0.50),
        "price_change_high_min_score": hard_rules.get("price_change_high_min_score", 0.86),
        "price_change_high_min_vol": hard_rules.get("price_change_high_min_vol", 1.0),
        
        # ADX要求
        "min_adx_with_low_vol": hard_rules.get("min_adx_with_low_vol", 18),
        
        # 陷阱检测
        "bb_squeeze_threshold": hard_rules.get("bb_squeeze_threshold", 0.01),
        "bb_squeeze_vol_min": hard_rules.get("bb_squeeze_vol_min", 1.5),
        "adx_trend_end_threshold": hard_rules.get("adx_trend_end_threshold", 40),
        "adx_trend_end_bb": hard_rules.get("adx_trend_end_bb", 0.02),
        "adx_trend_end_vol": hard_rules.get("adx_trend_end_vol", 1.0),
        
        # 止损规则
        "min_sl_atr_multiplier": hard_rules.get("min_sl_atr_multiplier", 2.0),
        "bb_squeeze_sl_atr_multiplier": hard_rules.get("bb_squeeze_sl_atr_multiplier", 2.5),
        "low_vol_sl_atr_multiplier": hard_rules.get("low_vol_sl_atr_multiplier", 3.0),
        
        # 风控
        "max_funding_rate": hard_rules.get("max_funding_rate", 0.0008),
        "min_orderbook_score": hard_rules.get("min_orderbook_score", 0.40),
        "max_slippage_to_sl_ratio": hard_rules.get("max_slippage_to_sl_ratio", 0.5),
        "low_liquidity_vol_min": hard_rules.get("low_liquidity_vol_min", 2.0),
    }


def get_trading_config(cfg: Dict[str, Any] = None) -> Dict[str, Any]:
    """获取交易配置"""
    if cfg is None:
        cfg = get_config()
    
    auto = cfg.get("auto_trading", {})
    futures = cfg.get("futures_trading", {})
    
    return {
        # 基础设置
        "enabled": auto.get("enabled", False),
        
        # 资金管理
        "total_usdt": auto.get("capital", {}).get("total_usdt", 20),
        "max_position_pct": auto.get("capital", {}).get("max_position_pct", 0.5),
        "min_position_usdt": auto.get("capital", {}).get("min_position_usdt", 5),
        "max_position_usdt": auto.get("capital", {}).get("max_position_usdt", 10),
        
        # 杠杆
        "max_leverage": futures.get("max_leverage", 20),
        "base_leverage": futures.get("base_leverage", 15),
        
        # 风控
        "max_positions": auto.get("risk", {}).get("max_positions", 2),
        "max_daily_trades": auto.get("safety", {}).get("max_daily_trades", 10),
        "max_daily_loss_pct": auto.get("safety", {}).get("max_daily_loss_pct", 0.2),
        
        # 移动止损
        "trailing_stop": auto.get("exit", {}).get("trailing_stop", True),
        "trailing_stop_activation_pct": auto.get("exit", {}).get("trailing_stop_activation_pct", 0.01),
        "trailing_stop_distance_pct": auto.get("exit", {}).get("trailing_stop_distance_pct", 0.005),
        
        # 保护性止损
        "breakeven_stop": auto.get("exit", {}).get("breakeven_stop", True),
        "breakeven_activation_pct": auto.get("exit", {}).get("breakeven_activation_pct", 0.01),
        "breakeven_buffer_pct": auto.get("exit", {}).get("breakeven_buffer_pct", 0.002),
    }


def get_okx_config(cfg: Dict[str, Any] = None) -> Dict[str, Any]:
    """获取OKX API配置（支持环境变量）"""
    if cfg is None:
        cfg = get_config()
    
    okx = cfg.get("auto_trading", {}).get("okx", {})
    
    # 优先使用环境变量
    api_key = os.getenv("OKX_API_KEY", okx.get("api_key", ""))
    secret = os.getenv("OKX_SECRET", okx.get("secret", ""))
    passphrase = os.getenv("OKX_PASSPHRASE", okx.get("passphrase", ""))
    
    # 清理环境变量格式
    if api_key.startswith("${"):
        api_key = ""
    if secret.startswith("${"):
        secret = ""
    if passphrase.startswith("${"):
        passphrase = ""
    
    return {
        "api_key": api_key,
        "secret": secret,
        "passphrase": passphrase,
        "testnet": okx.get("testnet", False),
    }


def get_claude_config(cfg: Dict[str, Any] = None) -> Dict[str, Any]:
    """获取Claude API配置"""
    if cfg is None:
        cfg = get_config()
    
    claude = cfg.get("claude", {})
    
    api_key = os.getenv("CLAUDE_API_KEY", claude.get("api_key", ""))
    if api_key.startswith("${"):
        api_key = ""
    
    return {
        "api_key": api_key,
        "model": claude.get("model", "claude-sonnet-4-5-20250929"),
        "max_tokens": claude.get("max_tokens", 1500),
        "temperature": claude.get("temperature", 0.2),
        "timeout": claude.get("timeout", 180),
    }


def validate_config(cfg: Dict[str, Any] = None) -> Dict[str, list]:
    """
    验证配置完整性
    
    Returns:
        {"errors": [...], "warnings": [...]}
    """
    if cfg is None:
        cfg = get_config()
    
    errors = []
    warnings = []
    
    # 检查必要配置
    if not cfg.get("reversal_strategy"):
        errors.append("缺少reversal_strategy配置区块")
    
    # 检查RSI阈值一致性
    rsi = get_rsi_thresholds(cfg)
    if rsi["long_max"] >= rsi["short_min"]:
        errors.append(f"RSI阈值错误: long_max({rsi['long_max']}) >= short_min({rsi['short_min']})")
    
    # 检查API配置
    okx = get_okx_config(cfg)
    if cfg.get("auto_trading", {}).get("enabled"):
        if not okx["api_key"]:
            errors.append("auto_trading已启用但缺少OKX API密钥")
    
    claude = get_claude_config(cfg)
    if not claude["api_key"]:
        warnings.append("缺少Claude API密钥，将使用本地回退策略")
    
    return {"errors": errors, "warnings": warnings}


# ==================== 测试代码 ====================

if __name__ == "__main__":
    print("配置工具测试")
    print("=" * 50)
    
    try:
        cfg = get_config()
        print("✅ 配置加载成功")
        
        # 测试RSI阈值
        rsi = get_rsi_thresholds(cfg)
        print(f"\nRSI阈值配置:")
        print(f"  做多上限: {rsi['long_max']}")
        print(f"  做空下限: {rsi['short_min']}")
        print(f"  极端超卖: {rsi['extreme_long']}")
        print(f"  极端超买: {rsi['extreme_short']}")
        
        # 测试硬规则配置
        hard = get_hard_rules_config(cfg)
        print(f"\n硬规则配置:")
        print(f"  最低评分: {hard['min_score']}")
        print(f"  最小成交量: {hard['min_volume_ratio']}x")
        
        # 验证配置
        result = validate_config(cfg)
        if result["errors"]:
            print(f"\n❌ 配置错误:")
            for e in result["errors"]:
                print(f"  - {e}")
        if result["warnings"]:
            print(f"\n⚠️ 配置警告:")
            for w in result["warnings"]:
                print(f"  - {w}")
        
        if not result["errors"] and not result["warnings"]:
            print("\n✅ 配置验证通过")
            
    except FileNotFoundError:
        print("❌ 未找到config.yaml")
    except Exception as e:
        print(f"❌ 配置加载失败: {e}")
