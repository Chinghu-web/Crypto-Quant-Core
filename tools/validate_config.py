#!/usr/bin/env python3
"""
配置验证脚本
检查config.yaml所有关键配置项
"""

import yaml
import sys
from typing import Dict, List, Tuple

def load_config() -> Dict:
    """加载配置文件"""
    try:
        with open('config.yaml', 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        print("❌ 未找到config.yaml文件")
        sys.exit(1)
    except Exception as e:
        print(f"❌ 加载配置失败: {e}")
        sys.exit(1)

def check_value(config: Dict, path: str, expected_type=None, required=True) -> Tuple[bool, str]:
    """检查配置值"""
    keys = path.split('.')
    value = config
    
    try:
        for key in keys:
            value = value[key]
        
        if required and value is None:
            return False, f"❌ {path}: 未设置"
        
        if expected_type and not isinstance(value, expected_type):
            return False, f"⚠️ {path}: 类型错误（期望{expected_type.__name__}，实际{type(value).__name__}）"
        
        return True, f"✅ {path}: {value}"
    
    except (KeyError, TypeError):
        if required:
            return False, f"❌ {path}: 缺失"
        else:
            return True, f"⚠️ {path}: 缺失（可选）"

def main():
    print("="*60)
    print("🔍 配置验证")
    print("="*60)
    print()
    
    config = load_config()
    issues = []
    warnings = []
    
    # 1. 基础配置
    print("📋 基础配置:")
    checks = [
        ('exchange.name', str, True),
        ('exchange.market_type', str, True),
        ('exchange.timeframe', str, True),
    ]
    
    for path, expected_type, required in checks:
        ok, msg = check_value(config, path, expected_type, required)
        print(f"  {msg}")
        if not ok:
            if required:
                issues.append(msg)
            else:
                warnings.append(msg)
    print()
    
    # 2. 观察系统
    print("⏳ 观察系统:")
    watch_enabled = config.get('watch', {}).get('enabled', False)
    print(f"  {'✅' if watch_enabled else '❌'} enabled: {watch_enabled}")
    
    if watch_enabled:
        checks = [
            ('watch.expire_minutes', (int, float), True),
            ('watch.check_interval_seconds', (int, float), True),
            ('watch.timing_ai', str, True),
        ]
        
        for path, expected_type, required in checks:
            ok, msg = check_value(config, path, expected_type, required)
            print(f"  {msg}")
            if not ok:
                issues.append(msg)
    print()
    
    # 3. 自动交易
    print("🤖 自动交易:")
    auto_trading = config.get('auto_trading', {})
    auto_enabled = auto_trading.get('enabled', False)
    print(f"  {'✅' if auto_enabled else '❌'} enabled: {auto_enabled}")
    
    if auto_enabled:
        okx_config = auto_trading.get('okx', {})
        has_key = bool(okx_config.get('api_key'))
        has_secret = bool(okx_config.get('secret'))
        has_pass = bool(okx_config.get('passphrase'))
        testnet = okx_config.get('testnet', False)
        
        print(f"  {'✅' if has_key else '❌'} API Key: {'已设置' if has_key else '未设置'}")
        print(f"  {'✅' if has_secret else '❌'} Secret: {'已设置' if has_secret else '未设置'}")
        print(f"  {'✅' if has_pass else '❌'} Passphrase: {'已设置' if has_pass else '未设置'}")
        print(f"  {'🧪' if testnet else '💰'} 模式: {'测试网' if testnet else '实盘'}")
        
        if not all([has_key, has_secret, has_pass]):
            issues.append("自动交易已启用但API凭证不完整")
        
        # 资金管理
        capital = auto_trading.get('capital', {})
        total = capital.get('total_usdt', 0)
        max_pos = capital.get('max_position_usdt', 0)
        
        print(f"  💰 总资金: ${total}")
        print(f"  📊 最大单笔: ${max_pos}")
        
        if total < max_pos:
            warnings.append("最大单笔仓位 > 总资金")
    print()
    
    # 4. AI审核
    print("🤖 AI审核:")
    
    # Claude
    claude_key = config.get('claude', {}).get('api_key')
    print(f"  {'✅' if claude_key else '❌'} Claude API Key: {'已设置' if claude_key else '未设置'}")
    
    if not claude_key:
        issues.append("Claude API Key未设置（必需）")
    
    # DeepSeek
    deepseek = config.get('deepseek', {})
    deepseek_enabled = deepseek.get('enabled', False)
    deepseek_key = deepseek.get('api_key')
    
    print(f"  {'✅' if deepseek_enabled else '❌'} DeepSeek: {'启用' if deepseek_enabled else '禁用'}")
    if deepseek_enabled:
        print(f"  {'✅' if deepseek_key else '❌'} DeepSeek API Key: {'已设置' if deepseek_key else '未设置'}")
        if not deepseek_key:
            warnings.append("DeepSeek已启用但API Key未设置")
    print()
    
    # 5. 信号阈值
    print("📊 信号阈值:")
    push_threshold = config.get('push', {}).get('thresholds', {}).get('majors', 0)
    review_min_score = config.get('review', {}).get('hard_rules', {}).get('min_score', 0)
    
    print(f"  📈 推送阈值: {push_threshold}")
    print(f"  🔍 审核最低分: {review_min_score}")
    
    if push_threshold > review_min_score:
        warnings.append(f"推送阈值({push_threshold}) > 审核最低分({review_min_score})，可能导致无信号")
    
    if push_threshold < 0.7:
        warnings.append(f"推送阈值({push_threshold})较低，信号量可能过多")
    
    if review_min_score > 0.95:
        warnings.append(f"审核最低分({review_min_score})过高，信号量可能极少")
    print()
    
    # 6. 双AI模式检查
    print("🔄 双AI模式:")
    both_enabled = claude_key and deepseek_enabled and deepseek_key
    print(f"  {'✅' if both_enabled else '❌'} 状态: {'启用' if both_enabled else '仅Claude'}")
    
    if both_enabled:
        print(f"  ℹ️  使用AND逻辑：双AI必须都通过")
        print(f"  ℹ️  预期信号量较低（高质量）")
    else:
        print(f"  ℹ️  使用单AI：仅Claude审核")
        print(f"  ℹ️  预期信号量适中")
    print()
    
    # 7. Telegram
    print("📱 Telegram:")
    tg_token = config.get('telegram', {}).get('bot_token')
    tg_chat = config.get('telegram', {}).get('chat_id')
    
    print(f"  {'✅' if tg_token else '❌'} Bot Token: {'已设置' if tg_token else '未设置'}")
    print(f"  {'✅' if tg_chat else '❌'} Chat ID: {'已设置' if tg_chat else '未设置'}")
    
    if not tg_token or not tg_chat:
        warnings.append("Telegram未配置，将无法接收通知")
    print()
    
    # 8. 总结
    print("="*60)
    if not issues:
        print("✅ 所有关键配置正常")
    else:
        print(f"❌ 发现 {len(issues)} 个问题:")
        for issue in issues:
            print(f"  • {issue}")
    
    if warnings:
        print(f"\n⚠️ 发现 {len(warnings)} 个警告:")
        for warning in warnings:
            print(f"  • {warning}")
    print("="*60)
    
    # 9. 建议
    print("\n💡 配置建议:")
    
    if not both_enabled:
        print("  • 考虑启用DeepSeek进行双AI审核（提高质量）")
    
    if push_threshold < 0.80:
        print("  • 考虑提高推送阈值到0.80-0.85（减少噪音）")
    
    if not watch_enabled:
        print("  • 建议启用观察系统（提高入场时机）")
    
    if auto_enabled and not testnet:
        print("  ⚠️ 当前为实盘模式，请确认配置正确！")
    
    print()
    
    sys.exit(0 if not issues else 1)

if __name__ == "__main__":
    main()
