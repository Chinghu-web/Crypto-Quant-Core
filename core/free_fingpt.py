# core/free_fingpt.py - 免费FinGPT情绪分析（CoinGecko版）
# 修改: 1) 技术指标由主系统提供 2) 社交数据用CoinGecko 3) 20分钟批量更新

import requests
import json
import os
import threading
from typing import Dict, Set, List
from datetime import datetime, timedelta, timezone
import time

class FreeFinGPT:
    """
    免费FinGPT - 市场情绪分析（CoinGecko版）
    
    核心改进:
    1. 技术指标：由主系统传入（从交易所获取）
    2. 社交数据：CoinGecko API（免费）
    3. 批量更新：20分钟更新一次，单次API调用获取60个币种
    4. 恐惧贪婪：日缓存
    
    API 调用频率:
    - CoinGecko: 每20分钟1次批量调用（60个币种）
    - 每小时3次，每天72次 < 免费额度 ✅
    - Alternative.me: 每天1次
    """
    
    # CoinGecko ID 映射表（扩展版 - 支持60+币种）
    SYMBOL_TO_COINGECKO_ID = {
        "BTC": "bitcoin",
        "ETH": "ethereum",
        "BNB": "binancecoin",
        "XRP": "ripple",
        "SOL": "solana",
        "DOGE": "dogecoin",
        "ADA": "cardano",
        "LINK": "chainlink",
        "AVAX": "avalanche-2",
        "DOT": "polkadot",
        "MATIC": "matic-network",
        "UNI": "uniswap",
        "LTC": "litecoin",
        "ATOM": "cosmos",
        "TRX": "tron",
        "NEAR": "near",
        "APT": "aptos",
        "ARB": "arbitrum",
        "OP": "optimism",
        "SUI": "sui",
        "TON": "the-open-network",
        "ICP": "internet-computer",
        "FIL": "filecoin",
        "HBAR": "hedera-hashgraph",
        "IMX": "immutable-x",
        "VET": "vechain",
        "INJ": "injective-protocol",
        "MKR": "maker",
        "AAVE": "aave",
        "GRT": "the-graph",
        "ALGO": "algorand",
        "SAND": "the-sandbox",
        "MANA": "decentraland",
        "AXS": "axie-infinity",
        "ETC": "ethereum-classic",
        "XLM": "stellar",
        "BCH": "bitcoin-cash",
        "FTM": "fantom",
        "THETA": "theta-token",
        "EOS": "eos",
        "EGLD": "elrond-erd-2",
        "ZEC": "zcash",
        "FLOW": "flow",
        "XTZ": "tezos",
        "KAVA": "kava",
        "LUNA": "terra-luna-2",
        "BSV": "bitcoin-sv",
        "NEO": "neo",
        "DASH": "dash",
        "WAVES": "waves",
        "ZIL": "zilliqa",
        "CHZ": "chiliz",
        "ENJ": "enjincoin",
        "CRV": "curve-dao-token",
        "COMP": "compound-governance-token",
        "SNX": "synthetix-network-token",
        "1INCH": "1inch",
        "BAT": "basic-attention-token",
        "WIF": "dogwifcoin",
        "BONK": "bonk",
        "1000BONK": "bonk",  # 同 BONK
        "PENGU": "pengu",
        "PUMP": "pump",
        "MLN": "melon",
        "ASTR": "astar",
        "ASTER": "astar",
        "COAI": "coai",
        "RVV": "revolutionvr",
        "XPIN": "xpin",
        "LIGHT": "lightning-bitcoin",
        "ALPACA": "alpaca-finance",
        "EVAA": "evaa"
    }
    
    def __init__(self, coingecko_api_key: str = "", config: Dict = None):
        self.cg_api_key = coingecko_api_key or os.getenv("COINGECKO_API_KEY", "")
        self.cg_base = "https://api.coingecko.com/api/v3"
        
        # 🆕 从配置读取更新间隔（默认10分钟）
        cg_cfg = config.get("coingecko", {}) if config else {}
        update_interval_minutes = cg_cfg.get("update_interval_minutes", 10)
        
        # 🆕 社交情绪数据缓存（可配置有效期）
        self.sentiment_cache = {}
        # 格式: {'BTC/USDT': {'data': {...}, 'expires_at': datetime}}
        
        # 恐惧贪婪指数日缓存
        self.fear_greed_cache = None
        self.fear_greed_date = None
        self.cache_dir = "data/cache"
        os.makedirs(self.cache_dir, exist_ok=True)
        
        # 🔥 滚动窗口机制
        self.registered_symbols = set()  # 当前周期需要的币种
        self.update_thread = None
        self.is_running = True
        self.lock = threading.Lock()
        
        # 配置（可通过config.yaml调整）
        self.cache_ttl = timedelta(minutes=update_interval_minutes)  # 缓存时长与更新间隔一致
        self.update_interval = update_interval_minutes * 60  # 转换为秒
        
        print(f"[FINGPT] ✅ CoinGecko API已配置 (API Key: {'有' if self.cg_api_key else '无'})")
        print(f"[FINGPT] 更新间隔: {update_interval_minutes}分钟 (滚动窗口模式)")
        print("[FINGPT] 初始化完成（技术指标由主系统提供，社交数据用CoinGecko）")
    
    def start_background_update(self):
        """🆕 启动后台批量更新任务"""
        if self.update_thread is not None:
            print("[FINGPT] 后台任务已在运行")
            return
        
        self.update_thread = threading.Thread(
            target=self._update_worker,
            daemon=True,
            name="FinGPT-CoinGecko-Updater"
        )
        self.update_thread.start()
        print(f"[FINGPT] 后台CoinGecko批量更新任务已启动（每{self.update_interval//60}分钟更新一次）")
    
    def stop(self):
        """停止后台任务"""
        self.is_running = False
        if self.update_thread:
            self.update_thread.join(timeout=5)
        print("[FINGPT] 后台任务已停止")
    
    def clear_old_registrations(self):
        """
        🔥 清理旧的币种注册（滚动窗口机制）
        主循环每个周期开始时调用，确保只更新当前需要的币种
        """
        with self.lock:
            old_count = len(self.registered_symbols)
            self.registered_symbols.clear()
            if old_count > 0:
                print(f"[FINGPT] 清理旧注册: {old_count}个币种，准备接受新周期注册")
    
    def _cleanup_unused_cache(self, current_symbols: List[str]):
        """
        🔥 清理不再需要的币种缓存
        删除不在当前币种列表中的缓存数据
        """
        with self.lock:
            all_cached = list(self.sentiment_cache.keys())
            removed_count = 0
            
            for symbol in all_cached:
                if symbol not in current_symbols:
                    del self.sentiment_cache[symbol]
                    removed_count += 1
            
            if removed_count > 0:
                print(f"[FINGPT] 清理缓存: 删除{removed_count}个不再需要的币种")
    
    def _update_worker(self):
        """🔥 后台批量更新工作线程（滚动窗口模式）"""
        # 首次启动延迟 30 秒（等待主循环注册币种）
        time.sleep(30)
        print(f"[FINGPT] CoinGecko批量更新任务开始运行（间隔{self.update_interval//60}分钟，滚动窗口）...")
        
        while self.is_running:
            try:
                # 🔥 获取当前注册的币种（不累积）
                with self.lock:
                    current_symbols = list(self.registered_symbols)
                
                if not current_symbols:
                    print(f"[FINGPT] 无币种需要更新，等待{self.update_interval//60}分钟...")
                    time.sleep(self.update_interval)
                    continue
                
                print(f"\n[FINGPT] 📊 开始更新 {len(current_symbols)} 个币种的社交数据（滚动窗口）...")
                
                # 🎯 批量更新 CoinGecko 数据
                try:
                    updated = self._batch_update_coingecko(current_symbols)
                    print(f"[FINGPT] ✅ 更新完成，成功更新 {updated}/{len(current_symbols)} 个币种")
                except Exception as e:
                    print(f"[FINGPT] ⚠️ 更新失败: {e}")
                
                # 🔥 清理不再需要的缓存
                self._cleanup_unused_cache(current_symbols)
                
                print(f"[FINGPT] 下次更新将在 {self.update_interval//60} 分钟后执行...\n")
                
                # 等待指定时间
                time.sleep(self.update_interval)
                
            except Exception as e:
                print(f"[FINGPT] ⚠️ 批量更新任务出错: {e}")
                time.sleep(60)
    
    def _batch_update_coingecko(self, symbols: List[str]) -> int:
        """🎯 批量更新CoinGecko数据（一次API调用获取多个币种）"""
        
        # 构建 CoinGecko ID 列表
        coin_ids = []
        id_to_symbol = {}
        
        for symbol in symbols:
            # 提取币种符号 (例如: BTC/USDT -> BTC)
            coin_symbol = symbol.split('/')[0].upper()
            cg_id = self.SYMBOL_TO_COINGECKO_ID.get(coin_symbol)
            
            if cg_id:
                coin_ids.append(cg_id)
                id_to_symbol[cg_id] = symbol
        
        if not coin_ids:
            print("[FINGPT] ⚠️ 没有找到有效的 CoinGecko ID")
            return 0
        
        # 构建API请求 (批量获取)
        url = f"{self.cg_base}/coins/markets"
        params = {
            "vs_currency": "usd",
            "ids": ",".join(coin_ids),
            "order": "market_cap_desc",
            "per_page": len(coin_ids),
            "page": 1,
            "sparkline": False,
            "price_change_percentage": "24h,7d"
        }
        
        headers = {}
        if self.cg_api_key:
            headers["x-cg-demo-api-key"] = self.cg_api_key
        
        try:
            response = requests.get(url, params=params, headers=headers, timeout=15)
            
            if response.status_code == 429:
                print("[FINGPT] ⚠️ CoinGecko API限流，使用缓存数据")
                return 0
            
            if response.status_code != 200:
                print(f"[FINGPT] ⚠️ CoinGecko API返回错误码: {response.status_code}")
                return 0
            
            data = response.json()
            updated_count = 0
            
            # 解析返回的数据
            for coin in data:
                coin_id = coin.get("id", "")
                symbol = id_to_symbol.get(coin_id)
                
                if not symbol:
                    continue
                
                # 提取CoinGecko数据
                sentiment_data = {
                    'market_cap_rank': coin.get('market_cap_rank', 999),
                    'price_change_24h': coin.get('price_change_percentage_24h', 0),
                    'price_change_7d': coin.get('price_change_percentage_7d', 0),
                    'market_cap': coin.get('market_cap', 0),
                    'total_volume': coin.get('total_volume', 0),
                    'circulating_supply': coin.get('circulating_supply', 0),
                    'current_price': coin.get('current_price', 0),
                    'score': self._calculate_sentiment_from_cg(coin)
                }
                
                with self.lock:
                    self.sentiment_cache[symbol] = {
                        'data': sentiment_data,
                        'expires_at': datetime.now() + self.cache_ttl
                    }
                
                updated_count += 1
            
            print(f"[FINGPT] 📥 从CoinGecko获取了 {len(data)} 个币种的数据")
            return updated_count
            
        except Exception as e:
            print(f"[FINGPT] ⚠️ CoinGecko批量更新失败: {e}")
            return 0
    
    def _calculate_sentiment_from_cg(self, coin: Dict) -> float:
        """🆕 根据CoinGecko数据计算情绪得分 [0-100]"""
        
        score = 50.0  # 中性起点
        
        # 1. 市值排名（权重30%）
        rank = coin.get('market_cap_rank')
        if rank is None:
            rank = 999  # 默认排名
        
        if rank <= 10:
            score += 15
        elif rank <= 30:
            score += 10
        elif rank <= 50:
            score += 5
        elif rank > 200:
            score -= 10
        
        # 2. 24小时价格变化（权重40%）
        change_24h = coin.get('price_change_percentage_24h', 0)
        if change_24h:
            # 价格变化 [-100%, +100%] 映射到 [-20, +20]
            score += max(-20, min(20, change_24h * 0.5))
        
        # 3. 7天价格变化（权重30%）
        change_7d = coin.get('price_change_percentage_7d', 0)
        if change_7d:
            # 7天变化权重较低
            score += max(-15, min(15, change_7d * 0.3))
        
        # 确保分数在 [0, 100] 范围内
        return max(0, min(100, score))
    
    def register_symbol(self, symbol: str):
        """🆕 注册需要监控的币种"""
        with self.lock:
            self.registered_symbols.add(symbol)
    
    def analyze(self, symbol: str, tech_indicators: Dict) -> Dict:
        """
        情绪分析主入口
        
        Args:
            symbol: 交易对 (如 "BTC/USDT")
            tech_indicators: 技术指标字典
                - rsi: RSI值
                - macd_cross: MACD交叉状态 ('golden', 'death', 'none')
                - bb_position: 布林带位置
                - vol_spike_ratio: 成交量倍数
                - adx: ADX值
        
        Returns:
            情绪分析结果字典
        """
        # 注册币种（如果还未注册）
        self.register_symbol(symbol)
        
        # 获取缓存的社交数据
        sentiment_data = self._get_cached_sentiment(symbol)
        
        # 转换技术指标为FinGPT格式
        technical_data = self._convert_tech_indicators(tech_indicators)
        
        # 生成综合摘要
        summary = self._generate_summary(sentiment_data, technical_data)
        
        return {
            'symbol': symbol,
            'sentiment': sentiment_data,
            'technical': technical_data,
            'summary': summary,
            'timestamp': datetime.now(timezone.utc).isoformat()
        }
    
    def _get_cached_sentiment(self, symbol: str) -> Dict:
        """🆕 获取缓存的情绪数据"""
        with self.lock:
            cached = self.sentiment_cache.get(symbol)
        
        # 如果有缓存且未过期
        if cached and datetime.now() < cached['expires_at']:
            data = cached['data']
            fear_greed = self._get_fear_greed_index_cached()
            
            # 计算综合情绪得分
            cg_score = data.get('score', 50)
            combined_score = self._calculate_combined_sentiment(fear_greed, cg_score)
            
            # 🔧 兼容 main.py 的字段格式（模拟 LunarCrush 返回格式）
            return {
                'score': combined_score,
                'fear_greed': fear_greed,
                'galaxy_score': cg_score,  # 用 CG评分 代替 Galaxy Score
                'alt_rank': data.get('market_cap_rank', 999),  # 用市值排名代替 AltRank
                'lc_sentiment': combined_score,  # 综合情绪
                'social_volume': 0,  # CoinGecko 没有社交量
                'social_dominance': 0,  # CoinGecko 没有社交占比
                'percent_change_24h': data.get('price_change_24h', 0),
                'data_freshness': 'fresh',
                'detail': f"恐惧贪婪{fear_greed}, 市值#{data.get('market_cap_rank', 999)}, CG评分{cg_score:.0f}"
            }
        
        # 如果没有缓存或已过期，返回默认值（等待更新）
        fear_greed = self._get_fear_greed_index_cached()
        return {
            'score': (fear_greed - 50) / 50,  # 转为 [-1, 1]
            'fear_greed': fear_greed,
            'galaxy_score': 50,  # 默认值
            'alt_rank': 999,  # 默认值
            'lc_sentiment': 0.5,
            'social_volume': 0,
            'social_dominance': 0,
            'percent_change_24h': 0,
            'data_freshness': 'waiting',
            'detail': f"恐惧贪婪{fear_greed}, 等待CoinGecko更新"
        }
    
    def _calculate_combined_sentiment(self, fear_greed: int, cg_score: float) -> float:
        """🆕 综合计算情绪得分（恐惧贪婪 + CoinGecko）"""
        # 恐惧贪婪: [0-100] -> [-1, 1]
        fg_score = (fear_greed - 50) / 50
        
        # CoinGecko评分: [0-100] -> [-1, 1]
        cg_normalized = (cg_score - 50) / 50
        
        # 加权平均：恐惧贪婪40%，CoinGecko 60%
        sentiment = fg_score * 0.4 + cg_normalized * 0.6
        
        return max(-1.0, min(1.0, sentiment))
    
    def _convert_tech_indicators(self, tech_indicators: Dict) -> Dict:
        """转换主系统的技术指标为 FinGPT 格式"""
        rsi = tech_indicators.get('rsi', 50.0)
        macd_cross = tech_indicators.get('macd_cross', 'none')
        bb_position = tech_indicators.get('bb_position', 0.0)
        vol_spike = tech_indicators.get('vol_spike_ratio', 1.0)
        
        # 计算综合评分
        score = 50
        
        if rsi < 30:
            score += 25
        elif rsi < 40:
            score += 15
        elif rsi > 70:
            score -= 25
        elif rsi > 60:
            score -= 15
        
        if macd_cross == 'golden':
            score += 20
        elif macd_cross == 'death':
            score -= 20
        
        if bb_position < -1.5:
            score += 15
        elif bb_position > 1.5:
            score -= 15
        
        if vol_spike > 1.5:
            score += 10
        
        score = max(0, min(100, score))
        
        # 判断信号
        if score > 70:
            signal = 'buy'
        elif score < 30:
            signal = 'sell'
        else:
            signal = 'hold'
        
        return {
            'signal': signal,
            'confidence': score,
            'rsi': float(rsi),
            'macd_signal': macd_cross,
            'bb_position': float(bb_position),
            'volume_spike': vol_spike > 1.5
        }
    
    def _get_fear_greed_index_cached(self) -> int:
        """获取恐惧贪婪指数（日缓存）"""
        today = datetime.now().date()
        
        # 检查内存缓存
        if self.fear_greed_cache is not None and self.fear_greed_date == today:
            return self.fear_greed_cache
        
        # 检查文件缓存
        cache_file = f"{self.cache_dir}/fear_greed_{today}.json"
        if os.path.exists(cache_file):
            try:
                with open(cache_file, 'r') as f:
                    data = json.load(f)
                    self.fear_greed_cache = data['value']
                    self.fear_greed_date = today
                    return self.fear_greed_cache
            except Exception:
                pass
        
        # 调用API
        try:
            response = requests.get("https://api.alternative.me/fng/", timeout=5)
            data = response.json()
            value = int(data['data'][0]['value'])
            
            self.fear_greed_cache = value
            self.fear_greed_date = today
            
            # 保存到文件
            try:
                with open(cache_file, 'w') as f:
                    json.dump({'value': value, 'date': str(today)}, f)
            except Exception:
                pass
            
            return value
            
        except Exception as e:
            print(f"[FINGPT] ⚠️ 恐惧贪婪指数获取失败: {e}")
            return self.fear_greed_cache if self.fear_greed_cache else 50
    
    def _generate_summary(self, sentiment_data, technical_data) -> str:
        """生成综合摘要"""
        sentiment_score = sentiment_data['score']
        
        if sentiment_score > 0.5:
            sentiment_desc = "极度积极"
        elif sentiment_score > 0.2:
            sentiment_desc = "偏积极"
        elif sentiment_score > -0.2:
            sentiment_desc = "中性"
        elif sentiment_score > -0.5:
            sentiment_desc = "偏消极"
        else:
            sentiment_desc = "极度消极"
        
        tech_signal = technical_data['signal']
        if tech_signal == 'buy':
            tech_desc = "技术面看多"
        elif tech_signal == 'sell':
            tech_desc = "技术面看空"
        else:
            tech_desc = "技术面中性"
        
        return f"市场情绪{sentiment_desc}，{tech_desc}"
    
    def _default_sentiment(self) -> Dict:
        """默认情绪数据"""
        return {
            'market_cap_rank': 999,
            'price_change_24h': 0,
            'price_change_7d': 0,
            'cg_score': 50,
            'score': 50
        }
    
    def get_cache_stats(self) -> Dict:
        """获取缓存统计"""
        with self.lock:
            total_symbols = len(self.registered_symbols)
            cached_symbols = len(self.sentiment_cache)
            
            valid_cache = sum(
                1 for entry in self.sentiment_cache.values()
                if datetime.now() < entry['expires_at']
            )
        
        return {
            'total_symbols': total_symbols,
            'cached_symbols': cached_symbols,
            'valid_cache': valid_cache,
            'cache_hit_rate': f"{valid_cache/cached_symbols*100:.1f}%" if cached_symbols > 0 else "0%",
            'next_update': f'{self.update_interval//60}分钟内'
        }


