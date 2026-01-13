"""
高级持仓管理模块 v2.3 (增强诊断版)
功能：
1. 移动止损（Trailing Stop）
2. 保护性止损（Breakeven Stop）
3. 趋势反转检测
4. 动态调整止盈止损
5. 🔥 v2.3: 增强诊断日志
"""

import ccxt
import time
from datetime import datetime
from typing import Dict, Optional, List


class PositionManager:
    """持仓管理器 - 动态调整止损止盈 v2.3"""

    def __init__(self, exchange: ccxt.Exchange, config: dict):
        """
        初始化持仓管理器

        Args:
            exchange: CCXT交易所实例
            config: auto_trading.exit 配置
        """
        self.exchange = exchange
        self.config = config

        # 移动止损配置
        self.trailing_stop = config.get('trailing_stop', False)
        self.trailing_activation = config.get('trailing_stop_activation_pct', 0.01)
        self.trailing_distance = config.get('trailing_stop_distance_pct', 0.005)
        self.trailing_step = config.get('trailing_stop_step_pct', 0.005)

        # 保护性止损配置
        self.breakeven_stop = config.get('breakeven_stop', False)
        self.breakeven_activation = config.get('breakeven_activation_pct', 0.01)
        self.breakeven_buffer = config.get('breakeven_buffer_pct', 0.002)

        # 🔥🔥🔥 阶梯式移动止损配置 v2.3
        self.tiered_trailing_stop = config.get('tiered_trailing_stop', False)
        raw_tiers = config.get('trailing_tiers', None)
        if raw_tiers and isinstance(raw_tiers, list):
            self.trailing_tiers = []
            for tier in raw_tiers:
                if isinstance(tier, dict):
                    self.trailing_tiers.append((tier['trigger_pct'], tier['lock_pct']))
                elif isinstance(tier, (list, tuple)) and len(tier) == 2:
                    self.trailing_tiers.append((tier[0], tier[1]))
        else:
            # 🔥 v2.3: 默认14阶梯（增加40%）
            self.trailing_tiers = [
                (0.004, 0.001), (0.01, 0.003), (0.02, 0.012), (0.03, 0.025),
                (0.04, 0.036), (0.05, 0.048), (0.06, 0.058), (0.08, 0.076),
                (0.10, 0.095), (0.15, 0.145), (0.20, 0.195), (0.30, 0.290),
                (0.40, 0.390),  # 🔥 v2.3新增: 40%档位
                (0.50, 0.480),
            ]

        # 趋势反转检测
        self.reversal_detection = config.get('reversal_detection', False)
        self.reversal_check_interval = config.get('reversal_check_interval_sec', 60)
        self.reversal_rsi_long = config.get('reversal_rsi_threshold_long', 75)
        self.reversal_rsi_short = config.get('reversal_rsi_threshold_short', 25)
        self.reversal_macd_check = config.get('reversal_macd_check', True)

        # 🔥 动态止盈配置（从config读取，可在yaml中调整）
        self.dynamic_take_profit = config.get('dynamic_take_profit', True)
        self.tp_extension_on_momentum = config.get('tp_extension_on_momentum', True)
        self.tp_tighten_on_weakness = config.get('tp_tighten_on_weakness', True)
        self.momentum_strong_threshold = config.get('momentum_strong_threshold', 0.01)    # 5分钟涨>1%算强势
        self.momentum_weak_threshold = config.get('momentum_weak_threshold', -0.005)      # 5分钟跌>0.5%算弱势
        self.tp_extension_pct = config.get('tp_extension_pct', 0.15)                      # 强势时止盈扩大15%
        self.tp_tighten_buffer_pct = config.get('tp_tighten_buffer_pct', 0.01)            # 弱势时止盈收紧到当前价+1%
        self.tp_min_profit_to_tighten = config.get('tp_min_profit_to_tighten', 0.02)      # 盈利>2%才允许收紧

        # 持仓跟踪
        self.position_data = {}  # {symbol: {entry_price, highest_price, lowest_price, sl_price, tp_price, ...}}
        self.last_check_time = {}

        print(f"[POSITION_MGR] 初始化完成")
        print(f"  阶梯止损: {self.tiered_trailing_stop}")  # 🔥 新增
        if self.tiered_trailing_stop:
            print(f"    阶梯数: {len(self.trailing_tiers)}")
            for pnl, lock in self.trailing_tiers[:3]:
                print(f"    盈利{pnl*100:.1f}% → 锁定{lock*100:.1f}%")
        print(f"  保护止损: {self.breakeven_stop} | 激活: {self.breakeven_activation*100:.1f}%")
        print(f"  移动止损: {self.trailing_stop} | 激活: {self.trailing_activation*100:.1f}%")
        print(f"  动态止盈: {self.dynamic_take_profit}")
        if self.dynamic_take_profit:
            print(f"    强势阈值: {self.momentum_strong_threshold*100:.1f}% | 扩大: {self.tp_extension_pct*100:.0f}%")
            print(f"    弱势阈值: {self.momentum_weak_threshold*100:.1f}% | 收紧到: +{self.tp_tighten_buffer_pct*100:.1f}%")
        print(f"  反转检测: {self.reversal_detection}")

    def register_position(self, symbol: str, side: str, entry_price: float,
                         amount: float, sl_price: float, tp_price: float, strategy_type: str = 'reversal'):
        """
        注册新持仓 - 🔥 支持双策略差异化参数

        Args:
            symbol: 交易对
            side: long/short
            entry_price: 入场价
            amount: 数量
            sl_price: 止损价
            tp_price: 止盈价
        """
        # 🔥🔥 根据策略类型获取对应参数
        strategy_params = self.config.get('strategy_params', {}).get(strategy_type, {})

        # 如果没有配置strategy_params，使用默认值
        if not strategy_params:
            strategy_params = {
                'trailing_stop_activation_pct': self.trailing_activation,
                'trailing_stop_distance_pct': self.trailing_distance,
                'reversal_rsi_threshold_long': self.reversal_rsi_long,
                'reversal_rsi_threshold_short': self.reversal_rsi_short,
                'reversal_check_interval_sec': self.reversal_check_interval
            }

        self.position_data[symbol] = {
            'side': side,
            'entry_price': entry_price,
            'amount': amount,
            'sl_price': sl_price,
            'tp_price': tp_price,
            'original_sl': sl_price,
            'original_tp': tp_price,
            'highest_price': entry_price,  # 做多时的最高价
            'lowest_price': entry_price,   # 做空时的最低价
            'highest_pnl_pct': 0,          # 🔥 历史最高盈利百分比
            'current_tier': -1,            # 🔥 当前阶梯（-1表示未激活）
            'breakeven_set': False,
            'trailing_activated': False,
            'tp_extended': False,           # 🔥 止盈是否已扩大
            'tp_tightened': False,          # 🔥 止盈是否已收紧
            'last_momentum_check': 0,       # 🔥 上次动能检查时间
            'last_update': datetime.now(),
            'strategy_type': strategy_type,  # 🔥 新增：策略类型
            'strategy_params': strategy_params  # 🔥 新增：策略专用参数
        }

        print(f"[POSITION_MGR] 注册持仓: {symbol} {side.upper()} | 策略: {strategy_type.upper()}")
        print(f"  入场: {entry_price:.4f} | 止损: {sl_price:.4f} | 止盈: {tp_price:.4f}")

    def update_position(self, symbol: str, current_price: float,
                       fetch_indicators: bool = False) -> Optional[Dict]:
        """
        更新持仓并检查是否需要调整止损止盈

        Args:
            symbol: 交易对
            current_price: 当前价格
            fetch_indicators: 是否获取技术指标（用于反转检测）

        Returns:
            更新建议字典，如果需要调整
        """
        if symbol not in self.position_data:
            return None

        pos = self.position_data[symbol]
        side = pos['side']
        entry_price = pos['entry_price']

        # 🔥 获取该持仓的策略参数
        strategy_params = pos.get('strategy_params', {})
        trailing_activation = strategy_params.get('trailing_stop_activation_pct', self.trailing_activation)
        trailing_distance = strategy_params.get('trailing_stop_distance_pct', self.trailing_distance)
        reversal_check_interval = strategy_params.get('reversal_check_interval_sec', self.reversal_check_interval)
        reversal_rsi_long = strategy_params.get('reversal_rsi_threshold_long', self.reversal_rsi_long)
        reversal_rsi_short = strategy_params.get('reversal_rsi_threshold_short', self.reversal_rsi_short)

        # 更新最高/最低价
        if side == 'long':
            pos['highest_price'] = max(pos['highest_price'], current_price)
        else:
            pos['lowest_price'] = min(pos['lowest_price'], current_price)

        # 计算盈亏百分比
        if side == 'long':
            pnl_pct = (current_price - entry_price) / entry_price
            peak_price = pos['highest_price']
            peak_pnl_pct = (peak_price - entry_price) / entry_price
        else:
            pnl_pct = (entry_price - current_price) / entry_price
            peak_price = pos['lowest_price']
            peak_pnl_pct = (entry_price - peak_price) / entry_price

        # 🔥 更新历史最高盈利
        if peak_pnl_pct > pos.get('highest_pnl_pct', 0):
            old_peak = pos.get('highest_pnl_pct', 0)
            pos['highest_pnl_pct'] = peak_pnl_pct
            if peak_pnl_pct - old_peak >= 0.01:  # 每涨1%打印一次
                print(f"[POSITION_MGR] 📈 {symbol} 新高盈利: {peak_pnl_pct*100:.2f}%")

        actions = []

        # 🔥🔥🔥 0. 阶梯式移动止损（优先级最高）
        if self.tiered_trailing_stop:
            tiered_action = self._apply_tiered_trailing_stop(
                symbol, side, entry_price, current_price, peak_pnl_pct, pos
            )
            if tiered_action:
                actions.append(tiered_action)
                # 阶梯式止损已处理，跳过传统保本和移动止损
                pos['breakeven_set'] = True
                pos['trailing_activated'] = True

        # 1. 保护性止损（盈利1%后移到成本价）- 阶梯式启用时跳过
        elif self.breakeven_stop and not pos['breakeven_set']:
            if pnl_pct >= self.breakeven_activation:
                new_sl = entry_price * (1 + self.breakeven_buffer) if side == 'long' else entry_price * (1 - self.breakeven_buffer)
                actions.append({
                    'type': 'breakeven_stop',
                    'old_sl': pos['sl_price'],
                    'new_sl': new_sl,
                    'reason': f'盈利{pnl_pct*100:.1f}%，移到保本价'
                })
                pos['sl_price'] = new_sl
                pos['breakeven_set'] = True

        # 2. 🔥 移动止损（跟随最高价移动）- 使用策略参数
        if self.trailing_stop:
            # 检查是否激活移动止损（趋势策略0.8%，反转策略1%）
            if not pos['trailing_activated'] and pnl_pct >= trailing_activation:
                pos['trailing_activated'] = True
                print(f"[POSITION_MGR] {symbol} 移动止损已激活（盈利{pnl_pct*100:.1f}%）")

            # 如果已激活，根据最高价调整止损（趋势策略0.3%，反转策略0.5%）
            if pos['trailing_activated']:
                if side == 'long':
                    # 做多：止损 = 最高价 - 距离
                    new_sl = peak_price * (1 - trailing_distance)
                    if new_sl > pos['sl_price']:
                        # 确保止损只往上移，不往下移
                        actions.append({
                            'type': 'trailing_stop',
                            'old_sl': pos['sl_price'],
                            'new_sl': new_sl,
                            'reason': f'最高价{peak_price:.4f}，提升止损'
                        })
                        pos['sl_price'] = new_sl
                else:
                    # 做空：止损 = 最低价 + 距离
                    new_sl = peak_price * (1 + trailing_distance)
                    if new_sl < pos['sl_price']:
                        # 确保止损只往下移，不往上移
                        actions.append({
                            'type': 'trailing_stop',
                            'old_sl': pos['sl_price'],
                            'new_sl': new_sl,
                            'reason': f'最低价{peak_price:.4f}，降低止损'
                        })
                        pos['sl_price'] = new_sl

        # 3. 🔥🔥 动态止盈（根据动能调整）
        if self.dynamic_take_profit and fetch_indicators:
            now = time.time()
            last_momentum_check = pos.get('last_momentum_check', 0)
            
            # 每30秒检查一次动能
            if now - last_momentum_check > 30:
                pos['last_momentum_check'] = now
                
                momentum = self._get_momentum(symbol)
                
                if momentum is not None:
                    current_tp = pos['tp_price']
                    
                    # 🚀 强势动能：扩大止盈目标
                    if self.tp_extension_on_momentum and momentum > self.momentum_strong_threshold:
                        if not pos.get('tp_extended', False):
                            if side == 'long':
                                # 做多：止盈上移
                                tp_distance = current_tp - entry_price
                                extension = tp_distance * self.tp_extension_pct
                                new_tp = current_tp + extension
                            else:
                                # 做空：止盈下移
                                tp_distance = entry_price - current_tp
                                extension = tp_distance * self.tp_extension_pct
                                new_tp = current_tp - extension
                            
                            actions.append({
                                'type': 'trailing_tp',
                                'old_tp': current_tp,
                                'new_tp': new_tp,
                                'reason': f'动能强劲({momentum*100:.2f}%)，扩大止盈{self.tp_extension_pct*100:.0f}%'
                            })
                            pos['tp_price'] = new_tp
                            pos['tp_extended'] = True
                            pos['tp_tightened'] = False  # 重置收紧标记
                            print(f"[POSITION_MGR] 🚀 {symbol} 动态止盈扩大: {current_tp:.6f} → {new_tp:.6f}")
                    
                    # ⚠️ 弱势动能：收紧止盈（锁定利润）
                    elif self.tp_tighten_on_weakness and momentum < self.momentum_weak_threshold:
                        if pnl_pct > self.tp_min_profit_to_tighten and not pos.get('tp_tightened', False):  # 盈利超过阈值才收紧
                            if side == 'long':
                                new_tp = current_price * (1 + self.tp_tighten_buffer_pct)
                                if new_tp < current_tp:  # 确保是收紧
                                    actions.append({
                                        'type': 'trailing_tp',
                                        'old_tp': current_tp,
                                        'new_tp': new_tp,
                                        'reason': f'动能减弱({momentum*100:.2f}%)，收紧止盈锁定利润'
                                    })
                                    pos['tp_price'] = new_tp
                                    pos['tp_tightened'] = True
                                    print(f"[POSITION_MGR] ⚠️ {symbol} 动态止盈收紧: {current_tp:.6f} → {new_tp:.6f}")
                            else:
                                new_tp = current_price * (1 - self.tp_tighten_buffer_pct)
                                if new_tp > current_tp:  # 做空时收紧是止盈价上移
                                    actions.append({
                                        'type': 'trailing_tp',
                                        'old_tp': current_tp,
                                        'new_tp': new_tp,
                                        'reason': f'动能减弱({momentum*100:.2f}%)，收紧止盈锁定利润'
                                    })
                                    pos['tp_price'] = new_tp
                                    pos['tp_tightened'] = True
                                    print(f"[POSITION_MGR] ⚠️ {symbol} 动态止盈收紧: {current_tp:.6f} → {new_tp:.6f}")

        # 4. 🔥 趋势反转检测（需要获取指标）- 使用策略参数
        if self.reversal_detection and fetch_indicators:
            now = time.time()
            last_check = self.last_check_time.get(symbol, 0)

            # 每隔一定时间检查一次（趋势策略30秒，反转策略60秒）
            if now - last_check > reversal_check_interval:
                self.last_check_time[symbol] = now

                reversal_signal = self._check_reversal(symbol, side, current_price, reversal_rsi_long, reversal_rsi_short)
                if reversal_signal:
                    actions.append({
                        'type': 'reversal_exit',
                        'reason': reversal_signal,
                        'action': 'close_position'
                    })

        # 更新时间
        pos['last_update'] = datetime.now()

        return actions if actions else None

    def _check_reversal(self, symbol: str, side: str, current_price: float,
                       rsi_threshold_long: float = 75, rsi_threshold_short: float = 25) -> Optional[str]:
        """
        检查趋势反转信号 - 🔥 使用策略参数

        Args:
            symbol: 交易对
            side: long/short
            current_price: 当前价格
            rsi_threshold_long: 做多时RSI阈值（趋势65，反转75）
            rsi_threshold_short: 做空时RSI阈值（趋势35，反转25）

        Returns:
            反转原因（如果有）
        """
        try:
            # 获取K线数据
            ohlcv = self.exchange.fetch_ohlcv(symbol, '5m', limit=50)

            if len(ohlcv) < 26:
                return None

            import pandas as pd
            import numpy as np

            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])

            # 计算RSI
            rsi = self._calculate_rsi(df['close'].values, period=14)

            # 计算MACD
            macd_line, signal_line, _ = self._calculate_macd(df['close'].values)

            current_rsi = rsi[-1]

            # 做多反转检测（趋势策略RSI>65触发，反转策略RSI>75触发）
            if side == 'long':
                # RSI超买
                if current_rsi > rsi_threshold_long:
                    return f"做多反转：RSI超买({current_rsi:.0f}>{rsi_threshold_long:.0f})"

                # MACD死叉
                if self.reversal_macd_check and len(macd_line) > 1 and len(signal_line) > 1:
                    if macd_line[-2] > signal_line[-2] and macd_line[-1] < signal_line[-1]:
                        return f"做多反转：MACD死叉"

            # 做空反转检测（趋势策略RSI<35触发，反转策略RSI<25触发）
            else:
                # RSI超卖
                if current_rsi < rsi_threshold_short:
                    return f"做空反转：RSI超卖({current_rsi:.0f}<{rsi_threshold_short:.0f})"

                # MACD金叉
                if self.reversal_macd_check and len(macd_line) > 1 and len(signal_line) > 1:
                    if macd_line[-2] < signal_line[-2] and macd_line[-1] > signal_line[-1]:
                        return f"做空反转：MACD金叉"

            return None

        except Exception as e:
            print(f"[POSITION_MGR] 反转检测失败: {e}")
            return None

    def _calculate_rsi(self, prices, period=14):
        """计算RSI"""
        import numpy as np

        deltas = np.diff(prices)
        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)

        avg_gain = np.convolve(gains, np.ones(period), 'valid') / period
        avg_loss = np.convolve(losses, np.ones(period), 'valid') / period

        rs = avg_gain / (avg_loss + 1e-10)
        rsi = 100 - (100 / (1 + rs))

        return rsi

    def _calculate_macd(self, prices, fast=12, slow=26, signal=9):
        """计算MACD"""
        import pandas as pd

        ema_fast = pd.Series(prices).ewm(span=fast, adjust=False).mean()
        ema_slow = pd.Series(prices).ewm(span=slow, adjust=False).mean()

        macd_line = ema_fast - ema_slow
        signal_line = macd_line.ewm(span=signal, adjust=False).mean()
        histogram = macd_line - signal_line

        return macd_line.values, signal_line.values, histogram.values

    def _apply_tiered_trailing_stop(self, symbol: str, side: str, entry_price: float,
                                    current_price: float, peak_pnl_pct: float,
                                    pos: Dict) -> Optional[Dict]:
        """
        🔥🔥🔥 阶梯式移动止损核心逻辑 v2.3 (增强诊断版)
        
        关键点：根据 **历史最高盈利** 而非当前盈利确定止损位置
        止损只升不降，锁定利润
        """
        current_tier = pos.get('current_tier', -1)
        old_sl = pos['sl_price']
        
        # 🔥 v2.3: 诊断日志 - 每次都打印状态
        current_pnl = (current_price - entry_price) / entry_price if side == 'long' else (entry_price - current_price) / entry_price
        print(f"[TIERED_SL] {symbol} 检查阶梯止损:")
        print(f"  当前盈亏: {current_pnl*100:.2f}% | 最高盈利: {peak_pnl_pct*100:.2f}%")
        print(f"  当前阶梯: {current_tier} | 当前止损: ${old_sl:.6f}")
        
        # 找到应该在的阶梯（根据历史最高盈利）
        new_tier = -1
        sl_lock_pct = 0
        
        for i, (pnl_thresh, sl_lock) in enumerate(self.trailing_tiers):
            if peak_pnl_pct >= pnl_thresh:
                new_tier = i
                sl_lock_pct = sl_lock
        
        print(f"  计算新阶梯: {new_tier} | 锁定比例: {sl_lock_pct*100:.1f}%")
        
        # 没有达到任何阶梯，或没有升级
        if new_tier < 0:
            print(f"  ❌ 未达到任何阶梯（最低要求盈利{self.trailing_tiers[0][0]*100:.1f}%）")
            return None
            
        if new_tier <= current_tier:
            print(f"  ⏸ 阶梯未升级（当前{current_tier} >= 新{new_tier}）")
            return None
        
        # 计算新止损价
        if side == 'long':
            new_sl = entry_price * (1 + sl_lock_pct)
            print(f"  新止损计算: ${entry_price:.6f} × (1 + {sl_lock_pct:.4f}) = ${new_sl:.6f}")
            # 止损只能上升
            if new_sl <= old_sl:
                print(f"  ⚠️ 新止损${new_sl:.6f} <= 旧止损${old_sl:.6f}，跳过")
                return None
        else:
            new_sl = entry_price * (1 - sl_lock_pct)
            print(f"  新止损计算: ${entry_price:.6f} × (1 - {sl_lock_pct:.4f}) = ${new_sl:.6f}")
            # 空单止损只能下降
            if new_sl >= old_sl:
                print(f"  ⚠️ 新止损${new_sl:.6f} >= 旧止损${old_sl:.6f}，跳过")
                return None
        
        # 更新持仓数据
        pos['current_tier'] = new_tier
        pos['sl_price'] = new_sl
        
        pnl_thresh, _ = self.trailing_tiers[new_tier]
        
        print(f"[POSITION_MGR] 🎯🎯🎯 {symbol} 阶梯升级成功!")
        print(f"  {current_tier} → {new_tier} | 最高盈利:{peak_pnl_pct*100:.1f}%")
        print(f"  止损: ${old_sl:.6f} → ${new_sl:.6f}")
        print(f"  锁定利润: {sl_lock_pct*100:.1f}%")
        
        return {
            'type': 'tiered_trailing_stop',
            'old_sl': old_sl,
            'new_sl': new_sl,
            'tier': new_tier,
            'peak_pnl_pct': peak_pnl_pct,
            'locked_pnl_pct': sl_lock_pct,
            'reason': f'历史最高{peak_pnl_pct*100:.1f}%，锁定{sl_lock_pct*100:.1f}%'
        }

    def _get_momentum(self, symbol: str, period: int = 5) -> Optional[float]:
        """
        🔥 获取短期动能（5分钟价格变化百分比）
        
        Args:
            symbol: 交易对
            period: 回看周期（分钟）
            
        Returns:
            动能百分比，正数表示上涨，负数表示下跌
        """
        try:
            ohlcv = self.exchange.fetch_ohlcv(symbol, '1m', limit=period + 1)
            if len(ohlcv) >= period + 1:
                old_price = ohlcv[0][4]   # period分钟前的收盘价
                new_price = ohlcv[-1][4]  # 最新收盘价
                momentum = (new_price - old_price) / old_price
                return momentum
            return None
        except Exception as e:
            print(f"[POSITION_MGR] 获取动能失败: {e}")
            return None

    def get_position_info(self, symbol: str) -> Optional[Dict]:
        """获取持仓信息"""
        return self.position_data.get(symbol)

    def remove_position(self, symbol: str):
        """移除持仓记录"""
        if symbol in self.position_data:
            del self.position_data[symbol]
            print(f"[POSITION_MGR] 移除持仓记录: {symbol}")

    def get_all_positions(self) -> List[str]:
        """获取所有持仓符号"""
        return list(self.position_data.keys())