# core/okx_stop_manager.py - OKX止损管理器 v1.0
# 用途：实现移动止损(Trailing Stop)和保护性止损(Breakeven Stop)的动态更新

import ccxt
import time
from typing import Dict, Any, Optional, List, Tuple
from datetime import datetime, timezone
from dataclasses import dataclass
import threading


@dataclass
class Position:
    """持仓信息"""
    symbol: str
    side: str  # long/short
    entry_price: float
    current_price: float
    contracts: float
    leverage: int
    unrealized_pnl: float
    unrealized_pnl_pct: float
    
    # 止损信息
    current_sl_price: float = 0.0
    current_tp_price: float = 0.0
    original_sl_price: float = 0.0
    original_tp_price: float = 0.0
    
    # 追踪状态
    highest_price: float = 0.0  # 做多时的最高价
    lowest_price: float = float('inf')  # 做空时的最低价
    trailing_stop_activated: bool = False
    breakeven_stop_activated: bool = False


class OKXStopManager:
    """
    OKX止损管理器
    
    功能：
    1. 移动止损（Trailing Stop）
       - 盈利超过激活阈值后开始追踪
       - 价格创新高/新低时移动止损
       
    2. 保护性止损（Breakeven Stop）
       - 盈利超过阈值后移动止损到成本价
       
    3. 止损单更新
       - OKX使用algo order进行止损
       - 更新时需要先取消旧单再下新单
    
    使用方式：
    ```python
    manager = OKXStopManager(config)
    
    # 启动管理循环
    manager.start()
    
    # 添加持仓追踪
    manager.track_position(symbol, side, entry_price, sl_price, tp_price)
    
    # 停止管理
    manager.stop()
    ```
    """
    
    def __init__(self, config: Dict[str, Any]):
        """
        初始化止损管理器
        
        Args:
            config: 配置字典
        """
        self.config = config
        
        # OKX配置
        okx_cfg = config.get("auto_trading", {}).get("okx", {})
        
        # 从环境变量或配置获取API密钥
        import os
        api_key = os.getenv("OKX_API_KEY", okx_cfg.get("api_key", ""))
        secret = os.getenv("OKX_SECRET", okx_cfg.get("secret", ""))
        passphrase = os.getenv("OKX_PASSPHRASE", okx_cfg.get("passphrase", ""))
        
        # 清理环境变量格式
        if api_key.startswith("${"):
            api_key = ""
        if secret.startswith("${"):
            secret = ""
        if passphrase.startswith("${"):
            passphrase = ""
        
        testnet = okx_cfg.get("testnet", False)
        
        # 初始化交易所
        self.exchange = ccxt.okx({
            'apiKey': api_key,
            'secret': secret,
            'password': passphrase,
            'enableRateLimit': True,
            'options': {
                'defaultType': 'swap',
            }
        })
        
        if testnet:
            self.exchange.set_sandbox_mode(True)
            print("[OKX_STOP] 使用模拟盘")
        
        # 止损配置
        exit_cfg = config.get("auto_trading", {}).get("exit", {})
        
        # 移动止损配置
        self.trailing_stop_enabled = exit_cfg.get("trailing_stop", True)
        self.trailing_activation_pct = exit_cfg.get("trailing_stop_activation_pct", 0.01)
        self.trailing_distance_pct = exit_cfg.get("trailing_stop_distance_pct", 0.005)
        self.trailing_step_pct = exit_cfg.get("trailing_stop_step_pct", 0.005)
        
        # 保护性止损配置
        self.breakeven_enabled = exit_cfg.get("breakeven_stop", True)
        self.breakeven_activation_pct = exit_cfg.get("breakeven_activation_pct", 0.01)
        self.breakeven_buffer_pct = exit_cfg.get("breakeven_buffer_pct", 0.002)
        
        # 检查间隔
        self.check_interval = exit_cfg.get("reversal_check_interval_sec", 60)
        
        # 持仓追踪
        self.positions: Dict[str, Position] = {}
        
        # 止损单ID缓存
        self.sl_order_ids: Dict[str, str] = {}
        self.tp_order_ids: Dict[str, str] = {}
        
        # 运行状态
        self._running = False
        self._thread: Optional[threading.Thread] = None
        
        print(f"[OKX_STOP] 初始化完成")
        print(f"  移动止损: {'启用' if self.trailing_stop_enabled else '禁用'}")
        print(f"    激活阈值: {self.trailing_activation_pct*100:.1f}%")
        print(f"    追踪距离: {self.trailing_distance_pct*100:.1f}%")
        print(f"  保护性止损: {'启用' if self.breakeven_enabled else '禁用'}")
        print(f"    激活阈值: {self.breakeven_activation_pct*100:.1f}%")
        print(f"    缓冲距离: {self.breakeven_buffer_pct*100:.1f}%")
    
    def start(self):
        """启动止损管理循环"""
        if self._running:
            return
        
        self._running = True
        self._thread = threading.Thread(target=self._management_loop, daemon=True)
        self._thread.start()
        print("[OKX_STOP] 管理循环已启动")
    
    def stop(self):
        """停止止损管理"""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        print("[OKX_STOP] 管理循环已停止")
    
    def track_position(
        self,
        symbol: str,
        side: str,
        entry_price: float,
        sl_price: float,
        tp_price: float,
        contracts: float = 0,
        leverage: int = 5,
    ):
        """
        添加持仓追踪
        
        Args:
            symbol: 交易对
            side: 方向 (long/short)
            entry_price: 入场价
            sl_price: 初始止损价
            tp_price: 初始止盈价
            contracts: 合约数量
            leverage: 杠杆倍数
        """
        position = Position(
            symbol=symbol,
            side=side.lower(),
            entry_price=entry_price,
            current_price=entry_price,
            contracts=contracts,
            leverage=leverage,
            unrealized_pnl=0,
            unrealized_pnl_pct=0,
            current_sl_price=sl_price,
            current_tp_price=tp_price,
            original_sl_price=sl_price,
            original_tp_price=tp_price,
            highest_price=entry_price,
            lowest_price=entry_price,
        )
        
        self.positions[symbol] = position
        print(f"[OKX_STOP] 追踪持仓: {symbol} {side} @{entry_price:.6f}")
        print(f"  SL: {sl_price:.6f} | TP: {tp_price:.6f}")
    
    def untrack_position(self, symbol: str):
        """移除持仓追踪"""
        if symbol in self.positions:
            del self.positions[symbol]
            print(f"[OKX_STOP] 停止追踪: {symbol}")
    
    def _management_loop(self):
        """止损管理主循环"""
        while self._running:
            try:
                self._check_all_positions()
            except Exception as e:
                print(f"[OKX_STOP] 管理循环异常: {e}")
            
            time.sleep(self.check_interval)
    
    def _check_all_positions(self):
        """检查所有持仓"""
        if not self.positions:
            return
        
        for symbol, position in list(self.positions.items()):
            try:
                self._check_position(position)
            except Exception as e:
                print(f"[OKX_STOP] 检查持仓异常 {symbol}: {e}")
    
    def _check_position(self, position: Position):
        """检查单个持仓"""
        symbol = position.symbol
        
        # 获取最新价格
        try:
            ticker = self.exchange.fetch_ticker(symbol)
            current_price = float(ticker['last'])
        except Exception as e:
            print(f"[OKX_STOP] 获取价格失败 {symbol}: {e}")
            return
        
        position.current_price = current_price
        
        # 计算未实现盈亏
        if position.side == 'long':
            position.unrealized_pnl_pct = (current_price - position.entry_price) / position.entry_price
        else:
            position.unrealized_pnl_pct = (position.entry_price - current_price) / position.entry_price
        
        # 更新极值
        if position.side == 'long':
            if current_price > position.highest_price:
                position.highest_price = current_price
        else:
            if current_price < position.lowest_price:
                position.lowest_price = current_price
        
        # 检查保护性止损
        if self.breakeven_enabled:
            self._check_breakeven_stop(position)
        
        # 检查移动止损
        if self.trailing_stop_enabled:
            self._check_trailing_stop(position)
    
    def _check_breakeven_stop(self, position: Position):
        """检查并执行保护性止损"""
        if position.breakeven_stop_activated:
            return  # 已激活
        
        # 检查是否达到激活条件
        if position.unrealized_pnl_pct < self.breakeven_activation_pct:
            return
        
        # 计算保本止损价
        if position.side == 'long':
            new_sl = position.entry_price * (1 + self.breakeven_buffer_pct)
            
            # 只有当新止损高于当前止损时才更新
            if new_sl <= position.current_sl_price:
                return
        else:
            new_sl = position.entry_price * (1 - self.breakeven_buffer_pct)
            
            # 只有当新止损低于当前止损时才更新
            if new_sl >= position.current_sl_price:
                return
        
        # 更新止损
        print(f"[OKX_STOP] 🛡️ 保护性止损激活 {position.symbol}")
        print(f"  盈利: {position.unrealized_pnl_pct*100:+.2f}%")
        print(f"  止损: {position.current_sl_price:.6f} → {new_sl:.6f}")
        
        success = self._update_stop_loss(position.symbol, new_sl, position.contracts)
        
        if success:
            position.current_sl_price = new_sl
            position.breakeven_stop_activated = True
    
    def _check_trailing_stop(self, position: Position):
        """检查并执行移动止损"""
        # 检查是否达到激活条件
        if position.unrealized_pnl_pct < self.trailing_activation_pct:
            return
        
        if not position.trailing_stop_activated:
            position.trailing_stop_activated = True
            print(f"[OKX_STOP] 📈 移动止损激活 {position.symbol}")
        
        # 计算新止损价
        if position.side == 'long':
            # 做多：止损跟随最高价
            new_sl = position.highest_price * (1 - self.trailing_distance_pct)
            
            # 检查是否需要移动（步进距离）
            sl_move_pct = (new_sl - position.current_sl_price) / position.entry_price
            if sl_move_pct < self.trailing_step_pct:
                return  # 移动幅度不够
            
            # 止损只能上移，不能下移
            if new_sl <= position.current_sl_price:
                return
        else:
            # 做空：止损跟随最低价
            new_sl = position.lowest_price * (1 + self.trailing_distance_pct)
            
            # 检查是否需要移动
            sl_move_pct = (position.current_sl_price - new_sl) / position.entry_price
            if sl_move_pct < self.trailing_step_pct:
                return
            
            # 止损只能下移，不能上移
            if new_sl >= position.current_sl_price:
                return
        
        # 更新止损
        print(f"[OKX_STOP] 📊 移动止损更新 {position.symbol}")
        print(f"  当前价: {position.current_price:.6f}")
        print(f"  止损: {position.current_sl_price:.6f} → {new_sl:.6f}")
        
        success = self._update_stop_loss(position.symbol, new_sl, position.contracts)
        
        if success:
            position.current_sl_price = new_sl
    
    def _update_stop_loss(self, symbol: str, new_sl_price: float, contracts: float) -> bool:
        """
        更新止损单
        
        OKX的止损单更新流程：
        1. 取消现有的止损单
        2. 下新的止损单
        
        Args:
            symbol: 交易对
            new_sl_price: 新止损价
            contracts: 合约数量
            
        Returns:
            是否成功
        """
        try:
            # 1. 取消现有止损单
            old_order_id = self.sl_order_ids.get(symbol)
            if old_order_id:
                try:
                    self.exchange.cancel_order(old_order_id, symbol, params={
                        'instType': 'SWAP',
                    })
                    print(f"  取消旧止损单: {old_order_id[:16]}...")
                except Exception as e:
                    # 订单可能已经不存在
                    print(f"  取消旧止损单失败（可能已成交）: {e}")
            
            # 2. 下新的止损单
            position = self.positions.get(symbol)
            if not position:
                return False
            
            side = position.side
            
            # 止损单方向与持仓相反
            order_side = 'sell' if side == 'long' else 'buy'
            
            # 使用条件单（algo order）
            order = self.exchange.create_order(
                symbol=symbol,
                type='stop',
                side=order_side,
                amount=contracts,
                price=None,  # 市价止损
                params={
                    'instType': 'SWAP',
                    'tdMode': 'cross',  # 全仓模式
                    'ordType': 'trigger',  # 触发单
                    'triggerPx': str(new_sl_price),
                    'triggerPxType': 'last',  # 最新价触发
                    'reduceOnly': True,
                }
            )
            
            # 保存新订单ID
            new_order_id = order.get('id', '')
            self.sl_order_ids[symbol] = new_order_id
            
            print(f"  新止损单: {new_order_id[:16]}... @{new_sl_price:.6f}")
            return True
            
        except Exception as e:
            print(f"[OKX_STOP] ❌ 更新止损失败 {symbol}: {e}")
            return False
    
    def update_take_profit(self, symbol: str, new_tp_price: float, contracts: float) -> bool:
        """
        更新止盈单
        
        Args:
            symbol: 交易对
            new_tp_price: 新止盈价
            contracts: 合约数量
        """
        try:
            # 取消现有止盈单
            old_order_id = self.tp_order_ids.get(symbol)
            if old_order_id:
                try:
                    self.exchange.cancel_order(old_order_id, symbol, params={
                        'instType': 'SWAP',
                    })
                except:
                    pass
            
            position = self.positions.get(symbol)
            if not position:
                return False
            
            side = position.side
            order_side = 'sell' if side == 'long' else 'buy'
            
            order = self.exchange.create_order(
                symbol=symbol,
                type='take_profit',
                side=order_side,
                amount=contracts,
                price=None,
                params={
                    'instType': 'SWAP',
                    'tdMode': 'cross',
                    'ordType': 'trigger',
                    'triggerPx': str(new_tp_price),
                    'triggerPxType': 'last',
                    'reduceOnly': True,
                }
            )
            
            new_order_id = order.get('id', '')
            self.tp_order_ids[symbol] = new_order_id
            
            if position:
                position.current_tp_price = new_tp_price
            
            print(f"[OKX_STOP] 更新止盈: {symbol} @{new_tp_price:.6f}")
            return True
            
        except Exception as e:
            print(f"[OKX_STOP] ❌ 更新止盈失败 {symbol}: {e}")
            return False
    
    def get_position_status(self, symbol: str) -> Optional[Dict]:
        """获取持仓状态"""
        position = self.positions.get(symbol)
        if not position:
            return None
        
        return {
            "symbol": position.symbol,
            "side": position.side,
            "entry_price": position.entry_price,
            "current_price": position.current_price,
            "unrealized_pnl_pct": position.unrealized_pnl_pct * 100,
            "current_sl": position.current_sl_price,
            "current_tp": position.current_tp_price,
            "original_sl": position.original_sl_price,
            "original_tp": position.original_tp_price,
            "trailing_activated": position.trailing_stop_activated,
            "breakeven_activated": position.breakeven_stop_activated,
            "highest_price": position.highest_price,
            "lowest_price": position.lowest_price,
        }
    
    def get_all_positions_status(self) -> List[Dict]:
        """获取所有持仓状态"""
        return [self.get_position_status(symbol) for symbol in self.positions]
    
    def print_status(self):
        """打印当前状态"""
        print("\n" + "=" * 60)
        print("📊 OKX止损管理器状态")
        print("=" * 60)
        
        if not self.positions:
            print("  无追踪持仓")
            return
        
        for symbol, pos in self.positions.items():
            print(f"\n{symbol} {pos.side.upper()}")
            print(f"  入场价: {pos.entry_price:.6f}")
            print(f"  当前价: {pos.current_price:.6f}")
            print(f"  盈亏: {pos.unrealized_pnl_pct*100:+.2f}%")
            print(f"  当前止损: {pos.current_sl_price:.6f}")
            print(f"  当前止盈: {pos.current_tp_price:.6f}")
            print(f"  保护性止损: {'✅ 已激活' if pos.breakeven_stop_activated else '⏳ 待激活'}")
            print(f"  移动止损: {'✅ 已激活' if pos.trailing_stop_activated else '⏳ 待激活'}")
        
        print("=" * 60)


