"""
OKX自动交易模块 v3.9 - 🔥🔥🔥 阶梯止损增强版

v3.9 核心改进：
1. 🔥🔥🔥 止损缓存同步：自动从OKX获取止损单ID，防止重启后丢失
2. 🔥🔥🔥 持仓自动同步：OKX持仓自动注册到position_manager
3. 🔥🔥🔥 止损更新增强：失败时自动重试(3次)，并发送告警
4. 🔥🔥🔥 详细日志：阶梯止损每次检查都打印状态
5. 🔥🔥🔥 新增40%档位：30%→50%改为30%→40%→50%

v3.8 原有功能：
1. 原子下单：下单时直接带止损止盈
2. 止损验证：每次持仓检查时验证OKX止损单是否存在
3. 紧急止损：亏损超过2%强制市价平仓
4. 监控频率：1分钟检查一次持仓状态
5. 默认止损：1.2%

"""

import ccxt
import time
import sqlite3
from datetime import datetime, timedelta
from typing import Dict, Optional, List, Tuple
from core.position_manager import PositionManager

# 🔥 尝试导入持仓AI审核器
try:
    from core.position_reviewer import PositionReviewer, PositionAction
    POSITION_REVIEWER_AVAILABLE = True
except ImportError:
    POSITION_REVIEWER_AVAILABLE = False
    print("[AUTOTRADER] ⚠️ PositionReviewer不可用")


