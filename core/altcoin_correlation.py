# core/altcoin_correlation.py - 山寨币与BTC相关性分析 v2.0
# 🔥 v2.0 更新：修复缓存key问题，成交量从币安实时获取

import ccxt
import numpy as np
import time
from typing import Dict, Any, Optional, Tuple, List
from datetime import datetime, timezone
from dataclasses import dataclass


@dataclass
class CorrelationResult:
    """相关性分析结果"""
    symbol: str
    correlation: float  # -1 到 1
    beta: float  # 贝塔系数
    is_independent: bool  # 是否独立于BTC
    independence_level: str  # highly_independent/independent/weakly_independent/correlated
    lag_minutes: int  # 滞后分钟数（正=滞后于BTC，负=领先于BTC）
    btc_change_1h: float  # BTC 1小时变化
    coin_change_1h: float  # 目标币1小时变化
    vol_spike_ratio: float  # 成交量倍数（实时获取）
    analysis_time: datetime
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "correlation": round(self.correlation, 3),
            "beta": round(self.beta, 2),
            "is_independent": self.is_independent,
            "independence_level": self.independence_level,
            "lag_minutes": self.lag_minutes,
            "btc_change_1h": round(self.btc_change_1h, 4),
            "coin_change_1h": round(self.coin_change_1h, 4),
            "vol_spike_ratio": round(self.vol_spike_ratio, 2),
        }


