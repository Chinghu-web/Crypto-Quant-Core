# -*- coding: utf-8 -*-
# core/enhanced_reporting.py - 增强报告系统 v2.1 (兼容多数据源)
# 🔥 v2.1 更新: 兼容 pushed_signals / watch_signals / signals 三种数据源

from __future__ import annotations

import os, json, sqlite3
from datetime import datetime, timedelta, time as dtime, timezone
from zoneinfo import ZoneInfo
from typing import Dict, Any, List, Optional, Tuple

try:
    from core.notifier import tg_send
except Exception:
    def tg_send(cfg, title, lines):
        print("─"*60)
        print(title)
        for ln in lines:
            print(ln)

_STATE_FILE = ".report_state.json"


# ==================== 🔥 数据库索引优化 ====================

def ensure_db_indexes(cfg: Dict[str, Any]) -> bool:
    """确保数据库有正确的索引"""
    db_path = _get_db_path(cfg)
    if not os.path.exists(db_path):
        return False
    
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = set(t[0] for t in cursor.fetchall())
        
        indexes_created = []
        
        if "signals" in tables:
            try:
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(ts)")
                indexes_created.append("idx_signals_ts")
            except:
                pass
        
        if "pushed_signals" in tables:
            try:
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_pushed_created ON pushed_signals(created_at)")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_pushed_symbol ON pushed_signals(symbol, created_at)")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_pushed_status ON pushed_signals(order_status)")
                indexes_created.append("idx_pushed_*")
            except:
                pass
        
        if "watch_signals" in tables:
            try:
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_watch_created ON watch_signals(created_at)")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_watch_status ON watch_signals(status, created_at)")
                indexes_created.append("idx_watch_*")
            except:
                pass
        
        conn.commit()
        conn.close()
        
        if indexes_created:
            print(f"[DB_INDEX] 创建/确认索引: {', '.join(indexes_created)}")
        
        return True
        
    except Exception as e:
        print(f"[DB_INDEX] 索引优化失败: {e}")
        return False


# ============ 辅助函数 ============
def _get_tz(cfg: Dict[str, Any]) -> ZoneInfo:
    tzname = (cfg.get("reporting", {}) or {}).get("timezone") or "Asia/Singapore"
    try:
        return ZoneInfo(tzname)
    except Exception:
        return ZoneInfo("UTC")

def _now_local(cfg: Dict[str, Any]) -> datetime:
    return datetime.now(_get_tz(cfg))

def _get_db_path(cfg: Dict[str, Any]) -> str:
    return ((cfg.get("analytics") or {}).get("storage") or {}).get("path") or "./signals.db"

def _get_watch_db_path(cfg: Dict[str, Any]) -> str:
    """获取观察系统数据库路径"""
    return cfg.get("watch", {}).get("db_path", "data/watch_signals.db")

def _parse_time_hhmm(s: str) -> Tuple[int,int]:
    try:
        hh, mm = s.strip().split(":")
        return int(hh), int(mm)
    except Exception:
        return 8, 0

