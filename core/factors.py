# core/factors.py
from typing import Dict, Any, Tuple
import numpy as np
import pandas as pd
from .utils import ema, atr, rsi, macd, bollinger, obv, realized_vol, wick_scores

def trend_volume_blocks_for_majors(df: pd.DataFrame, p: Dict[str,Any]) -> Tuple[float,float,float,float]:
    df = df.copy()
    df["ema_f"], df["ema_s"] = ema(df["close"], p["ema_fast"]), ema(df["close"], p["ema_slow"])
    rsi14 = rsi(df["close"], 14)
    _, _, hist = macd(df["close"])
    _, bb_u, bb_l = bollinger(df["close"], 20, 2.0)

    cross_up   = (df["ema_f"].iloc[-2] <= df["ema_s"].iloc[-2]) and (df["ema_f"].iloc[-1] > df["ema_s"].iloc[-1])
    cross_down = (df["ema_f"].iloc[-2] >= df["ema_s"].iloc[-2]) and (df["ema_f"].iloc[-1] < df["ema_s"].iloc[-1])
    vol_ok  = df["volume"].iloc[-1] > df["volume"].rolling(10).mean().iloc[-1]
    rsi_bull = (rsi14.iloc[-1] < 60)
    rsi_bear = (rsi14.iloc[-1] > 40)
    macd_up  = (hist.iloc[-1] > hist.iloc[-2])
    macd_dn  = (hist.iloc[-1] < hist.iloc[-2])
    bb_up    = df["close"].iloc[-1] > bb_u.iloc[-1]
    bb_dn    = df["close"].iloc[-1] < bb_l.iloc[-1]
    lw, uw   = wick_scores(df)

    trend_long  = float(np.mean([cross_up,  vol_ok, rsi_bull, (macd_up or bb_up)]))
    trend_short = float(np.mean([cross_down, vol_ok, rsi_bear, (macd_dn or bb_dn)]))
    volume_long  = float(np.mean([vol_ok, lw, bb_up]))
    volume_short = float(np.mean([vol_ok, uw, bb_dn]))
    return trend_long, trend_short, volume_long, volume_short

def trend_volume_blocks_for_anomaly(df: pd.DataFrame, p: Dict[str,Any]) -> Tuple[float,float,float,float]:
    df = df.copy()
    vma = df["volume"].rolling(p["vol_ma"]).mean()
    vol_spike = (df["volume"].iloc[-1] > p["spike_ratio"] * max(vma.iloc[-1], 1e-12))
    hh = df["high"].rolling(p["breakout_lookback"]).max()
    ll = df["low"].rolling(p["breakout_lookback"]).min()
    up_break   = df["close"].iloc[-1] >= hh.iloc[-2]
    down_break = df["close"].iloc[-1] <= ll.iloc[-2]
    lw, uw = wick_scores(df)
    trend_long  = float(np.mean([vol_spike, up_break]))
    trend_short = float(np.mean([vol_spike, down_break]))
    # 盘口不平衡 & 情绪等在 main 里统一加
    volume_long  = float(np.mean([vol_spike, lw]))
    volume_short = float(np.mean([vol_spike, uw]))
    return trend_long, trend_short, volume_long, volume_short

def trend_volume_blocks_for_accum(df: pd.DataFrame, p: Dict[str,Any]) -> Tuple[float,float,float,float,float]:
    df = df.copy()
    df["obv"] = obv(df)
    obv_slope = df["obv"].iloc[-1] - df["obv"].iloc[-p["obv_lookback"]]
    vol_s = realized_vol(df["close"], p["compress_win_short"])
    vol_l = realized_vol(df["close"], p["compress_win_long"])
    ratio_ok = (isinstance(vol_s,float) and isinstance(vol_l,float) and vol_l>0)
    ratio = (vol_s/vol_l) if ratio_ok else np.nan
    compressed = (ratio==ratio) and (ratio <  p["compress_ratio_max"])
    expanded   = (ratio==ratio) and (ratio > (1.0/p["compress_ratio_max"]))
    price_flat = (abs(df["close"].iloc[-1] - df["close"].rolling(30).mean().iloc[-1]) / max(df["close"].iloc[-1],1e-12) < 0.005)
    vol_shrink = (df["volume"].iloc[-1] < df["volume"].rolling(30).mean().iloc[-1])
    div_bull = price_flat and vol_shrink and (obv_slope > 0)
    div_bear = price_flat and (not vol_shrink) and (obv_slope < 0)
    lw, uw = wick_scores(df)

    trend_long  = float(np.mean([obv_slope>0, compressed or div_bull]))
    trend_short = float(np.mean([obv_slope<0, expanded   or div_bear]))
    volume_long  = float(np.mean([lw, compressed]))
    volume_short = float(np.mean([uw, expanded]))
    return trend_long, trend_short, volume_long, volume_short, float(ratio if ratio==ratio else 0.0)
