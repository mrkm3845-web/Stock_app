"""テクニカル『型（チャートパターン）』の機械的検出モジュール。

方針（先読み排除）:
- 判定は当日終値まで（過去データのみ）で行い、未来のバーを参照しない。
- ピボット（局所高値/安値）は ``right`` 本の確定を待ってから使用する。
- 返り値は入力の日付軸に整列したブール配列（当日に発生/成立＝True）。

用途:
- 研究: ``back_tester/research_patterns.py``（型ごとの将来リターンをイベントスタディ）。
- 将来: スコアへの反映（本モジュールは純関数のみで副作用なし）。

入力 ``p`` は次のいずれか:
- ``common.features.compute_daily_features`` が返す dict（dates/open/high/low/close/volume/sma*/atr14/rsi14）
- OHLCV を持つ ``pandas.DataFrame``（内部で特徴量へ変換）
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view


# --------------------------------------------------------------------------- 基本ヘルパ
def _to_arrays(p):
    """dict ならそのまま、DataFrame なら compute_daily_features で変換して返す。"""
    if isinstance(p, pd.DataFrame):
        from common import features as F

        return F.compute_daily_features(p)
    return p


def _arr(a, key):
    v = a.get(key)
    if v is None:
        return None
    return np.asarray(v, dtype=float)


def _sma(a, n):
    return pd.Series(np.asarray(a, dtype=float)).rolling(n).mean().values


def _shift(a, n=1):
    return pd.Series(np.asarray(a, dtype=float)).shift(n).values


def _cache_nan(arr):
    """NaN や非有限を False に落とした bool 配列。"""
    return np.nan_to_num(np.asarray(arr, dtype=float), nan=0.0) > 0.0


def pivot_lows(low, left=5, right=5):
    """安値ピボット（局所最小）。``right`` 本確認後に使うこと（先読み回避は呼び出し側の責務）。

    ベクトル化版（sliding_window_view）。判定は従来と同一（中心が窓内の唯一の最小）。
    """
    low = np.asarray(low, dtype=float)
    n = len(low)
    out = np.zeros(n, dtype=bool)
    w = left + right + 1
    if n < w:
        return out
    sw = sliding_window_view(low, w)
    vals = sw[:, left]
    mins = sw.min(axis=1)
    uniq = np.count_nonzero(sw == vals[:, None], axis=1) == 1
    ok = np.isfinite(vals) & (vals == mins) & uniq
    out[left:left + len(ok)] = ok
    return out


def pivot_highs(high, left=5, right=5):
    """高値ピボット（局所最大）。``right`` 本確認後に使うこと。ベクトル化版。"""
    high = np.asarray(high, dtype=float)
    n = len(high)
    out = np.zeros(n, dtype=bool)
    w = left + right + 1
    if n < w:
        return out
    sw = sliding_window_view(high, w)
    vals = sw[:, left]
    maxs = sw.max(axis=1)
    uniq = np.count_nonzero(sw == vals[:, None], axis=1) == 1
    ok = np.isfinite(vals) & (vals == maxs) & uniq
    out[left:left + len(ok)] = ok
    return out


# --------------------------------------------------------------------------- 移動平均クロス系
def ma_cross(p, fast=5, slow=25):
    """単純移動平均の GC / DC（発生当日＝True）。"""
    a = _to_arrays(p)
    c = np.asarray(a["close"] if isinstance(a, dict) else a, dtype=float)
    f = _sma(c, fast)
    s = _sma(c, slow)
    pf = _shift(f, 1)
    ps = _shift(s, 1)
    valid = np.isfinite(f) & np.isfinite(s) & np.isfinite(pf) & np.isfinite(ps)
    gc = valid & (f > s) & (pf <= ps)
    dc = valid & (f < s) & (pf >= ps)
    return gc, dc


def weekly_ma_cross(p, fast=13, slow=52):
    """週足クロスを日次に前詰め（週の途中は直前確定週を使用＝先読み回避）。"""
    a = _to_arrays(p)
    dates = pd.to_datetime(np.asarray(a["dates"]))
    s = pd.Series(np.asarray(a["close"], dtype=float), index=dates)
    w = s.resample("W-FRI").last().dropna()
    f = w.rolling(fast).mean()
    sl = w.rolling(slow).mean()
    gc_w = ((f > sl) & (f.shift(1) <= sl.shift(1))).dropna()
    dc_w = ((f < sl) & (f.shift(1) >= sl.shift(1))).dropna()
    gc = gc_w.reindex(dates, method="ffill").fillna(False).values.astype(bool)
    dc = dc_w.reindex(dates, method="ffill").fillna(False).values.astype(bool)
    return gc, dc


# --------------------------------------------------------------------------- 形状パターン
def double_bottom(p, left=5, right=5, tol=0.04, min_gap=10, max_gap=60,
                  vol_mult=0.0):
    """ダブルボトム: 隣接する安値ピボット2つ（高さ差 tol 以内）→ネックライン上抜けで成立。"""
    a = _to_arrays(p)
    low = _arr(a, "low")
    high = _arr(a, "high")
    close = _arr(a, "close")
    vol = _arr(a, "volume")
    n = len(close)
    flags = np.zeros(n, dtype=bool)
    piv = np.where(pivot_lows(low, left, right))[0]
    if len(piv) < 2:
        return flags
    avg_vol = pd.Series(vol).rolling(20).mean().shift(1).values
    for j in range(1, len(piv)):
        j2 = int(piv[j])
        i1 = int(piv[j - 1])
        gap = j2 - i1
        if gap < min_gap or gap > max_gap:
            continue
        lo1, lo2 = low[i1], low[j2]
        if not (np.isfinite(lo1) and np.isfinite(lo2)) or lo1 <= 0 or lo2 <= 0:
            continue
        if abs(lo2 - lo1) / lo1 > tol:
            continue
        neck = float(np.nanmax(high[i1:j2 + 1]))
        if not np.isfinite(neck) or neck <= max(lo1, lo2):
            continue
        floor = min(lo1, lo2)
        lo = j2 + right
        hi = min(j2 + max_gap * 2, n)
        if lo >= hi:
            continue
        seg = close[lo:hi]
        over_idx = np.nonzero(seg > neck)[0]
        if over_idx.size == 0:
            continue
        under_idx = np.nonzero(seg < floor * (1.0 - tol))[0]
        first_under = int(under_idx[0]) if under_idx.size else None
        for oi in over_idx:
            oi = int(oi)
            if first_under is not None and first_under < oi:
                break
            k = lo + oi
            if vol_mult > 0:
                av = avg_vol[k]
                if not np.isfinite(av) or av <= 0 or vol[k] < vol_mult * av:
                    continue
            flags[k] = True
            break
    return flags


def double_top(p, left=5, right=5, tol=0.04, min_gap=10, max_gap=60,
               vol_mult=0.0):
    """ダブルトップ: 隣接する高値ピボット2つ→ネックライン下抜けで成立（弱気）。"""
    a = _to_arrays(p)
    low = _arr(a, "low")
    high = _arr(a, "high")
    close = _arr(a, "close")
    vol = _arr(a, "volume")
    n = len(close)
    flags = np.zeros(n, dtype=bool)
    piv = np.where(pivot_highs(high, left, right))[0]
    if len(piv) < 2:
        return flags
    avg_vol = pd.Series(vol).rolling(20).mean().shift(1).values
    for j in range(1, len(piv)):
        j2 = int(piv[j])
        i1 = int(piv[j - 1])
        gap = j2 - i1
        if gap < min_gap or gap > max_gap:
            continue
        hi1, hi2 = high[i1], high[j2]
        if not (np.isfinite(hi1) and np.isfinite(hi2)) or hi1 <= 0 or hi2 <= 0:
            continue
        if abs(hi2 - hi1) / hi1 > tol:
            continue
        neck = float(np.nanmin(low[i1:j2 + 1]))
        if not np.isfinite(neck) or neck >= min(hi1, hi2):
            continue
        ceiling = max(hi1, hi2)
        lo = j2 + right
        hi = min(j2 + max_gap * 2, n)
        if lo >= hi:
            continue
        seg = close[lo:hi]
        under_idx = np.nonzero(seg < neck)[0]
        if under_idx.size == 0:
            continue
        over_idx = np.nonzero(seg > ceiling * (1.0 + tol))[0]
        first_over = int(over_idx[0]) if over_idx.size else None
        for ui in under_idx:
            ui = int(ui)
            if first_over is not None and first_over < ui:
                break
            k = lo + ui
            if vol_mult > 0:
                av = avg_vol[k]
                if not np.isfinite(av) or av <= 0 or vol[k] < vol_mult * av:
                    continue
            flags[k] = True
            break
    return flags


def range_breakout(p, lookback=20, width_atr_max=3.0, vol_mult=1.5):
    """レンジ（ボックス）上放れ: 直近 lookback 日の値幅/ATR が小さく、終値が上限を上抜け。

    ベクトル化版。直近 lookback 日（当日を除く）の高値/安値と平均出来高を pandas rolling で算出。
    """
    a = _to_arrays(p)
    high = _arr(a, "high")
    low = _arr(a, "low")
    close = _arr(a, "close")
    vol = _arr(a, "volume")
    atr = _arr(a, "atr14")
    n = len(close)
    flags = np.zeros(n, dtype=bool)
    if atr is None or n < lookback + 1:
        return flags
    rh = pd.Series(high).rolling(lookback).max().shift(1).values
    rl = pd.Series(low).rolling(lookback).min().shift(1).values
    avg_vol = pd.Series(vol).rolling(lookback).mean().shift(1).values
    with np.errstate(invalid="ignore", divide="ignore"):
        width_ok = (rh - rl) / atr <= width_atr_max
    vol_ok = (vol_mult <= 0) | (np.isfinite(avg_vol) & (avg_vol > 0) & (vol >= vol_mult * avg_vol))
    ok = (np.isfinite(rh) & np.isfinite(rl) & np.isfinite(atr) & (atr > 0)
          & (rh > rl) & (close > rh) & width_ok & vol_ok)
    flags[lookback:] = ok[lookback:]
    return flags


def triangle_breakout(p, lookback=40, sub=10, vol_mult=1.0):
    """三角持ち合い（収束）からの上放れ。高値線が下向き・安値線が上向きで幅が縮小。

    判定は当日を除いた確定値（高値線・安値線の前日値）で行うため、当日の上放れバーで
    「高値線が上向きに転じる」問題を避ける。
    """
    a = _to_arrays(p)
    high = _arr(a, "high")
    low = _arr(a, "low")
    close = _arr(a, "close")
    vol = _arr(a, "volume")
    n = len(close)
    flags = np.zeros(n, dtype=bool)
    rh = pd.Series(high).rolling(sub).max().values
    rl = pd.Series(low).rolling(sub).min().values
    rh_prev = _shift(rh, 1)   # 当日を除く直近 sub 本の高値線
    rl_prev = _shift(rl, 1)
    avg_vol = pd.Series(vol).rolling(20).mean().shift(1).values
    start = max(lookback + sub, 2 * sub)
    for i in range(start, n):
        if not (np.isfinite(rh_prev[i]) and np.isfinite(rh_prev[i - sub]) and np.isfinite(rl_prev[i])
                and np.isfinite(rl_prev[i - sub]) and np.isfinite(rh_prev[i - lookback])
                and np.isfinite(rl_prev[i - lookback])):
            continue
        upper_falling = rh_prev[i] < rh_prev[i - sub]
        lower_rising = rl_prev[i] > rl_prev[i - sub]
        width_now = rh_prev[i] - rl_prev[i]
        width_prev = rh_prev[i - lookback] - rl_prev[i - lookback]
        if width_prev <= 0 or width_now > width_prev * 0.9:
            continue
        if not (upper_falling and lower_rising):
            continue
        if close[i] <= rh_prev[i]:
            continue
        if vol_mult > 0:
            av = avg_vol[i]
            if not np.isfinite(av) or av <= 0 or vol[i] < vol_mult * av:
                continue
        flags[i] = True
    return flags


def flag_pennant(p, run_days=10, run_min=0.15, flag_days=10,
                 max_retrace=0.6, vol_mult=1.0):
    """フラッグ/ペナント: 急騰（run_days で run_min 以上）後の浅いもち合い→上放れ。ベクトル化版。"""
    a = _to_arrays(p)
    high = _arr(a, "high")
    low = _arr(a, "low")
    close = _arr(a, "close")
    vol = _arr(a, "volume")
    n = len(close)
    flags = np.zeros(n, dtype=bool)
    span = run_days + flag_days
    if n <= span:
        return flags
    avg_vol = pd.Series(vol).rolling(20).mean().shift(1).values
    idx = np.arange(n)
    ps = idx - flag_days - run_days
    pe = idx - flag_days
    run = np.full(n, np.nan)
    pole = np.full(n, np.nan)
    valid = ps >= 0
    with np.errstate(invalid="ignore", divide="ignore"):
        run[valid] = close[pe[valid]] / close[ps[valid]] - 1.0
    pole[valid] = close[pe[valid]] - close[ps[valid]]
    flag_high = pd.Series(high).rolling(flag_days).max().shift(1).values
    flag_low = pd.Series(low).rolling(flag_days).min().shift(1).values
    close_pe = np.full(n, np.nan)
    close_pe[valid] = close[pe[valid]]
    with np.errstate(invalid="ignore", divide="ignore"):
        retrace = (close_pe - flag_low) / pole
    vol_ok = (vol_mult <= 0) | (np.isfinite(avg_vol) & (avg_vol > 0) & (vol >= vol_mult * avg_vol))
    ok = (valid & (run >= run_min) & (pole > 0) & np.isfinite(flag_high) & np.isfinite(flag_low)
          & (retrace >= 0) & (retrace <= max_retrace) & (close > flag_high) & vol_ok)
    flags[span:] = ok[span:]
    return flags


def cup_with_handle(p, cup_min=30, cup_max=120, handle_max=15,
                    depth_min=0.12, depth_max=0.45, tol=0.05, vol_mult=1.0):
    """カップウィズハンドル（簡易版）: 左リム→カップ底→回復→浅いハンドル→リム上抜け。

    ベクトル化版（窓内の最大高値・最小安値・最大終値で近似）。研究用途の機械判定。
    """
    a = _to_arrays(p)
    high = _arr(a, "high")
    low = _arr(a, "low")
    close = _arr(a, "close")
    vol = _arr(a, "volume")
    n = len(close)
    flags = np.zeros(n, dtype=bool)
    if n < cup_max + handle_max + 1:
        return flags
    sh = handle_max + 1
    rim = pd.Series(high).rolling(cup_max).max().shift(sh).values
    cup_low = pd.Series(low).rolling(cup_max).min().shift(sh).values
    rec = pd.Series(close).rolling(cup_max).max().shift(sh).values
    handle_low = pd.Series(low).rolling(handle_max).min().shift(1).values
    avg_vol = pd.Series(vol).rolling(20).mean().shift(1).values
    with np.errstate(invalid="ignore", divide="ignore"):
        depth = (rim - cup_low) / rim
        handle_retrace = (rim - handle_low) / rim
    depth_ok = (depth >= depth_min) & (depth <= depth_max)
    handle_ok = handle_retrace <= np.maximum(0.4 * depth, 0.08)
    vol_ok = (vol_mult <= 0) | (np.isfinite(avg_vol) & (avg_vol > 0) & (vol >= vol_mult * avg_vol))
    ok = (np.isfinite(rim) & (rim > 0) & np.isfinite(cup_low) & np.isfinite(rec)
          & depth_ok & (rec >= rim * (1.0 - tol)) & np.isfinite(handle_low)
          & handle_ok & (close > rim) & vol_ok)
    flags[cup_min:] = ok[cup_min:]
    return flags


def trend_structure(p, left=5, right=5):
    """上昇トレンド構造（高値切り上げ & 安値切り上げ）の状態を表す bool 配列。"""
    a = _to_arrays(p)
    high = _arr(a, "high")
    low = _arr(a, "low")
    n = len(high)
    state = np.zeros(n, dtype=bool)
    hidx = np.where(pivot_highs(high, left, right))[0]
    lidx = np.where(pivot_lows(low, left, right))[0]
    for i in range(n):
        hs = hidx[hidx + right <= i]
        ls = lidx[lidx + right <= i]
        if len(hs) >= 2 and len(ls) >= 2:
            if high[hs[-1]] > high[hs[-2]] and low[ls[-1]] > low[ls[-2]]:
                state[i] = True
    return state


def pullback_continuation(p, slow=25, uptrend=200, slope_lookback=20, max_dist=0.05):
    """押し目継続: 上昇トレンド中に短期線（既定5日）を割れ→当日回復、かつ中期線近辺。"""
    a = _to_arrays(p)
    close = _arr(a, "close")
    sma25 = _arr(a, "sma25") if a.get("sma25") is not None else _sma(close, slow)
    sma200 = _arr(a, "sma200") if a.get("sma200") is not None else _sma(close, uptrend)
    sma5 = _arr(a, "sma5") if a.get("sma5") is not None else _sma(close, 5)
    n = len(close)
    up = (close > sma200) & (sma200 > _shift(sma200, slope_lookback))
    reclaim = (close > sma5) & (_shift(close, 1) <= _shift(sma5, 1))
    near = np.isfinite(sma25) & (np.abs(close / sma25 - 1.0) <= max_dist)
    return _cache_nan(up & reclaim & near)


# --------------------------------------------------------------------------- まとめ
# 研究・将来の反映で使う基本構成（名前 -> 呼び出し）。
def pattern_functions():
    """型名 -> (関数, 追加kwargs) のレジストリ（基本構成）。"""
    return {
        "GC(5,25)": (ma_cross, {"fast": 5, "slow": 25, "kind": "gc"}),
        "DC(5,25)": (ma_cross, {"fast": 5, "slow": 25, "kind": "dc"}),
        "GC(25,75)": (ma_cross, {"fast": 25, "slow": 75, "kind": "gc"}),
        "DC(25,75)": (ma_cross, {"fast": 25, "slow": 75, "kind": "dc"}),
        "GC(20,60)": (ma_cross, {"fast": 20, "slow": 60, "kind": "gc"}),
        "DC(20,60)": (ma_cross, {"fast": 20, "slow": 60, "kind": "dc"}),
        "GC(週足13,52)": (weekly_ma_cross, {"fast": 13, "slow": 52, "kind": "gc"}),
        "DC(週足13,52)": (weekly_ma_cross, {"fast": 13, "slow": 52, "kind": "dc"}),
        "ダブルボトム": (double_bottom, {}),
        "ダブルトップ": (double_top, {}),
        "レンジブレイク": (range_breakout, {}),
        "三角持ち合い上放れ": (triangle_breakout, {}),
        "フラッグ/ペナント": (flag_pennant, {}),
        "カップウィズハンドル": (cup_with_handle, {}),
        "上昇トレンド構造(HH/HL)": (trend_structure, {}),
        "押し目継続": (pullback_continuation, {}),
    }


def _last_event_days(flags, recent_days=3):
    f = np.asarray(flags, dtype=bool)
    n = len(f)
    if n == 0:
        return None
    lo = max(0, n - recent_days)
    idx = np.nonzero(f[lo:])[0]
    if idx.size == 0:
        return None
    return int(n - 1 - (lo + int(idx[-1])))


def latest_pattern_summary(p, recent_days=3):
    """直近 recent_days 本以内に発生した型を短い文字列で返す（AIプロンプト用）。

    例: "GC(20,60)直後 / DC(5,25)2日前 / ダブルボトム"
    型は平均的な傾向の参考情報であり、売買判断を単独で決めるものではない。
    """
    a = _to_arrays(p)
    items = []

    def add(label, flags):
        d = _last_event_days(flags, recent_days)
        if d is not None:
            items.append(label if d == 0 else f"{label}{d}日前")

    try:
        gc20, dc20 = ma_cross(a, 20, 60)
        _, dc5 = ma_cross(a, 5, 25)
        add("GC(20,60)", gc20)
        add("DC(5,25)", dc5)
        add("DC(20,60)", dc20)
    except Exception:
        pass
    for label, fn in (("ダブルボトム", double_bottom), ("レンジブレイク", range_breakout),
                      ("カップ", cup_with_handle), ("フラッグ", flag_pennant)):
        try:
            add(label, fn(a))
        except Exception:
            pass
    return " / ".join(items)


def compute_patterns(p):
    """1銘柄分の全パターン（bool配列）を返す。"""
    out = {}
    for name, (fn, kw) in pattern_functions().items():
        try:
            kind = kw.get("kind")
            call_kw = {k: v for k, v in kw.items() if k != "kind"}
            res = fn(p, **call_kw)
            if kind == "gc":
                out[name] = np.asarray(res[0], dtype=bool)
            elif kind == "dc":
                out[name] = np.asarray(res[1], dtype=bool)
            else:
                out[name] = np.asarray(res, dtype=bool)
        except Exception:
            continue
    return out
