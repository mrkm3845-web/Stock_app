"""共通特徴量・スコア計算モジュール。

スクリーナー（main7.py）とバックテストが同じ式を使うための単一情報源。
GC日数・5日平均売買代金・売買代金増加率・週足トレンド・ATR・総合スコアを提供する。
"""
import numpy as np
import pandas as pd


def trading_value(close, volume):
    """売買代金（千円）。既存スクリーナー/バックテストと同一式。"""
    return (close * volume) / 1000.0


def compute_daily_features(df):
    """日足DataFrameからテクニカル特徴量を計算する。

    Parameters
    ----------
    df : pd.DataFrame
        index=datetime, columns=[Open, High, Low, Close, Volume]

    Returns
    -------
    dict of np.ndarray
        dates, open, high, low, close, volume, sma5, sma25, sma200,
        gc_days, avg_val_5d, val_ratio_5d, atr14, atr_pct
    """
    df = df.sort_index()
    c = df["Close"].values.astype(float)
    o = df["Open"].values.astype(float)
    h = df["High"].values.astype(float)
    l = df["Low"].values.astype(float)
    v = df["Volume"].values.astype(float)
    dates = df.index.values

    sma5 = pd.Series(c).rolling(5).mean().values
    sma25 = pd.Series(c).rolling(25).mean().values
    sma200 = pd.Series(c).rolling(200).mean().values

    t_val = trading_value(c, v)

    # 5日平均売買代金（前日までの5日平均。当日を含めない）
    avg_val_5d = pd.Series(t_val).shift(1).rolling(5).mean().fillna(0).values

    # 売買代金増加率（当日 / 5営業日前）。ゼロ割・欠損は 1.0 扱い。
    val_5d_ago = pd.Series(t_val).shift(5).values
    val_ratio_5d = np.where(
        (~np.isnan(val_5d_ago)) & (val_5d_ago > 0),
        t_val / np.where(val_5d_ago > 0, val_5d_ago, 1.0),
        1.0,
    )

    # ゴールデンクロス（SMA5 が SMA25 を下から上へ抜けた日からの経過日数）
    prev_sma5 = pd.Series(sma5).shift(1).values
    prev_sma25 = pd.Series(sma25).shift(1).values
    gc_cross = (sma5 > sma25) & (prev_sma5 <= prev_sma25)
    gc_cross[0] = False
    gc_days = np.full(len(c), 999, dtype=int)
    last_idx = -9999
    for i in range(len(c)):
        if gc_cross[i]:
            last_idx = i
            gc_days[i] = 0
        elif last_idx >= 0:
            gc_days[i] = i - last_idx

    # ATR(14)（平均真値幅）
    prev_close = pd.Series(c).shift(1).fillna(c[0]).values
    tr = np.maximum.reduce([
        h - l,
        np.abs(h - prev_close),
        np.abs(l - prev_close),
    ])
    atr14 = pd.Series(tr).rolling(14).mean().values
    atr_pct = np.where(c > 0, atr14 / c, 0.0)

    return {
        "dates": dates,
        "open": o,
        "high": h,
        "low": l,
        "close": c,
        "volume": v,
        "sma5": sma5,
        "sma25": sma25,
        "sma200": sma200,
        "gc_days": gc_days,
        "avg_val_5d": avg_val_5d,
        "val_ratio_5d": val_ratio_5d,
        "atr14": atr14,
        "atr_pct": atr_pct,
    }


def compute_weekly_features(df):
    """週足にリサンプリングし、週足トレンドを返す。"""
    w = (
        df.resample("W-FRI")
        .agg({"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"})
        .dropna()
    )
    if len(w) == 0:
        return {
            "dates": np.array([]),
            "close": np.array([]),
            "sma13": np.array([]),
            "trend_up": False,
            "weekly_volume_ratio": 1.0,
        }

    c = w["Close"].values.astype(float)
    v = w["Volume"].values.astype(float)
    sma13 = pd.Series(c).rolling(13).mean().values
    trend_up = bool(len(c) >= 13 and not np.isnan(sma13[-1]) and c[-1] > sma13[-1])

    vol_ratio = 1.0
    if len(v) >= 2 and v[-2] > 0:
        vol_ratio = float(v[-1] / v[-2])

    return {
        "dates": w.index.values,
        "close": c,
        "sma13": sma13,
        "trend_up": trend_up,
        "weekly_volume_ratio": vol_ratio,
    }


def tier_for_price(price, tiers):
    """株価に該当する価格帯を返す。"""
    for t in tiers:
        if t["min"] <= price < t["max"]:
            return t
    return tiers[-1] if tiers else None


