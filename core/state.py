# core/state.py — 数据库建表/迁移
# [V4-MOD] 删除sentiment_history表（Reddit情绪已废弃，使用FinGPT）
import sqlite3
from typing import Iterable

# ========== 基础表结构 ==========
SCHEMA_SIGNALS = """
CREATE TABLE IF NOT EXISTS signals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  category TEXT NOT NULL,
  symbol TEXT NOT NULL,
  price REAL NOT NULL,
  entry REAL NOT NULL,
  tp REAL NOT NULL,
  sl REAL NOT NULL,
  score REAL NOT NULL,
  rationale TEXT,
  bias TEXT,
  llm_json TEXT,
  policy_version TEXT,
  ab_bucket TEXT,
  
  -- 胜率跟踪字段
  outcome TEXT DEFAULT 'PENDING',
  limit_moderate REAL,
  limit_aggressive REAL,
  limit_conservative REAL,
  fill_price REAL,
  fill_time TEXT,
  exit_price REAL,
  exit_time TEXT,
  exit_reason TEXT,
  return_pct REAL,
  leverage INTEGER DEFAULT 5,
  tp_price REAL,
  sl_price REAL
);
"""

# 保留 outcomes 表用于兼容性
SCHEMA_OUTCOMES = """
CREATE TABLE IF NOT EXISTS outcomes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  signal_id INTEGER NOT NULL,
  horizon_min INTEGER NOT NULL,
  ret REAL NOT NULL,
  hit_tp INTEGER NOT NULL DEFAULT 0,
  hit_sl INTEGER NOT NULL DEFAULT 0,
  end_ts TEXT NOT NULL,
  hit_tp_ts TEXT,
  hit_sl_ts TEXT,
  max_runup REAL,
  max_drawdown REAL,
  UNIQUE(signal_id, horizon_min)
);
"""

INDEXES: Iterable[str] = (
    "CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(ts);",
    "CREATE INDEX IF NOT EXISTS idx_signals_symbol ON signals(symbol);",
    "CREATE INDEX IF NOT EXISTS idx_signals_outcome ON signals(outcome);",
    "CREATE INDEX IF NOT EXISTS idx_outcomes_signal ON outcomes(signal_id);",
)

# ========== 迁移工具 ==========
def _ensure_table(cur: sqlite3.Cursor, create_sql: str) -> None:
    cur.executescript(create_sql)

def _ensure_column(cur: sqlite3.Cursor, table: str, col: str, coltype: str, default: str = None) -> None:
    cur.execute(f"PRAGMA table_info({table})")
    cols = [r[1] for r in cur.fetchall()]
    if col not in cols:
        if default:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype} DEFAULT {default};")
        else:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype};")

def _ensure_indexes(cur: sqlite3.Cursor, indexes: Iterable[str]) -> None:
    for sql in indexes:
        cur.execute(sql)

# ========== 对外入口 ==========
def ensure_db(db_path: str) -> None:
    """
    幂等: 反复调用安全
    - 创建 signals/outcomes 表
    - 补齐所需列
    - 建立常用索引
    - 删除已废弃的表
    """
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    # 创建核心表
    _ensure_table(cur, SCHEMA_SIGNALS)
    _ensure_table(cur, SCHEMA_OUTCOMES)

    # 删除已废弃的表
    try:
        cur.execute("DROP TABLE IF EXISTS outcomes_multi;")
        cur.execute("DROP TABLE IF EXISTS sentiment_history;")  # Reddit情绪表已废弃
    except Exception:
        pass  # 如果不存在，忽略错误

    # signals 胜率跟踪列保障（用于旧版本数据库升级）
    for name, typ, default in [
        ("outcome", "TEXT", "'PENDING'"), ("limit_moderate", "REAL", None),
        ("limit_aggressive", "REAL", None), ("limit_conservative", "REAL", None),
        ("fill_price", "REAL", None), ("fill_time", "TEXT", None),
        ("exit_price", "REAL", None), ("exit_time", "TEXT", None),
        ("exit_reason", "TEXT", None), ("return_pct", "REAL", None),
        ("leverage", "INTEGER", "5"), ("tp_price", "REAL", None), ("sl_price", "REAL", None),
    ]:
        _ensure_column(cur, "signals", name, typ, default)

    # 索引
    _ensure_indexes(cur, INDEXES)

    conn.commit()
    conn.close()
    
    print("[STATE] 数据库结构已确认 (V4-FinGPT+XGBoost Ready)")