class AltcoinCorrelationAnalyzer:
    """
    山寨币与BTC相关性分析器
    
    功能：
    1. 计算与BTC的相关系数
    2. 检测独立行情（低相关性 = 高质量信号）
    3. 计算滞后时间（大多数山寨币滞后BTC 1-5分钟）
    4. 实时获取成交量数据
    
    核心理念：
    - 高相关性 + BTC下跌 = 山寨币做多风险高
    - 低相关性 = 独立行情，信号质量更高
    - 负相关性 = 对冲机会
    
    缓存策略：
    - 🔥 缓存key不包含成交量（避免频繁失效）
    - 相关性数据缓存5分钟
    - 成交量实时从币安获取
    """
    
    # 🔥 缓存：key只用symbol，不包含vol
    _correlation_cache: Dict[str, Tuple[CorrelationResult, float]] = {}
    _btc_data_cache: Dict[str, Tuple[Any, float]] = {}
    
    # 独立性阈值
    HIGHLY_INDEPENDENT_THRESHOLD = 0.2
    INDEPENDENT_THRESHOLD = 0.3
    WEAKLY_INDEPENDENT_THRESHOLD = 0.4
    
    def __init__(self, config: Dict[str, Any] = None):
        """
        初始化分析器
        
        Args:
            config: 配置字典
        """
        self.config = config or {}
        
        # 交易所实例
        self.exchange = ccxt.binance({
            'enableRateLimit': True,
            'options': {'defaultType': 'future'}
        })
        
        # 缓存配置
        self.cache_ttl = 300  # 5分钟缓存
        self.btc_cache_ttl = 60  # BTC数据1分钟缓存
        
        # 分析配置
        sig_cfg = self.config.get("signal_classification", {})
        indep = sig_cfg.get("independence", {})
        
        self.highly_independent = indep.get("highly_independent", self.HIGHLY_INDEPENDENT_THRESHOLD)
        self.independent = indep.get("independent", self.INDEPENDENT_THRESHOLD)
        self.weakly_independent = indep.get("weakly_independent", self.WEAKLY_INDEPENDENT_THRESHOLD)
        
        print(f"[CORRELATION] 分析器初始化完成")
        print(f"  独立性阈值: 极度独立<{self.highly_independent} | 独立<{self.independent} | 弱独立<{self.weakly_independent}")
    
    def analyze(self, symbol: str, use_cache: bool = True) -> CorrelationResult:
        """
        分析指定币种与BTC的相关性
        
        Args:
            symbol: 交易对 (如 "ETH/USDT:USDT")
            use_cache: 是否使用缓存
            
        Returns:
            CorrelationResult对象
        """
        now = time.time()
        
        # 🔥 缓存key只用symbol，不包含vol
        cache_key = symbol
        
        # 检查缓存
        if use_cache and cache_key in self._correlation_cache:
            cached_result, cached_time = self._correlation_cache[cache_key]
            if now - cached_time < self.cache_ttl:
                # 🔥 实时更新成交量
                cached_result.vol_spike_ratio = self._get_realtime_volume(symbol)
                return cached_result
        
        # 获取BTC数据
        btc_data = self._get_btc_data()
        if btc_data is None:
            return self._default_result(symbol)
        
        # 获取目标币数据
        coin_data = self._get_coin_data(symbol)
        if coin_data is None:
            return self._default_result(symbol)
        
        # 计算相关性
        correlation, beta = self._calculate_correlation(btc_data, coin_data)
        
        # 检测滞后
        lag_minutes = self._detect_lag(btc_data, coin_data)
        
        # 判断独立性
        abs_corr = abs(correlation)
        if abs_corr < self.highly_independent:
            independence_level = "highly_independent"
            is_independent = True
        elif abs_corr < self.independent:
            independence_level = "independent"
            is_independent = True
        elif abs_corr < self.weakly_independent:
            independence_level = "weakly_independent"
            is_independent = True
        else:
            independence_level = "correlated"
            is_independent = False
        
        # 计算价格变化
        btc_change = self._calculate_change(btc_data)
        coin_change = self._calculate_change(coin_data)
        
        # 🔥 实时获取成交量
        vol_spike = self._get_realtime_volume(symbol)
        
        result = CorrelationResult(
            symbol=symbol,
            correlation=correlation,
            beta=beta,
            is_independent=is_independent,
            independence_level=independence_level,
            lag_minutes=lag_minutes,
            btc_change_1h=btc_change,
            coin_change_1h=coin_change,
            vol_spike_ratio=vol_spike,
            analysis_time=datetime.now(timezone.utc),
        )
        
        # 缓存结果
        self._correlation_cache[cache_key] = (result, now)
        
        return result
    
    def _get_btc_data(self) -> Optional[np.ndarray]:
        """获取BTC价格数据（带缓存）"""
        now = time.time()
        cache_key = "BTC/USDT:USDT"
        
        if cache_key in self._btc_data_cache:
            data, cached_time = self._btc_data_cache[cache_key]
            if now - cached_time < self.btc_cache_ttl:
                return data
        
        try:
            ohlcv = self.exchange.fetch_ohlcv(
                "BTC/USDT:USDT", 
                '1m', 
                limit=120  # 2小时数据
            )
            
            if not ohlcv or len(ohlcv) < 60:
                return None
            
            # 提取收盘价
            closes = np.array([c[4] for c in ohlcv])
            
            self._btc_data_cache[cache_key] = (closes, now)
            return closes
            
        except Exception as e:
            print(f"[CORRELATION] 获取BTC数据失败: {e}")
            return None
    
    def _get_coin_data(self, symbol: str) -> Optional[np.ndarray]:
        """获取目标币价格数据"""
        try:
            ohlcv = self.exchange.fetch_ohlcv(symbol, '1m', limit=120)
            
            if not ohlcv or len(ohlcv) < 60:
                return None
            
            closes = np.array([c[4] for c in ohlcv])
            return closes
            
        except Exception as e:
            print(f"[CORRELATION] 获取{symbol}数据失败: {e}")
            return None
    
    def _calculate_correlation(
        self, 
        btc_data: np.ndarray, 
        coin_data: np.ndarray
    ) -> Tuple[float, float]:
        """
        计算相关系数和贝塔系数
        
        Returns:
            (correlation, beta)
        """
        # 对齐数据长度
        min_len = min(len(btc_data), len(coin_data))
        btc = btc_data[-min_len:]
        coin = coin_data[-min_len:]
        
        # 计算收益率
        btc_returns = np.diff(btc) / btc[:-1]
        coin_returns = np.diff(coin) / coin[:-1]
        
        # 处理异常值
        btc_returns = np.clip(btc_returns, -0.1, 0.1)
        coin_returns = np.clip(coin_returns, -0.1, 0.1)
        
        # 计算相关系数
        if np.std(btc_returns) > 0 and np.std(coin_returns) > 0:
            correlation = np.corrcoef(btc_returns, coin_returns)[0, 1]
        else:
            correlation = 0.0
        
        # 计算贝塔系数
        btc_var = np.var(btc_returns)
        if btc_var > 0:
            covariance = np.cov(btc_returns, coin_returns)[0, 1]
            beta = covariance / btc_var
            # 限制贝塔范围（Meme币可能有极端值）
            beta = np.clip(beta, -10.0, 10.0)
        else:
            beta = 1.0
        
        return float(correlation), float(beta)
    
    def _detect_lag(
        self, 
        btc_data: np.ndarray, 
        coin_data: np.ndarray
    ) -> int:
        """
        检测滞后时间
        
        大多数山寨币滞后BTC 1-5分钟，检测最佳滞后
        
        Returns:
            滞后分钟数（正=滞后，负=领先）
        """
        min_len = min(len(btc_data), len(coin_data))
        btc = btc_data[-min_len:]
        coin = coin_data[-min_len:]
        
        btc_returns = np.diff(btc) / btc[:-1]
        coin_returns = np.diff(coin) / coin[:-1]
        
        best_lag = 0
        best_corr = 0
        
        # 检测-10到+10分钟的滞后
        for lag in range(-10, 11):
            if lag == 0:
                shifted_btc = btc_returns
                shifted_coin = coin_returns
            elif lag > 0:
                shifted_btc = btc_returns[:-lag]
                shifted_coin = coin_returns[lag:]
            else:
                shifted_btc = btc_returns[-lag:]
                shifted_coin = coin_returns[:lag]
            
            if len(shifted_btc) < 30:
                continue
            
            if np.std(shifted_btc) > 0 and np.std(shifted_coin) > 0:
                corr = np.corrcoef(shifted_btc, shifted_coin)[0, 1]
                if abs(corr) > abs(best_corr):
                    best_corr = corr
                    best_lag = lag
        
        return best_lag
    
    def _calculate_change(self, data: np.ndarray) -> float:
        """计算1小时价格变化"""
        if len(data) < 60:
            return 0.0
        
        price_now = data[-1]
        price_1h = data[-60]
        
        if price_1h > 0:
            return (price_now - price_1h) / price_1h
        return 0.0
    
    def _get_realtime_volume(self, symbol: str) -> float:
        """
        🔥 实时从币安获取成交量倍数
        
        计算方式：当前成交量 / 20周期平均成交量
        """
        try:
            ohlcv = self.exchange.fetch_ohlcv(symbol, '1m', limit=25)
            
            if not ohlcv or len(ohlcv) < 21:
                return 1.0
            
            volumes = [c[5] for c in ohlcv]
            
            # 当前成交量
            vol_now = volumes[-1]
            
            # 20周期平均（排除当前）
            vol_ma = np.mean(volumes[-21:-1])
            
            if vol_ma > 0:
                return vol_now / vol_ma
            return 1.0
            
        except Exception as e:
            print(f"[CORRELATION] 获取成交量失败 {symbol}: {e}")
            return 1.0
    
    def _default_result(self, symbol: str) -> CorrelationResult:
        """返回默认结果"""
        return CorrelationResult(
            symbol=symbol,
            correlation=0.5,
            beta=1.0,
            is_independent=False,
            independence_level="correlated",
            lag_minutes=0,
            btc_change_1h=0.0,
            coin_change_1h=0.0,
            vol_spike_ratio=1.0,
            analysis_time=datetime.now(timezone.utc),
        )
    
    def get_trading_signal_quality(self, result: CorrelationResult, side: str) -> Dict[str, Any]:
        """
        根据相关性分析评估信号质量
        
        Args:
            result: 相关性分析结果
            side: 交易方向 (long/short)
            
        Returns:
            信号质量评估
        """
        quality = {
            "score_adjustment": 0.0,
            "risk_level": "medium",
            "warnings": [],
            "opportunities": [],
        }
        
        side = side.lower()
        
        # 独立行情奖励
        if result.is_independent:
            if result.independence_level == "highly_independent":
                quality["score_adjustment"] += 0.10
                quality["opportunities"].append("🌟 极度独立行情，信号质量高")
            elif result.independence_level == "independent":
                quality["score_adjustment"] += 0.05
                quality["opportunities"].append("✅ 独立行情")
            elif result.independence_level == "weakly_independent":
                quality["score_adjustment"] += 0.02
        
        # BTC趋势风险
        btc_move = result.btc_change_1h
        
        if side == "long":
            if btc_move < -0.02:  # BTC跌超2%
                quality["score_adjustment"] -= 0.15
                quality["risk_level"] = "high"
                quality["warnings"].append(f"⚠️ BTC下跌{btc_move*100:.1f}%，做多风险高")
            elif btc_move < -0.01:
                quality["score_adjustment"] -= 0.05
                quality["warnings"].append(f"⚠️ BTC下跌{btc_move*100:.1f}%")
            elif btc_move > 0.02:
                quality["score_adjustment"] += 0.05
                quality["opportunities"].append(f"📈 BTC上涨{btc_move*100:.1f}%，顺势做多")
        else:
            if btc_move > 0.02:  # BTC涨超2%
                quality["score_adjustment"] -= 0.15
                quality["risk_level"] = "high"
                quality["warnings"].append(f"⚠️ BTC上涨{btc_move*100:.1f}%，做空风险高")
            elif btc_move > 0.01:
                quality["score_adjustment"] -= 0.05
                quality["warnings"].append(f"⚠️ BTC上涨{btc_move*100:.1f}%")
            elif btc_move < -0.02:
                quality["score_adjustment"] += 0.05
                quality["opportunities"].append(f"📉 BTC下跌{btc_move*100:.1f}%，顺势做空")
        
        # 高相关性 + 逆势风险
        if result.correlation > 0.7:
            if (side == "long" and btc_move < 0) or (side == "short" and btc_move > 0):
                quality["score_adjustment"] -= 0.10
                quality["warnings"].append(f"⚠️ 高相关性({result.correlation:.2f})且逆BTC趋势")
        
        # 成交量确认
        if result.vol_spike_ratio >= 2.0:
            quality["score_adjustment"] += 0.05
            quality["opportunities"].append(f"📊 成交量放大{result.vol_spike_ratio:.1f}x")
        elif result.vol_spike_ratio < 0.5:
            quality["warnings"].append("⚠️ 成交量萎缩")
        
        # 滞后提示
        if abs(result.lag_minutes) >= 3:
            if result.lag_minutes > 0:
                quality["warnings"].append(f"⏱️ 滞后BTC {result.lag_minutes}分钟")
            else:
                quality["opportunities"].append(f"⏱️ 领先BTC {-result.lag_minutes}分钟")
        
        return quality
    
    def clear_cache(self):
        """清除所有缓存"""
        self._correlation_cache.clear()
        self._btc_data_cache.clear()
        print("[CORRELATION] 缓存已清除")


