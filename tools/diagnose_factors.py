# diagnose_factors.py
import yaml
import ccxt
from core.utils import funding_score, oi_trend_score, orderbook_strength_fetch, llm_sentiment_score_symbol

# 加载配置
with open("config.yaml", "r", encoding="utf-8") as f:
    cfg = yaml.safe_load(f)

# 初始化交易所
ex = ccxt.binance({
    'enableRateLimit': True,
    'options': {'defaultType': 'future'}
})

test_symbol = "BTC/USDT:USDT"

print("="*60)
print("因子诊断报告")
print("="*60)

# 1. 资金费率
print("\n1️⃣ 资金费率 (Funding)")
print("-"*60)
try:
    score = funding_score(test_symbol, cfg)
    print(f"✅ 成功: score={score}")
    
    # 获取原始费率
    funding_symbol = cfg.get("funding", {}).get("symbol_map", {}).get(test_symbol, test_symbol)
    print(f"   Symbol映射: {test_symbol} -> {funding_symbol}")
    
    try:
        funding_data = ex.fetch_funding_rate(funding_symbol)
        print(f"   原始费率: {funding_data['fundingRate']*100:.4f}%")
        print(f"   下次结算: {funding_data.get('fundingTimestamp', 'unknown')}")
    except Exception as e:
        print(f"   ⚠️  无法获取原始费率: {e}")
        
except Exception as e:
    print(f"❌ 失败: {e}")
    import traceback
    traceback.print_exc()

# 2. OI未平仓量
print("\n2️⃣ OI未平仓量 (Open Interest)")
print("-"*60)
try:
    score = oi_trend_score(test_symbol, cfg)
    print(f"✅ 成功: score={score}")
    
    # 测试原始API
    oi_symbol = cfg.get("oi", {}).get("symbol_map", {}).get(test_symbol, test_symbol.replace("/", "").replace(":USDT", ""))
    print(f"   Symbol映射: {test_symbol} -> {oi_symbol}")
    
    try:
        oi_data = ex.fetch_open_interest(oi_symbol)
        print(f"   当前OI: {oi_data.get('openInterestAmount', 'N/A')}")
    except Exception as e:
        print(f"   ⚠️  无法获取原始OI: {e}")
        
except Exception as e:
    print(f"❌ 失败: {e}")
    import traceback
    traceback.print_exc()

# 3. 订单簿强度
print("\n3️⃣ 订单簿强度 (Orderbook)")
print("-"*60)
try:
    score = orderbook_strength_fetch(ex, test_symbol, limit=30)
    print(f"✅ 成功: score={score}")
    
    # 测试原始订单簿
    ob = ex.fetch_order_book(test_symbol, limit=10)
    print(f"   买单深度: {len(ob['bids'])} 档")
    print(f"   卖单深度: {len(ob['asks'])} 档")
    if ob['bids'] and ob['asks']:
        print(f"   最优买价: {ob['bids'][0][0]}")
        print(f"   最优卖价: {ob['asks'][0][0]}")
        
except Exception as e:
    print(f"❌ 失败: {e}")
    import traceback
    traceback.print_exc()

# 4. 情感分析
print("\n4️⃣ 情感分析 (Sentiment)")
print("-"*60)
try:
    result = llm_sentiment_score_symbol(test_symbol, cfg)
    print(f"✅ 成功: score={result.get('score', 0.5)}")
    print(f"   X新闻: {result.get('x', {}).get('n', 0)}条")
    print(f"   RSS新闻: {result.get('rss', {}).get('n', 0)}条")
    
    # 测试RSS源
    print("\n   测试RSS源:")
    import feedparser
    test_feeds = [
        "https://www.coindesk.com/arc/outboundfeeds/rss/",
        "https://cointelegraph.com/rss"
    ]
    for feed_url in test_feeds[:2]:
        try:
            d = feedparser.parse(feed_url)
            print(f"   - {feed_url}")
            print(f"     状态: {d.get('status', 'unknown')}")
            print(f"     文章: {len(d.entries)}篇")
        except Exception as e:
            print(f"   - {feed_url}: ❌ {e}")
            
except Exception as e:
    print(f"❌ 失败: {e}")
    import traceback
    traceback.print_exc()

print("\n" + "="*60)
print("诊断完成")
print("="*60)