# ==================== 使用示例 ====================
if __name__ == "__main__":
    """测试CoinGecko版本"""
    
    # 从环境变量读取API Key（可选）
    cg_key = os.getenv("COINGECKO_API_KEY", "")
    
    fingpt = FreeFinGPT(coingecko_api_key=cg_key)
    
    # 启动后台批量更新
    fingpt.start_background_update()
    
    # 模拟主系统传入的技术指标（来自交易所）
    tech_indicators = {
        'rsi': 65.3,
        'macd_cross': 'golden',
        'bb_width': 0.015,
        'bb_position': 0.5,
        'vol_spike_ratio': 1.2,
        'adx': 45.0
    }
    
    # 测试分析
    symbols = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
    
    for symbol in symbols:
        print(f"\n{'='*60}")
        print(f"分析 {symbol}")
        print('='*60)
        
        result = fingpt.analyze(symbol, tech_indicators)
        
        print(f"\n📊 情绪分析:")
        print(f"  - 情绪得分: {result['sentiment']['score']:+.2f}")
        print(f"  - 恐惧贪婪: {result['sentiment']['fear_greed']}")
        print(f"  - 市值排名: #{result['sentiment']['market_cap_rank']}")
        print(f"  - 24h涨跌: {result['sentiment']['price_change_24h']:+.2f}%")
        print(f"  - 7d涨跌: {result['sentiment']['price_change_7d']:+.2f}%")
        
        print(f"\n🔧 技术分析:")
        print(f"  - 信号: {result['technical']['signal'].upper()}")
        print(f"  - RSI: {result['technical']['rsi']:.1f}")
        
        print(f"\n💡 综合: {result['summary']}")
        
        time.sleep(1)
    
    # 查看缓存统计
    print(f"\n{'='*60}")
    stats = fingpt.get_cache_stats()
    print(f"缓存统计: {stats}")
    
    # 等待30秒观察批量更新
    print("\n等待30秒观察批量更新...")
    time.sleep(30)
    
    fingpt.stop()
    print("✅ 测试完成！")