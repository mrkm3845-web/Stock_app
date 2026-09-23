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

    # RSI(14)・Wilder 平滑（過熱度判定用）
    delta = pd.Series(c).diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
    rs = avg_gain / avg_loss
    rsi14 = 100.0 - (100.0 / (1.0 + rs))
    rsi14 = rsi14.where(avg_loss != 0, 100.0)
    rsi14 = rsi14.where(avg_gain.notna(), np.nan).values

    # トレンド系（スコア再設計用・Phase 1 の研究で有効と確認）
    sma200_prev = pd.Series(sma200).shift(20).values
    sma200_slope = np.where(
        (~np.isnan(sma200_prev)) & (sma200_prev > 0), sma200 / sma200_prev - 1.0, np.nan
    )
    roll_max_52w = pd.Series(c).rolling(252, min_periods=1).max().values
    pos_52w = np.where(roll_max_52w > 0, c / roll_max_52w, np.nan)
    prev120 = pd.Series(c).shift(120).values
    ret_120d = np.where(
        (~np.isnan(prev120)) & (prev120 > 0), c / prev120 - 1.0, np.nan
    )
    dist_sma200 = np.where(
        (~np.isnan(sma200)) & (sma200 > 0), c / sma200 - 1.0, np.nan
    )

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
        "rsi14": rsi14,
        "sma200_slope": sma200_slope,
        "pos_52w": pos_52w,
        "ret_120d": ret_120d,
        "dist_sma200": dist_sma200,
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


def _clamp01(x):
    """NaN/None を 0 とし、0〜1 に収める。"""
    try:
        xf = float(x)
    except (TypeError, ValueError):
        return 0.0
    if not np.isfinite(xf):
        return 0.0
    return min(1.0, max(0.0, xf))


def compute_technical_score(feat, params, weekly_trend_up=None, relax=None):
    """総合スコア（0〜100）。Phase 2: トレンド系（持続的な上昇）で構成。

    Phase 1 の研究（分位分析）で有効と確認された指標のみを使う。
    - pos_52w      : 52週高値からの位置（高いほど上昇トレンド）
    - dist_sma200  : 200日線からの乖離率（30%で満点）
    - sma200_slope : 200日線の傾き（20営業日、10%で満点）
    - ret_120d     : 120日リターン（60%で満点）

    旧成分（daily_gc / val_ratio / weekly_trend）は研究で逆効果（分位スプレッドが
    マイナス）と分かったため除外した。
    """
    w = params.get("score_weights", {})

    def _last(key):
        arr = feat.get(key)
        if arr is None or len(arr) == 0:
            return 0.0
        return arr[-1]

    score = 0.0
    score += w.get("pos_52w", 30) * _clamp01(_last("pos_52w"))
    score += w.get("dist_sma200", 25) * _clamp01(_last("dist_sma200") / 0.30)
    score += w.get("sma200_slope", 25) * _clamp01(_last("sma200_slope") / 0.10)
    score += w.get("ret_120d", 20) * _clamp01(_last("ret_120d") / 0.60)

    return round(float(score), 1)


def evaluate_warnings(price, val_ratio, params, extra=None):
    """警告ルールを評価する（安全なミニ評価器）。

    extra に株価・出来高以外の指標（乖離率・RSI 等）を渡すと、価格帯に依存しない
    過熱警告ルールも評価できる。env のキーは条件式の変数名と一致させる。
    """
    env = {"price": float(price), "val_ratio": float(val_ratio)}
    if extra:
        for k, v in extra.items():
            if v is None:
                continue
            try:
                env[k] = float(v)
            except (TypeError, ValueError):
                continue
    out = []
    for rule in params.get("warnings", []):
        if _eval_condition(rule.get("condition", ""), env):
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

    try:
        monthly = df.resample("ME").agg({"Close": "last"}).dropna()
    except ValueError:
        # pandas 2.1以前は "ME" が存在しないため旧エイリアスへフォールバック
        monthly = df.resample("M").agg({"Close": "last"}).dropna()
    monthly_trend_up = False
    if len(monthly) >= 6:
        m_close = monthly["Close"].values.astype(float)
        m_ma6 = float(np.mean(m_close[-6:]))
        monthly_trend_up = bool(m_close[-1] > m_ma6)

    ret5 = (price / c[-6] - 1.0) * 100 if len(c) >= 6 else 0.0
    ret20 = (price / c[-21] - 1.0) * 100 if len(c) >= 21 else 0.0

    sma5 = float(feat["sma5"][-1]) if not np.isnan(feat["sma5"][-1]) else None
    sma25 = float(feat["sma25"][-1]) if not np.isnan(feat["sma25"][-1]) else None
    sma200 = float(feat["sma200"][-1]) if not np.isnan(feat["sma200"][-1]) else None

    def _dist(sma):
        return round((price / sma - 1.0) * 100, 2) if sma and sma > 0 else None

    # 連騰日数（直近終値ベースで何日連続して前日比プラスか）
    run_up_days = 0
    for k in range(len(c) - 1, 0, -1):
        if c[k] > c[k - 1]:
            run_up_days += 1
        else:
            break

    gap_pct = round((o[-1] / c[-2] - 1.0) * 100, 2) if len(c) >= 2 and c[-2] > 0 else 0.0
    rsi_val = feat["rsi14"][-1]
    rsi14 = round(float(rsi_val), 1) if not (rsi_val is None or np.isnan(rsi_val)) else None
    pos_52w = feat["pos_52w"][-1]
    pos_52w = round(float(pos_52w), 3) if not (pos_52w is None or np.isnan(pos_52w)) else None

    return {
        "price": round(price, 1),
        "sma5": round(sma5, 1) if sma5 else None,
        "sma25": round(sma25, 1) if sma25 else None,
        "sma200": round(sma200, 1) if sma200 else None,
        "support_20d": round(low20, 1),
        "resistance_20d": round(high20, 1),
        "upper_shadow_atr": round(upper_shadow, 2),
        "lower_shadow_atr": round(lower_shadow, 2),
        "range_position": round(range_pos, 2),
        "weekly_trend_up": bool(weekly["trend_up"]),
        "monthly_trend_up": monthly_trend_up,
        "ret_5d_pct": round(ret5, 2),
        "ret_20d_pct": round(ret20, 2),
        "dist_sma5_pct": _dist(sma5),
        "dist_sma25_pct": _dist(sma25),
        "dist_sma200_pct": _dist(sma200),
        "atr_pct": round((atr / price) * 100, 2) if price > 0 else None,
        "rsi14": rsi14,
        "run_up_days": run_up_days,
        "gap_pct": gap_pct,
        "pos_52w": pos_52w,
        "gc_days": int(feat["gc_days"][-1]) if int(feat["gc_days"][-1]) < 900 else None,
        "val_ratio_5d": round(float(feat["val_ratio_5d"][-1]), 2),
        "avg_val_5d": int(round(float(feat["avg_val_5d"][-1]))),
        "atr14": round(atr, 1),
    }