# ==================== 便捷函数 ====================

def create_stop_manager(config: Dict[str, Any] = None) -> OKXStopManager:
    """
    创建止损管理器实例
    
    Args:
        config: 配置字典，为None则从config.yaml加载
    """
    if config is None:
        import yaml
        with open("config.yaml", "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
    
    return OKXStopManager(config)


# ==================== 测试代码 ====================

if __name__ == "__main__":
    import yaml
    
    print("OKX止损管理器测试")
    print("=" * 60)
    
    # 加载配置
    try:
        with open("config.yaml", "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
    except FileNotFoundError:
        print("未找到config.yaml，使用默认配置")
        config = {
            "auto_trading": {
                "okx": {},
                "exit": {
                    "trailing_stop": True,
                    "trailing_stop_activation_pct": 0.01,
                    "trailing_stop_distance_pct": 0.005,
                    "breakeven_stop": True,
                    "breakeven_activation_pct": 0.01,
                    "breakeven_buffer_pct": 0.002,
                }
            }
        }
    
    # 创建管理器
    manager = OKXStopManager(config)
    
    # 模拟添加持仓
    print("\n模拟追踪持仓...")
    manager.track_position(
        symbol="ETH/USDT:USDT",
        side="long",
        entry_price=2000.0,
        sl_price=1960.0,
        tp_price=2200.0,
        contracts=0.1,
        leverage=5,
    )
    
    # 打印状态
    manager.print_status()
    
    # 模拟价格变动
    print("\n模拟价格上涨到 2025 (盈利1.25%)...")
    pos = manager.positions.get("ETH/USDT:USDT")
    if pos:
        pos.current_price = 2025.0
        pos.highest_price = 2025.0
        pos.unrealized_pnl_pct = 0.0125
    
    # 手动检查
    manager._check_all_positions()
    manager.print_status()
    
    print("\n测试完成！")