def _load_state() -> Dict[str, Any]:
    if os.path.exists(_STATE_FILE):
        try:
            with open(_STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def _save_state(st: Dict[str, Any]):
    try:
        with open(_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def _parse_ts_any(val) -> Optional[datetime]:
    if val is None:
        return None
    try:
        if isinstance(val, (int, float)):
            x = float(val)
            if x > 1e12:
                return datetime.fromtimestamp(x/1000.0, timezone.utc)
            else:
                return datetime.fromtimestamp(x, timezone.utc)
        s = str(val).replace("Z", "+00:00")
        dtv = datetime.fromisoformat(s)
        if dtv.tzinfo is None:
            dtv = dtv.replace(tzinfo=timezone.utc)
        return dtv.astimezone(timezone.utc)
    except Exception:
        return None


# ============ 🔥 兼容多数据源的性能统计 ============
def _get_performance_stats(db_path: str, start_utc: datetime, end_utc: datetime, cfg: Dict = None) -> Dict[str, Any]:
    """
    🔥 v2.1: 兼容多种数据源的胜率统计
    
    数据源优先级:
    1. pushed_signals 表 (观察系统触发的信号)
    2. watch_signals 表 (观察队列统计)
    3. signals 表 (旧版兼容)
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    
    stats = {
        'total': 0,
        'filled': 0,
        'no_fill': 0,
        'win': 0,
        'loss': 0,
        'timeout': 0,
        'win_rate': 0.0,
        'avg_return': 0.0,
        'signals_detail': [],
        'by_score': {},
        'by_symbol': {},
        'by_side': {},
        'watched': 0,
        'triggered': 0,
        'abandoned': 0,
        'expired': 0,
        'trigger_rate': 0.0
    }
    
    try:
        # ========== 第一步：检查 watch_signals 表（可能在不同数据库）==========
        watch_db_path = _get_watch_db_path(cfg) if cfg else "data/watch_signals.db"
        
        if os.path.exists(watch_db_path):
            try:
                watch_conn = sqlite3.connect(watch_db_path)
                watch_conn.row_factory = sqlite3.Row
                watch_cur = watch_conn.cursor()
                
                watch_cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='watch_signals'")
                if watch_cur.fetchone():
                    watch_query = """
                        SELECT status, COUNT(*) as cnt
                        FROM watch_signals
                        WHERE created_at >= ? AND created_at < ?
                        GROUP BY status
                    """
                    watch_cur.execute(watch_query, (start_utc.isoformat(), end_utc.isoformat()))
                    
                    for row in watch_cur.fetchall():
                        status = row['status']
                        cnt = row['cnt']
                        stats['watched'] += cnt
                        
                        if status == 'triggered':
                            stats['triggered'] = cnt
                        elif status == 'abandoned':
                            stats['abandoned'] = cnt
                        elif status == 'expired':
                            stats['expired'] = cnt
                    
                    if stats['watched'] > 0:
                        stats['trigger_rate'] = stats['triggered'] / stats['watched'] * 100
                
                watch_conn.close()
            except Exception as e:
                print(f"[REPORT] watch_signals查询失败: {e}")
        
        # ========== 第二步：检查 pushed_signals 表 ==========
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='pushed_signals'")
        has_pushed_table = cur.fetchone() is not None
        
        signals = []
        
        if has_pushed_table:
            # 获取表的列信息
            cur.execute("PRAGMA table_info(pushed_signals)")
            columns = {row[1] for row in cur.fetchall()}
            
            # 构建查询（只查询存在的列）
            select_cols = ['id', 'symbol', 'side', 'created_at']
            
            if 'entry_price' in columns:
                select_cols.append('entry_price')
            if 'sl_price' in columns:
                select_cols.append('sl_price')
            if 'tp_price' in columns:
                select_cols.append('tp_price')
            if 'rsi' in columns:
                select_cols.append('rsi')
            if 'adx' in columns:
                select_cols.append('adx')
            if 'score' in columns:
                select_cols.append('score')
            if 'order_status' in columns:
                select_cols.append('order_status')
            if 'final_pnl' in columns:
                select_cols.append('final_pnl')
            if 'exit_reason' in columns:
                select_cols.append('exit_reason')
            if 'fill_time' in columns:
                select_cols.append('fill_time')
            if 'exit_time' in columns:
                select_cols.append('exit_time')
            if 'auto_traded' in columns:
                select_cols.append('auto_traded')
            
            query = f"""
                SELECT {', '.join(select_cols)}
                FROM pushed_signals
                WHERE created_at >= ? AND created_at < ?
                ORDER BY created_at DESC
            """
            
            rows = cur.execute(query, (start_utc.isoformat(), end_utc.isoformat())).fetchall()
            
            for row in rows:
                row_dict = dict(row)
                
                outcome = 'UNKNOWN'
                return_pct = None
                
                order_status = row_dict.get('order_status', '')
                final_pnl = row_dict.get('final_pnl')
                exit_reason = row_dict.get('exit_reason', '')
                auto_traded = row_dict.get('auto_traded', 0)
                entry_price = row_dict.get('entry_price', 0)
                
                # 🔥 v3.2修复: 增强结果判断逻辑
                if order_status == 'filled' or order_status == 'closed':
                    stats['filled'] += 1
                    
                    # 优先使用exit_reason判断
                    if exit_reason:
                        exit_reason_lower = exit_reason.lower()
                        if 'tp' in exit_reason_lower or 'profit' in exit_reason_lower or 'take' in exit_reason_lower:
                            outcome = 'WIN'
                            stats['win'] += 1
                        elif 'sl' in exit_reason_lower or 'stop' in exit_reason_lower or 'loss' in exit_reason_lower:
                            outcome = 'LOSS'
                            stats['loss'] += 1
                        elif 'timeout' in exit_reason_lower or 'expire' in exit_reason_lower:
                            outcome = 'TIMEOUT'
                            stats['timeout'] += 1
                        elif 'reversal' in exit_reason_lower or 'manual' in exit_reason_lower:
                            # 反向/手动平仓，用PnL判断
                            if final_pnl is not None:
                                if final_pnl > 0:
                                    outcome = 'WIN'
                                    stats['win'] += 1
                                else:
                                    outcome = 'LOSS'
                                    stats['loss'] += 1
                            else:
                                outcome = 'CLOSED'
                                stats['timeout'] += 1
                        else:
                            # unknown等其他情况，用PnL判断
                            if final_pnl is not None:
                                if final_pnl > 0:
                                    outcome = 'WIN'
                                    stats['win'] += 1
                                else:
                                    outcome = 'LOSS'
                                    stats['loss'] += 1
                            else:
                                outcome = 'UNKNOWN'
                                stats['timeout'] += 1
                    elif final_pnl is not None:
                        # 没有exit_reason但有PnL
                        if final_pnl > 0:
                            outcome = 'WIN'
                            stats['win'] += 1
                        else:
                            outcome = 'LOSS'
                            stats['loss'] += 1
                    else:
                        # 都没有，说明还在持仓中
                        outcome = 'HOLDING'  # 已成交持仓中
                    
                    return_pct = final_pnl
                    
                elif order_status in ('cancelled', 'expired', 'rejected'):
                    outcome = 'NO_FILL'
                    stats['no_fill'] += 1
                elif auto_traded == 1:
                    # 🔥 修复: 检查是否有fill_time判断是否真正成交
                    fill_time = row_dict.get('fill_time')
                    if fill_time:
                        outcome = 'HOLDING'  # 已成交持仓中
                        stats['filled'] += 1
                    else:
                        outcome = 'PENDING'  # 等待成交
                        stats['no_fill'] += 1
                else:
                    outcome = 'WAITING'  # 等待下单
                    stats['no_fill'] += 1
                
                signals.append({
                    'id': row_dict.get('id'),
                    'symbol': row_dict.get('symbol'),
                    'bias': row_dict.get('side'),
                    'score': row_dict.get('score', 0),
                    'outcome': outcome,
                    'return_pct': return_pct,
                    'fill_time': row_dict.get('fill_time'),
                    'exit_time': row_dict.get('exit_time'),
                    'exit_reason': exit_reason,
                    'ts': row_dict.get('created_at'),
                    'category': 'majors',
                    'entry': row_dict.get('entry_price', 0),
                })
        
        # ========== 第三步：如果没有pushed_signals，尝试signals表 ==========
        if not signals:
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='signals'")
            if cur.fetchone():
                query = """
                    SELECT id, ts, symbol, category, bias, score, price, entry
                    FROM signals
                    WHERE ts >= ? AND ts < ?
                    ORDER BY ts DESC
                """
                
                rows = cur.execute(query, (start_utc.isoformat(), end_utc.isoformat())).fetchall()
                
                for row in rows:
                    row_dict = dict(row)
                    signals.append({
                        'id': row_dict.get('id'),
                        'symbol': row_dict.get('symbol'),
                        'bias': row_dict.get('bias'),
                        'score': row_dict.get('score', 0),
                        'outcome': 'UNKNOWN',  # 旧表没有结果数据
                        'return_pct': None,
                        'ts': row_dict.get('ts'),
                        'category': row_dict.get('category', 'majors'),
                        'entry': row_dict.get('entry', row_dict.get('price', 0)),
                    })
                    stats['total'] += 1
        
        # ========== 第四步：汇总统计 ==========
        stats['total'] = len(signals)
        stats['signals_detail'] = signals
        
        if stats['filled'] > 0:
            stats['win_rate'] = stats['win'] / stats['filled'] * 100
        
        # 计算平均收益
        returns = [s['return_pct'] for s in signals if s['return_pct'] is not None]
        if returns:
            stats['avg_return'] = sum(returns) / len(returns)
        
        # 按评分区间统计
        score_ranges = {'0.85+': [], '0.75-0.85': [], '<0.75': []}
        for s in signals:
            score = s.get('score', 0) or 0
            if score >= 0.85:
                score_ranges['0.85+'].append(s)
            elif score >= 0.75:
                score_ranges['0.75-0.85'].append(s)
            else:
                score_ranges['<0.75'].append(s)
        
        for range_name, sigs in score_ranges.items():
            if sigs:
                wins = len([s for s in sigs if s['outcome'] == 'WIN'])
                closed = len([s for s in sigs if s['outcome'] in ('WIN', 'LOSS', 'TIMEOUT')])
                stats['by_score'][range_name] = {
                    'total': len(sigs),
                    'win': wins,
                    'closed': closed,
                    'win_rate': (wins / closed * 100) if closed > 0 else 0
                }
        
        # 按币种统计
        symbol_stats = {}
        for s in signals:
            sym = s.get('symbol', 'UNKNOWN')
            if sym not in symbol_stats:
                symbol_stats[sym] = {'total': 0, 'win': 0, 'closed': 0}
            symbol_stats[sym]['total'] += 1
            if s['outcome'] == 'WIN':
                symbol_stats[sym]['win'] += 1
            if s['outcome'] in ('WIN', 'LOSS', 'TIMEOUT'):
                symbol_stats[sym]['closed'] += 1
        
        for sym, data in symbol_stats.items():
            data['win_rate'] = (data['win'] / data['closed'] * 100) if data['closed'] > 0 else 0
        stats['by_symbol'] = symbol_stats
        
        # 按方向统计
        side_stats = {'long': {'total': 0, 'win': 0, 'closed': 0}, 'short': {'total': 0, 'win': 0, 'closed': 0}}
        for s in signals:
            side = s.get('bias', 'unknown')
            if side in side_stats:
                side_stats[side]['total'] += 1
                if s['outcome'] == 'WIN':
                    side_stats[side]['win'] += 1
                if s['outcome'] in ('WIN', 'LOSS', 'TIMEOUT'):
                    side_stats[side]['closed'] += 1
        
        for side, data in side_stats.items():
            data['win_rate'] = (data['win'] / data['closed'] * 100) if data['closed'] > 0 else 0
        stats['by_side'] = side_stats
        
    except Exception as e:
        print(f"[REPORT_ERR] 统计失败: {e}")
        import traceback
        traceback.print_exc()
    
    finally:
        conn.close()
    
    return stats


# ============ 调参建议 ============
def _generate_tuning_suggestions(stats: Dict[str, Any], cfg: Dict[str, Any]) -> List[str]:
    """根据统计数据生成调参建议"""
    suggestions = []
    
    # 1. 整体胜率分析
    win_rate = stats.get('win_rate', 0)
    
    if win_rate < 30:
        suggestions.append(f"⚠️ 胜率偏低({win_rate:.1f}%),建议:")
        suggestions.append("   - 提高信号评分阈值(当前建议≥0.80)")
        suggestions.append("   - 加强RSI反转确认(等待更极端值)")
    elif win_rate >= 60:
        suggestions.append(f"✅ 胜率优秀({win_rate:.1f}%),可考虑:")
        suggestions.append("   - 适当增加仓位或杠杆")
        suggestions.append("   - 放宽入场条件增加信号数量")
    
    # 2. 触发率分析
    trigger_rate = stats.get('trigger_rate', 0)
    if trigger_rate > 0:
        if trigger_rate < 30:
            suggestions.append(f"📉 观察触发率低({trigger_rate:.1f}%),建议:")
            suggestions.append("   - 缩短观察期时间")
            suggestions.append("   - 放宽入场时机条件")
        elif trigger_rate > 80:
            suggestions.append(f"📈 触发率高({trigger_rate:.1f}%),可考虑:")
            suggestions.append("   - 加严入场条件提高质量")
    
    # 3. 多空方向分析
    by_side = stats.get('by_side', {})
    if by_side:
        long_data = by_side.get('long', {})
        short_data = by_side.get('short', {})
        
        long_wr = long_data.get('win_rate', 0)
        short_wr = short_data.get('win_rate', 0)
        
        if long_wr - short_wr > 20:
            suggestions.append(f"📊 做多胜率({long_wr:.1f}%)明显高于做空({short_wr:.1f}%)")
            suggestions.append("   - 建议减少做空信号或提高做空门槛")
        elif short_wr - long_wr > 20:
            suggestions.append(f"📊 做空胜率({short_wr:.1f}%)明显高于做多({long_wr:.1f}%)")
            suggestions.append("   - 建议减少做多信号或提高做多门槛")
    
    # 4. 成交率分析
    fill_rate = (stats['filled'] / stats['total'] * 100) if stats['total'] > 0 else 0
    
    if fill_rate < 50 and stats['total'] > 5:
        suggestions.append(f"⏳ 成交率偏低({fill_rate:.1f}%),建议:")
        suggestions.append("   - 检查入场价格是否过于保守")
        suggestions.append("   - 或使用市价单代替限价单")
    
    return suggestions


# ============ 报告触发判断 ============
def should_run_daily_report(cfg: Dict[str, Any]) -> bool:
    rep = cfg.get("reporting", {}) or {}
    daily = rep.get("daily_report", {}) or {}
    if not (rep.get("enabled", True) and daily.get("enabled", True)):
        return False
    
    hh, mm = _parse_time_hhmm(daily.get("send_time") or "09:00")
    now = _now_local(cfg)
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    
    if abs((now - target).total_seconds()) > 60 * 60:
        return False
    
    st = _load_state()
    key = now.date().isoformat()
    if st.get("daily_ran") == key:
        return False
    
    return True

def should_run_weekly_report(cfg: Dict[str, Any]) -> bool:
    rep = cfg.get("reporting", {}) or {}
    weekly = rep.get("weekly_report", {}) or {}
    if not (rep.get("enabled", True) and weekly.get("enabled", True)):
        return False
    
    send_day = int(weekly.get("send_day", 0))
    hh, mm = _parse_time_hhmm(weekly.get("send_time") or "09:00")
    now = _now_local(cfg)
    
    if now.weekday() != send_day:
        return False
    
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if abs((now - target).total_seconds()) > 10 * 60:
        return False
    
    st = _load_state()
    iso = now.isocalendar()
    key = f"{iso.year}-W{iso.week:02d}"
    if st.get("weekly_ran") == key:
        return False
    
    return True


# ============ 日报(含胜率统计) ============
def report_daily_enhanced(cfg: Dict[str, Any]) -> bool:
    """生成并推送日报 - 昨日信号表现"""
    ensure_db_indexes(cfg)
    
    rep = cfg.get("reporting", {}) or {}
    daily = rep.get("daily_report", {}) or {}
    if not (rep.get("enabled", True) and daily.get("enabled", True)):
        return False

    tz = _get_tz(cfg)
    now_local = _now_local(cfg)
    
    yesterday = now_local.date() - timedelta(days=1)
    day_start = datetime.combine(yesterday, dtime(0,0,0), tzinfo=tz)
    day_end = datetime.combine(yesterday, dtime(23,59,59), tzinfo=tz)
    start_utc = day_start.astimezone(timezone.utc)
    end_utc = day_end.astimezone(timezone.utc)

    db_path = _get_db_path(cfg)
    stats = _get_performance_stats(db_path, start_utc, end_utc, cfg)
    
    # 即使没有信号也生成报告（显示观察队列情况）
    if stats['total'] == 0 and stats['watched'] == 0:
        return False

    lines: List[str] = []
    lines.append(f"<b>📊 昨日信号表现 · {yesterday.isoformat()}</b>")
    lines.append("")
    
    # 观察系统统计
    if stats['watched'] > 0:
        lines.append(f"<b>👁 观察队列:</b> {stats['watched']}个")
        lines.append(f"  ✅ 触发: {stats['triggered']}个 ({stats['trigger_rate']:.1f}%)")
        lines.append(f"  ❌ 放弃: {stats['abandoned']}个")
        lines.append(f"  ⏱️ 过期: {stats['expired']}个")
        lines.append("")
    
    if stats['total'] > 0:
        lines.append(f"<b>📤 推送信号:</b> {stats['total']}个")
        lines.append(f"  已成交: {stats['filled']}个 | 未成交: {stats['no_fill']}个")
        
        if stats['filled'] > 0:
            lines.append(f"  胜率: {stats['win']}/{stats['filled']} = {stats['win_rate']:.1f}%")
            if stats['avg_return'] != 0:
                lines.append(f"  平均收益: {stats['avg_return']:+.2f}%")
        
        lines.append("")
        
        # 详细结果
        lines.append("<b>📋 详细结果:</b>")
        for s in stats['signals_detail'][:10]:
            symbol = s['symbol']
            bias = (s['bias'] or 'unknown').upper()
            outcome = s['outcome']
            
            if outcome == 'WIN':
                emoji = "✅"
                detail = f"+{s['return_pct']:.2f}%" if s['return_pct'] else "止盈"
            elif outcome == 'LOSS':
                emoji = "❌"
                detail = f"{s['return_pct']:.2f}%" if s['return_pct'] else "止损"
            elif outcome == 'TIMEOUT':
                emoji = "⏱️"
                detail = f"超时 ({s['return_pct']:.2f}%)" if s['return_pct'] else "超时"
            elif outcome == 'NO_FILL':
                emoji = "⏳"
                detail = "未成交"
            elif outcome == 'FILLED':
                emoji = "⌛"
                detail = "持仓中"
            elif outcome == 'PENDING':
                emoji = "🔄"
                detail = "待成交"
            else:
                emoji = "❓"
                detail = "未知"
            
            lines.append(f"  {emoji} {symbol} {bias} - {detail}")
        
        if len(stats['signals_detail']) > 10:
            lines.append(f"  ... 还有 {len(stats['signals_detail']) - 10} 个信号未显示")

    tg_send(cfg, f"📊 昨日信号表现 · {yesterday.isoformat()}", lines)

    st = _load_state()
    st["daily_ran"] = now_local.date().isoformat()
    _save_state(st)
    return True


# ============ 周报(含胜率+调参建议) ============
def report_weekly_enhanced(cfg: Dict[str, Any], ex=None) -> bool:
    """生成并推送周报 - 本周汇总+调参建议"""
    ensure_db_indexes(cfg)
    
    rep = cfg.get("reporting", {}) or {}
    weekly = rep.get("weekly_report", {}) or {}
    if not (rep.get("enabled", True) and weekly.get("enabled", True)):
        return False
    
    tz = _get_tz(cfg)
    now_local = _now_local(cfg)
    
    start_local = datetime.combine((now_local.date() - timedelta(days=7)), dtime(0,0,0), tzinfo=tz)
    end_local = datetime.combine(now_local.date(), dtime(0,0,0), tzinfo=tz)
    start_utc = start_local.astimezone(timezone.utc)
    end_utc = end_local.astimezone(timezone.utc)

    db_path = _get_db_path(cfg)
    stats = _get_performance_stats(db_path, start_utc, end_utc, cfg)
    
    min_signals = int(weekly.get("min_signals", 3))  # 降低阈值
    if stats['total'] < min_signals and stats['watched'] < min_signals:
        tg_send(cfg, f"🗓️ 周报 · 截止 {end_local.date().isoformat()}", [
            f"过去7天信号 {stats['total']} 个，观察 {stats['watched']} 个",
            f"低于阈值 {min_signals}，未生成正式周报。"
        ])
        return False

    lines: List[str] = []
    lines.append(f"<b>🗓️ 周报 · {start_local.date().isoformat()} ~ {end_local.date().isoformat()}</b>")
    lines.append("")
    
    # 观察系统统计
    if stats['watched'] > 0:
        lines.append(f"<b>👁 观察队列:</b>")
        lines.append(f"  总计: {stats['watched']}个 | 触发率: {stats['trigger_rate']:.1f}%")
        lines.append(f"  触发: {stats['triggered']} | 放弃: {stats['abandoned']} | 过期: {stats['expired']}")
        lines.append("")
    
    lines.append(f"<b>📦 本周汇总:</b>")
    lines.append(f"  总推送: {stats['total']}个")
    lines.append(f"  成交: {stats['filled']}个 | 未成交: {stats['no_fill']}个")
    
    if stats['filled'] > 0:
        lines.append(f"  胜率: {stats['win']}/{stats['filled']} = {stats['win_rate']:.1f}%")
        lines.append(f"  止盈: {stats['win']}个 | 止损: {stats['loss']}个 | 超时: {stats['timeout']}个")
        if stats['avg_return'] != 0:
            lines.append(f"  平均收益: {stats['avg_return']:+.2f}%")
    
    lines.append("")
    
    if stats['by_score']:
        lines.append("<b>📊 按评分区间:</b>")
        for range_name, data in stats['by_score'].items():
            if data['total'] > 0:
                lines.append(f"  {range_name}: 胜率{data['win_rate']:.1f}% ({data['win']}/{data['closed']})")
        lines.append("")
    
    if stats['by_symbol']:
        lines.append("<b>💰 高频币种(TOP5):</b>")
        top5 = sorted(stats['by_symbol'].items(), key=lambda x: x[1]['total'], reverse=True)[:5]
        for sym, data in top5:
            if data['total'] > 0:
                lines.append(f"  {sym}: 胜率{data['win_rate']:.1f}% ({data['total']}个信号)")
        lines.append("")
    
    if stats['by_side']:
        lines.append("<b>🔄 按方向:</b>")
        for side, data in stats['by_side'].items():
            if data['total'] > 0:
                lines.append(f"  {side.upper()}: 胜率{data['win_rate']:.1f}% ({data['win']}/{data['closed']})")
        lines.append("")
    
    # 调参建议
    suggestions = _generate_tuning_suggestions(stats, cfg)
    if suggestions:
        lines.append("<b>💡 优化建议:</b>")
        for sug in suggestions:
            lines.append(sug)

    tg_send(cfg, f"🗓️ 周报 · 截止 {end_local.date().isoformat()}", lines)

    st = _load_state()
    iso = now_local.isocalendar()
    st["weekly_ran"] = f"{iso.year}-W{iso.week:02d}"
    _save_state(st)
    return True