ENTRY_TYPES = ("breakout_chase", "pullback_wait", "probe_only", "wait")


def compute_entry_plan(ctx, params):
    """過熱度から「追いかけ／押し目待ち／打診／見送り」のエントリー案を組み立てる。

    上昇銘柄を除外するのではなく、高値掴み（イナゴ買い）を避けるための
    「入口の調整」を行うためのルールベース安全網。AI の entry_type が
    無い・不正な場合のフォールバックとしても使う。
    """
    eg = (params or {}).get("entry_guard", {})
    dist25 = ctx.get("dist_sma25_pct")
    rsi = ctx.get("rsi14")
    ret5 = ctx.get("ret_5d_pct")
    price = ctx.get("price") or 0.0
    atr = ctx.get("atr14") or 0.0
    sma5 = ctx.get("sma5")
    sma25 = ctx.get("sma25")

    flags = []
    if dist25 is not None and dist25 >= eg.get("dist_sma25_moderate", 18.0):
        flags.append("sma25_extended")
    if rsi is not None and rsi >= eg.get("rsi_watch", 75.0):
        flags.append("rsi_high")
    if ret5 is not None and ret5 >= eg.get("ret5_watch", 20.0):
        flags.append("ret5_high")

    d = dist25 if dist25 is not None else 0.0
    if (d >= eg.get("dist_sma25_extreme", 45.0)
            or (dist25 is not None and dist25 >= eg.get("dist_sma25_strong", 30.0)
                and rsi is not None and rsi >= eg.get("rsi_hot", 80.0))):
        level = "extreme"
    elif (d >= eg.get("dist_sma25_strong", 30.0)
          or (rsi is not None and rsi >= eg.get("rsi_hot", 80.0))
          or (ret5 is not None and ret5 >= eg.get("ret5_hot", 30.0))):
        level = "strong"
    elif (d >= eg.get("dist_sma25_moderate", 18.0)
          or (rsi is not None and rsi >= eg.get("rsi_watch", 75.0))
          or (ret5 is not None and ret5 >= eg.get("ret5_watch", 20.0))):
        level = "moderate"
    else:
        level = "low"

    shallow = price - eg.get("pullback_atr_shallow", 0.5) * atr
    deep = price - eg.get("pullback_atr_deep", 1.0) * atr
    zone_high = max([v for v in (sma5, shallow) if v] or [shallow])
    zone_low = min([v for v in (sma5, deep) if v] or [deep])
    if zone_low > zone_high:
        zone_low, zone_high = zone_high, zone_low

    if level == "low":
        entry_type = "breakout_chase"
        entry_price = price
    elif level == "moderate":
        entry_type = "pullback_wait"
        entry_price = zone_high
    elif level == "strong":
        entry_type = "probe_only"
        entry_price = zone_low
    else:
        entry_type = "wait"
        entry_price = zone_low

    # 押し目待ちの下値は 25 日線より深追いしない（トレンド破壊を避ける）
    if sma25 and entry_type in ("pullback_wait", "probe_only", "wait"):
        entry_price = max(entry_price, round(sma25, 1))
        zone_low = max(zone_low, round(sma25, 1))

    return {
        "overheat_level": level,
        "overheat_flags": flags,
        "suggested_entry_type": entry_type,
        "entry_price": int(round(entry_price)) if entry_price else None,
        "entry_zone_low": int(round(zone_low)) if zone_low else None,
        "entry_zone_high": int(round(zone_high)) if zone_high else None,
        "wait_days": int(eg.get("pullback_wait_days", 5)),
    }
