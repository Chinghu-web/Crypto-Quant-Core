# param_diagnostics.py - 参数诊断和手动调整建议（适配三档入场 + LLM微调架构）
import sqlite3
import pandas as pd
import yaml
import json
from typing import Dict, Any
from datetime import datetime, timedelta

class ParamDiagnostics:
    def __init__(self, cfg_path="config.yaml", db_path="./signals.db"):
        with open(cfg_path) as f:
            self.cfg = yaml.safe_load(f)
        self.db_path = db_path
        
        # 合约交易配置
        self.base_leverage = self.cfg.get("futures_trading", {}).get("base_leverage", 20)
        self.market_type = self.cfg.get("exchange", {}).get("market_type", "spot")
        
    def analyze_strategy(self, strategy: str, days: int = 30, horizon: int = 720):
        """分析单个策略的表现，给出调参建议"""
        conn = sqlite3.connect(self.db_path)
        
        # 获取历史数据（使用outcomes_multi表）
        query = f"""
            SELECT 
                s.id, s.ts, s.symbol, s.price, s.entry, s.tp, s.sl, s.score,
                s.bias, s.llm_json, s.rationale,
                o.horizon_12h_ret as ret,
                o.horizon_12h_hit_tp as hit_tp,
                o.horizon_12h_hit_sl as hit_sl,
                o.max_profit_pct as max_runup,
                o.max_loss_pct as max_drawdown
            FROM signals s
            LEFT JOIN outcomes_multi o ON s.id = o.signal_id
            WHERE s.category = '{strategy}'
            AND s.ts > datetime('now', '-{days} days')
            ORDER BY s.ts DESC
        """
        
        df = pd.read_sql(query, conn)
        conn.close()
        
        if df.empty or len(df) < 10:
            return {
                "strategy": strategy,
                "sample_count": len(df),
                "status": "数据不足",
                "message": f"仅有{len(df)}个样本，建议至少积累30个样本后再分析",
                "market_type": self.market_type,
                "leverage": self.base_leverage
            }
        
        # 计算关键指标
        stats = self._calculate_stats(df, strategy)
        
        # 诊断问题
        problems = self._diagnose_problems(df, stats, strategy)
        
        # 生成调参建议
        suggestions = self._generate_suggestions(problems, strategy)
        
        return {
            "strategy": strategy,
            "sample_count": len(df),
            "days_analyzed": days,
            "market_type": self.market_type,
            "base_leverage": self.base_leverage,
            "stats": stats,
            "problems": problems,
            "suggestions": suggestions,
            "current_params": self.cfg.get(strategy, {}).get('params', {})
        }
    
    def _calculate_stats(self, df: pd.DataFrame, strategy: str) -> Dict:
        """计算统计指标（考虑杠杆 + 三档入场）"""
        returns = df['ret'].dropna()
        hit_tp = df['hit_tp'].fillna(0)
        hit_sl = df['hit_sl'].fillna(0)
        max_runup = df['max_runup'].fillna(0)
        max_drawdown = df['max_drawdown'].fillna(0)
        
        total = len(df)
        win_count = hit_tp.sum()
        loss_count = hit_sl.sum()
        
        # 从LLM JSON中提取平均杠杆和三档使用情况
        leverages = []
        entry_types = {'aggressive': 0, 'moderate': 0, 'conservative': 0}
        
        for llm_json_str in df['llm_json'].dropna():
            try:
                llm_data = json.loads(llm_json_str)
                leverages.append(llm_data.get('recommended_leverage', self.base_leverage))
                
                # 统计哪个档位被使用了（如果有记录）
                entry_used = llm_data.get('entry_type_used', 'moderate')
                if entry_used in entry_types:
                    entry_types[entry_used] += 1
                    
            except:
                leverages.append(self.base_leverage)
        
        avg_leverage = sum(leverages) / len(leverages) if leverages else self.base_leverage
        
        # 计算实际收益（考虑杠杆）
        actual_returns = returns * avg_leverage if self.market_type == "future" else returns
        
        # LLM调整统计
        llm_adjustments = {'sl_adjusted': 0, 'tp_adjusted': 0, 'lev_adjusted': 0}
        for llm_json_str in df['llm_json'].dropna():
            try:
                llm_data = json.loads(llm_json_str)
                adj = llm_data.get('_adjustments', {})
                if 'sl_adjust_pct' in adj and adj['sl_adjust_pct'] != 0:
                    llm_adjustments['sl_adjusted'] += 1
                if 'tp_adjust_pct' in adj and adj['tp_adjust_pct'] != 0:
                    llm_adjustments['tp_adjusted'] += 1
                if 'leverage_adjust' in adj and adj['leverage_adjust'] != 0:
                    llm_adjustments['lev_adjusted'] += 1
            except:
                pass
        
        return {
            "total_signals": total,
            "win_count": int(win_count),
            "loss_count": int(loss_count),
            "win_rate": win_count / max(1, win_count + loss_count),
            "avg_return": float(returns.mean()),
            "avg_return_with_leverage": float(actual_returns.mean()),
            "avg_leverage": float(avg_leverage),
            "avg_win": float(returns[returns > 0].mean()) if len(returns[returns > 0]) > 0 else 0,
            "avg_loss": float(returns[returns < 0].mean()) if len(returns[returns < 0]) > 0 else 0,
            "profit_factor": abs(returns[returns > 0].sum() / returns[returns < 0].sum()) if returns[returns < 0].sum() != 0 else 0,
            "max_runup_avg": float(max_runup.mean()),
            "max_drawdown_avg": float(max_drawdown.mean()),
            "sharpe_ratio": float(actual_returns.mean() / actual_returns.std()) if actual_returns.std() > 0 else 0,
            "max_consecutive_losses": self._max_consecutive(hit_sl),
            "max_consecutive_wins": self._max_consecutive(hit_tp),
            "entry_distribution": entry_types,
            "llm_adjustment_rate": {
                "sl": llm_adjustments['sl_adjusted'] / max(1, total),
                "tp": llm_adjustments['tp_adjusted'] / max(1, total),
                "leverage": llm_adjustments['lev_adjusted'] / max(1, total)
            }
        }
    
    def _max_consecutive(self, series):
        """计算最大连续次数"""
        max_count = 0
        current_count = 0
        for val in series:
            if val == 1:
                current_count += 1
                max_count = max(max_count, current_count)
            else:
                current_count = 0
        return int(max_count)
    
    def _diagnose_problems(self, df: pd.DataFrame, stats: Dict, strategy: str) -> list:
        """诊断具体问题（适配新架构）"""
        problems = []
        current_params = self.cfg.get(strategy, {}).get('params', {})
        leverage = stats['avg_leverage']
        
        # 问题1：胜率太低
        if stats['win_rate'] < 0.45:
            problems.append({
                "type": "低胜率",
                "severity": "高",
                "value": f"{stats['win_rate']:.1%}",
                "description": f"胜率低于45%，{leverage:.0f}倍杠杆下风险极高",
                "possible_causes": [
                    "LLM三档设置可能过于激进",
                    f"当前min_score={self.cfg.get(strategy, {}).get('min_score', 0.70)}可能太低",
                    "adaptive_stops计算的止盈止损可能不合理",
                    "市场环境不适合当前策略"
                ]
            })
        
        # 问题2：止盈过远（基于系统计算）
        avg_runup = stats['max_runup_avg']
        
        if avg_runup < 0.015:  # 平均浮盈连1.5%都达不到
            problems.append({
                "type": "止盈目标过高",
                "severity": "中",
                "value": f"平均最大浮盈{avg_runup:.2%}",
                "description": "价格很少能达到止盈位，建议检查adaptive_stops算法",
                "possible_causes": [
                    "adaptive_stops的TP倍数设置过大",
                    "合约市场波动可能不足以达到目标",
                    "LLM很少向下调整TP（过于乐观）"
                ]
            })
        
        # 问题3：止损过紧
        sl_hit_rate = stats['loss_count'] / max(1, stats['total_signals'])
        if sl_hit_rate > 0.30:
            problems.append({
                "type": "止损触发过频",
                "severity": "高",
                "value": f"止损触发率{sl_hit_rate:.1%}",
                "description": f"频繁触发止损，{leverage:.0f}倍杠杆下每次止损实际亏损{stats['avg_loss']*leverage:.1%}",
                "possible_causes": [
                    "adaptive_stops的SL计算过于保守",
                    "LLM倾向于收紧止损（应该放宽）",
                    "市场噪音大，需要更大的止损空间"
                ]
            })
        
        # 问题4：盈亏比差
        if stats['profit_factor'] < 1.5 and stats['profit_factor'] > 0:
            problems.append({
                "type": "盈亏比不佳",
                "severity": "高",
                "value": f"{stats['profit_factor']:.2f}",
                "description": f"{leverage:.0f}倍杠杆下必须有至少1.5的盈亏比才能长期盈利",
                "possible_causes": [
                    "adaptive_stops计算的TP/SL比例不合理",
                    "LLM调整方向可能有问题",
                    "入场时机不够精确"
                ]
            })
        
        # 问题5：LLM调整异常
        llm_adj = stats['llm_adjustment_rate']
        if llm_adj['sl'] < 0.1 and llm_adj['tp'] < 0.1:
            problems.append({
                "type": "LLM调整率过低",
                "severity": "中",
                "value": f"SL:{llm_adj['sl']:.1%} TP:{llm_adj['tp']:.1%}",
                "description": "LLM几乎不调整止盈止损，可能失去了微调的意义",
                "possible_causes": [
                    "LLM Prompt可能需要优化，鼓励更多调整",
                    "系统计算的默认值已经很合理",
                    "LLM过于保守"
                ]
            })
        
        # 问题6：三档使用不均衡
        entry_dist = stats['entry_distribution']
        total_entries = sum(entry_dist.values())
        if total_entries > 0:
            moderate_pct = entry_dist.get('moderate', 0) / total_entries
            if moderate_pct < 0.5:  # 适中档应该是主力
                problems.append({
                    "type": "三档使用失衡",
                    "severity": "低",
                    "value": f"激进:{entry_dist.get('aggressive', 0)} 适中:{entry_dist.get('moderate', 0)} 保守:{entry_dist.get('conservative', 0)}",
                    "description": "适中档使用率过低，可能LLM三档设置有问题",
                    "possible_causes": [
                        "LLM的三档ATR倍数设置不合理",
                        "市场行情导致某些档位很少成交",
                        "建议检查LLM返回的三档价格"
                    ]
                })
        
        # 问题7：连续亏损风险
        if stats['max_consecutive_losses'] >= 5:
            problems.append({
                "type": "连续亏损风险",
                "severity": "高",
                "value": f"最大连续亏损{stats['max_consecutive_losses']}次",
                "description": f"{leverage:.0f}倍杠杆下连续5次止损可能导致严重亏损",
                "possible_causes": [
                    "策略在某些市场环境下完全失效",
                    "需要增加市场环境过滤",
                    "考虑添加每日最大亏损限制"
                ]
            })
        
        # 问题8：强平风险检查（合约专属）
        if self.market_type == "future":
            liquidation_distance = 1.0 / leverage
            # 从adaptive_stops获取平均止损距离
            avg_sl_distance = abs(stats['avg_loss'])
            
            if avg_sl_distance > liquidation_distance * 0.5:
                problems.append({
                    "type": "强平风险",
                    "severity": "极高",
                    "value": f"止损约{avg_sl_distance:.1%}, 强平距离{liquidation_distance:.1%}",
                    "description": f"止损距离太接近强平距离，{leverage:.0f}倍杠杆下极度危险",
                    "possible_causes": [
                        "杠杆倍数过高",
                        "adaptive_stops计算的止损距离不合理",
                        f"建议降低杠杆或收紧止损到{liquidation_distance * 0.4:.1%}"
                    ]
                })
        
        # 问题9：异动策略专属 - 假突破率高
        if strategy == "anomaly":
            broke_signals = df[df['rationale'].str.contains('broke_high|broke_low', na=False)]
            if len(broke_signals) > 5:
                broke_win_rate = broke_signals['hit_tp'].sum() / max(1, len(broke_signals))
                if broke_win_rate < 0.40:
                    problems.append({
                        "type": "假突破率高",
                        "severity": "中",
                        "value": f"突破信号胜率{broke_win_rate:.1%}",
                        "description": "突破后经常回撤，可能是假突破",
                        "possible_causes": [
                            f"spike_ratio={current_params.get('spike_ratio', 3.0)}太低",
                            "突破确认不够充分",
                            f"建议提高到{min(5.0, current_params.get('spike_ratio', 3.0) + 0.5)}"
                        ]
                    })
        
        return problems
    
    def _generate_suggestions(self, problems: list, strategy: str) -> list:
        """根据问题生成具体的调参建议（适配新架构）"""
        suggestions = []
        current_params = self.cfg.get(strategy, {}).get('params', {})
        
        for problem in problems:
            if problem['type'] == "低胜率":
                current_score = self.cfg.get(strategy, {}).get('min_score', 0.70)
                new_score = min(0.90, current_score + 0.05)
                suggestions.append({
                    "problem": "低胜率",
                    "action": "提高信号质量阈值",
                    "param": "min_score",
                    "current": current_score,
                    "suggested": new_score,
                    "reason": "合约交易下，必须只接受最高质量的信号",
                    "priority": "高"
                })
            
            elif problem['type'] == "止盈目标过高":
                suggestions.append({
                    "problem": "止盈目标过高",
                    "action": "检查adaptive_stops算法",
                    "param": "adaptive_stops.tp_multiplier",
                    "current": "系统自动计算",
                    "suggested": "建议在adaptive_stops.py中降低TP倍数",
                    "reason": "价格很少达到系统计算的TP，需要更保守的目标",
                    "priority": "中",
                    "implementation": "修改core/adaptive_stops.py中的TP计算逻辑"
                })
                
                # 同时建议优化LLM Prompt
                suggestions.append({
                    "problem": "止盈目标过高",
                    "action": "优化LLM Prompt",
                    "param": "llm_gate.prompt",
                    "current": "当前Prompt",
                    "suggested": "在Prompt中强调：合约交易应该快速获利了结",
                    "reason": "让LLM倾向于向下调整TP（更保守）",
                    "priority": "中",
                    "implementation": "修改core/llm_gate.py中的_build_decision_prompt函数"
                })
            
            elif problem['type'] == "止损触发过频":
                suggestions.append({
                    "problem": "止损触发过频",
                    "action": "检查adaptive_stops的SL计算",
                    "param": "adaptive_stops.sl_multiplier",
                    "current": "系统自动计算",
                    "suggested": "建议在adaptive_stops.py中放宽SL空间",
                    "reason": "给价格适当波动空间，但不能太宽以免接近强平",
                    "priority": "高",
                    "warning": "调整后务必检查是否仍远离强平距离",
                    "implementation": "修改core/adaptive_stops.py中的SL计算逻辑"
                })
            
            elif problem['type'] == "盈亏比不佳":
                suggestions.append({
                    "problem": "盈亏比不佳",
                    "action": "调整TP/SL比例",
                    "param": "adaptive_stops算法",
                    "current": "系统计算",
                    "suggested": "确保TP/SL至少为2:1",
                    "reason": "合约交易下目标至少2:1的盈亏比",
                    "priority": "高",
                    "implementation": "在adaptive_stops.py中确保TP是SL的至少2倍"
                })
            
            elif problem['type'] == "LLM调整率过低":
                suggestions.append({
                    "problem": "LLM调整率过低",
                    "action": "优化LLM Prompt以鼓励调整",
                    "param": "llm_gate.prompt",
                    "current": "当前Prompt",
                    "suggested": "在Prompt中明确说明：系统默认值只是参考，鼓励根据市场情况调整",
                    "reason": "让LLM发挥微调作用",
                    "priority": "低",
                    "implementation": "修改core/llm_gate.py中的Prompt模板"
                })
            
            elif problem['type'] == "三档使用失衡":
                suggestions.append({
                    "problem": "三档使用失衡",
                    "action": "优化LLM三档设置逻辑",
                    "param": "llm_gate.三档ATR倍数",
                    "current": "LLM自由决定",
                    "suggested": "在Prompt中给出更明确的三档设置指导",
                    "reason": "确保适中档是主力入场方式",
                    "priority": "低",
                    "implementation": "优化_build_decision_prompt中的三档策略框架说明"
                })
            
            elif problem['type'] == "假突破率高" and strategy == "anomaly":
                current_spike = current_params.get('spike_ratio', 3.0)
                new_spike = min(5.0, current_spike + 0.5)
                suggestions.append({
                    "problem": "假突破率高",
                    "action": "提高成交量暴增阈值",
                    "param": "spike_ratio",
                    "current": current_spike,
                    "suggested": new_spike,
                    "reason": "只捕获更强的成交量信号，过滤假突破",
                    "priority": "中"
                })
            
            elif problem['type'] == "连续亏损风险":
                suggestions.append({
                    "problem": "连续亏损风险",
                    "action": "建议添加风控规则",
                    "param": "runtime.max_daily_losses",
                    "current": "无限制",
                    "suggested": "3次",
                    "reason": "连续3次止损后当天停止交易，避免情绪化决策",
                    "priority": "极高",
                    "implementation": "需要在main.py中添加每日亏损计数器"
                })
            
            elif problem['type'] == "强平风险":
                suggestions.append({
                    "problem": "强平风险",
                    "action": "降低杠杆或调整止损",
                    "param": "futures_trading.base_leverage",
                    "current": self.base_leverage,
                    "suggested": max(10, self.base_leverage - 5),
                    "reason": "当前配置下强平风险过高，必须降低杠杆",
                    "priority": "极高",
                    "warning": "这是生死攸关的问题，必须立即处理"
                })
        
        # 按优先级排序
        priority_order = {"极高": 0, "高": 1, "中": 2, "低": 3}
        suggestions.sort(key=lambda x: priority_order.get(x.get('priority', '低'), 3))
        
        return suggestions
    
    def generate_report(self, strategies: list = None, days: int = 30):
        """生成完整的诊断报告"""
        if strategies is None:
            strategies = ['majors', 'anomaly', 'accum']
        
        report = {
            "generated_at": datetime.utcnow().isoformat(),
            "analysis_period_days": days,
            "market_type": self.market_type,
            "base_leverage": self.base_leverage,
            "architecture": "三档入场 + adaptive_stops + LLM微调",
            "strategies": {}
        }
        
        for strategy in strategies:
            if strategy in self.cfg:
                analysis = self.analyze_strategy(strategy, days)
                report["strategies"][strategy] = analysis
        
        return report
    
    def print_report(self, report: Dict):
        """打印易读的报告"""
        print("\n" + "="*70)
        print(f"📊 参数诊断报告 - {report['market_type'].upper()}市场")
        print(f"架构: {report['architecture']}")
        print(f"生成时间: {report['generated_at']}")
        print(f"分析周期: 最近{report['analysis_period_days']}天")
        if report['market_type'] == 'future':
            print(f"⚡ 基础杠杆: {report['base_leverage']}x")
        print("="*70 + "\n")
        
        for strategy, analysis in report["strategies"].items():
            print(f"\n【{strategy.upper()}策略】")
            print(f"样本数: {analysis['sample_count']}")
            
            if analysis.get('status') == '数据不足':
                print(f"⚠️  {analysis['message']}\n")
                continue
            
            # 打印统计数据
            stats = analysis['stats']
            print(f"\n📈 表现统计:")
            print(f"  胜率: {stats['win_rate']:.1%} ({stats['win_count']}胜 / {stats['loss_count']}负)")
            print(f"  平均收益: {stats['avg_return']:.2%}")
            if report['market_type'] == 'future':
                print(f"  实际收益(含杠杆): {stats['avg_return_with_leverage']:.2%}")
                print(f"  平均杠杆: {stats['avg_leverage']:.1f}x")
            print(f"  盈亏比: {stats['profit_factor']:.2f}")
            print(f"  夏普率: {stats['sharpe_ratio']:.2f}")
            print(f"  平均最大浮盈: {stats['max_runup_avg']:.2%}")
            print(f"  平均最大回撤: {stats['max_drawdown_avg']:.2%}")
            print(f"  最大连胜: {stats['max_consecutive_wins']}次")
            print(f"  最大连亏: {stats['max_consecutive_losses']}次")
            
            # 三档使用情况
            entry_dist = stats['entry_distribution']
            if sum(entry_dist.values()) > 0:
                print(f"\n📍 三档入场分布:")
                print(f"  激进档: {entry_dist.get('aggressive', 0)}")
                print(f"  适中档: {entry_dist.get('moderate', 0)} ⭐")
                print(f"  保守档: {entry_dist.get('conservative', 0)}")
            
            # LLM调整情况
            llm_adj = stats['llm_adjustment_rate']
            print(f"\n🤖 LLM调整率:")
            print(f"  止损调整: {llm_adj['sl']:.1%}")
            print(f"  止盈调整: {llm_adj['tp']:.1%}")
            print(f"  杠杆调整: {llm_adj['leverage']:.1%}")
            
            # 打印问题
            if analysis['problems']:
                print(f"\n⚠️  发现问题:")
                for i, problem in enumerate(analysis['problems'], 1):
                    severity_emoji = {"极高": "🔴🔴", "高": "🔴", "中": "🟡", "低": "🟢"}
                    print(f"\n  {i}. {problem['type']} {severity_emoji.get(problem['severity'], '⚪')}")
                    print(f"     当前值: {problem['value']}")
                    print(f"     说明: {problem['description']}")
            else:
                print(f"\n✅ 未发现明显问题")
            
            # 打印建议
            if analysis['suggestions']:
                print(f"\n💡 调参建议 (按优先级排序):")
                for i, sug in enumerate(analysis['suggestions'], 1):
                    priority_emoji = {"极高": "🚨", "高": "⚠️", "中": "ℹ️", "低": "💭"}
                    print(f"\n  {i}. {sug['action']} {priority_emoji.get(sug.get('priority', '低'), '')}")
                    print(f"     针对问题: {sug['problem']}")
                    print(f"     参数: {sug['param']}")
                    print(f"     当前值: {sug['current']}")
                    print(f"     建议值: {sug['suggested']}")
                    print(f"     理由: {sug['reason']}")
                    if 'warning' in sug:
                        print(f"     ⚠️  警告: {sug['warning']}")
                    if 'implementation' in sug:
                        print(f"     🔧 实现: {sug['implementation']}")
            else:
                print(f"\n✅ 当前参数表现良好，暂无调整建议")
            
            print(f"\n" + "-"*70)
    
    def save_report(self, report: Dict, filename: str = None):
        """保存报告为JSON文件"""
        if filename is None:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            filename = f"param_diagnostics_{timestamp}.json"
        
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
        
        print(f"\n📄 报告已保存到: {filename}")
        return filename


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description='参数诊断工具（适配三档入场+LLM微调架构）')
    parser.add_argument('--days', type=int, default=30, help='分析天数')
    parser.add_argument('--strategies', nargs='+', default=['majors', 'anomaly', 'accum'], help='要分析的策略')
    parser.add_argument('--save', type=str, help='保存报告文件名')
    
    args = parser.parse_args()
    
    # 使用示例
    print("\n🔍 正在分析参数表现...")
    print(f"📅 分析周期: 最近{args.days}天")
    print(f"📋 策略列表: {', '.join(args.strategies)}\n")
    
    diagnostics = ParamDiagnostics()
    
    # 生成报告
    report = diagnostics.generate_report(strategies=args.strategies, days=args.days)
    
    # 打印报告
    diagnostics.print_report(report)
    
    # 保存报告
    if args.save:
        diagnostics.save_report(report, args.save)
    else:
        diagnostics.save_report(report)
    
    # 打印总结建议
    print("\n" + "="*70)
    print("📌 关键建议总结")
    print("="*70)
    
    high_priority_count = 0
    for strategy, analysis in report["strategies"].items():
        if analysis.get('suggestions'):
            high_priority = [s for s in analysis['suggestions'] if s.get('priority') in ['极高', '高']]
            if high_priority:
                print(f"\n【{strategy.upper()}】{len(high_priority)}个高优先级建议:")
                for sug in high_priority:
                    print(f"  🚨 {sug['action']}: {sug['reason']}")
                high_priority_count += len(high_priority)
    
    if high_priority_count == 0:
        print("\n✅ 所有策略表现良好，无紧急调整需求")
    else:
        print(f"\n⚠️  共有{high_priority_count}个高优先级问题需要处理")
    
    print("\n" + "="*70)
    print("💡 下一步行动:")
    print("="*70)
    print("1. 查看生成的JSON报告文件，获取详细数据")
    print("2. 根据'极高'和'高'优先级建议调整参数")
    print("3. 对于需要修改代码的建议（如adaptive_stops算法）:")
    print("   - 修改 core/adaptive_stops.py")
    print("   - 修改 core/llm_gate.py 中的Prompt")
    print("4. 对于配置参数的建议，手动修改 config.yaml")
    print("5. 调整后重新运行系统，7-14天后再次运行本诊断工具")
    print("\n⚠️  注意: 本工具只提供建议，不会自动修改任何文件")
    print("="*70 + "\n")