def compute_technical_score(feat, params, weekly_trend_up, relax=None):
    """総合スコア（0〜100）。Stage1 は技術指標のみ。"""
    sig = params.get("signals", {})
    w = params.get("score_weights", {})
    relax = relax or {}

    gc_window = relax.get("gc_window", sig.get("gc_window", 3))
    val_min = relax.get("val_ratio_min", sig.get("val_ratio_min", 1.2))
    avg_min = relax.get("avg_val_min_k", sig.get("avg_val_min_k", 30000))

    close = float(feat["close"][-1])
    score = 0.0

    if weekly_trend_up:
        score += w.get("weekly_trend", 25)

    g = int(feat["gc_days"][-1])
    if 0 <= g <= gc_window:
        score += w.get("daily_gc", 20)

    vr = float(feat["val_ratio_5d"][-1])
    if vr >= val_min:
        score += w.get("val_ratio", 20) * min(1.0, vr / 3.0)

    av = float(feat["avg_val_5d"][-1])
    if av >= avg_min:
        score += w.get("avg_val", 15)

    sma200 = feat["sma200"][-1]
    if not np.isnan(sma200) and close > sma200:
        score += w.get("trend_sma200", 20)

    return round(float(score), 1)


def evaluate_warnings(price, val_ratio, params):
    """警告ルールを評価する（安全なミニ評価器）。"""
    out = []
    for rule in params.get("warnings", []):
        if _eval_condition(rule.get("condition", ""), {"price": float(price), "val_ratio": float(val_ratio)}):
            out.append(rule)
    return out


def _eval_condition(cond, env):
    """'price <= 1000 and val_ratio >= 4.0' 形式を評価。変数は env から。"""
    if not cond:
        return False
    results = []
    for p in cond.split(" and "):
        p = p.strip()
        matched = False
        for op in (">=", "<=", ">", "<", "=="):
            if op in p:
                left, right = p.split(op, 1)
                left = left.strip()
                right = float(right.strip())
                lval = env.get(left)
                if lval is None:
                    return False
                if op == ">=":
                    results.append(lval >= right)
                elif op == "<=":
                    results.append(lval <= right)
                elif op == ">":
                    results.append(lval > right)
                elif op == "<":
                    results.append(lval < right)
                elif op == "==":
                    results.append(lval == right)
                matched = True
                break
        if not matched:
            return False
    return all(results)


def compute_stock_context(df, feat=None, weekly=None):
    """AI に渡す銘柄別スカラー情報（画像なしで分かる範囲のローソク足由来特徴量）。"""
    if feat is None:
        feat = compute_daily_features(df)
    if weekly is None:
        weekly = compute_weekly_features(df)

    c = df["Close"].values.astype(float)
    o = df["Open"].values.astype(float)
    h = df["High"].values.astype(float)
    l = df["Low"].values.astype(float)

    price = float(c[-1])
    high20 = float(np.max(h[-20:])) if len(h) >= 20 else float(np.max(h))
    low20 = float(np.min(l[-20:])) if len(l) >= 20 else float(np.min(l))

    atr = feat["atr14"][-1]
    atr = float(atr) if not np.isnan(atr) and atr > 0 else max(price * 0.02, 1.0)

    body_top = max(float(o[-1]), float(c[-1]))
    body_bottom = min(float(o[-1]), float(c[-1]))
    upper_shadow = (float(h[-1]) - body_top) / atr
    lower_shadow = (body_bottom - float(l[-1])) / atr
    range_pos = (price - low20) / (high20 - low20) if high20 > low20 else 0.5

    monthly = df.resample("M").agg({"Close": "last"}).dropna()
    monthly_trend_up = False
    if len(monthly) >= 6:
        m_close = monthly["Close"].values.astype(float)
        m_ma6 = float(np.mean(m_close[-6:]))
        monthly_trend_up = bool(m_close[-1] > m_ma6)

    ret5 = (price / c[-6] - 1.0) * 100 if len(c) >= 6 else 0.0
    ret20 = (price / c[-21] - 1.0) * 100 if len(c) >= 21 else 0.0

    return {
        "price": round(price, 1),
        "sma5": round(float(feat["sma5"][-1]), 1) if not np.isnan(feat["sma5"][-1]) else None,
        "sma25": round(float(feat["sma25"][-1]), 1) if not np.isnan(feat["sma25"][-1]) else None,
        "sma200": round(float(feat["sma200"][-1]), 1) if not np.isnan(feat["sma200"][-1]) else None,
        "support_20d": round(low20, 1),
        "resistance_20d": round(high20, 1),
        "upper_shadow_atr": round(upper_shadow, 2),
        "lower_shadow_atr": round(lower_shadow, 2),
        "range_position": round(range_pos, 2),
        "weekly_trend_up": bool(weekly["trend_up"]),
        "monthly_trend_up": monthly_trend_up,
        "ret_5d_pct": round(ret5, 2),
        "ret_20d_pct": round(ret20, 2),
        "gc_days": int(feat["gc_days"][-1]) if int(feat["gc_days"][-1]) < 900 else None,
        "val_ratio_5d": round(float(feat["val_ratio_5d"][-1]), 2),
        "avg_val_5d": int(round(float(feat["avg_val_5d"][-1]))),
        "atr14": round(atr, 1),
    }