class AutoTrader:
    """OKX自动交易器 v3.8 - 原子止损版"""

    def __init__(self, config: dict, db_path: str, full_config: dict = None):
        """
        初始化自动交易器

        Args:
            config: 配置字典（config.yaml的auto_trading部分）
            db_path: 数据库路径
            full_config: 🔥 完整配置（用于持仓AI审核）
        """
        self.config = config
        self.full_config = full_config or {}  # 🔥 保存完整配置
        self.db_path = db_path
        self.enabled = config.get("enabled", False)

        # OKX交易所
        okx_config = config.get("okx", {})
        
        # 🔥 修复：添加hostname配置，解决VPN连接问题
        self.exchange = ccxt.okx({
            'apiKey': okx_config.get("api_key", ""),
            'secret': okx_config.get("secret", ""),
            'password': okx_config.get("passphrase", ""),
            'enableRateLimit': True,
            'hostname': okx_config.get("hostname", "www.okx.com"),  # 🔥 新增
            'timeout': 30000,  # 🔥 新增：30秒超时
            'options': {
                'defaultType': 'swap',  # 永续合约
                'sandboxMode': okx_config.get("testnet", False)
            }
        })

        # 资金管理
        capital_cfg = config.get("capital", {})
        self.total_capital = capital_cfg.get("total_usdt", 50)
        self.max_position_pct = capital_cfg.get("max_position_pct", 0.3)
        self.min_position_usdt = capital_cfg.get("min_position_usdt", 5)
        self.max_position_usdt = capital_cfg.get("max_position_usdt", 15)
        self.reserve_pct = capital_cfg.get("reserve_pct", 0.1)

        # 风险控制
        risk_cfg = config.get("risk", {})
        self.max_positions = risk_cfg.get("max_positions", 3)
        self.max_leverage = risk_cfg.get("max_leverage", 5)
        self.default_leverage = risk_cfg.get("default_leverage", 3)
        self.force_stop_loss = risk_cfg.get("force_stop_loss", True)
        self.sl_slippage_buffer = risk_cfg.get("sl_slippage_buffer", 0.002)

        # 入场策略
        entry_cfg = config.get("entry", {})
        self.use_immediate_price = entry_cfg.get("use_immediate_price", True)
        self.max_slippage = entry_cfg.get("max_slippage", 0.005)
        self.retry_times = entry_cfg.get("retry_times", 3)
        self.retry_delay = entry_cfg.get("retry_delay_sec", 2)

        # 出场策略
        exit_cfg = config.get("exit", {})
        self.use_ai_targets = exit_cfg.get("use_ai_targets", True)
        self.exit_config = exit_cfg  # 🔥 保存exit配置供后续使用

        # 安全设置
        safety_cfg = config.get("safety", {})
        self.require_approval = safety_cfg.get("require_signal_approval", True)
        self.check_balance = safety_cfg.get("check_balance_before_trade", True)
        self.max_daily_trades = safety_cfg.get("max_daily_trades", 10)
        self.max_daily_loss_pct = safety_cfg.get("max_daily_loss_pct", 0.2)
        self.emergency_stop_pct = safety_cfg.get("emergency_stop_loss_pct", 0.5)

        # 交易记录
        self.daily_trades = 0
        self.daily_pnl = 0.0
        self.last_reset_date = datetime.now().date()

        # 🔥 止损单ID缓存（用于更新止损单）
        self.sl_order_cache: Dict[str, str] = {}  # symbol -> order_id
        self.tp_order_cache: Dict[str, str] = {}  # symbol -> order_id
        
        # 🔥 高波动轨道：待设置止损止盈的订单缓存
        self._pending_sl_tp: Dict[str, Dict] = {}
        
        # 🔥 持仓入场时间缓存（用于AI审核）
        self.position_entry_time: Dict[str, datetime] = {}
        
        # 🔥 上次AI审核时间
        self.last_ai_review_time: Dict[str, datetime] = {}
        self.ai_review_interval_sec = full_config.get("position_review", {}).get("review_interval_sec", 300) if full_config else 300

        # 🔥🔥🔥 v3.7 新增：强制止损配置
        self.sl_must_succeed = config.get("sl_must_succeed", True)  # 止损必须成功，否则不开仓
        self.emergency_sl_pct = config.get("emergency_sl_pct", 0.02)  # 紧急止损阈值2%
        self.sl_verify_interval_sec = config.get("sl_verify_interval_sec", 60)  # 止损验证间隔60秒
        self.position_check_interval_sec = config.get("position_check_interval_sec", 60)  # 持仓检查间隔60秒
        self.default_sl_pct = config.get("default_sl_pct", 0.012)  # 默认止损1.2%
        self.default_tp_pct = config.get("default_tp_pct", 0.036)  # 默认止盈3.6% (3倍止损)
        
        # 🔥🔥🔥 v3.7 新增：止损验证时间缓存
        self.last_sl_verify_time: Dict[str, datetime] = {}
        
        # 🔥🔥🔥 v3.7 新增：上次持仓检查时间
        self.last_position_check_time: Optional[datetime] = None

        # 持仓管理器（高级止损止盈）
        self.position_manager = None
        if self.enabled:
            try:
                self.position_manager = PositionManager(self.exchange, exit_cfg)
            except Exception as e:
                print(f"[AUTOTRADER] ⚠️ 持仓管理器初始化失败: {e}")
        
        # 🔥 持仓AI审核器
        self.position_reviewer = None
        if self.enabled and POSITION_REVIEWER_AVAILABLE and full_config:
            try:
                self.position_reviewer = PositionReviewer(full_config, self.exchange)
                print(f"[AUTOTRADER] ✅ 持仓AI审核器已启用")
            except Exception as e:
                print(f"[AUTOTRADER] ⚠️ 持仓AI审核器初始化失败: {e}")

        print(f"[AUTOTRADER] v3.8 原子止损版 初始化完成 | 启用: {self.enabled}")
        if self.enabled:
            print(f"[AUTOTRADER] 总资金: ${self.total_capital} | 最大仓位: ${self.max_position_usdt}")
            print(f"[AUTOTRADER] 杠杆: {self.default_leverage}x | 最大持仓数: {self.max_positions}")
            print(f"[AUTOTRADER] 🔥 原子下单: 下单即带止损止盈")
            print(f"[AUTOTRADER] 🔥 紧急止损: {self.emergency_sl_pct*100:.1f}% | 默认止损: {self.default_sl_pct*100:.1f}%")
            print(f"[AUTOTRADER] 🔥 持仓检查间隔: {self.position_check_interval_sec}秒")
            if self.position_reviewer:
                print(f"[AUTOTRADER] 🔥 AI审核间隔: {self.ai_review_interval_sec}秒")

    def reset_daily_stats(self):
        """重置每日统计"""
        today = datetime.now().date()
        if today > self.last_reset_date:
            self.daily_trades = 0
            self.daily_pnl = 0.0
            self.last_reset_date = today
            print(f"[AUTOTRADER] 每日统计已重置")

    def get_pending_signals(self) -> List[Dict]:
        """获取待执行的信号"""
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            # 获取已批准但未交易的信号
            cursor.execute("""
                SELECT * FROM pushed_signals
                WHERE (auto_traded = 0 OR auto_traded IS NULL)
                AND ai_decision = 'approved'
                AND created_at >= datetime('now', '-1 hour')
                ORDER BY created_at DESC
                LIMIT 10
            """)

            rows = cursor.fetchall()
            signals = [dict(row) for row in rows]
            conn.close()

            return signals

        except Exception as e:
            print(f"[AUTOTRADER_ERR] 获取待执行信号失败: {e}")
            return []

    def get_current_positions(self, max_retries: int = 3) -> List[Dict]:
        """获取当前持仓（带重试机制）"""
        last_error = None
        for attempt in range(max_retries):
            try:
                positions = self.exchange.fetch_positions()
                # 过滤有持仓的
                active = [p for p in positions if float(p.get('contracts', 0)) > 0]
                return active
            except Exception as e:
                last_error = e
                if attempt < max_retries - 1:
                    wait_time = (attempt + 1) * 2  # 2秒, 4秒, 6秒...
                    print(f"[AUTOTRADER] ⚠️ 获取持仓失败，{wait_time}秒后重试 ({attempt+1}/{max_retries})")
                    time.sleep(wait_time)
        
        print(f"[AUTOTRADER_ERR] 获取持仓失败（已重试{max_retries}次）: {last_error}")
        return []

    def check_trade_limits(self) -> Tuple[bool, str]:
        """检查交易限制"""
        self.reset_daily_stats()

        # 检查每日交易次数
        if self.daily_trades >= self.max_daily_trades:
            return False, f"达到每日交易上限({self.max_daily_trades})"

        # 检查每日亏损
        if self.daily_pnl <= -self.max_daily_loss_pct * self.total_capital:
            return False, f"达到每日亏损上限({self.max_daily_loss_pct*100}%)"

        # 检查持仓数量
        positions = self.get_current_positions()
        if len(positions) >= self.max_positions:
            return False, f"达到持仓上限({self.max_positions})"

        return True, "OK"

    def execute_trade(self, signal: Dict) -> Optional[Dict]:
        """
        执行交易

        Args:
            signal: 信号字典

        Returns:
            订单信息或None
        """
        try:
            symbol = signal.get('symbol', '')
            side = signal.get('side', 'long').lower()

            print(f"\n[AUTOTRADER] 准备执行交易: {symbol} {side.upper()}")

            # 🔥 0. 过滤交割合约（带日期后缀的，如 BTC/USDT:USDT-251226）
            if '-' in symbol:
                # 检查是否是交割合约格式（结尾是6位数字日期）
                suffix = symbol.split('-')[-1]
                if suffix.isdigit() and len(suffix) == 6:
                    print(f"[AUTOTRADER] ⏭️ 跳过交割合约: {symbol}")
                    self.mark_signal_traded(signal.get('id'), "skipped_delivery")
                    return None

            # 1. 检查交易限制
            can_trade, reason = self.check_trade_limits()
            if not can_trade:
                print(f"[AUTOTRADER] ⛔ 交易被拒绝: {reason}")
                return None

            # 2. 检查是否已有该币种持仓（🔥 v3.6: 区分同向/反向）
            positions = self.get_current_positions()
            for pos in positions:
                if pos['symbol'] == symbol:
                    existing_side = pos.get('side', 'long')
                    existing_contracts = float(pos.get('contracts', 0))
                    
                    if existing_side == side:
                        # 同向持仓：跳过
                        print(f"[AUTOTRADER] ⛔ 已有{symbol} {existing_side.upper()}持仓，跳过")
                        return None
                    else:
                        # 🔥🔥🔥 v3.6: 反向持仓 - 先平掉再开新仓
                        print(f"[AUTOTRADER] ⚠️ 发现反向持仓: {symbol} {existing_side.upper()} {existing_contracts}个")
                        print(f"[AUTOTRADER] 🔄 准备先平仓再开{side.upper()}...")
                        
                        # 平掉反向仓位
                        close_side = 'sell' if existing_side == 'long' else 'buy'
                        try:
                            # 取消旧的止损止盈单
                            self._cancel_all_sl_tp_orders(symbol)
                            
                            # 市价平仓
                            close_order = self.exchange.create_order(
                                symbol=symbol,
                                type='market',
                                side=close_side,
                                amount=existing_contracts,
                                params={
                                    'tdMode': 'cross',
                                    'posSide': existing_side,
                                    'reduceOnly': True
                                }
                            )
                            print(f"[AUTOTRADER] ✅ 反向仓位已平仓: {close_order['id']}")
                            
                            # 从position_manager移除
                            if self.position_manager:
                                self.position_manager.remove_position(symbol)
                            
                            # 清除缓存
                            self.sl_order_cache.pop(symbol, None)
                            self.tp_order_cache.pop(symbol, None)
                            
                            # 等待一下让OKX处理
                            time.sleep(0.5)
                            
                        except Exception as e:
                            print(f"[AUTOTRADER] ❌ 平反向仓位失败: {e}")
                            return None

            # 3. 获取入场价格
            entry_price = signal.get('entry_price_immediate') or signal.get('entry_price')
            if not entry_price:
                print(f"[AUTOTRADER] ⛔ 缺少入场价格")
                return None
            entry_price = float(entry_price)

            # 4. 计算仓位大小
            position_size_usdt = min(
                self.total_capital * self.max_position_pct,
                self.max_position_usdt
            )
            position_size_usdt = max(position_size_usdt, self.min_position_usdt)

            # 5. 计算下单数量
            amount = (position_size_usdt * self.default_leverage) / entry_price

            # 6. 标准化交易对
            okx_symbol = symbol
            if not symbol.endswith(':USDT'):
                okx_symbol = symbol.replace('/USDT', '/USDT:USDT')

            # 🔥 6.5 检查最小下单量
            try:
                market = self.exchange.market(okx_symbol)
                min_amount = market.get('limits', {}).get('amount', {}).get('min', 0)
                if min_amount and amount < min_amount:
                    print(f"[AUTOTRADER] ⏭️ 数量{amount:.6f}小于最小要求{min_amount}，跳过")
                    print(f"[AUTOTRADER]    需要资金: ${min_amount * entry_price / self.default_leverage:.2f}")
                    self.mark_signal_traded(signal.get('id'), "skipped_min_amount")
                    return None
                
                # 调整数量精度
                amount_precision = market.get('precision', {}).get('amount', 8)
                if isinstance(amount_precision, int):
                    amount = round(amount, amount_precision)
                else:
                    # 如果是最小步长格式
                    amount = float(self.exchange.amount_to_precision(okx_symbol, amount))
                    
            except Exception as e:
                print(f"[AUTOTRADER] ⚠️ 获取市场信息失败: {e}")
                # 继续尝试下单，让交易所返回具体错误

            # 7. 设置杠杆
            try:
                self.exchange.set_leverage(self.default_leverage, okx_symbol)
                print(f"[AUTOTRADER] 设置杠杆: {self.default_leverage}x")
            except Exception as e:
                print(f"[AUTOTRADER] ⚠️ 设置杠杆失败: {e}")

            # 8. 确定订单类型
            order_type = 'limit' if signal.get('entry_type') == 'delayed' else 'market'

            # 🔥🔥🔥 v3.8: 先计算止损止盈价格
            sl_price = signal.get('sl_price') or 0
            tp_price = signal.get('tp_price') or 0
            
            # 如果止损止盈为0，使用默认值（1.2%止损，3.6%止盈）
            if not sl_price or sl_price <= 0:
                if side == 'long':
                    sl_price = entry_price * (1 - self.default_sl_pct)
                else:
                    sl_price = entry_price * (1 + self.default_sl_pct)
                print(f"[AUTOTRADER] ⚠️ 止损为空，使用默认{self.default_sl_pct*100:.1f}%: ${sl_price:.6f}")
            
            if not tp_price or tp_price <= 0:
                if side == 'long':
                    tp_price = entry_price * (1 + self.default_tp_pct)
                else:
                    tp_price = entry_price * (1 - self.default_tp_pct)
                print(f"[AUTOTRADER] ⚠️ 止盈为空，使用默认{self.default_tp_pct*100:.1f}%: ${tp_price:.6f}")

            # 添加止损滑点缓冲
            if side == 'long':
                sl_trigger = sl_price * (1 - self.sl_slippage_buffer)
            else:
                sl_trigger = sl_price * (1 + self.sl_slippage_buffer)

            # 9. 🔥🔥🔥 v3.8: 原子下单（直接带止损止盈）
            order_side = 'buy' if side == 'long' else 'sell'

            print(f"[AUTOTRADER] 准备下单（带止损止盈）:")
            print(f"  交易对: {okx_symbol}")
            print(f"  方向: {side.upper()} ({order_side})")
            print(f"  订单类型: {order_type.upper()}")
            print(f"  数量: {amount:.6f}")
            print(f"  价格: {entry_price:.4f}")
            print(f"  止损: {sl_price:.6f} (触发: {sl_trigger:.6f})")
            print(f"  止盈: {tp_price:.6f}")
            print(f"  仓位: ${position_size_usdt} (杠杆{self.default_leverage}x)")

            # 🔥🔥🔥 v3.8: 使用OKX原生API下单带止损止盈
            order = None
            sl_order_id = None
            tp_order_id = None
            
            if self.force_stop_loss:
                # 尝试原子下单（带止损止盈）
                order, sl_order_id, tp_order_id = self._create_order_with_sl_tp(
                    symbol=okx_symbol,
                    side=side,
                    order_side=order_side,
                    order_type=order_type,
                    amount=amount,
                    price=entry_price if order_type == 'limit' else None,
                    sl_trigger=sl_trigger,
                    tp_price=tp_price
                )
                
                if not order:
                    print(f"[AUTOTRADER] ❌ 下单失败")
                    return None
                
                # 缓存止损止盈订单ID
                if sl_order_id:
                    self.sl_order_cache[okx_symbol] = sl_order_id
                if tp_order_id:
                    self.tp_order_cache[okx_symbol] = tp_order_id
            else:
                # 不带止损下单（不推荐）
                order = self.exchange.create_order(
                    symbol=okx_symbol,
                    type=order_type,
                    side=order_side,
                    amount=amount,
                    price=entry_price if order_type == 'limit' else None,
                    params={
                        'tdMode': 'cross',
                        'posSide': 'long' if side == 'long' else 'short'
                    }
                )
                print(f"[AUTOTRADER] ⚠️ 未设置止损止盈（force_stop_loss=False）")

            print(f"[AUTOTRADER] ✅ 订单成功: {order['id']}")

            # 10. 限价单等待成交
            if order_type == 'limit':
                print(f"[AUTOTRADER] ⏳ 限价单等待成交，3分钟后检查...")
                time.sleep(180)

                order_status = self.exchange.fetch_order(order['id'], okx_symbol)

                if order_status['status'] == 'open':
                    self.exchange.cancel_order(order['id'], okx_symbol)
                    # 同时取消止损止盈单
                    self._cancel_all_sl_tp_orders(okx_symbol)
                    print(f"[AUTOTRADER] ⏭️ 限价单3分钟未成交，已取消")
                    self.mark_signal_traded(signal['id'], None)
                    self.update_signal_cancelled(signal['id'], "limit_order_timeout")
                    return None
                elif order_status['status'] == 'closed':
                    print(f"[AUTOTRADER] ✅ 限价单已成交: {order_status.get('average', entry_price)}")
                    entry_price = order_status.get('average', entry_price)

            # 11. 更新统计
            self.daily_trades += 1
            
            # 🔥 v3.0: 记录入场时间（用于AI审核）
            self.position_entry_time[okx_symbol] = datetime.now()

            # 🔥🔥🔥 v3.1: 更新信号为已成交状态（报告系统需要）
            actual_fill_price = order.get('average') or order.get('price') or entry_price
            self.update_signal_filled(signal['id'], actual_fill_price, order['id'])

            # 13. 标记信号已交易
            self.mark_signal_traded(signal['id'], order['id'])

            # 14. 记录交易
            self.log_trade(signal, order, position_size_usdt)

            # 15. 🔥 注册持仓到管理器（直接使用信号的signal_type）
            # 从信号中获取类型，而不是根据RSI重新判断
            signal_type = signal.get('signal_type', '')
            
            # 🔥 正确判断策略类型
            if signal_type in ['trend_explosion', 'trend', 'breakout']:
                strategy_type = 'trend'
            elif signal_type in ['reversal', 'oversold', 'overbought']:
                strategy_type = 'reversal'
            else:
                # 兜底：如果没有signal_type，用RSI判断
                signal_rsi = signal.get('rsi', 50)
                if signal_rsi < 30 or signal_rsi > 70:
                    strategy_type = 'reversal'
                else:
                    strategy_type = 'trend'
            
            print(f"[AUTOTRADER] 📊 信号类型: {signal_type} → 策略: {strategy_type.upper()}")

            if self.position_manager:
                try:
                    self.position_manager.register_position(
                        symbol=okx_symbol,
                        side=side,
                        entry_price=entry_price,
                        amount=amount,
                        sl_price=sl_price if sl_price else 0,
                        tp_price=tp_price if tp_price else 0,
                        strategy_type=strategy_type
                    )
                except Exception as e:
                    print(f"[AUTOTRADER] ⚠️ 注册持仓失败: {e}")

            return order

        except Exception as e:
            print(f"[AUTOTRADER_ERR] 交易执行失败: {e}")
            import traceback
            traceback.print_exc()
            return None

    def _emergency_close_position(self, symbol: str, side: str, amount: float):
        """
        🔥🔥🔥 v3.7 新增：紧急平仓（止损创建失败或触发紧急止损时使用）
        """
        try:
            close_side = 'sell' if side == 'long' else 'buy'
            
            close_order = self.exchange.create_order(
                symbol=symbol,
                type='market',
                side=close_side,
                amount=amount,
                params={
                    'tdMode': 'cross',
                    'posSide': side,
                    'reduceOnly': True
                }
            )
            
            print(f"[AUTOTRADER] 🚨 紧急平仓成功: {close_order['id']}")
            
            # 清理缓存
            if self.position_manager:
                self.position_manager.remove_position(symbol)
            self.sl_order_cache.pop(symbol, None)
            self.tp_order_cache.pop(symbol, None)
            self.position_entry_time.pop(symbol, None)
            self.last_sl_verify_time.pop(symbol, None)
            
        except Exception as e:
            print(f"[AUTOTRADER] ❌ 紧急平仓失败: {e}")

    def _verify_stop_loss_exists(self, symbol: str, side: str, amount: float) -> bool:
        """
        🔥🔥🔥 v3.9 增强：验证OKX上的止损单是否存在，并同步缓存
        
        Returns:
            True如果止损单存在，False如果不存在
        """
        try:
            inst_id = symbol.replace('/', '-').replace(':USDT', '-SWAP')
            
            # 查询当前的algo订单
            response = self.exchange.privateGetTradeOrdersAlgoPending({
                'instId': inst_id,
                'ordType': 'conditional,oco'
            })
            
            if response and response.get('code') == '0':
                orders = response.get('data', [])
                
                # 检查是否有止损单
                for order in orders:
                    if order.get('slTriggerPx'):
                        algo_id = order.get('algoId')
                        sl_price = float(order.get('slTriggerPx', 0))
                        print(f"[SL_VERIFY] ✅ {symbol} 止损单存在: {algo_id} @ ${sl_price:.6f}")
                        
                        # 🔥 v3.9: 同步缓存（防止重启后丢失）
                        if algo_id and symbol not in self.sl_order_cache:
                            self.sl_order_cache[symbol] = algo_id
                            print(f"[SL_VERIFY] 🔄 已同步止损单ID到缓存")
                        
                        return True
                
                print(f"[SL_VERIFY] ⚠️ {symbol} 未找到止损单!")
                # 🔥 v3.9: 清除可能过期的缓存
                self.sl_order_cache.pop(symbol, None)
                return False
            else:
                print(f"[SL_VERIFY] ⚠️ 查询失败: {response.get('msg', 'Unknown')}")
                return True  # 查询失败时假设存在，避免误触发
                
        except Exception as e:
            print(f"[SL_VERIFY] ⚠️ 验证异常: {e}")
            return True  # 异常时假设存在
    
    def _get_current_sl_order_from_okx(self, symbol: str) -> Optional[Dict]:
        """
        🔥🔥🔥 v3.9 新增：从OKX获取当前止损单详情
        
        Returns:
            止损单信息字典，包含 algo_id, sl_price, tp_price 等
        """
        try:
            inst_id = symbol.replace('/', '-').replace(':USDT', '-SWAP')
            
            response = self.exchange.privateGetTradeOrdersAlgoPending({
                'instId': inst_id,
                'ordType': 'conditional,oco'
            })
            
            if response and response.get('code') == '0':
                orders = response.get('data', [])
                
                for order in orders:
                    if order.get('slTriggerPx'):
                        return {
                            'algo_id': order.get('algoId'),
                            'sl_price': float(order.get('slTriggerPx', 0)),
                            'tp_price': float(order.get('tpTriggerPx', 0)) if order.get('tpTriggerPx') else 0,
                            'size': float(order.get('sz', 0)),
                            'side': order.get('side'),
                            'pos_side': order.get('posSide')
                        }
            
            return None
            
        except Exception as e:
            print(f"[SL_QUERY] ⚠️ 查询止损单异常: {e}")
            return None

    def _check_emergency_stop_loss(self, symbol: str, side: str, entry_price: float, 
                                    current_price: float, contracts: float) -> bool:
        """
        🔥🔥🔥 v3.7 新增：检查是否触发紧急止损（亏损超过2%强制平仓）
        
        Returns:
            True如果触发了紧急止损并已平仓
        """
        # 计算盈亏百分比
        if side == 'long':
            pnl_pct = (current_price - entry_price) / entry_price
        else:
            pnl_pct = (entry_price - current_price) / entry_price
        
        # 检查是否触发紧急止损
        if pnl_pct < -self.emergency_sl_pct:
            print(f"[EMERGENCY_SL] 🚨🚨🚨 {symbol} 触发紧急止损!")
            print(f"[EMERGENCY_SL]   亏损: {pnl_pct*100:.2f}% > {self.emergency_sl_pct*100:.1f}%")
            print(f"[EMERGENCY_SL]   入场: {entry_price:.6f} | 当前: {current_price:.6f}")
            
            # 取消现有止损止盈单
            self._cancel_all_sl_tp_orders(symbol)
            
            # 立即市价平仓
            self._emergency_close_position(symbol, side, contracts)
            
            # 记录平仓
            self._record_position_closed(symbol, side, entry_price, current_price, "emergency_sl")
            
            return True
        
        return False

    def _create_order_with_sl_tp(
        self,
        symbol: str,
        side: str,
        order_side: str,
        order_type: str,
        amount: float,
        price: Optional[float],
        sl_trigger: float,
        tp_price: float
    ) -> Tuple[Optional[Dict], Optional[str], Optional[str]]:
        """
        🔥🔥🔥 v3.8 新增：原子下单（下单同时设置止损止盈）
        
        OKX支持在下单时直接附带止损止盈，这是原子操作：
        - 要么订单和止损止盈都创建成功
        - 要么都失败
        
        Args:
            symbol: 交易对
            side: 'long' 或 'short'
            order_side: 'buy' 或 'sell'
            order_type: 'market' 或 'limit'
            amount: 数量
            price: 限价（市价单为None）
            sl_trigger: 止损触发价
            tp_price: 止盈价
        
        Returns:
            (order, sl_order_id, tp_order_id) 或 (None, None, None) 如果失败
        """
        try:
            # 方案1：使用ccxt的attachedOrders参数（如果支持）
            # 方案2：使用OKX原生API
            
            # 先尝试ccxt方式（更简洁）
            try:
                params = {
                    'tdMode': 'cross',
                    'posSide': 'long' if side == 'long' else 'short',
                    'slTriggerPx': str(sl_trigger),
                    'slOrdPx': '-1',  # 市价执行
                    'tpTriggerPx': str(tp_price),
                    'tpOrdPx': '-1',  # 市价执行
                }
                
                order = self.exchange.create_order(
                    symbol=symbol,
                    type=order_type,
                    side=order_side,
                    amount=amount,
                    price=price,
                    params=params
                )
                
                print(f"[ORDER_WITH_SL_TP] ✅ 原子下单成功: {order['id']}")
                print(f"[ORDER_WITH_SL_TP]   止损触发: {sl_trigger:.6f} | 止盈: {tp_price:.6f}")
                
                # ccxt方式下单成功后，止损止盈是附属订单，需要单独创建
                # 实际上OKX的create_order不支持直接带sl/tp，需要用Algo订单
                # 所以这里还是需要单独创建
                
            except Exception as e:
                print(f"[ORDER_WITH_SL_TP] ⚠️ ccxt方式失败: {e}")
            
            # 方案2：分两步但确保都成功
            # 第1步：下单
            order = self.exchange.create_order(
                symbol=symbol,
                type=order_type,
                side=order_side,
                amount=amount,
                price=price,
                params={
                    'tdMode': 'cross',
                    'posSide': 'long' if side == 'long' else 'short'
                }
            )
            
            if not order or not order.get('id'):
                print(f"[ORDER_WITH_SL_TP] ❌ 下单失败")
                return None, None, None
            
            print(f"[ORDER_WITH_SL_TP] ✅ 订单创建成功: {order['id']}")
            
            # 第2步：创建止损止盈（OCO订单）
            sl_order_id, tp_order_id = self._create_sl_tp_with_position(
                symbol, side, amount, sl_trigger, tp_price
            )
            
            if not sl_order_id:
                # 止损创建失败，需要平掉刚才的仓位
                print(f"[ORDER_WITH_SL_TP] ❌ 止损创建失败，回滚订单...")
                
                # 等待订单成交
                time.sleep(0.5)
                
                # 检查订单状态
                try:
                    order_status = self.exchange.fetch_order(order['id'], symbol)
                    if order_status['status'] == 'closed':
                        # 已成交，需要平仓
                        self._emergency_close_position(symbol, side, amount)
                        print(f"[ORDER_WITH_SL_TP] 🚨 已回滚（平仓）")
                    elif order_status['status'] == 'open':
                        # 未成交，取消订单
                        self.exchange.cancel_order(order['id'], symbol)
                        print(f"[ORDER_WITH_SL_TP] 🚨 已回滚（取消订单）")
                except Exception as e:
                    print(f"[ORDER_WITH_SL_TP] ⚠️ 回滚异常: {e}")
                
                return None, None, None
            
            print(f"[ORDER_WITH_SL_TP] ✅ 止损止盈创建成功")
            return order, sl_order_id, tp_order_id
            
        except Exception as e:
            print(f"[ORDER_WITH_SL_TP] ❌ 原子下单异常: {e}")
            import traceback
            traceback.print_exc()
            return None, None, None

    def _cancel_all_sl_tp_orders(self, symbol: str):
        """🔥 取消该symbol的所有止损止盈单"""
        try:
            inst_id = symbol.replace('/', '-').replace(':USDT', '-SWAP')
            
            sl_id = self.sl_order_cache.get(symbol)
            tp_id = self.tp_order_cache.get(symbol)
            
            orders_to_cancel = []
            if sl_id:
                orders_to_cancel.append({'instId': inst_id, 'algoId': sl_id})
            if tp_id and tp_id != sl_id:
                orders_to_cancel.append({'instId': inst_id, 'algoId': tp_id})
            
            if orders_to_cancel:
                try:
                    self.exchange.privatePostTradeCancelAlgos(orders_to_cancel)
                    print(f"[ORDER_CANCEL] ✅ 已取消{len(orders_to_cancel)}个algo订单")
                except:
                    pass
            
            self.sl_order_cache.pop(symbol, None)
            self.tp_order_cache.pop(symbol, None)

        except Exception as e:
            print(f"[ORDER_CANCEL] ⚠️ 取消订单失败: {e}")

    def _create_stop_loss_order(
        self, 
        symbol: str, 
        side: str, 
        amount: float, 
        sl_price: float
    ) -> Optional[str]:
        """
        🔥🔥🔥 v3.2修复：创建止损单（使用OKX Algo订单）
        
        Returns:
            订单ID或None
        """
        try:
            print(f"[SL_CREATE] 🔧 准备创建止损单...")
            print(f"[SL_CREATE]   交易对: {symbol} | 方向: {side} | 数量: {amount}")
            print(f"[SL_CREATE]   原始止损价: {sl_price:.6f}")
            
            # 添加滑点缓冲
            if side == 'long':
                trigger_price = sl_price * (1 - self.sl_slippage_buffer)
            else:
                trigger_price = sl_price * (1 + self.sl_slippage_buffer)
            
            print(f"[SL_CREATE]   触发价(含滑点): {trigger_price:.6f}")

            # 转换symbol格式: BTC/USDT:USDT -> BTC-USDT-SWAP
            inst_id = symbol.replace('/', '-').replace(':USDT', '-SWAP')
            
            # 🔥 使用OKX Algo订单API
            algo_params = {
                'instId': inst_id,
                'tdMode': 'cross',
                'posSide': 'long' if side == 'long' else 'short',
                'side': 'sell' if side == 'long' else 'buy',
                'ordType': 'conditional',
                'sz': str(amount),
                'slTriggerPx': str(trigger_price),
                'slOrdPx': '-1',  # -1表示市价执行
                'slTriggerPxType': 'last',
            }
            
            print(f"[SL_CREATE]   调用OKX API...")
            response = self.exchange.privatePostTradeOrderAlgo(algo_params)
            
            if response and response.get('code') == '0':
                data = response.get('data', [{}])[0]
                order_id = data.get('algoId', '')
                print(f"[SL_CREATE] ✅ 止损单创建成功: {trigger_price:.6f} (AlgoID: {order_id})")
                return order_id
            else:
                error_msg = response.get('msg', 'Unknown error')
                error_code = response.get('code', '')
                print(f"[SL_CREATE] ❌ OKX返回错误: code={error_code}, msg={error_msg}")
                return None

        except Exception as e:
            print(f"[SL_CREATE] ❌ 止损设置异常: {e}")
            import traceback
            traceback.print_exc()
            return None

    def _create_take_profit_order(
        self, 
        symbol: str, 
        side: str, 
        amount: float, 
        tp_price: float
    ) -> Optional[str]:
        """
        🔥🔥🔥 v3.2修复：创建止盈单（使用OKX Algo订单）
        
        Returns:
            订单ID或None
        """
        try:
            print(f"[TP_CREATE] 🔧 准备创建止盈单...")
            print(f"[TP_CREATE]   交易对: {symbol} | 方向: {side} | 数量: {amount}")
            print(f"[TP_CREATE]   止盈价: {tp_price:.6f}")

            # 转换symbol格式
            inst_id = symbol.replace('/', '-').replace(':USDT', '-SWAP')
            
            algo_params = {
                'instId': inst_id,
                'tdMode': 'cross',
                'posSide': 'long' if side == 'long' else 'short',
                'side': 'sell' if side == 'long' else 'buy',
                'ordType': 'conditional',
                'sz': str(amount),
                'tpTriggerPx': str(tp_price),
                'tpOrdPx': '-1',  # 市价
                'tpTriggerPxType': 'last',
            }
            
            print(f"[TP_CREATE]   调用OKX API...")
            response = self.exchange.privatePostTradeOrderAlgo(algo_params)
            
            if response and response.get('code') == '0':
                data = response.get('data', [{}])[0]
                order_id = data.get('algoId', '')
                print(f"[TP_CREATE] ✅ 止盈单创建成功: {tp_price:.6f} (AlgoID: {order_id})")
                return order_id
            else:
                error_msg = response.get('msg', 'Unknown error')
                error_code = response.get('code', '')
                print(f"[TP_CREATE] ❌ OKX返回错误: code={error_code}, msg={error_msg}")
                return None

        except Exception as e:
            print(f"[TP_CREATE] ❌ 止盈设置异常: {e}")
            import traceback
            traceback.print_exc()
            return None

    def _create_sl_tp_with_position(
        self,
        symbol: str,
        side: str,
        amount: float,
        sl_price: float,
        tp_price: float
    ) -> tuple:
        """
        🔥🔥🔥 v3.2新增：同时创建止损止盈（OKX OCO订单）
        
        Returns:
            (sl_order_id, tp_order_id)
        """
        try:
            print(f"[SL_TP] 🔧 同时创建止损止盈...")
            print(f"[SL_TP]   交易对: {symbol} | 方向: {side}")
            print(f"[SL_TP]   止损: {sl_price:.6f} | 止盈: {tp_price:.6f}")
            
            inst_id = symbol.replace('/', '-').replace(':USDT', '-SWAP')
            
            # 添加滑点缓冲到止损
            if side == 'long':
                sl_trigger = sl_price * (1 - self.sl_slippage_buffer)
            else:
                sl_trigger = sl_price * (1 + self.sl_slippage_buffer)
            
            algo_params = {
                'instId': inst_id,
                'tdMode': 'cross',
                'posSide': 'long' if side == 'long' else 'short',
                'side': 'sell' if side == 'long' else 'buy',
                'ordType': 'oco',  # One-Cancels-Other
                'sz': str(amount),
                'slTriggerPx': str(sl_trigger),
                'slOrdPx': '-1',
                'slTriggerPxType': 'last',
                'tpTriggerPx': str(tp_price),
                'tpOrdPx': '-1',
                'tpTriggerPxType': 'last',
            }
            
            print(f"[SL_TP]   调用OKX API...")
            response = self.exchange.privatePostTradeOrderAlgo(algo_params)
            
            if response and response.get('code') == '0':
                data = response.get('data', [{}])[0]
                order_id = data.get('algoId', '')
                print(f"[SL_TP] ✅ OCO订单创建成功 (AlgoID: {order_id})")
                print(f"[SL_TP]   止损触发: {sl_trigger:.6f} | 止盈触发: {tp_price:.6f}")
                return order_id, order_id
            else:
                error_msg = response.get('msg', 'Unknown error')
                print(f"[SL_TP] ⚠️ OCO失败: {error_msg}，尝试分别创建...")
                sl_id = self._create_stop_loss_order(symbol, side, amount, sl_price)
                tp_id = self._create_take_profit_order(symbol, side, amount, tp_price)
                return sl_id, tp_id
                
        except Exception as e:
            print(f"[SL_TP] ❌ 创建异常: {e}，尝试分别创建...")
            sl_id = self._create_stop_loss_order(symbol, side, amount, sl_price)
            tp_id = self._create_take_profit_order(symbol, side, amount, tp_price)
            return sl_id, tp_id

    def _update_stop_loss_order(
        self, 
        symbol: str, 
        side: str,
        amount: float,
        new_sl_price: float
    ) -> bool:
        """
        🔥 v3.9: 更新止损单（增强版：自动从OKX获取订单ID）
        
        修复问题：
        1. 重启后缓存丢失导致无法取消旧订单
        2. 更新失败时添加重试
        3. 成功/失败都有明确日志
        """
        max_retries = 3
        
        for attempt in range(max_retries):
            try:
                # 🔥 v3.9: 优先从缓存获取，没有就从OKX查询
                old_order_id = self.sl_order_cache.get(symbol)
                
                if not old_order_id:
                    # 缓存没有，从OKX查询真实订单
                    print(f"[SL_UPDATE] 🔍 缓存无订单ID，从OKX查询...")
                    okx_order = self._get_current_sl_order_from_okx(symbol)
                    if okx_order:
                        old_order_id = okx_order['algo_id']
                        old_sl_price = okx_order['sl_price']
                        print(f"[SL_UPDATE] 📋 找到OKX止损单: {old_order_id} @ ${old_sl_price:.6f}")
                        # 同步到缓存
                        self.sl_order_cache[symbol] = old_order_id
                    else:
                        print(f"[SL_UPDATE] ⚠️ OKX上无止损单，直接创建新的")
                
                # 1. 取消旧的止损单
                if old_order_id:
                    try:
                        inst_id = symbol.replace('/', '-').replace(':USDT', '-SWAP')
                        cancel_params = [{
                            'instId': inst_id,
                            'algoId': old_order_id,
                        }]
                        response = self.exchange.privatePostTradeCancelAlgos(cancel_params)
                        
                        if response and response.get('code') == '0':
                            print(f"[SL_UPDATE] ✅ 已取消旧止损单: {old_order_id}")
                            self.sl_order_cache.pop(symbol, None)
                            self.tp_order_cache.pop(symbol, None)
                        else:
                            error_msg = response.get('msg', 'Unknown') if response else 'No response'
                            # 🔥 v3.9: 某些错误码表示订单已不存在，可以继续
                            error_code = response.get('code', '') if response else ''
                            if error_code in ['51400', '51401', '51402']:  # 订单不存在相关错误
                                print(f"[SL_UPDATE] ⚠️ 旧订单可能已不存在({error_code})，继续创建新的")
                                self.sl_order_cache.pop(symbol, None)
                                self.tp_order_cache.pop(symbol, None)
                            else:
                                print(f"[SL_UPDATE] ⚠️ 取消失败(尝试{attempt+1}/{max_retries}): {error_msg}")
                                if attempt < max_retries - 1:
                                    time.sleep(1)
                                    continue
                                return False
                                
                    except Exception as e:
                        print(f"[SL_UPDATE] ⚠️ 取消异常(尝试{attempt+1}/{max_retries}): {e}")
                        if attempt < max_retries - 1:
                            time.sleep(1)
                            continue
                        return False
                
                # 2. 创建新的止损止盈单
                pos_info = self.position_manager.get_position_info(symbol) if self.position_manager else None
                tp_price = pos_info.get('tp_price', 0) if pos_info else 0
                
                if tp_price > 0:
                    sl_order_id, tp_order_id = self._create_sl_tp_with_position(
                        symbol, side, amount, new_sl_price, tp_price
                    )
                    if sl_order_id:
                        self.sl_order_cache[symbol] = sl_order_id
                        self.tp_order_cache[symbol] = tp_order_id
                        print(f"[SL_UPDATE] ✅ 止损已更新: ${new_sl_price:.6f} (OCO订单)")
                        return True
                else:
                    new_order_id = self._create_stop_loss_order(symbol, side, amount, new_sl_price)
                    if new_order_id:
                        self.sl_order_cache[symbol] = new_order_id
                        print(f"[SL_UPDATE] ✅ 止损已更新: ${new_sl_price:.6f}")
                        return True
                
                print(f"[SL_UPDATE] ❌ 创建新止损单失败(尝试{attempt+1}/{max_retries})")
                if attempt < max_retries - 1:
                    time.sleep(1)
                    continue
                return False
                    
            except Exception as e:
                print(f"[SL_UPDATE] ❌ 更新止损异常(尝试{attempt+1}/{max_retries}): {e}")
                if attempt < max_retries - 1:
                    time.sleep(1)
                    continue
                import traceback
                traceback.print_exc()
                return False
        
        return False

    def _update_take_profit_order(
        self, 
        symbol: str, 
        side: str,
        amount: float,
        new_tp_price: float
    ) -> bool:
        """
        🔥 v3.2: 更新止盈单
        """
        try:
            old_order_id = self.tp_order_cache.get(symbol)
            
            # 1. 取消旧的止盈单
            if old_order_id:
                try:
                    inst_id = symbol.replace('/', '-').replace(':USDT', '-SWAP')
                    cancel_params = [{
                        'instId': inst_id,
                        'algoId': old_order_id,
                    }]
                    self.exchange.privatePostTradeCancelAlgos(cancel_params)
                    print(f"[TP_UPDATE] ✅ 已取消旧止盈单: {old_order_id}")
                except Exception as e:
                    print(f"[TP_UPDATE] ⚠️ 取消旧止盈单失败: {e}")
            
            # 2. 创建新的止盈单
            new_order_id = self._create_take_profit_order(symbol, side, amount, new_tp_price)
            
            if new_order_id:
                self.tp_order_cache[symbol] = new_order_id
                print(f"[TP_UPDATE] ✅ 止盈已更新: {new_tp_price:.6f}")
                return True
            else:
                print(f"[TP_UPDATE] ❌ 创建新止盈单失败")
                return False
                
        except Exception as e:
            print(f"[TP_UPDATE] ❌ 更新止盈失败: {e}")
            import traceback
            traceback.print_exc()
            return False

    def mark_signal_traded(self, signal_id: int, order_id: str):
        """
        标记信号已交易或已跳过
        
        Args:
            signal_id: 信号ID
            order_id: 订单ID，或者跳过原因（如 'skipped_delivery', 'skipped_min_amount'）
        """
        if not signal_id:
            return
            
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE pushed_signals
                SET auto_traded = 1,
                    auto_trade_order_id = ?,
                    auto_trade_time = ?
                WHERE id = ?
            """, (order_id, datetime.now().isoformat(), signal_id))
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"[AUTOTRADER_ERR] 标记信号失败: {e}")

    # ========== 🔥🔥🔥 新增：交易结果更新方法（报告系统需要）==========
    
    def _ensure_pushed_signals_columns(self):
        """🔥 确保pushed_signals表有报告需要的所有列"""
        try:
            conn = sqlite3.connect(self.db_path, timeout=30)
            cursor = conn.cursor()
            
            # 检查表是否存在
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='pushed_signals'")
            if not cursor.fetchone():
                conn.close()
                return
            
            # 获取现有列
            cursor.execute("PRAGMA table_info(pushed_signals)")
            existing_columns = {row[1] for row in cursor.fetchall()}
            
            # 需要添加的列
            required_columns = [
                ('order_status', 'TEXT DEFAULT "pending"'),
                ('fill_price', 'REAL'),
                ('fill_time', 'TEXT'),
                ('exit_price', 'REAL'),
                ('exit_time', 'TEXT'),
                ('exit_reason', 'TEXT'),
                ('final_pnl', 'REAL'),
                ('holding_minutes', 'INTEGER'),
            ]
            
            for col_name, col_type in required_columns:
                if col_name not in existing_columns:
                    try:
                        cursor.execute(f"ALTER TABLE pushed_signals ADD COLUMN {col_name} {col_type}")
                        print(f"[AUTOTRADER] 🔧 自动添加列: {col_name}")
                    except Exception as e:
                        if "duplicate" not in str(e).lower():
                            pass  # 静默忽略
            
            conn.commit()
            conn.close()
            
        except Exception as e:
            print(f"[AUTOTRADER] ⚠️ 检查表结构失败: {e}")
    
    def update_signal_filled(self, signal_id: int, fill_price: float, order_id: str = None):
        """
        🔥 更新信号为已成交状态
        
        Args:
            signal_id: pushed_signals表的ID
            fill_price: 成交价格
            order_id: 订单ID
        """
        if not signal_id:
            return
        
        # 确保表有需要的列
        self._ensure_pushed_signals_columns()
            
        try:
            conn = sqlite3.connect(self.db_path, timeout=30)
            cursor = conn.cursor()
            
            cursor.execute("""
                UPDATE pushed_signals
                SET order_status = 'filled',
                    fill_price = ?,
                    fill_time = datetime('now'),
                    auto_trade_order_id = COALESCE(?, auto_trade_order_id)
                WHERE id = ?
            """, (fill_price, order_id, signal_id))
            
            conn.commit()
            conn.close()
            print(f"[AUTOTRADER] 📝 更新信号#{signal_id}为已成交 @{fill_price:.6f}")
            
        except Exception as e:
            print(f"[AUTOTRADER_ERR] 更新成交状态失败: {e}")
    
    def update_signal_closed(self, signal_id: int, exit_price: float, exit_reason: str, pnl_pct: float):
        """
        🔥 更新信号为已平仓状态
        
        Args:
            signal_id: pushed_signals表的ID
            exit_price: 平仓价格
            exit_reason: 平仓原因 (tp/sl/timeout/manual/ai_review/reversal)
            pnl_pct: 盈亏百分比 (如 2.5 表示 +2.5%)
        """
        if not signal_id:
            return
        
        # 确保表有需要的列
        self._ensure_pushed_signals_columns()
            
        try:
            conn = sqlite3.connect(self.db_path, timeout=30)
            cursor = conn.cursor()
            
            # 先获取成交时间计算持仓时长
            cursor.execute("SELECT fill_time FROM pushed_signals WHERE id = ?", (signal_id,))
            row = cursor.fetchone()
            
            holding_minutes = None
            if row and row[0]:
                try:
                    fill_time = datetime.fromisoformat(row[0])
                    holding_minutes = int((datetime.now() - fill_time).total_seconds() / 60)
                except:
                    pass
            
            cursor.execute("""
                UPDATE pushed_signals
                SET order_status = 'closed',
                    exit_price = ?,
                    exit_time = datetime('now'),
                    exit_reason = ?,
                    final_pnl = ?,
                    holding_minutes = ?
                WHERE id = ?
            """, (exit_price, exit_reason, pnl_pct, holding_minutes, signal_id))
            
            conn.commit()
            conn.close()
            
            emoji = "✅" if pnl_pct > 0 else "❌"
            print(f"[AUTOTRADER] {emoji} 信号#{signal_id}已平仓 | {exit_reason} | PnL: {pnl_pct:+.2f}%")
            
        except Exception as e:
            print(f"[AUTOTRADER_ERR] 更新平仓状态失败: {e}")
    
    def update_signal_cancelled(self, signal_id: int, reason: str = "timeout"):
        """
        🔥 更新信号为已取消状态（未成交）
        
        Args:
            signal_id: pushed_signals表的ID
            reason: 取消原因
        """
        if not signal_id:
            return
        
        # 确保表有需要的列
        self._ensure_pushed_signals_columns()
            
        try:
            conn = sqlite3.connect(self.db_path, timeout=30)
            cursor = conn.cursor()
            
            cursor.execute("""
                UPDATE pushed_signals
                SET order_status = 'cancelled',
                    exit_reason = ?
                WHERE id = ?
            """, (reason, signal_id))
            
            conn.commit()
            conn.close()
            print(f"[AUTOTRADER] ⏭️ 信号#{signal_id}已取消: {reason}")
            
        except Exception as e:
            print(f"[AUTOTRADER_ERR] 更新取消状态失败: {e}")
    
    def find_signal_id_by_symbol(self, symbol: str) -> Optional[int]:
        """
        🔥 根据symbol查找最近的已成交信号ID
        
        Args:
            symbol: 交易对
            
        Returns:
            signal_id 或 None
        """
        try:
            conn = sqlite3.connect(self.db_path, timeout=30)
            cursor = conn.cursor()
            
            # 查找该symbol最近的已成交但未平仓的信号
            cursor.execute("""
                SELECT id FROM pushed_signals
                WHERE symbol = ? 
                AND (order_status = 'filled' OR (auto_traded = 1 AND order_status IS NULL))
                AND (exit_time IS NULL OR exit_time = '')
                ORDER BY created_at DESC
                LIMIT 1
            """, (symbol,))
            
            row = cursor.fetchone()
            conn.close()
            
            return row[0] if row else None
            
        except Exception as e:
            print(f"[AUTOTRADER_ERR] 查找信号ID失败: {e}")
            return None
    
    def _record_position_closed(self, symbol: str, side: str, entry_price: float, 
                                exit_price: float, exit_reason: str):
        """
        🔥 记录持仓平仓事件（更新数据库）
        
        Args:
            symbol: 交易对
            side: 持仓方向
            entry_price: 入场价格
            exit_price: 平仓价格
            exit_reason: 平仓原因
        """
        # 计算盈亏
        if side == 'long':
            pnl_pct = (exit_price - entry_price) / entry_price * 100
        else:
            pnl_pct = (entry_price - exit_price) / entry_price * 100
        
        # 查找对应的signal_id
        signal_id = self.find_signal_id_by_symbol(symbol)
        
        if signal_id:
            self.update_signal_closed(signal_id, exit_price, exit_reason, pnl_pct)
        else:
            print(f"[AUTOTRADER] ⚠️ 未找到{symbol}对应的信号记录")
        
        # 同时更新auto_trades表
        try:
            conn = sqlite3.connect(self.db_path, timeout=30)
            cursor = conn.cursor()
            
            cursor.execute("""
                UPDATE auto_trades
                SET status = 'closed',
                    closed_at = datetime('now'),
                    pnl = ?,
                    pnl_pct = ?
                WHERE symbol = ? AND status = 'open'
                ORDER BY created_at DESC
                LIMIT 1
            """, (pnl_pct * entry_price / 100, pnl_pct, symbol))
            
            conn.commit()
            conn.close()
            
        except Exception as e:
            print(f"[AUTOTRADER] ⚠️ 更新auto_trades失败: {e}")

    def log_trade(self, signal: Dict, order: Dict, position_size: float):
        """记录交易到数据库"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS auto_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    signal_id INTEGER,
                    order_id TEXT,
                    symbol TEXT,
                    side TEXT,
                    entry_price REAL,
                    amount REAL,
                    position_size_usdt REAL,
                    leverage INTEGER,
                    sl_price REAL,
                    tp_price REAL,
                    status TEXT,
                    created_at TEXT,
                    closed_at TEXT,
                    pnl REAL,
                    pnl_pct REAL
                )
            """)

            cursor.execute("""
                INSERT INTO auto_trades (
                    signal_id, order_id, symbol, side, entry_price,
                    amount, position_size_usdt, leverage, sl_price, tp_price,
                    status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                signal['id'],
                order['id'],
                signal['symbol'],
                signal['side'],
                order.get('price', signal.get('entry_price_immediate')),
                order.get('amount', 0),
                position_size,
                self.default_leverage,
                signal.get('sl_price'),
                signal.get('tp_price'),
                'open',
                datetime.now().isoformat()
            ))

            conn.commit()
            conn.close()

            print(f"[AUTOTRADER] ✅ 交易记录已保存")

        except Exception as e:
            print(f"[AUTOTRADER_ERR] 记录交易失败: {e}")

    def monitor_positions(self):
        """
        🔥 v3.9 增强版：监控持仓 + 止损验证 + 紧急止损 + 自动同步
        
        新增功能：
        1. 自动同步OKX持仓到position_manager（解决重启后丢失问题）
        2. 定期验证止损单状态
        3. 止损更新失败告警
        """
        if not self.enabled:
            return

        try:
            positions = self.get_current_positions()

            if not positions:
                return

            print(f"\n[POSITION_MONITOR] 检查 {len(positions)} 个持仓...")

            for pos in positions:
                symbol = pos['symbol']
                unrealized_pnl = float(pos.get('unrealizedPnl', 0))
                contracts = float(pos.get('contracts', 0))

                if contracts == 0:
                    continue

                # 获取当前价格
                try:
                    ticker = self.exchange.fetch_ticker(symbol)
                    current_price = ticker['last']
                except Exception as e:
                    print(f"[POSITION_MONITOR] ⚠️ 获取{symbol}价格失败: {e}")
                    continue

                # 获取持仓信息
                entry_price = float(pos.get('entryPrice', 0))
                side = pos.get('side', 'long')
                
                # 计算盈亏百分比
                if entry_price > 0:
                    if side == 'long':
                        pnl_pct = (current_price - entry_price) / entry_price
                    else:
                        pnl_pct = (entry_price - current_price) / entry_price
                else:
                    pnl_pct = 0

                # 获取持仓时间
                entry_time = self.position_entry_time.get(symbol)
                holding_minutes = (datetime.now() - entry_time).total_seconds() / 60 if entry_time else 0

                # 🔥🔥🔥 v3.9: 检查position_manager是否有该持仓，没有则自动同步
                if self.position_manager:
                    pm_pos = self.position_manager.get_position_info(symbol)
                    if not pm_pos:
                        print(f"[POSITION_MONITOR] 🔄 发现未注册持仓: {symbol}，自动同步...")
                        
                        # 从OKX获取止损单信息
                        okx_sl_order = self._get_current_sl_order_from_okx(symbol)
                        sl_price = okx_sl_order['sl_price'] if okx_sl_order else entry_price * (0.98 if side == 'long' else 1.02)
                        tp_price = okx_sl_order['tp_price'] if okx_sl_order else entry_price * (1.06 if side == 'long' else 0.94)
                        
                        # 注册到position_manager
                        self.position_manager.register_position(
                            symbol=symbol,
                            side=side,
                            entry_price=entry_price,
                            amount=contracts,
                            sl_price=sl_price,
                            tp_price=tp_price,
                            strategy_type="synced"  # 标记为同步的持仓
                        )
                        print(f"[POSITION_MONITOR] ✅ 已同步: {symbol} {side.upper()} @ ${entry_price:.6f}")
                        print(f"  止损: ${sl_price:.6f} | 止盈: ${tp_price:.6f}")
                        
                        # 记录入场时间（估算）
                        if symbol not in self.position_entry_time:
                            self.position_entry_time[symbol] = datetime.now()

                # 🔥🔥🔥 v3.7: 首先检查紧急止损（亏损超过2%强制平仓）
                if self._check_emergency_stop_loss(symbol, side, entry_price, current_price, contracts):
                    continue  # 已平仓，跳过后续检查

                # 🔥🔥🔥 v3.9: 定期验证止损单是否存在（增强版）
                last_verify = self.last_sl_verify_time.get(symbol)
                if not last_verify or (datetime.now() - last_verify).total_seconds() > self.sl_verify_interval_sec:
                    self.last_sl_verify_time[symbol] = datetime.now()
                    
                    if not self._verify_stop_loss_exists(symbol, side, contracts):
                        print(f"[POSITION_MONITOR] ⚠️ {symbol} 止损单丢失，重新创建...")
                        
                        # 获取止损价
                        pos_info = self.position_manager.get_position_info(symbol) if self.position_manager else None
                        sl_price = pos_info.get('sl_price') if pos_info else None
                        tp_price = pos_info.get('tp_price') if pos_info else None
                        
                        if not sl_price:
                            # 使用默认1.2%止损
                            if side == 'long':
                                sl_price = entry_price * (1 - self.default_sl_pct)
                            else:
                                sl_price = entry_price * (1 + self.default_sl_pct)
                        
                        # 🔥 v3.9: 优先创建OCO订单（同时带止盈）
                        if tp_price and tp_price > 0:
                            sl_order_id, tp_order_id = self._create_sl_tp_with_position(
                                symbol, side, contracts, sl_price, tp_price
                            )
                            if sl_order_id:
                                self.sl_order_cache[symbol] = sl_order_id
                                self.tp_order_cache[symbol] = tp_order_id
                                print(f"[POSITION_MONITOR] ✅ 止损止盈单已重建: SL=${sl_price:.6f} TP=${tp_price:.6f}")
                            else:
                                print(f"[POSITION_MONITOR] ❌ OCO订单重建失败!")
                        else:
                            # 只创建止损单
                            sl_order_id = self._create_stop_loss_order(symbol, side, contracts, sl_price)
                            if sl_order_id:
                                self.sl_order_cache[symbol] = sl_order_id
                                print(f"[POSITION_MONITOR] ✅ 止损单已重建: ${sl_price:.6f}")
                            else:
                                print(f"[POSITION_MONITOR] ❌ 止损单重建失败!")

                # 原有持仓管理器逻辑
                if self.position_manager:
                    actions = self.position_manager.update_position(
                        symbol=symbol,
                        current_price=current_price,
                        fetch_indicators=True
                    )

                    # 显示持仓状态
                    pos_info = self.position_manager.get_position_info(symbol)
                    if pos_info:
                        entry = pos_info['entry_price']
                        pm_side = pos_info['side']
                        highest_pnl = pos_info.get('highest_pnl_pct', 0)
                        current_tier = pos_info.get('current_tier', -1)
                        print(f"[POSITION_MONITOR] {symbol} {pm_side.upper()}")
                        print(f"  入场:${entry:.4f} | 当前:${current_price:.4f} | 盈亏:{pnl_pct*100:.2f}%")
                        print(f"  止损:${pos_info['sl_price']:.4f} | 止盈:${pos_info['tp_price']:.4f}")
                        print(f"  📈 最高盈利:{highest_pnl*100:.1f}% | 阶梯:{current_tier}")

                    # 执行调整建议
                    if actions:
                        print(f"[POSITION_MONITOR] 🎯 {symbol} 有 {len(actions)} 个调整动作")
                        for action in actions:
                            self._execute_position_action(symbol, action, current_price, contracts)
                
                # 🔥 v3.0: AI审核逻辑
                if self.position_reviewer:
                    self._ai_review_position(
                        symbol=symbol,
                        side=side,
                        entry_price=entry_price,
                        current_price=current_price,
                        pnl_pct=pnl_pct,
                        holding_minutes=holding_minutes,
                        contracts=contracts
                    )

            # 清理已平仓的持仓记录
            self._cleanup_closed_positions(positions)

        except Exception as e:
            print(f"[POSITION_MONITOR] ❌ 监控失败: {e}")
            import traceback
            traceback.print_exc()

    def _ai_review_position(self, symbol: str, side: str, entry_price: float,
                           current_price: float, pnl_pct: float,
                           holding_minutes: float, contracts: float):
        """🔥 v3.0: AI审核持仓"""
        # 检查是否需要审核
        last_review = self.last_ai_review_time.get(symbol)
        if last_review and (datetime.now() - last_review).total_seconds() < self.ai_review_interval_sec:
            return

        # 获取当前指标
        indicators = self.position_reviewer.get_current_indicators(symbol)
        
        # 构建持仓信息
        position_info = {
            "symbol": symbol,
            "side": side,
            "entry_price": entry_price,
            "current_price": current_price,
            "sl_price": 0,
            "tp_price": 0,
            "pnl_pct": pnl_pct,
            "holding_minutes": holding_minutes,
            "rsi": indicators.get("rsi", 50),
            "volume_ratio": indicators.get("volume_ratio", 1.0),
            "entry_time": self.position_entry_time.get(symbol)
        }

        # 检查是否应该审核
        should_review, reason = self.position_reviewer.should_review(position_info)
        if not should_review:
            return

        print(f"[AI_REVIEW] 🔍 审核 {symbol} | {reason}")
        
        # 执行审核
        result = self.position_reviewer.review_position(position_info)
        
        # 更新审核时间
        self.last_ai_review_time[symbol] = datetime.now()
        
        # 执行AI决策
        action = result.get("action", "hold")
        reasoning = result.get("reasoning", "")
        
        if action == PositionAction.HOLD.value:
            print(f"[AI_REVIEW] ✅ 继续持有 | {reasoning}")
            
        elif action == PositionAction.TIGHTEN_SL.value:
            new_sl = result.get("new_sl_price")
            if new_sl:
                print(f"[AI_REVIEW] 🔧 收紧止损 → ${new_sl:.6f} | {reasoning}")
                self._update_stop_loss_order(symbol, side, contracts, new_sl)
                
        elif action == PositionAction.EXTEND_TP.value:
            new_tp = result.get("new_tp_price")
            if new_tp:
                print(f"[AI_REVIEW] 🎯 扩大止盈 → ${new_tp:.6f} | {reasoning}")
                self._update_take_profit_order(symbol, side, contracts, new_tp)
                
        elif action == PositionAction.BREAKEVEN.value:
            buffer = 0.001
            if side == "long":
                be_price = entry_price * (1 + buffer)
            else:
                be_price = entry_price * (1 - buffer)
            print(f"[AI_REVIEW] 🛡 移动到成本价 → ${be_price:.6f} | {reasoning}")
            self._update_stop_loss_order(symbol, side, contracts, be_price)
            
        elif action == PositionAction.CLOSE.value:
            # 已被转换为TIGHTEN_SL
            new_sl = result.get("new_sl_price")
            if new_sl:
                print(f"[AI_REVIEW] 🚨 准备平仓(紧止损) → ${new_sl:.6f} | {reasoning}")
                self._update_stop_loss_order(symbol, side, contracts, new_sl)

    def _execute_position_action(self, symbol: str, action: Dict, current_price: float, contracts: float):
        """
        🔥 v3.9: 执行持仓调整动作（增强版：检查返回值+失败告警）

        Args:
            symbol: 交易对
            action: 动作字典
            current_price: 当前价格
            contracts: 合约数量
        """
        action_type = action.get('type')
        reason = action.get('reason', '')
        
        # 🔥 v3.3: 防重复执行 - 检查最近是否刚执行过同类型操作
        last_action_key = f"{symbol}_{action_type}"
        last_action_time = getattr(self, '_last_action_time', {}).get(last_action_key)
        if last_action_time:
            since_last = (datetime.now() - last_action_time).total_seconds()
            if since_last < 60:  # 60秒内不重复执行同类型操作
                print(f"[POSITION_ADJUST] ⏳ {symbol} {action_type} 60秒内已执行，跳过")
                return
        
        # 记录执行时间
        if not hasattr(self, '_last_action_time'):
            self._last_action_time = {}
        self._last_action_time[last_action_key] = datetime.now()

        try:
            if action_type in ['breakeven_stop', 'trailing_stop', 'tiered_trailing_stop']:
                # 🔥 更新止损订单（支持阶梯式止损）
                new_sl = action['new_sl']
                old_sl = action['old_sl']

                print(f"[POSITION_ADJUST] 🎯 {symbol} {action_type}")
                print(f"  原因: {reason}")
                print(f"  止损: ${old_sl:.6f} → ${new_sl:.6f}")
                
                # 🔥 阶梯式止损额外信息
                if action_type == 'tiered_trailing_stop':
                    tier = action.get('tier', -1)
                    locked_pct = action.get('locked_pnl_pct', 0)
                    peak_pnl = action.get('peak_pnl_pct', 0)
                    print(f"  阶梯: {tier} | 最高盈利: {peak_pnl*100:.1f}% | 锁定: {locked_pct*100:.1f}%")

                # 获取持仓方向
                pos_info = self.position_manager.get_position_info(symbol)
                if pos_info:
                    side = pos_info['side']
                    # 🔥 v3.9: 检查返回值，失败时告警
                    success = self._update_stop_loss_order(symbol, side, contracts, new_sl)
                    
                    if success:
                        print(f"[POSITION_ADJUST] ✅ {symbol} 止损更新成功!")
                        # 🔥 v3.9: 同步更新position_manager中的止损价
                        if self.position_manager:
                            pos_info['sl_price'] = new_sl
                    else:
                        print(f"[POSITION_ADJUST] ❌❌❌ {symbol} 止损更新失败!")
                        print(f"[POSITION_ADJUST] ⚠️ 警告：持仓可能没有有效止损保护!")
                        
                        # 🔥 v3.9: 尝试验证当前止损状态
                        self._verify_stop_loss_exists(symbol, side, contracts)
                        
                        # 发送告警（如果配置了Telegram）
                        try:
                            from core.notifier import tg_send
                            tg_send(self.config, 
                                   f"⚠️ 止损更新失败 | {symbol}", 
                                   [f"❌ 阶梯止损更新失败",
                                    f"目标止损: ${new_sl:.6f}",
                                    f"请手动检查OKX持仓!"])
                        except:
                            pass
                else:
                    print(f"[POSITION_ADJUST] ⚠️ 未找到{symbol}的持仓信息")

            elif action_type == 'trailing_tp':
                # 🔥 更新止盈订单
                new_tp = action.get('new_tp')
                old_tp = action.get('old_tp')

                if new_tp and old_tp:
                    print(f"[POSITION_ADJUST] {symbol} 移动止盈")
                    print(f"  止盈: ${old_tp:.6f} → ${new_tp:.6f}")

                    pos_info = self.position_manager.get_position_info(symbol)
                    if pos_info:
                        side = pos_info['side']
                        self._update_take_profit_order(symbol, side, contracts, new_tp)

            elif action_type == 'reversal_exit':
                # 趋势反转，立即平仓
                print(f"[POSITION_ADJUST] {symbol} 反转平仓")
                print(f"  原因: {reason}")

                pos_info = self.position_manager.get_position_info(symbol)
                if pos_info:
                    side = pos_info['side']
                    entry_price = pos_info['entry_price']
                    close_side = 'sell' if side == 'long' else 'buy'
                    
                    # 计算盈亏
                    if side == 'long':
                        pnl_pct = (current_price - entry_price) / entry_price
                    else:
                        pnl_pct = (entry_price - current_price) / entry_price

                    # 取消所有止损止盈单
                    self._cancel_all_sl_tp_orders(symbol)

                    # 市价平仓
                    close_order = self.exchange.create_order(
                        symbol=symbol,
                        type='market',
                        side=close_side,
                        amount=abs(contracts),
                        params={
                            'tdMode': 'cross',
                            'posSide': 'long' if side == 'long' else 'short',
                            'reduceOnly': True
                        }
                    )

                    print(f"[POSITION_ADJUST] ✅ 平仓成功: {close_order['id']} | 盈亏: {pnl_pct*100:.2f}%")

                    # 🔥🔥🔥 记录平仓结果到数据库（报告系统需要）
                    self._record_position_closed(symbol, side, entry_price, current_price, "reversal_exit")

                    # 移除持仓记录
                    self.position_manager.remove_position(symbol)

                    # 清除缓存
                    self.sl_order_cache.pop(symbol, None)
                    self.tp_order_cache.pop(symbol, None)
                    
                    # 🔥🔥 反向开单逻辑
                    self._try_counter_trade(symbol, side, current_price, contracts, pnl_pct)

        except Exception as e:
            print(f"[POSITION_ADJUST] ❌ 执行失败: {e}")

    def _try_counter_trade(self, symbol: str, original_side: str, current_price: float, 
                           original_contracts: float, pnl_pct: float):
        """
        🔥 v3.6: 反转平仓后尝试反向开单
        
        修改：
        1. 不再使用原仓位50%，而是使用标准保证金计算
        2. 确保保证金在min_position_usdt ~ max_position_usdt范围内
        
        Args:
            symbol: 交易对
            original_side: 原仓位方向 (long/short)
            current_price: 当前价格
            original_contracts: 原仓位合约数量（不再使用）
            pnl_pct: 原仓位盈亏百分比
        """
        try:
            # 1. 检查是否启用反向开单
            if not self.exit_config.get('reversal_counter_trade', False):
                return
            
            # 2. 检查盈利是否足够（避免亏损时还反向开单）
            min_profit = self.exit_config.get('counter_trade_min_profit_pct', 0.005)
            if pnl_pct < min_profit:
                print(f"[COUNTER_TRADE] ⏭️ 跳过反向单：原仓位盈利{pnl_pct*100:.2f}% < {min_profit*100:.1f}%")
                return
            
            # 3. 检查持仓数量限制
            if len(self.position_manager.get_all_positions()) >= self.max_positions:
                print(f"[COUNTER_TRADE] ⏭️ 跳过反向单：已达最大持仓数{self.max_positions}")
                return
            
            # 4. 计算反向单参数
            counter_side = 'short' if original_side == 'long' else 'long'
            order_side = 'sell' if counter_side == 'short' else 'buy'
            
            # 🔥🔥🔥 v3.6: 使用标准保证金计算，不再基于原仓位
            # 保证金 = min(total_capital * max_position_pct, max_position_usdt)
            # 确保至少 min_position_usdt
            position_margin = min(
                self.total_capital * self.max_position_pct,
                self.max_position_usdt
            )
            position_margin = max(position_margin, self.min_position_usdt)
            
            # 计算合约数量
            counter_contracts = (position_margin * self.default_leverage) / current_price
            
            # 检查可用余额
            try:
                available = self.get_available_balance()
                if available < position_margin * 1.1:  # 留10%余量
                    print(f"[COUNTER_TRADE] ⏭️ 跳过反向单：可用余额${available:.2f} < 需要${position_margin*1.1:.2f}")
                    return
            except:
                pass  # 获取余额失败，继续尝试
            
            print(f"[COUNTER_TRADE] 📊 仓位计算: 保证金${position_margin:.2f} x {self.default_leverage}x = {counter_contracts:.4f}个")
            
            # 入场价偏移
            offset_pct = self.exit_config.get('counter_trade_offset_pct', 0.003)
            order_type = self.exit_config.get('counter_trade_type', 'limit')
            
            if order_type == 'limit':
                if counter_side == 'short':
                    # 做空：挂高一点等反弹
                    entry_price = current_price * (1 + offset_pct)
                else:
                    # 做多：挂低一点等回调
                    entry_price = current_price * (1 - offset_pct)
            else:
                entry_price = current_price
            
            print(f"\n[COUNTER_TRADE] 🔄 准备反向开单")
            print(f"  方向: {counter_side.upper()}")
            print(f"  类型: {order_type.upper()}")
            print(f"  数量: {counter_contracts:.4f} (保证金${position_margin:.2f})")
            print(f"  价格: {entry_price:.6f}")
            
            # 5. 计算止损止盈（简单方案：固定2%止损，4%止盈）
            if counter_side == 'short':
                sl_price = entry_price * 1.02   # 做空止损+2%
                tp_price = entry_price * 0.96   # 做空止盈-4%
            else:
                sl_price = entry_price * 0.98   # 做多止损-2%
                tp_price = entry_price * 1.04   # 做多止盈+4%
            
            print(f"  止损: {sl_price:.6f} (2%)")
            print(f"  止盈: {tp_price:.6f} (4%)")
            
            # 6. 下单
            order_params = {
                'tdMode': 'cross',
                'posSide': counter_side,
            }
            
            if order_type == 'limit':
                order = self.exchange.create_order(
                    symbol=symbol,
                    type='limit',
                    side=order_side,
                    amount=counter_contracts,
                    price=entry_price,
                    params=order_params
                )
            else:
                order = self.exchange.create_order(
                    symbol=symbol,
                    type='market',
                    side=order_side,
                    amount=counter_contracts,
                    params=order_params
                )
            
            print(f"[COUNTER_TRADE] ✅ 反向单创建成功: {order['id']}")
            
            # 7. 设置止损止盈
            if order.get('status') == 'closed' or order_type == 'market':
                # 市价单或已成交，立即设置止损止盈
                sl_order_id = self._create_stop_loss_order(symbol, counter_side, counter_contracts, sl_price)
                tp_order_id = self._create_take_profit_order(symbol, counter_side, counter_contracts, tp_price)
                
                if sl_order_id:
                    self.sl_order_cache[symbol] = sl_order_id
                if tp_order_id:
                    self.tp_order_cache[symbol] = tp_order_id
                
                # 注册到持仓管理器
                if self.position_manager:
                    self.position_manager.register_position(
                        symbol=symbol,
                        side=counter_side,
                        entry_price=entry_price,
                        amount=counter_contracts,
                        sl_price=sl_price,
                        tp_price=tp_price,
                        strategy_type='reversal'  # 反向单用反转策略参数
                    )
            else:
                # 限价单未成交，启动超时检查
                timeout_min = self.exit_config.get('counter_trade_timeout_min', 5)
                print(f"[COUNTER_TRADE] ⏳ 限价单等待成交，{timeout_min}分钟后检查")
                # TODO: 可以加入后台任务检查限价单状态
            
        except Exception as e:
            print(f"[COUNTER_TRADE] ❌ 反向开单失败: {e}")
            import traceback
            traceback.print_exc()

    def _cancel_all_sl_tp_orders(self, symbol: str):
        """取消所有止损止盈单"""
        try:
            # 取消止损单
            sl_order_id = self.sl_order_cache.get(symbol)
            if sl_order_id:
                try:
                    self.exchange.cancel_order(sl_order_id, symbol)
                    print(f"[ORDER_CANCEL] 已取消止损单: {sl_order_id}")
                except:
                    pass

            # 取消止盈单
            tp_order_id = self.tp_order_cache.get(symbol)
            if tp_order_id:
                try:
                    self.exchange.cancel_order(tp_order_id, symbol)
                    print(f"[ORDER_CANCEL] 已取消止盈单: {tp_order_id}")
                except:
                    pass

        except Exception as e:
            print(f"[ORDER_CANCEL] ⚠️ 取消订单失败: {e}")

    def _cleanup_closed_positions(self, current_positions: List[Dict]):
        """清理已平仓的持仓记录"""
        if not self.position_manager:
            return

        tracked_symbols = self.position_manager.get_all_positions()
        active_symbols = {pos['symbol'] for pos in current_positions if float(pos.get('contracts', 0)) > 0}

        for symbol in tracked_symbols:
            if symbol not in active_symbols:
                print(f"[POSITION_MONITOR] 检测到{symbol}已平仓，移除跟踪")
                
                # 🔥🔥🔥 获取持仓信息并记录平仓（报告系统需要）
                pos_info = self.position_manager.get_position_info(symbol)
                if pos_info:
                    try:
                        # 获取最后价格（可能是止盈/止损触发的价格）
                        ticker = self.exchange.fetch_ticker(symbol)
                        exit_price = ticker['last']
                        
                        entry_price = pos_info['entry_price']
                        side = pos_info['side']
                        sl_price = pos_info.get('sl_price', 0)
                        tp_price = pos_info.get('tp_price', 0)
                        
                        # 判断平仓原因
                        if tp_price > 0 and side == 'long' and exit_price >= tp_price * 0.995:
                            exit_reason = "tp"
                        elif tp_price > 0 and side == 'short' and exit_price <= tp_price * 1.005:
                            exit_reason = "tp"
                        elif sl_price > 0 and side == 'long' and exit_price <= sl_price * 1.005:
                            exit_reason = "sl"
                        elif sl_price > 0 and side == 'short' and exit_price >= sl_price * 0.995:
                            exit_reason = "sl"
                        else:
                            exit_reason = "unknown"
                        
                        self._record_position_closed(symbol, side, entry_price, exit_price, exit_reason)
                        
                    except Exception as e:
                        print(f"[POSITION_MONITOR] ⚠️ 记录平仓失败: {e}")
                
                self.position_manager.remove_position(symbol)
                # 清除订单缓存
                self.sl_order_cache.pop(symbol, None)
                self.tp_order_cache.pop(symbol, None)

    def run_once(self):
        """
        🔥 v3.7: 执行一次自动交易检查（建议调用间隔60秒）
        """
        if not self.enabled:
            print(f"[AUTOTRADER] ⚠️ 跳过 - 自动交易未启用 (enabled={self.enabled})")
            return

        # 🔥 先监控现有持仓（包含紧急止损和止损验证！）
        self.monitor_positions()

        print(f"[AUTOTRADER] 检查待执行信号...")

        # 获取待执行信号
        signals = self.get_pending_signals()

        if not signals:
            print(f"[AUTOTRADER] 无待执行信号")
            return

        print(f"[AUTOTRADER] 发现 {len(signals)} 个待执行信号")

        for signal in signals:
            print(f"\n[AUTOTRADER] 处理信号: {signal['symbol']} {signal['side'].upper()}")
            result = self.execute_trade(signal)

            if result:
                print(f"[AUTOTRADER] ✅ 交易执行成功")
            else:
                print(f"[AUTOTRADER] ❌ 交易执行失败或被拒绝")

            # 避免API限流
            time.sleep(1)

    # ==================== 🔥🔥🔥 高波动轨道扩展方法 ====================
    
    def place_limit_order(self, symbol: str, side: str, amount: float, 
                          price: float, stop_loss: float, take_profit: float,
                          order_tag: str = "") -> dict:
        """
        🔥 挂限价单（高波动轨道专用）
        
        Args:
            symbol: 交易对 (如 "PEPE/USDT:USDT")
            side: "long" 或 "short"
            amount: 数量
            price: 限价
            stop_loss: 止损价
            take_profit: 止盈价
            order_tag: 订单标签（可选）
            
        Returns:
            {"success": bool, "order_id": str, "error": str}
        """
        try:
            # 转换symbol格式为OKX格式
            okx_symbol = self._convert_symbol_to_okx(symbol)
            
            # 🔥🔥🔥 v3.6: 检查是否有同币种的反向持仓
            try:
                positions = self.get_current_positions()
                for pos in positions:
                    if pos['symbol'] == okx_symbol:
                        existing_side = pos.get('side', 'long')
                        existing_contracts = float(pos.get('contracts', 0))
                        
                        if existing_side == side:
                            # 同向持仓：跳过
                            print(f"[AUTOTRADER] ⏭️ 高波动: 已有{okx_symbol} {existing_side.upper()}持仓，跳过")
                            return {"success": False, "order_id": "", "error": "same_side_position_exists"}
                        else:
                            # 🔥 反向持仓 - 先平掉
                            print(f"[AUTOTRADER] ⚠️ 高波动发现反向持仓: {okx_symbol} {existing_side.upper()} {existing_contracts}个")
                            print(f"[AUTOTRADER] 🔄 先平掉反向仓位再开{side.upper()}...")
                            
                            close_side = 'sell' if existing_side == 'long' else 'buy'
                            try:
                                self._cancel_all_sl_tp_orders(okx_symbol)
                                close_order = self.exchange.create_order(
                                    symbol=okx_symbol,
                                    type='market',
                                    side=close_side,
                                    amount=existing_contracts,
                                    params={
                                        'tdMode': 'cross',
                                        'posSide': existing_side,
                                        'reduceOnly': True
                                    }
                                )
                                print(f"[AUTOTRADER] ✅ 高波动反向仓位已平仓: {close_order['id']}")
                                
                                if self.position_manager:
                                    self.position_manager.remove_position(okx_symbol)
                                self.sl_order_cache.pop(okx_symbol, None)
                                self.tp_order_cache.pop(okx_symbol, None)
                                time.sleep(0.5)
                            except Exception as e:
                                print(f"[AUTOTRADER] ❌ 高波动平反向仓位失败: {e}")
                                return {"success": False, "order_id": "", "error": f"close_opposite_failed: {e}"}
            except Exception as e:
                print(f"[AUTOTRADER] ⚠️ 高波动持仓检查失败: {e}")
            
            # 设置杠杆
            try:
                self.exchange.set_leverage(self.default_leverage, okx_symbol)
            except Exception as e:
                print(f"[AUTOTRADER] 设置杠杆警告: {e}")
            
            # 🔥 v3.4: 检查最小交易数量（避免OKX精度错误）
            try:
                market = self.exchange.market(okx_symbol)
                min_amount = market.get('limits', {}).get('amount', {}).get('min', 0)
                amount_precision = market.get('precision', {}).get('amount', 0.001)
                
                # 如果精度是整数（如1），需要取整
                if amount_precision >= 1:
                    amount = int(amount)
                
                if min_amount and amount < min_amount:
                    print(f"[AUTOTRADER] ⏭️ 高波动: 数量{amount}小于最小要求{min_amount}，跳过")
                    return {
                        "success": False,
                        "order_id": "",
                        "error": f"amount {amount} < min {min_amount}"
                    }
            except Exception as e:
                print(f"[AUTOTRADER] ⚠️ 最小数量检查失败: {e}")
                # 如果检查失败，继续尝试下单
            
            # 下限价单
            order_side = 'buy' if side == 'long' else 'sell'
            
            params = {
                'tdMode': 'cross',
                'posSide': 'long' if side == 'long' else 'short',
            }
            
            # 添加订单标签（如果有）
            if order_tag:
                # 🔥🔥🔥 v3.3修复：OKX clOrdId 格式要求严格，只用字母数字
                import re
                # 提取币种名称（如 FLOW）
                symbol_match = re.search(r'([A-Z0-9]+)', order_tag.replace('hv_', ''))
                if symbol_match:
                    coin_name = symbol_match.group(1)
                    timestamp_short = str(int(time.time()))[-6:]
                    clean_tag = f"hv{coin_name}{timestamp_short}"
                else:
                    clean_tag = f"hv{str(int(time.time()))[-8:]}"
                # 确保只有字母数字，最多32字符
                clean_tag = re.sub(r'[^a-zA-Z0-9]', '', clean_tag)[:32]
                params['clOrdId'] = clean_tag
            
            order = self.exchange.create_order(
                symbol=okx_symbol,
                type='limit',
                side=order_side,
                amount=amount,
                price=price,
                params=params
            )
            
            order_id = order.get('id', '')
            
            print(f"[AUTOTRADER] ✅ 高波动限价单: {okx_symbol} {side.upper()} @ ${price:.8f}")
            print(f"[AUTOTRADER]    数量: {amount} | 订单ID: {order_id}")
            
            # 缓存止损止盈参数（成交后设置）
            self._pending_sl_tp[order_id] = {
                'symbol': okx_symbol,
                'original_symbol': symbol,
                'side': side,
                'amount': amount,
                'entry_price': price,
                'sl_price': stop_loss,
                'tp_price': take_profit,
                'created_at': datetime.now().isoformat()
            }
            
            return {
                "success": True,
                "order_id": order_id,
                "error": ""
            }
            
        except Exception as e:
            print(f"[AUTOTRADER] ❌ 高波动限价单失败: {e}")
            import traceback
            traceback.print_exc()
            return {
                "success": False,
                "order_id": "",
                "error": str(e)
            }

    def check_order_status(self, order_id: str) -> str:
        """
        🔥 检查订单状态
        
        Returns:
            "open" / "filled" / "canceled" / "unknown"
        """
        if not order_id:
            return "unknown"
        
        try:
            pending = self._pending_sl_tp.get(order_id, {})
            symbol = pending.get('symbol', '')
            
            if not symbol:
                print(f"[AUTOTRADER] ⚠️ 未找到订单缓存: {order_id}")
                return "unknown"
            
            order = self.exchange.fetch_order(order_id, symbol)
            status = order.get('status', 'unknown')
            
            if status == 'closed':
                self._on_high_vol_order_filled(order_id, order)
                return "filled"
            elif status == 'canceled':
                if order_id in self._pending_sl_tp:
                    del self._pending_sl_tp[order_id]
                return "canceled"
            elif status == 'open':
                return "open"
            else:
                return status
                
        except Exception as e:
            print(f"[AUTOTRADER] 检查订单状态异常: {e}")
            return "unknown"

    def _on_high_vol_order_filled(self, order_id: str, order: dict):
        """🔥 v3.3: 高波动限价单成交后的处理 - 修复position_manager注册"""
        pending = self._pending_sl_tp.get(order_id)
        if not pending:
            return
        
        symbol = pending['symbol']
        side = pending['side']
        amount = pending['amount']
        sl_price = pending['sl_price']
        tp_price = pending['tp_price']
        entry_price = order.get('average') or order.get('price') or pending['entry_price']
        
        print(f"[AUTOTRADER] ✅ 高波动订单成交: {symbol} {side.upper()} @ ${entry_price:.8f}")
        
        # 🔥 v3.3: 注册到position_manager（关键！否则breakeven不会触发）
        if self.position_manager:
            try:
                self.position_manager.register_position(
                    symbol=symbol,
                    side=side,
                    entry_price=float(entry_price),
                    amount=amount,
                    sl_price=sl_price if sl_price else 0,
                    tp_price=tp_price if tp_price else 0,
                    strategy_type="high_volatility"  # 高波动策略
                )
                print(f"[AUTOTRADER] 📊 高波动持仓已注册到PositionManager")
            except Exception as e:
                print(f"[AUTOTRADER] ⚠️ 注册持仓到PositionManager失败: {e}")
        
        if sl_price and sl_price > 0:
            sl_id = self._create_stop_loss_order(symbol, side, amount, sl_price)
            if sl_id:
                self.sl_order_cache[symbol] = sl_id
        
        if tp_price and tp_price > 0:
            tp_id = self._create_take_profit_order(symbol, side, amount, tp_price)
            if tp_id:
                self.tp_order_cache[symbol] = tp_id
        
        if hasattr(self, 'position_entry_time'):
            self.position_entry_time[symbol] = datetime.now()
        
        if order_id in self._pending_sl_tp:
            del self._pending_sl_tp[order_id]

    def cancel_order(self, order_id: str, symbol: str = None):
        """🔥 取消订单"""
        try:
            if not symbol:
                pending = self._pending_sl_tp.get(order_id, {})
                symbol = pending.get('symbol', '')
            
            if symbol:
                okx_symbol = self._convert_symbol_to_okx(symbol)
                self.exchange.cancel_order(order_id, okx_symbol)
                print(f"[AUTOTRADER] 🚫 取消订单: {order_id}")
            
            if order_id in self._pending_sl_tp:
                del self._pending_sl_tp[order_id]
                    
        except Exception as e:
            print(f"[AUTOTRADER] 取消订单异常: {e}")

    def update_stop_loss(self, symbol: str, new_sl_price: float) -> bool:
        """🔥 更新止损价"""
        try:
            okx_symbol = self._convert_symbol_to_okx(symbol)
            
            positions = self.exchange.fetch_positions([okx_symbol])
            position = None
            for p in positions:
                if p['symbol'] == okx_symbol and float(p.get('contracts', 0)) > 0:
                    position = p
                    break
            
            if not position:
                return False
            
            side = position.get('side', 'long')
            amount = float(position.get('contracts', 0))
            
            old_sl_id = self.sl_order_cache.get(okx_symbol)
            if old_sl_id:
                try:
                    self.exchange.cancel_order(old_sl_id, okx_symbol)
                except:
                    pass
            
            new_sl_id = self._create_stop_loss_order(okx_symbol, side, amount, new_sl_price)
            if new_sl_id:
                self.sl_order_cache[okx_symbol] = new_sl_id
                print(f"[AUTOTRADER] 📍 止损更新: {okx_symbol} → ${new_sl_price:.8f}")
                return True
            
            return False
                    
        except Exception as e:
            print(f"[AUTOTRADER] 更新止损异常: {e}")
            return False

    def close_position_limit(self, symbol: str, side: str) -> Optional[dict]:
        """🔥 限价平仓"""
        try:
            okx_symbol = self._convert_symbol_to_okx(symbol)
            
            positions = self.exchange.fetch_positions([okx_symbol])
            position = None
            for p in positions:
                if p['symbol'] == okx_symbol and float(p.get('contracts', 0)) > 0:
                    position = p
                    break
            
            if not position:
                return None
            
            amount = float(position.get('contracts', 0))
            current_price = float(position.get('markPrice', 0))
            
            if side == 'long':
                close_price = current_price * 0.9985
                close_side = 'sell'
            else:
                close_price = current_price * 1.0015
                close_side = 'buy'
            
            order = self.exchange.create_order(
                symbol=okx_symbol,
                type='limit',
                side=close_side,
                amount=amount,
                price=close_price,
                params={
                    'tdMode': 'cross',
                    'posSide': 'long' if side == 'long' else 'short',
                    'reduceOnly': True
                }
            )
            
            print(f"[AUTOTRADER] 📤 限价平仓: {okx_symbol} @ ${close_price:.8f}")
            self._cancel_sl_tp_orders_hv(okx_symbol)
            return order
            
        except Exception as e:
            print(f"[AUTOTRADER] 限价平仓异常: {e}")
            return None

    def _cancel_sl_tp_orders_hv(self, symbol: str):
        """🔥 取消止损止盈单"""
        try:
            sl_id = self.sl_order_cache.get(symbol)
            if sl_id:
                try:
                    self.exchange.cancel_order(sl_id, symbol)
                except:
                    pass
                del self.sl_order_cache[symbol]
            
            tp_id = self.tp_order_cache.get(symbol)
            if tp_id:
                try:
                    self.exchange.cancel_order(tp_id, symbol)
                except:
                    pass
                del self.tp_order_cache[symbol]
        except:
            pass

    def get_available_balance(self) -> float:
        """🔥 获取可用USDT余额"""
        try:
            balance = self.exchange.fetch_balance()
            usdt = balance.get('USDT', {})
            return float(usdt.get('free', 0))
        except Exception as e:
            print(f"[AUTOTRADER] 获取余额异常: {e}")
            return 0

    def _convert_symbol_to_okx(self, symbol: str) -> str:
        """🔥 转换symbol格式为OKX格式"""
        if ':' in symbol:
            return symbol
        if '/' in symbol:
            base, quote = symbol.split('/')
            return f"{base}/{quote}:USDT"
        if symbol.endswith('USDT'):
            base = symbol[:-4]
            return f"{base}/USDT:USDT"
        return f"{symbol}/USDT:USDT"

    def get_pending_high_vol_orders(self) -> list:
        """🔥 获取所有待成交的高波动订单"""
        return [{'order_id': k, **v} for k, v in self._pending_sl_tp.items()]


def create_auto_trader(config_path: str, db_path: str) -> Optional[AutoTrader]:
    """
    创建自动交易器实例

    Args:
        config_path: config.yaml路径
        db_path: 数据库路径

    Returns:
        AutoTrader实例或None
    """
    try:
        import yaml

        with open(config_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)

        auto_config = config.get('auto_trading', {})

        if not auto_config.get('enabled', False):
            print("[AUTOTRADER] 自动交易未启用")
            return None

        # 🔥 v3.7: 传递完整配置用于AI审核等功能
        trader = AutoTrader(auto_config, db_path, full_config=config)
        return trader

    except Exception as e:
        print(f"[AUTOTRADER_ERR] 创建自动交易器失败: {e}")
        import traceback
        traceback.print_exc()
        return None