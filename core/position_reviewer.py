# core/position_reviewer.py - [v1.0 持仓AI审核模块]
# -*- coding: utf-8 -*-
"""
持仓AI审核器 v1.0

功能：
1. 每5分钟检查持仓
2. 满足条件时调用DeepSeek审核
3. 执行决策：平仓/调整止损止盈/移动到成本价
4. 方向错误时设紧止损减少损失

审核触发条件（满足任一）：
- 持仓时间 >= 10分钟
- 盈亏在 -1% ~ +2% 的尴尬区域
- BTC大幅波动 > 1%
- 成交量异常 > 2x

AI输出决策类型：
- hold: 继续持有
- close: 平仓（设紧止损而非直接平仓）
- tighten_sl: 收紧止损
- extend_tp: 扩大止盈
- breakeven: 移动到成本价
"""

import requests
import json
import math
import pandas as pd
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from enum import Enum


class PositionAction(Enum):
    """持仓操作类型"""
    HOLD = "hold"
    CLOSE = "close"
    TIGHTEN_SL = "tighten_sl"
    EXTEND_TP = "extend_tp"
    BREAKEVEN = "breakeven"


class PositionReviewer:
    """持仓AI审核器"""
    
    def __init__(self, config: Dict, exchange):
        """
        初始化持仓审核器
        
        Args:
            config: 完整配置字典
            exchange: 交易所实例（用于获取市场数据）
        """
        self.config = config
        self.exchange = exchange
        
        # DeepSeek配置
        deepseek_cfg = config.get("deepseek", {})
        self.deepseek_api_key = deepseek_cfg.get("api_key", "")
        self.deepseek_model = deepseek_cfg.get("model", "deepseek-chat")
        self.deepseek_base_url = deepseek_cfg.get("base_url", "https://api.deepseek.com/v1")
        self.deepseek_timeout = deepseek_cfg.get("timeout", 60)
        
        # 审核配置
        pr_cfg = config.get("position_review", {})
        self.enabled = pr_cfg.get("enabled", True)
        self.review_interval_sec = pr_cfg.get("review_interval_sec", 300)  # 5分钟
        self.min_holding_time_min = pr_cfg.get("min_holding_time_min", 10)  # 最少10分钟
        
        # 触发条件阈值
        self.pnl_awkward_min = pr_cfg.get("pnl_awkward_min", -0.01)  # -1%
        self.pnl_awkward_max = pr_cfg.get("pnl_awkward_max", 0.02)   # +2%
        self.btc_move_threshold = pr_cfg.get("btc_move_threshold", 0.01)  # 1%
        self.volume_spike_threshold = pr_cfg.get("volume_spike_threshold", 2.0)  # 2x
        
        # 安全配置
        self.close_use_tight_sl = pr_cfg.get("close_use_tight_sl", True)
        self.tight_sl_pct = pr_cfg.get("tight_sl_pct", 0.003)  # 0.3%
        self.min_review_interval_sec = pr_cfg.get("min_review_interval_sec", 120)  # 最少2分钟
        
        # 缓存
        self._last_review_time: Dict[str, datetime] = {}
        self._btc_price_cache: Dict[str, float] = {}
        self._btc_cache_time: Optional[datetime] = None
        
        print(f"[POSITION_REVIEWER] v1.0 初始化 | 启用: {self.enabled}")
        if self.enabled:
            print(f"[POSITION_REVIEWER] 审核间隔: {self.review_interval_sec}秒 | 最少持仓: {self.min_holding_time_min}分钟")
    
    def should_review(self, position: Dict) -> Tuple[bool, str]:
        """判断是否应该审核此持仓"""
        if not self.enabled:
            return False, "审核器未启用"
        
        symbol = position.get("symbol", "")
        pnl_pct = position.get("pnl_pct", 0)
        holding_minutes = position.get("holding_minutes", 0)
        volume_ratio = position.get("volume_ratio", 1.0)
        
        # 1. 检查最小审核间隔
        last_review = self._last_review_time.get(symbol)
        if last_review:
            since_last = (datetime.now() - last_review).total_seconds()
            if since_last < self.min_review_interval_sec:
                return False, f"距上次审核仅{since_last:.0f}秒"
        
        # 2. 检查持仓时间
        if holding_minutes < self.min_holding_time_min:
            return False, f"持仓仅{holding_minutes:.0f}分钟"
        
        # 3. 检查是否在尴尬区域
        if self.pnl_awkward_min <= pnl_pct <= self.pnl_awkward_max:
            return True, f"盈亏{pnl_pct*100:.2f}%处于尴尬区域"
        
        # 4. 检查BTC波动
        btc_change = self._get_btc_change()
        if abs(btc_change) >= self.btc_move_threshold:
            return True, f"BTC波动{btc_change*100:.2f}%"
        
        # 5. 检查成交量异常
        if volume_ratio >= self.volume_spike_threshold:
            return True, f"成交量{volume_ratio:.1f}x异常"
        
        # 6. 定期审核
        if last_review:
            since_last = (datetime.now() - last_review).total_seconds()
            if since_last >= self.review_interval_sec:
                return True, f"定期审核（{since_last/60:.0f}分钟）"
        else:
            return True, "首次审核"
        
        return False, "无触发条件"
    
    def review_position(self, position: Dict) -> Dict:
        """审核持仓"""
        symbol = position.get("symbol", "UNKNOWN")
        
        print(f"\n[POSITION_REVIEW] 🔍 审核 {symbol}...")
        
        self._last_review_time[symbol] = datetime.now()
        
        result = self._deepseek_review(position)
        
        if not result:
            print(f"[POSITION_REVIEW] ⚠️ 审核失败，默认持有")
            return {
                "action": PositionAction.HOLD.value,
                "reasoning": "审核失败，默认持有",
                "urgency": "low"
            }
        
        action = result.get("action", "hold")
        reasoning = result.get("reasoning", "")
        
        print(f"[POSITION_REVIEW] 决策: {action.upper()} | {reasoning}")
        
        # 平仓转为紧止损
        if action == "close" and self.close_use_tight_sl:
            result = self._convert_close_to_tight_sl(position, result)
        
        return result
    
    def _deepseek_review(self, position: Dict) -> Optional[Dict]:
        """调用DeepSeek审核持仓"""
        if not self.deepseek_api_key:
            print(f"[POSITION_REVIEW] ⚠️ DeepSeek未配置")
            return None
            
        try:
            prompt = self._build_review_prompt(position)
            
            headers = {
                "Authorization": f"Bearer {self.deepseek_api_key}",
                "Content-Type": "application/json"
            }
            
            data = {
                "model": self.deepseek_model,
                "messages": [
                    {"role": "system", "content": "你是专业的加密货币持仓管理专家。审核持仓状态，判断是否需要调整。"},
                    {"role": "user", "content": prompt}
                ],
                "temperature": 0.2,
                "max_tokens": 500
            }
            
            response = requests.post(
                f"{self.deepseek_base_url}/chat/completions",
                headers=headers,
                json=data,
                timeout=self.deepseek_timeout
            )
            
            response.raise_for_status()
            result = response.json()
            content = result["choices"][0]["message"]["content"]
            
            return self._parse_json_response(content)
            
        except Exception as e:
            print(f"[POSITION_REVIEW] ⚠️ DeepSeek调用失败: {e}")
            return None
    
    def _build_review_prompt(self, position: Dict) -> str:
        """构建持仓审核prompt"""
        symbol = position.get("symbol", "UNKNOWN")
        side = position.get("side", "long")
        entry_price = position.get("entry_price", 0)
        current_price = position.get("current_price", 0)
        sl_price = position.get("sl_price", 0)
        tp_price = position.get("tp_price", 0)
        pnl_pct = position.get("pnl_pct", 0)
        rsi = position.get("rsi", 50)
        volume_ratio = position.get("volume_ratio", 1.0)
        holding_minutes = position.get("holding_minutes", 0)
        
        # 计算止损止盈距离
        if entry_price > 0:
            if side == "long":
                sl_dist = (entry_price - sl_price) / entry_price * 100 if sl_price > 0 else 0
                tp_dist = (tp_price - entry_price) / entry_price * 100 if tp_price > 0 else 0
            else:
                sl_dist = (sl_price - entry_price) / entry_price * 100 if sl_price > 0 else 0
                tp_dist = (entry_price - tp_price) / entry_price * 100 if tp_price > 0 else 0
        else:
            sl_dist = 0
            tp_dist = 0
        
        btc_change = self._get_btc_change()
        btc_status = "上涨" if btc_change > 0.005 else "下跌" if btc_change < -0.005 else "横盘"
        
        prompt = f"""## 持仓审核

### 持仓信息
- 币种: {symbol}
- 方向: {side.upper()}
- 入场价: ${entry_price:.6f}
- 当前价: ${current_price:.6f}
- 盈亏: {pnl_pct*100:+.2f}%
- 持仓: {holding_minutes:.0f}分钟

### 止损止盈
- 止损: ${sl_price:.6f} ({sl_dist:.2f}%)
- 止盈: ${tp_price:.6f} ({tp_dist:.2f}%)

### 市场状态
- RSI: {rsi:.1f}
- 成交量: {volume_ratio:.2f}x
- BTC: {btc_status} ({btc_change*100:+.2f}%)

### 判断要点
1. 方向是否正确？
2. 是否需要调整止损止盈？
3. 是否应该提前离场？

### 决策选项
- **hold**: 继续持有
- **close**: 准备平仓（设紧止损）
- **tighten_sl**: 收紧止损
- **extend_tp**: 扩大止盈
- **breakeven**: 移动到成本价

### 返回JSON:
```json
{{
    "action": "hold"/"close"/"tighten_sl"/"extend_tp"/"breakeven",
    "new_sl_price": 新止损价(仅调整时需要),
    "new_tp_price": 新止盈价(仅调整时需要),
    "reasoning": "15字以内理由",
    "urgency": "low"/"medium"/"high"
}}
```

⚠️ 注意：
- 盈利<1%时不建议breakeven
- close会转为紧止损{self.tight_sl_pct*100:.1f}%
只返回JSON！
"""
        return prompt
    
    def _convert_close_to_tight_sl(self, position: Dict, result: Dict) -> Dict:
        """将平仓决策转换为紧止损"""
        current_price = position.get("current_price", 0)
        side = position.get("side", "long")
        
        if side == "long":
            new_sl = current_price * (1 - self.tight_sl_pct)
        else:
            new_sl = current_price * (1 + self.tight_sl_pct)
        
        result["action"] = PositionAction.TIGHTEN_SL.value
        result["new_sl_price"] = new_sl
        result["reasoning"] = f"平仓→紧止损{self.tight_sl_pct*100:.1f}%"
        result["_original_action"] = "close"
        
        print(f"[POSITION_REVIEW] 🔄 平仓转紧止损: ${new_sl:.6f}")
        
        return result
    
    def _get_btc_change(self) -> float:
        """获取BTC近期价格变化"""
        try:
            now = datetime.now()
            if self._btc_cache_time and (now - self._btc_cache_time).total_seconds() < 60:
                current = self._btc_price_cache.get("current", 0)
                prev = self._btc_price_cache.get("prev", 0)
                if prev > 0:
                    return (current - prev) / prev
            
            ohlcv = self.exchange.fetch_ohlcv("BTC/USDT:USDT", "5m", limit=6)
            if ohlcv and len(ohlcv) >= 6:
                current = ohlcv[-1][4]
                prev = ohlcv[-6][4]
                
                self._btc_price_cache = {"current": current, "prev": prev}
                self._btc_cache_time = now
                
                return (current - prev) / prev
        except Exception as e:
            pass
        
        return 0.0
    
    def get_current_indicators(self, symbol: str) -> Dict:
        """获取当前市场指标"""
        try:
            ohlcv = self.exchange.fetch_ohlcv(symbol, "1m", limit=100)
            if not ohlcv or len(ohlcv) < 60:
                return {"current_price": 0, "rsi": 50, "volume_ratio": 1.0}
            
            df = pd.DataFrame(ohlcv, columns=["ts", "open", "high", "low", "close", "volume"])
            
            current_price = float(df["close"].iloc[-1])
            
            # RSI
            delta = df["close"].diff()
            gain = delta.where(delta > 0, 0).rolling(14).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
            rs = gain / loss
            rsi_val = float(100 - (100 / (1 + rs)).iloc[-1])
            if math.isnan(rsi_val):
                rsi_val = 50
            
            # 成交量
            vol_ma = df["volume"].rolling(20).mean().iloc[-1]
            vol_last = df["volume"].iloc[-1]
            volume_ratio = float(vol_last / vol_ma) if vol_ma > 0 else 1.0
            
            return {
                "current_price": current_price,
                "rsi": rsi_val,
                "volume_ratio": volume_ratio
            }
            
        except Exception as e:
            print(f"[POSITION_REVIEW] ⚠️ 获取指标失败: {e}")
            return {"current_price": 0, "rsi": 50, "volume_ratio": 1.0}
    
    @staticmethod
    def _parse_json_response(content: str) -> Optional[Dict]:
        """解析JSON响应"""
        import re
        try:
            return json.loads(content)
        except:
            pass
        
        json_match = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', content)
        if json_match:
            try:
                return json.loads(json_match.group(1))
            except:
                pass
        
        try:
            start = content.find('{')
            end = content.rfind('}')
            if start != -1 and end != -1:
                return json.loads(content[start:end+1])
        except:
            pass
        
        return None
    
    def clear_review_cache(self, symbol: str = None):
        """清除审核缓存"""
        if symbol:
            self._last_review_time.pop(symbol, None)
        else:
            self._last_review_time.clear()