# ==================== 便捷函数 ====================

_analyzer: Optional[AltcoinCorrelationAnalyzer] = None


def get_correlation_analyzer(config: Dict[str, Any] = None) -> AltcoinCorrelationAnalyzer:
    """获取全局分析器实例"""
    global _analyzer
    if _analyzer is None:
        _analyzer = AltcoinCorrelationAnalyzer(config)
    return _analyzer


def analyze_correlation(symbol: str, config: Dict[str, Any] = None) -> CorrelationResult:
    """便捷函数：分析单个币种"""
    analyzer = get_correlation_analyzer(config)
    return analyzer.analyze(symbol)


def get_signal_quality(
    symbol: str, 
    side: str, 
    config: Dict[str, Any] = None
) -> Dict[str, Any]:
    """便捷函数：获取信号质量评估"""
    analyzer = get_correlation_analyzer(config)
    result = analyzer.analyze(symbol)
    return analyzer.get_trading_signal_quality(result, side)


# ==================== 🔥 兼容main.py的接口 ====================

def get_cached_correlation(
    symbol: str,
    df = None,  # pd.DataFrame
    btc_df = None,  # pd.DataFrame
    btc_status: Dict[str, Any] = None,
    vol_spike_ratio: float = 1.0,
    config: Dict[str, Any] = None,
) -> Dict[str, Any]:
    """
    🔥 获取缓存的相关性数据（兼容main.py调用）
    
    Args:
        symbol: 交易对符号
        df: 该币种的OHLCV DataFrame
        btc_df: BTC的OHLCV DataFrame
        btc_status: BTC市场状态字典
        vol_spike_ratio: 成交量spike比率
        config: 配置字典
    
    Returns:
        {
            "correlation": 相关系数,
            "lag_minutes": 滞后时间,
            "independence": 独立性评分,
            "btc_trend": BTC趋势,
            "recommendation": 建议,
            "entry_multiplier": 入场价倍数调整,
            ...
        }
    """
    import numpy as np
    
    # 默认返回值
    default_result = {
        "correlation": 0.0,
        "lag_minutes": 0,
        "independence": 0.5,
        "independence_category": "independent",
        "btc_trend": btc_status.get("trend", "neutral") if btc_status else "neutral",
        "btc_1h_change": btc_status.get("change_1h", 0) if btc_status else 0,
        "recommendation": "",
        "entry_multiplier": 1.0,
        "quality_score": 0.5,
        "vol_spike_ratio": vol_spike_ratio,
        "cached": False,
    }
    
    # 如果没有数据，返回默认值
    if df is None or btc_df is None or len(df) < 30 or len(btc_df) < 30:
        return default_result
    
    try:
        # 计算相关性
        # 确保两个DataFrame长度一致
        min_len = min(len(df), len(btc_df))
        
        # 获取收盘价
        if 'close' in df.columns:
            coin_close = df['close'].tail(min_len).values
        elif 4 in df.columns:
            coin_close = df[4].tail(min_len).values
        else:
            return default_result
        
        if 'close' in btc_df.columns:
            btc_close = btc_df['close'].tail(min_len).values
        elif 4 in btc_df.columns:
            btc_close = btc_df[4].tail(min_len).values
        else:
            return default_result
        
        # 计算收益率
        coin_returns = np.diff(coin_close) / coin_close[:-1]
        btc_returns = np.diff(btc_close) / btc_close[:-1]
        
        # 去除NaN和Inf
        mask = np.isfinite(coin_returns) & np.isfinite(btc_returns)
        coin_returns = coin_returns[mask]
        btc_returns = btc_returns[mask]
        
        if len(coin_returns) < 20:
            return default_result
        
        # 🔥 检查标准差是否为0（避免除以0警告）
        coin_std = np.std(coin_returns)
        btc_std = np.std(btc_returns)
        
        if coin_std == 0 or btc_std == 0:
            # 标准差为0意味着价格没有变化，无法计算相关性
            return default_result
        
        # 计算皮尔逊相关系数
        correlation = np.corrcoef(coin_returns, btc_returns)[0, 1]
        if np.isnan(correlation):
            correlation = 0.0
        
        # 计算滞后相关性（找最佳滞后）
        best_lag = 0
        best_lag_corr = abs(correlation)
        for lag in range(1, min(6, len(coin_returns) // 10)):  # 最多5分钟滞后
            if lag < len(coin_returns):
                lagged_corr = np.corrcoef(coin_returns[lag:], btc_returns[:-lag])[0, 1]
                if not np.isnan(lagged_corr) and abs(lagged_corr) > best_lag_corr:
                    best_lag_corr = abs(lagged_corr)
                    best_lag = lag
        
        # 独立性评分（相关性越低，独立性越高）
        independence = 1.0 - abs(correlation)
        
        # 独立性分类
        if independence >= 0.7:
            independence_category = "highly_independent"
        elif independence >= 0.5:
            independence_category = "independent"
        elif independence >= 0.3:
            independence_category = "weakly_independent"
        else:
            independence_category = "correlated"
        
        # BTC趋势
        btc_trend = btc_status.get("trend", "neutral") if btc_status else "neutral"
        btc_1h_change = btc_status.get("change_1h", 0) if btc_status else 0
        
        # 计算入场价倍数调整
        entry_multiplier = 1.0
        
        if independence >= 0.7:
            # 高度独立，不需要调整
            entry_multiplier = 1.0
            recommendation = "高独立性，可正常入场"
        elif btc_trend in ["crash", "dump"]:
            # BTC暴跌，需要更保守
            if correlation > 0.5:
                entry_multiplier = 2.0  # 高相关+BTC跌，要更低价格才入场
                recommendation = "⚠️ 高相关+BTC下跌，建议等待更低价格"
            else:
                entry_multiplier = 1.2
                recommendation = "BTC下跌中，适度保守"
        elif btc_trend in ["moon", "rally"]:
            # BTC暴涨
            if correlation > 0.5:
                entry_multiplier = 0.8  # 高相关+BTC涨，可以追一点
                recommendation = "高相关+BTC上涨，可适度追入"
            else:
                entry_multiplier = 1.0
                recommendation = "BTC上涨中"
        else:
            # 中性市场
            if correlation > 0.7:
                entry_multiplier = 1.3
                recommendation = "高相关，稍保守入场"
            else:
                entry_multiplier = 1.0
                recommendation = "正常入场"
        
        # 成交量调整
        if vol_spike_ratio > 3.0:
            entry_multiplier *= 0.9  # 放量时可以稍微激进
        elif vol_spike_ratio < 0.5:
            entry_multiplier *= 1.2  # 缩量时更保守
        
        # 质量评分
        quality_score = independence * 0.4 + min(vol_spike_ratio / 3.0, 1.0) * 0.3 + 0.3
        
        return {
            "correlation": round(correlation, 4),
            "lag_minutes": best_lag,
            "independence": round(independence, 4),
            "independence_category": independence_category,
            "btc_trend": btc_trend,
            "btc_1h_change": btc_1h_change,
            "recommendation": recommendation,
            "entry_multiplier": round(entry_multiplier, 2),
            "quality_score": round(quality_score, 4),
            "vol_spike_ratio": vol_spike_ratio,
            "cached": False,
        }
        
    except Exception as e:
        print(f"[CORRELATION] 计算异常 {symbol}: {e}")
        return default_result


def format_correlation_message(corr_data: Dict[str, Any]) -> str:
    """
    🔥 格式化相关性信息为消息字符串（兼容main.py调用）
    
    Args:
        corr_data: get_cached_correlation返回的字典
    
    Returns:
        格式化的消息字符串
    """
    corr = corr_data.get("correlation", 0)
    lag = corr_data.get("lag_minutes", 0)
    independence = corr_data.get("independence", 0)
    btc_trend = corr_data.get("btc_trend", "unknown")
    btc_change = corr_data.get("btc_1h_change", 0)
    recommendation = corr_data.get("recommendation", "")
    
    # 相关性描述
    if abs(corr) >= 0.8:
        corr_desc = "高度相关"
    elif abs(corr) >= 0.5:
        corr_desc = "中度相关"
    elif abs(corr) >= 0.3:
        corr_desc = "弱相关"
    else:
        corr_desc = "独立"
    
    # BTC趋势emoji
    trend_emoji = {
        "crash": "📉💥",
        "dump": "📉",
        "dip": "📉",
        "neutral": "➡️",
        "pump": "📈",
        "rally": "📈",
        "moon": "📈🚀",
    }.get(btc_trend, "❓")
    
    lines = [
        f"📊 BTC相关性: {corr:.2f} ({corr_desc})",
        f"⏱️ 滞后: {lag}分钟",
        f"🎯 独立性: {independence:.1%}",
        f"₿ BTC趋势: {trend_emoji} {btc_trend} ({btc_change:+.2%})",
    ]
    
    if recommendation:
        lines.append(f"💡 建议: {recommendation}")
    
    return "\n".join(lines)


# ==================== 测试代码 ====================

if __name__ == "__main__":
    print("山寨币相关性分析器测试")
    print("=" * 60)
    
    # 创建分析器
    analyzer = AltcoinCorrelationAnalyzer()
    
    # 测试几个币种
    test_symbols = [
        "ETH/USDT:USDT",
        "SOL/USDT:USDT",
        "DOGE/USDT:USDT",
    ]
    
    for symbol in test_symbols:
        print(f"\n分析 {symbol}...")
        result = analyzer.analyze(symbol)
        
        print(f"  相关系数: {result.correlation:.3f}")
        print(f"  贝塔系数: {result.beta:.2f}")
        print(f"  独立性: {result.independence_level}")
        print(f"  滞后: {result.lag_minutes}分钟")
        print(f"  BTC变化: {result.btc_change_1h*100:+.2f}%")
        print(f"  币种变化: {result.coin_change_1h*100:+.2f}%")
        print(f"  成交量: {result.vol_spike_ratio:.1f}x")
        
        # 测试信号质量
        quality = analyzer.get_trading_signal_quality(result, "long")
        print(f"  做多评分调整: {quality['score_adjustment']:+.2f}")
        if quality["warnings"]:
            print(f"  警告: {quality['warnings']}")
        if quality["opportunities"]:
            print(f"  机会: {quality['opportunities']}")
    
    print("\n" + "=" * 60)
    print("测试完成！")