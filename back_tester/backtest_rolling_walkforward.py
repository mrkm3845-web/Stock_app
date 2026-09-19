"""
backtest_rolling_walkforward.py
日本株スイング戦略 ローリングウォークフォワード検証（スコア式再現版）

方針:
- 共通特徴量 common/features.py を使い、スクリーナー（main8.py）と同一の
  スコア式（週足トレンド / 日足GC / 売買代金増加率 / 5日平均代金 / SMA200）を再現して検証する。
- 期間を学習12ヶ月 / 検証3ヶ月 / スライド3ヶ月で区切る（walk_end は実行日の前月末＝ローリング）。
- 毎営業日、スコア上位K銘柄を選び、翌日寄りでエントリー。
- エグジットは「固定TP/SL」と「ATRトレーリング」を比較する。
- 資金制約（最大同時保有数・固定比率サイズ）を加味したポートフォリオ指標を計算する。
- ベンチマークは TOPIX ETF (1306.T) の同時期バイ&ホールド。
- 出力は results/backtest_walkforward_result.json / CSV / Markdown。

既知のバイアス（レポートにも注記）:
- サバイバーシップバイアス: 現在のJPX上場銘柄のみ対象（過去に上場廃止された銘柄は欠落）。
- 多重検定: 条件数を増やすほど偶然の好成績が出やすくなる点に留意。
- 実運用の Stage2（AI）は未検証。本検証は技術スコアのランキング力と機械的エグジットのみを対象とする。
"""

import io
import itertools
import json
import os
import re
import sys
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import requests
import yfinance as yf

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))  # リポジトリ直下（common/ と docs/ を参照）
from common.config import load_strategy_params  # noqa: E402
from common import features as F  # noqa: E402

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(BASE_DIR, "data", "backtest_cache")
OUTPUT_DIR = os.path.join(BASE_DIR, "results")

CONFIG = {
    "markets": ["プライム", "スタンダード"],
    "max_stocks_sample": None,  # テスト時は 100 などに絞る
    "train_months": 12,
    "test_months": 3,
    "step_months": 3,
    "walk_start": "2022-01-01",
    "walk_end": None,  # None なら実行日の前月末（ローリング）
    "fee_rate": 0.0005,
    "slippage_rate": 0.001,
    "same_day_tp_sl": "loss",
    "top_k": 5,            # スコア上位K銘柄（weekly_top_picks に相当）
    "max_positions": 5,    # 同時保有上限（ポートフォリオ集計用）
    "min_avg_val_k": 10000,  # 流動性フロア（5日平均売買代金・千円）
    "min_trades_test": 100,  # 有効条件の最低テスト取引数
    "benchmark_ticker": "1306.T",  # TOPIX ETF
    "quantile_n": 10,          # 分位分析の分位数（スコアの選定エッジ検証用）
    "quantile_hold_days": 5,   # 分位分析の将来リターン保有日数（TP/SLなし）
}

SIGNAL_GRID = [
    {"gc_window": 1, "val_ratio_min": 1.5, "avg_val_min_k": 30000},
    {"gc_window": 3, "val_ratio_min": 1.2, "avg_val_min_k": 30000},
    {"gc_window": 3, "val_ratio_min": 1.5, "avg_val_min_k": 30000},
    {"gc_window": 3, "val_ratio_min": 2.0, "avg_val_min_k": 30000},
]
EXIT_MODES = ["fixed", "atr_trail"]

# 株価帯別エグジット最適化の探索グリッド（price_tiers の tp/sl/atr/保有日数を最適化）
TIER_EXIT_GRID = {
    "fixed": {
        "tp_pct": [0.05, 0.08, 0.12, 0.15],
        "sl_pct": [0.02, 0.03, 0.05, 0.07],
        "max_hold_days": [7, 14, 20],
    },
    "atr_trail": {
        "atr_sl_mult": [1.5, 2.0, 2.5, 3.0],
        "max_hold_days": [7, 14, 20],
    },
}
TIER_EXIT_MIN_TRADES_TEST = 100


# --------------------------------------------------------------------------- データ取得
def fetch_jpx_stock_list(markets):
    page_url = "https://www.jpx.co.jp/markets/statistics-equities/misc/01.html"
    headers = {"User-Agent": "Mozilla/5.0"}
    excel_url = None
    try:
        res = requests.get(page_url, headers=headers, timeout=30)
        if res.status_code == 200:
            m = re.search(r'href="([^"]+data_j\.xls[x]?)"', res.text)
            if m:
                rel = m.group(1)
                excel_url = "https://www.jpx.co.jp" + rel if rel.startswith("/") else rel
    except Exception:
        pass
    if not excel_url:
        excel_url = "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xls"

    try:
        res = requests.get(excel_url, headers=headers, timeout=30)
        if res.status_code == 200:
            try:
                df = pd.read_excel(io.BytesIO(res.content), engine="xlrd")
            except Exception:
                df = pd.read_excel(io.BytesIO(res.content), engine="openpyxl")
            df = df[["コード", "銘柄名", "市場・商品区分", "33業種区分"]]
            df["コード"] = df["コード"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
            pattern = "|".join(markets)
            return df[df["市場・商品区分"].str.contains(pattern, na=False)].to_dict("records")
    except Exception as e:
        print(f">> JPX取得失敗: {e}")
    return []


def _read_cached(f):
    try:
        df = pd.read_csv(f, index_col=0, parse_dates=True)
        df.index = pd.to_datetime(df.index).tz_localize(None)
        return df
    except Exception:
        return None


def _is_fresh(df, end_dt, stale_days=7):
    if df is None or len(df) == 0:
        return False
    try:
        return df.index.max() >= end_dt - timedelta(days=stale_days)
    except Exception:
        return False


def download_stock_data(codes, start, end):
    """日足データをキャッシュに保持しつつ、末尾が古ければ再取得（ローリング対応）。"""
    os.makedirs(CACHE_DIR, exist_ok=True)
    end_dt = datetime.strptime(end, "%Y-%m-%d")
    start_dt = datetime.strptime(start, "%Y-%m-%d") - timedelta(days=60)
    adjusted_start = start_dt.strftime("%Y-%m-%d")

    stock_dfs = {}
    missing = []
    for code in codes:
        f = os.path.join(CACHE_DIR, f"{code}.csv")
        df = _read_cached(f) if os.path.exists(f) else None
        if df is not None and len(df) >= 30 and _is_fresh(df, end_dt):
            stock_dfs[code] = df
        else:
            missing.append(code)

    if missing:
        print(f">> {len(missing)} 銘柄をダウンロード中...")
        batch_size = 100
        for i in range(0, len(missing), batch_size):
            batch = missing[i:i + batch_size]
            tickers = [f"{c}.T" for c in batch]
            try:
                data = yf.download(tickers, start=adjusted_start, end=end, group_by="ticker",
                                   auto_adjust=True, progress=False, threads=True)
                for c in batch:
                    try:
                        sym = f"{c}.T"
                        if sym in data.columns.levels[0]:
                            df = data[sym].dropna(how="all")[["Open", "High", "Low", "Close", "Volume"]].dropna()
                            if len(df) >= 30:
                                df.index = pd.to_datetime(df.index).tz_localize(None)
                                df.to_csv(os.path.join(CACHE_DIR, f"{c}.csv"))
                                stock_dfs[c] = df
                    except Exception:
                        pass
            except Exception as e:
                print(f"Batch download error: {e}")

    print(f">> 有効日足データ: {len(stock_dfs)} 銘柄")
    return stock_dfs


def download_benchmark(ticker, start, end):
    os.makedirs(CACHE_DIR, exist_ok=True)
    f = os.path.join(CACHE_DIR, f"benchmark_{ticker.replace('.', '_')}.csv")
    end_dt = datetime.strptime(end, "%Y-%m-%d")
    start_dt = datetime.strptime(start, "%Y-%m-%d") - timedelta(days=60)
    df = _read_cached(f) if os.path.exists(f) else None
    if df is None or not _is_fresh(df, end_dt):
        try:
            data = yf.download(ticker, start=start_dt.strftime("%Y-%m-%d"), end=end,
                               auto_adjust=True, progress=False)
            if isinstance(data.columns, pd.MultiIndex):
                close = data["Close"] if "Close" in data.columns.levels[0] else None
                s = close.iloc[:, 0] if close is not None else data.iloc[:, 0]
            elif "Close" in data.columns:
                s = data["Close"]
            else:
                s = data.iloc[:, 0]
            if isinstance(s, pd.DataFrame):
                s = s.iloc[:, 0]
            s = s.dropna()
            s.index = pd.to_datetime(s.index).tz_localize(None)
            s.to_csv(f)
            df = s
        except Exception as e:
            print(f">> ベンチマーク取得失敗: {e}")
    return df


# --------------------------------------------------------------------------- 特徴量・スコア（スクリーナーと同一式）
def weekly_trend_daily(df):
    """週足（金曜終値）SMA13 の上抜きを日次に前詰めした bool 配列。

    - 当日（金曜）引け時点でその週の終値は確定しているため、金曜日のシグナルは当日に利用可。
    - 週の途中（月〜木）は直前の「確定した週」のトレンドを使う（先読み回避）。
    """
    w = df["Close"].resample("W-FRI").last().dropna()
    if len(w) == 0:
        return np.zeros(len(df), dtype=bool)
    sma13 = w.rolling(13).mean()
    trend = w > sma13
    daily = trend.reindex(df.index, method="ffill").fillna(False)
    return daily.values.astype(bool)


def score_series(feat, weekly_up, sig, weights):
    """compute_technical_score と同一式のスコアを全バー分ベクトル化して返す。"""
    gc = feat["gc_days"]
    vr = feat["val_ratio_5d"]
    av = feat["avg_val_5d"]
    sma200 = feat["sma200"]
    close = feat["close"]

    score = np.zeros(len(close), dtype=float)
    score += weights.get("weekly_trend", 0) * weekly_up.astype(float)
    score += weights.get("daily_gc", 0) * ((gc >= 0) & (gc <= sig.get("gc_window", 3))).astype(float)
    score += weights.get("val_ratio", 0) * np.minimum(1.0, np.where(vr > 0, vr / 3.0, 0.0)) * (vr >= sig.get("val_ratio_min", 1.2)).astype(float)
    score += weights.get("avg_val", 0) * (av >= sig.get("avg_val_min_k", 30000)).astype(float)
    score += weights.get("trend_sma200", 0) * ((~np.isnan(sma200)) & (close > sma200)).astype(float)
    return score


def prepare_stocks(stock_dfs):
    prepared = {}
    for code, df in stock_dfs.items():
        try:
            feat = F.compute_daily_features(df)
            if len(feat["close"]) < 35:
                continue
            weekly_up = weekly_trend_daily(df)
            eligible = (
                weekly_up
                & (feat["avg_val_5d"] >= CONFIG["min_avg_val_k"])
                & (~np.isnan(feat["sma200"]))
                & (feat["close"] > 0)
            )
            prepared[code] = {**feat, "weekly_up": weekly_up, "eligible": eligible}
        except Exception:
            continue
    return prepared


# --------------------------------------------------------------------------- フォールド生成
def make_folds(cfg):
    folds = []
    cur = datetime.strptime(cfg["walk_start"], "%Y-%m-%d")
    end = datetime.strptime(cfg["walk_end"], "%Y-%m-%d")
    train_d = timedelta(days=cfg["train_months"] * 30)
    test_d = timedelta(days=cfg["test_months"] * 30)
    step_d = timedelta(days=cfg["step_months"] * 30)

    while cur + train_d + test_d <= end:
        train_start = cur
        train_end = cur + train_d
        test_start = train_end
        test_end = test_start + test_d
        folds.append((
            train_start.strftime("%Y-%m-%d"),
            train_end.strftime("%Y-%m-%d"),
            test_start.strftime("%Y-%m-%d"),
            test_end.strftime("%Y-%m-%d"),
        ))
        cur += step_d
    return folds


# --------------------------------------------------------------------------- スコア上位Kの選定
def build_longform(prepared, sig, weights, start_dt, end_dt):
    """全銘柄の「（日付, code, インデックス, スコア）」を numpy 配列に展開する。"""
    ts = np.datetime64(start_dt)
    te = np.datetime64(end_dt)
    codes_l, dates_l, idx_l, score_l = [], [], [], []
    for code, p in prepared.items():
        dates = p["dates"]
        mask = p["eligible"] & (dates >= ts) & (dates <= te)
        ii = np.where(mask)[0]
        if len(ii) == 0:
            continue
        s = score_series(p, p["weekly_up"], sig, weights)
        codes_l.append(np.repeat(code, len(ii)))
        dates_l.append(dates[ii])
        idx_l.append(ii)
        score_l.append(s[ii])
    if not codes_l:
        return None
    return (
        np.concatenate(codes_l),
        np.concatenate(dates_l),
        np.concatenate(idx_l),
        np.concatenate(score_l),
    )


def select_topk_daily(longform, start_dt, end_dt, top_k):
    """各営業日（カレンダー日）ごとにスコア上位K銘柄を選ぶ。"""
    codes, dates, idx, score = longform
    ts = np.datetime64(start_dt)
    te = np.datetime64(end_dt)
    m = (dates >= ts) & (dates <= te)
    d = dates[m]
    c = codes[m]
    i = idx[m]
    s = score[m]
    if len(d) == 0:
        return []

    order = np.lexsort((-s, d))  # 日付昇順 → スコア降順
    d = d[order]
    c = c[order]
    i = i[order]

    picked = []
    n = len(d)
    j = 0
    while j < n:
        k = j
        while k < n and d[k] == d[j]:
            k += 1
        cnt = min(top_k, k - j)
        for t in range(cnt):
            picked.append((c[j + t], int(i[j + t])))
        j = k
    return picked


# --------------------------------------------------------------------------- 検証基盤（Phase 0）
# スコアの「選定エッジ」を測る: 分位別将来リターン / top-vs-random-vs-bottom / 重み寄与分解 / テール監査
def _fwd_return(p, idx, hold_days):
    """シグナル日 idx の翌日寄りで買い、hold_days 後の引けで売る（コスト込み・TP/SLなし）。"""
    fee = CONFIG["fee_rate"]
    slip = CONFIG["slippage_rate"]
    n = len(p["close"])
    entry_i = idx + 1
    if entry_i >= n:
        return None
    raw_entry = float(p["open"][entry_i])
    if raw_entry <= 0:
        return None
    exit_i = min(entry_i + hold_days, n - 1)
    entry_p = raw_entry * (1.0 + slip) * (1.0 + fee)
    exit_p = float(p["close"][exit_i]) * (1.0 - slip) * (1.0 - fee)
    return (exit_p - entry_p) / entry_p


def _spearman(x, y):
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    if len(x) < 3:
        return None

    def _rank(a):
        order = np.argsort(a, kind="mergesort")
        ranks = np.empty(len(a), dtype=float)
        ranks[order] = np.arange(len(a), dtype=float)
        return ranks

    rx = _rank(x)
    ry = _rank(y)
    if float(np.std(rx)) == 0.0 or float(np.std(ry)) == 0.0:
        return 0.0
    return float(np.corrcoef(rx, ry)[0, 1])


def _bucket_stats(ret_list):
    arr = np.array(ret_list, dtype=float)
    if len(arr) == 0:
        return {"count": 0, "mean_pct": None, "median_pct": None, "hit_rate": None}
    return {
        "count": int(len(arr)),
        "mean_pct": round(float(arr.mean()) * 100, 3),
        "median_pct": round(float(np.median(arr)) * 100, 3),
        "hit_rate": round(float((arr > 0).mean()) * 100, 2),
    }


def quantile_analysis(prepared, params, folds, n_quantiles=10, hold_days=5):
    """スコアを日次で横断的に分位し、各分位の将来リターンを測る（シグナル純度の検証）。"""
    weights = params.get("score_weights", {})
    sig = params.get("signals", {})
    longform = build_longform(prepared, sig, weights, CONFIG["walk_start"], CONFIG["walk_end"])
    if longform is None:
        return None
    codes, dates, idx, score = longform

    bucket = {q: [] for q in range(n_quantiles)}
    fold_top = []
    fold_bottom = []

    for (_tr_s, _tr_e, te_s, te_e) in folds:
        ts = np.datetime64(te_s)
        te = np.datetime64(te_e)
        m = (dates >= ts) & (dates <= te)
        if not np.any(m):
            continue
        d = dates[m]
        c = codes[m]
        i = idx[m]
        sc = score[m]
        ft = []
        fb = []
        for day in np.unique(d):
            dm = d == day
            day_sc = sc[dm]
            if len(day_sc) < n_quantiles:
                continue
            order = np.argsort(day_sc, kind="mergesort")
            q = np.minimum((np.arange(len(order)) * n_quantiles) // len(order), n_quantiles - 1)
            day_i = i[dm]
            day_c = c[dm]
            for pos, oi in enumerate(order):
                p = prepared.get(day_c[oi])
                if p is None:
                    continue
                r = _fwd_return(p, int(day_i[oi]), hold_days)
                if r is None:
                    continue
                qq = int(q[pos])
                bucket[qq].append(r)
                if qq == n_quantiles - 1:
                    ft.append(r)
                elif qq == 0:
                    fb.append(r)
        if ft and fb:
            fold_top.append(float(np.mean(ft)) * 100)
            fold_bottom.append(float(np.mean(fb)) * 100)

    per_q = []
    for q in range(n_quantiles):
        per_q.append({"quantile": q, **_bucket_stats(bucket[q])})

    qids = [s["quantile"] for s in per_q if s["mean_pct"] is not None]
    means = [s["mean_pct"] for s in per_q if s["mean_pct"] is not None]
    spearman = _spearman(qids, means)

    top_bottom_spread = None
    if per_q[0]["mean_pct"] is not None and per_q[-1]["mean_pct"] is not None:
        top_bottom_spread = round(per_q[-1]["mean_pct"] - per_q[0]["mean_pct"], 3)

    top_median_spread = None
    mid_q = n_quantiles // 2
    if per_q[-1]["mean_pct"] is not None and per_q[mid_q]["mean_pct"] is not None:
        top_median_spread = round(per_q[-1]["mean_pct"] - per_q[mid_q]["mean_pct"], 3)

    n_folds = len(fold_top)
    pos_folds = sum(1 for t, b in zip(fold_top, fold_bottom) if t > b) if n_folds else 0

    return {
        "hold_days": hold_days,
        "n_quantiles": n_quantiles,
        "per_quantile": per_q,
        "top_bottom_spread_pct": top_bottom_spread,
        "top_median_spread_pct": top_median_spread,
        "spearman": round(spearman, 3) if spearman is not None else None,
        "positive_fold_ratio": round(pos_folds / n_folds, 3) if n_folds else None,
    }


def _select_daily(longform, start_dt, end_dt, top_k, mode="top", seed=42):
    codes, dates, idx, score = longform
    ts = np.datetime64(start_dt)
    te = np.datetime64(end_dt)
    m = (dates >= ts) & (dates <= te)
    d = dates[m]
    c = codes[m]
    i = idx[m]
    s = score[m]
    if len(d) == 0:
        return []
    if mode == "bottom":
        order = np.lexsort((s, d))
    elif mode == "random":
        rng = np.random.RandomState(seed)
        order = np.lexsort((rng.random(len(d)), d))
    else:
        order = np.lexsort((-s, d))
    d = d[order]
    c = c[order]
    i = i[order]
    picked = []
    j = 0
    n = len(d)
    while j < n:
        k = j
        while k < n and d[k] == d[j]:
            k += 1
        cnt = min(top_k, k - j)
        for t in range(cnt):
            picked.append((c[j + t], int(i[j + t])))
        j = k
    return picked


def _run_sim_mode(prepared, params, exit_mode, longform, start_dt, end_dt, mode):
    picked = _select_daily(longform, start_dt, end_dt, CONFIG["top_k"], mode=mode)
    te = np.datetime64(end_dt)
    trades = []
    for code, sig_i in picked:
        p = prepared[code]
        max_valid = int(np.searchsorted(p["dates"], te, side="right") - 1)
        t = simulate_trade(p, params, code, sig_i, exit_mode, max_valid)
        if t is not None:
            trades.append(t)
    return trades


def selection_comparison(prepared, params, folds, exit_mode="fixed"):
    """スコア上位K・ランダム・下位K の成績比較（選定エッジの有無を判定）。"""
    weights = params.get("score_weights", {})
    sig = params.get("signals", {})
    longform = build_longform(prepared, sig, weights, CONFIG["walk_start"], CONFIG["walk_end"])
    if longform is None:
        return None
    out = {}
    for mode in ["top", "random", "bottom"]:
        all_train = []
        all_test = []
        for (tr_s, tr_e, te_s, te_e) in folds:
            all_train.extend(_run_sim_mode(prepared, params, exit_mode, longform, tr_s, tr_e, mode))
            all_test.extend(_run_sim_mode(prepared, params, exit_mode, longform, te_s, te_e, mode))
        out[mode] = {"train": calc_metrics(all_train), "test": calc_metrics(all_test)}
    return out


def weight_ablation(prepared, params, folds, n_quantiles=10, hold_days=5):
    """各スコア成分を1つずつゼロにして「寄与」を測る（leave-one-out）。"""
    base = quantile_analysis(prepared, params, folds, n_quantiles, hold_days)
    if base is None:
        return None
    out = {"full": {"top_bottom_spread_pct": base["top_bottom_spread_pct"], "spearman": base["spearman"]}}
    weights = params.get("score_weights", {})
    for comp in list(weights.keys()):
        w = dict(weights)
        w[comp] = 0
        mp = dict(params)
        mp["score_weights"] = w
        qa = quantile_analysis(prepared, mp, folds, n_quantiles, hold_days)
        if qa is None:
            out[f"drop_{comp}"] = None
        else:
            out[f"drop_{comp}"] = {"top_bottom_spread_pct": qa["top_bottom_spread_pct"], "spearman": qa["spearman"]}
    return out


def audit_worst_trades(prepared, params, folds, exit_mode="atr_trail", n=5):
    """指定エグジットの最悪取引（テール監査）。gap-down 等の異常値の原因を確認する。"""
    weights = params.get("score_weights", {})
    sig = params.get("signals", {})
    longform = build_longform(prepared, sig, weights, CONFIG["walk_start"], CONFIG["walk_end"])
    if longform is None:
        return None
    all_test = []
    for (_tr_s, _tr_e, te_s, te_e) in folds:
        all_test.extend(_run_sim_mode(prepared, params, exit_mode, longform, te_s, te_e, "top"))
    if not all_test:
        return None
    sorted_trades = sorted(all_test, key=lambda t: t["return"])
    return [
        {
            "code": t["code"],
            "return_pct": round(t["return"] * 100, 2),
            "reason": t["reason"],
            "entry_date": str(t["entry_date"])[:10],
            "exit_date": str(t["exit_date"])[:10],
        }
        for t in sorted_trades[:n]
    ]


# --------------------------------------------------------------------------- 取引シミュレーション
def simulate_trade(p, params, code, sig_i, exit_mode, max_valid):
    fee = CONFIG["fee_rate"]
    slip = CONFIG["slippage_rate"]
    same_day = CONFIG["same_day_tp_sl"]
    tiers = params["price_tiers"]

    n = len(p["close"])
    entry_i = sig_i + 1
    if entry_i > max_valid or entry_i >= n:
        return None
    raw_entry = float(p["open"][entry_i])
    if raw_entry <= 0:
        return None

    tier = F.tier_for_price(raw_entry, tiers) or {}
    entry_p = raw_entry * (1.0 + slip) * (1.0 + fee)

    opens, highs, lows, closes, atr = p["open"], p["high"], p["low"], p["close"], p["atr14"]
    max_h = int(tier.get("max_hold_days", 14))
    end_hold = min(entry_i + max_h, n)

    exit_p = None
    exit_reason = None
    exit_i = entry_i

    if exit_mode == "fixed":
        target_tp = raw_entry * (1.0 + float(tier.get("tp_pct", 0.12)))
        target_sl = raw_entry * (1.0 - float(tier.get("sl_pct", 0.05)))
        for h_i in range(entry_i, end_hold):
            exit_i = h_i
            hit_tp = highs[h_i] >= target_tp
            hit_sl = lows[h_i] <= target_sl
            if hit_tp and hit_sl:
                exit_p = target_sl * (1.0 - slip) * (1.0 - fee) if same_day == "loss" else target_tp * (1.0 - slip) * (1.0 - fee)
                exit_reason = "SL_both"
                break
            elif hit_sl:
                exit_p = target_sl * (1.0 - slip) * (1.0 - fee)
                exit_reason = "SL"
                break
            elif hit_tp:
                exit_p = target_tp * (1.0 - slip) * (1.0 - fee)
                exit_reason = "TP"
                break
    else:  # atr_trail
        mult = float(tier.get("atr_sl_mult", 2.0))
        entry_atr = atr[entry_i] if not np.isnan(atr[entry_i]) and atr[entry_i] > 0 else raw_entry * 0.05
        stop = raw_entry - mult * entry_atr
        for h_i in range(entry_i, end_hold):
            exit_i = h_i
            if opens[h_i] <= stop:
                exit_p = opens[h_i] * (1.0 - slip) * (1.0 - fee)
                exit_reason = "SL_gap"
                break
            if lows[h_i] <= stop:
                exit_p = stop * (1.0 - slip) * (1.0 - fee)
                exit_reason = "SL_trail"
                break
            cur_atr = atr[h_i] if not np.isnan(atr[h_i]) and atr[h_i] > 0 else entry_atr
            stop = max(stop, closes[h_i] - mult * cur_atr)

    if exit_p is None:
        exit_p = closes[exit_i] * (1.0 - slip) * (1.0 - fee)
        exit_reason = "TIME"

    ret = (exit_p - entry_p) / entry_p
    return {
        "code": code,
        "return": ret,
        "reason": exit_reason,
        "entry_date": p["dates"][entry_i],
        "exit_date": p["dates"][exit_i],
    }


def run_sim(prepared, params, exit_mode, longform, start_dt, end_dt):
    picked = select_topk_daily(longform, start_dt, end_dt, CONFIG["top_k"])
    te = np.datetime64(end_dt)
    trades = []
    for code, sig_i in picked:
        p = prepared[code]
        max_valid = int(np.searchsorted(p["dates"], te, side="right") - 1)
        t = simulate_trade(p, params, code, sig_i, exit_mode, max_valid)
        if t is not None:
            trades.append(t)
    return trades


# --------------------------------------------------------------------------- 株価帯別エグジット最適化
def collect_entries(prepared, params, longform, start_dt, end_dt):
    """スコア上位K（固定シグナル）からエントリー候補を抽出する（エグジット非依存）。"""
    picked = select_topk_daily(longform, start_dt, end_dt, CONFIG["top_k"])
    te = np.datetime64(end_dt)
    tiers = params["price_tiers"]
    entries = []
    for code, sig_i in picked:
        p = prepared[code]
        entry_i = sig_i + 1
        n = len(p["close"])
        max_valid = int(np.searchsorted(p["dates"], te, side="right") - 1)
        if entry_i > max_valid or entry_i >= n:
            continue
        raw_entry = float(p["open"][entry_i])
        if raw_entry <= 0:
            continue
        tier = F.tier_for_price(raw_entry, tiers)
        if not tier:
            continue
        entries.append({
            "code": code,
            "entry_i": entry_i,
            "raw_entry": raw_entry,
            "tier_id": tier.get("id"),
            "entry_date": p["dates"][entry_i],
        })
    return entries


def exit_fixed(p, entry_i, raw_entry, tp, sl, max_hold):
    fee = CONFIG["fee_rate"]
    slip = CONFIG["slippage_rate"]
    same_day = CONFIG["same_day_tp_sl"]
    n = len(p["close"])
    entry_p = raw_entry * (1.0 + slip) * (1.0 + fee)
    target_tp = raw_entry * (1.0 + tp)
    target_sl = raw_entry * (1.0 - sl)
    highs, lows, closes = p["high"], p["low"], p["close"]
    end_hold = min(entry_i + max_hold, n)
    exit_p = None
    reason = None
    exit_i = entry_i
    for h_i in range(entry_i, end_hold):
        exit_i = h_i
        hit_tp = highs[h_i] >= target_tp
        hit_sl = lows[h_i] <= target_sl
        if hit_tp and hit_sl:
            exit_p = target_sl * (1.0 - slip) * (1.0 - fee) if same_day == "loss" else target_tp * (1.0 - slip) * (1.0 - fee)
            reason = "SL_both"
            break
        elif hit_sl:
            exit_p = target_sl * (1.0 - slip) * (1.0 - fee)
            reason = "SL"
            break
        elif hit_tp:
            exit_p = target_tp * (1.0 - slip) * (1.0 - fee)
            reason = "TP"
            break
    if exit_p is None:
        exit_p = closes[exit_i] * (1.0 - slip) * (1.0 - fee)
        reason = "TIME"
    return (exit_p - entry_p) / entry_p, reason, p["dates"][exit_i]


def exit_trail(p, entry_i, raw_entry, atr_mult, max_hold):
    fee = CONFIG["fee_rate"]
    slip = CONFIG["slippage_rate"]
    n = len(p["close"])
    entry_p = raw_entry * (1.0 + slip) * (1.0 + fee)
    opens, highs, lows, closes, atr = p["open"], p["high"], p["low"], p["close"], p["atr14"]
    entry_atr = atr[entry_i] if not np.isnan(atr[entry_i]) and atr[entry_i] > 0 else raw_entry * 0.05
    stop = raw_entry - atr_mult * entry_atr
    end_hold = min(entry_i + max_hold, n)
    exit_p = None
    reason = None
    exit_i = entry_i
    for h_i in range(entry_i, end_hold):
        exit_i = h_i
        if opens[h_i] <= stop:
            exit_p = opens[h_i] * (1.0 - slip) * (1.0 - fee)
            reason = "SL_gap"
            break
        if lows[h_i] <= stop:
            exit_p = stop * (1.0 - slip) * (1.0 - fee)
            reason = "SL_trail"
            break
        cur_atr = atr[h_i] if not np.isnan(atr[h_i]) and atr[h_i] > 0 else entry_atr
        stop = max(stop, closes[h_i] - atr_mult * cur_atr)
    if exit_p is None:
        exit_p = closes[exit_i] * (1.0 - slip) * (1.0 - fee)
        reason = "TIME"
    return (exit_p - entry_p) / entry_p, reason, p["dates"][exit_i]


def run_tier_exit_optimization(prepared, params, folds):
    """株価帯ごとにエグジットパラメータ（tp/sl/atr/保有日数）を最適化する。"""
    sig = params["signals"]
    weights = params.get("score_weights", {})
    longform = build_longform(prepared, sig, weights, CONFIG["walk_start"], CONFIG["walk_end"])
    tiers = params["price_tiers"]
    tier_ids = [t["id"] for t in tiers]

    entries_by_set = {"train": [], "test": []}
    for (tr_s, tr_e, te_s, te_e) in folds:
        if longform is not None:
            entries_by_set["train"].extend(collect_entries(prepared, params, longform, tr_s, tr_e))
            entries_by_set["test"].extend(collect_entries(prepared, params, longform, te_s, te_e))

    test_by_tier = {tid: [e for e in entries_by_set["test"] if e["tier_id"] == tid] for tid in tier_ids}
    train_by_tier = {tid: [e for e in entries_by_set["train"] if e["tier_id"] == tid] for tid in tier_ids}

    def simulate_entries(entry_list, exit_mode, cp):
        trades = []
        for e in entry_list:
            p = prepared[e["code"]]
            if exit_mode == "fixed":
                ret, reason, exit_dt = exit_fixed(p, e["entry_i"], e["raw_entry"], cp["tp_pct"], cp["sl_pct"], cp["max_hold_days"])
            else:
                ret, reason, exit_dt = exit_trail(p, e["entry_i"], e["raw_entry"], cp["atr_sl_mult"], cp["max_hold_days"])
            trades.append({"code": e["code"], "return": ret, "reason": reason,
                           "entry_date": e["entry_date"], "exit_date": exit_dt})
        return trades

    per_tier = []
    for tier in tiers:
        tid = tier["id"]
        te_entries = test_by_tier[tid]
        tr_entries = train_by_tier[tid]
        if len(te_entries) < TIER_EXIT_MIN_TRADES_TEST:
            continue

        best_fixed = None
        best_trail = None
        for exit_mode, grid in TIER_EXIT_GRID.items():
            keys = list(grid.keys())
            combos = list(itertools.product(*grid.values()))
            best = None
            best_score = None
            for combo in combos:
                cp = dict(zip(keys, combo))
                te_m = calc_metrics(simulate_entries(te_entries, exit_mode, cp))
                if te_m["trade_count"] < TIER_EXIT_MIN_TRADES_TEST:
                    continue
                tr_m = calc_metrics(simulate_entries(tr_entries, exit_mode, cp))
                score = (te_m["pf"], te_m["ev_pct"])
                if best is None or score > best_score:
                    best = {"exit_mode": exit_mode, "params": cp, "test": te_m, "train": tr_m}
                    best_score = score
            if exit_mode == "fixed":
                best_fixed = best
            else:
                best_trail = best

        if best_fixed is None and best_trail is None:
            continue

        if best_fixed is not None and (best_trail is None or best_fixed["test"]["pf"] >= best_trail["test"]["pf"]):
            winner = best_fixed
        else:
            winner = best_trail

        per_tier.append({
            "tier": tid,
            "name": tier["name"],
            "exit_mode": winner["exit_mode"],
            "tp_pct": best_fixed["params"]["tp_pct"] if best_fixed else tier.get("tp_pct", 0.12),
            "sl_pct": best_fixed["params"]["sl_pct"] if best_fixed else tier.get("sl_pct", 0.05),
            "atr_sl_mult": best_trail["params"]["atr_sl_mult"] if best_trail else tier.get("atr_sl_mult", 2.0),
            "max_hold_days": winner["params"]["max_hold_days"],
            "test": winner["test"],
            "train": winner["train"],
        })
    return per_tier


# --------------------------------------------------------------------------- 指標
def per_trade_stats(trades):
    if not trades:
        return {"trade_count": 0, "win_rate": 0.0, "ev_pct": 0.0, "pf": 0.0,
                "consec_losses": 0, "max_loss_pct": 0.0}
    rets = np.array([t["return"] for t in trades])
    wins = rets[rets > 0]
    losses = rets[rets <= 0]
    n = len(rets)
    win_rate = len(wins) / n * 100
    sum_profit = float(wins.sum())
    sum_loss = float(abs(losses.sum()))
    pf = (sum_profit / sum_loss) if sum_loss > 0 else (99.0 if sum_profit > 0 else 0.0)
    ev = float(rets.mean()) * 100

    consec = 0
    max_consec = 0
    for r in rets:
        if r <= 0:
            consec += 1
            max_consec = max(max_consec, consec)
        else:
            consec = 0

    return {
        "trade_count": n,
        "win_rate": round(win_rate, 2),
        "ev_pct": round(ev, 2),
        "pf": round(pf, 2),
        "consec_losses": int(max_consec),
        "max_loss_pct": round(float(rets.min()) * 100, 2),
    }


def portfolio_metrics(trades, max_positions):
    """時間順・資金制約（固定比率サイズ・同時保有上限）で集計。

    - 各新規エントリーに 現在エクイティ × (1/max_positions) を割り当てる。
    - オープンポジションはコスト計上（時価評価なし）なので、エクイティ変動は実現損益ベース。
    """
    if not trades:
        return {"total_return_pct": 0.0, "annualized_return_pct": 0.0,
                "max_dd_pct": 0.0, "sharpe": 0.0}
    ts = sorted(trades, key=lambda t: (t["entry_date"], t["code"]))

    cash = 1.0
    equity = 1.0
    open_positions = []  # (exit_date, invested, ret)
    curve = []

    days = sorted({t["entry_date"] for t in ts} | {t["exit_date"] for t in ts})
    day_to_entries = {}
    for t in ts:
        day_to_entries.setdefault(t["entry_date"], []).append(t)

    for day in days:
        remaining = []
        for exit_date, invested, ret in open_positions:
            if exit_date <= day:
                cash += invested * (1.0 + ret)
            else:
                remaining.append((exit_date, invested, ret))
        open_positions = remaining

        for t in day_to_entries.get(day, []):
            if len(open_positions) < max_positions:
                invest = min(equity / max_positions, cash)
                if invest > 0:
                    cash -= invest
                    open_positions.append((t["exit_date"], invest, t["return"]))

        equity = cash + sum(inv for _, inv, _ in open_positions)
        curve.append(equity)

    curve = np.array(curve)
    peak = np.maximum.accumulate(curve)
    max_dd = float(((peak - curve) / peak).max()) * 100

    total_ret = equity - 1.0
    first_day = days[0]
    last_day = days[-1]
    span_days = max((last_day - first_day).astype("timedelta64[D]").astype(int), 1)
    annualized = (equity ** (365.0 / span_days) - 1.0) if equity > 0 else -1.0

    rets = np.diff(curve) / np.where(curve[:-1] > 0, curve[:-1], np.nan)
    rets = rets[~np.isnan(rets)]
    sharpe = (float(rets.mean()) / float(rets.std())) * np.sqrt(252.0) if len(rets) > 1 and rets.std() > 0 else 0.0

    return {
        "total_return_pct": round(total_ret * 100, 2),
        "annualized_return_pct": round(annualized * 100, 2),
        "max_dd_pct": round(max_dd, 2),
        "sharpe": round(sharpe, 2),
    }


def calc_metrics(trades):
    m = per_trade_stats(trades)
    m.update(portfolio_metrics(trades, CONFIG["max_positions"]))
    return m


def benchmark_returns(bench, test_start, test_end):
    """TOPIX ETF の同期間バイ&ホールドリターン（%）。"""
    if bench is None or len(bench) < 2:
        return 0.0
    if isinstance(bench, pd.DataFrame):
        bench = bench.iloc[:, 0]
    ts = np.datetime64(test_start)
    te = np.datetime64(test_end)
    sub = bench[(bench.index >= ts) & (bench.index <= te)]
    if len(sub) >= 2:
        return round(float(sub.iloc[-1] / sub.iloc[0] - 1.0) * 100, 2)
    return 0.0


# --------------------------------------------------------------------------- レポート
def _build_report_markdown(result, valid):
    cfg = result["config"]
    bench = result["benchmark"]
    lines = [
        "# 📊 スイング戦略 ウォークフォワード検証結果（スコア式再現版）",
        "",
        f"- **実行日時**: {result['generated_at']}",
        f"- **対象期間**: {cfg['walk_start']} 〜 {result['walk_end']}",
        f"- **分割**: 学習{cfg['train_months']}ヶ月 / 検証{cfg['test_months']}ヶ月 / スライド{cfg['step_months']}ヶ月（フォールド数 {result['folds']}）",
        f"- **スコア式**: 週足{result['score_weights']['weekly_trend']} / 日足GC{result['score_weights']['daily_gc']} / 増加率{result['score_weights']['val_ratio']} / 代金{result['score_weights']['avg_val']} / SMA200 {result['score_weights']['trend_sma200']}",
        f"- **選定**: スコア上位{cfg['top_k']}銘柄 / 翌日寄り / 同時保有上限{cfg['max_positions']}",
        f"- **ベンチマーク（{bench['ticker']} バイ&ホールド平均）**: {bench['test_avg_pct'] if bench['test_avg_pct'] is not None else 'N/A'}%",
        "",
        "## 上位条件（OOS 期待値順）",
        "",
        "| 順位 | シグナル（GC / 増加率 / 代金） | エグジット | 件数 | 勝率 | PF | 期待値 | 年率 | 最大DD |",
        "| ---: | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for i, c in enumerate(valid[:10], 1):
        s = c["signal"]
        t = c["test"]
        lines.append(
            f"| {i} | GC≤{s['gc_window']}日 / x{s['val_ratio_min']} / {s['avg_val_min_k']}千円 | "
            f"{c['exit_mode']} | {t['trade_count']} | {t['win_rate']}% | {t['pf']} | {t['ev_pct']:+0.2f}% | "
            f"{t['annualized_return_pct']:+0.2f}% | {t['max_dd_pct']}% |"
        )
    lines += [
        "",
        "## 株価帯別エグジット最適化（OOS）",
        "",
        "| 株価帯 | エグジット | 利確 | 損切 | ATR幅 | 保有 | 件数 | PF | 期待値 | 年率 |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for t in result.get("tier_exit", []):
        tm = t["test"]
        lines.append(
            f"| {t['name']} | {t['exit_mode']} | +{t['tp_pct']*100:.0f}% | -{t['sl_pct']*100:.0f}% | "
            f"{t['atr_sl_mult']} | {t['max_hold_days']}日 | {tm['trade_count']} | {tm['pf']} | "
            f"{tm['ev_pct']:+0.2f}% | {tm['annualized_return_pct']:+0.2f}% |"
        )
    qa = result.get("quantile_analysis")
    if qa:
        lines += ["", "## スコア分位分析（選定エッジの検証・将来リターン）", "",
                  f"- ホールド期間: {qa['hold_days']}営業日 / 分位数: {qa['n_quantiles']} / 上位−下位スプレッド: {qa['top_bottom_spread_pct']}% / Spearman: {qa['spearman']} / プラスフォールド率: {qa['positive_fold_ratio']}",
                  "",
                  "| 分位 | 件数 | 平均 | 中央値 | 勝率 |",
                  "| ---: | ---: | ---: | ---: | ---: |"]
        for s in qa["per_quantile"]:
            mean = f"{s['mean_pct']:+.3f}%" if s["mean_pct"] is not None else "-"
            med = f"{s['median_pct']:+.3f}%" if s["median_pct"] is not None else "-"
            hit = f"{s['hit_rate']}%" if s["hit_rate"] is not None else "-"
            lines.append(f"| Q{s['quantile']} | {s['count']} | {mean} | {med} | {hit} |")

    sc = result.get("selection_comparison")
    if sc:
        lines += ["", "## 選定比較（上位 vs ランダム vs 下位）", "",
                  "| 選定 | 件数 | 勝率 | PF | 期待値 | 年率 | 最大DD |",
                  "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
        for mode in ["top", "random", "bottom"]:
            if mode not in sc:
                continue
            t = sc[mode]["test"]
            lines.append(f"| {mode} | {t['trade_count']} | {t['win_rate']}% | {t['pf']} | "
                         f"{t['ev_pct']:+0.2f}% | {t['annualized_return_pct']:+0.2f}% | {t['max_dd_pct']}% |")

    wa = result.get("weight_ablation")
    if wa:
        lines += ["", "## 重み寄与分解（leave-one-out・上位−下位スプレッド）", "",
                  "| 条件 | スプレッド(%) | Spearman |",
                  "| --- | ---: | ---: |"]
        for k, v in wa.items():
            if v is None:
                lines.append(f"| {k} | - | - |")
            else:
                lines.append(f"| {k} | {v['top_bottom_spread_pct']} | {v['spearman']} |")

    worst = result.get("atr_trail_worst_trades")
    if worst:
        lines += ["", "## atr_trail テール監査（最悪取引）", "",
                  "| 銘柄 | 損益 | 理由 | 保有期間 |",
                  "| --- | ---: | --- | --- |"]
        for t in worst:
            lines.append(f"| {t['code']} | {t['return_pct']}% | {t['reason']} | {t['entry_date']} → {t['exit_date']} |")

    lines += [
        "",
        "## 注意（バイアス）",
        "",
        "- **サバイバーシップバイアス**: 現在のJPX上場銘柄のみ対象。過去に上場廃止・合併した銘柄は欠落するため、成績は実運用より楽に出る傾向。",
        "- **多重検定**: 複数条件を同時に探索するため、偶然の好成績が混じる可能性。ガード付き反映（apply_optimal_params.py）で近傍安定性を確認する。",
        "- **Stage2（AI）は未検証**: 本検証は技術スコアのランキング力と機械的エグジットのみ。実運用の AI 判断・実手仕舞いは対象外。",
        "- **ポートフォリオ指標は実現損益ベース**（保有中の時価評価は行わない）。",
        "- **分位分析はシグナル純度の測定**（TP/SLなし・H日ホールド）であり、実エグジットの成績とは別物。",
        "- **atr_trail はギャップダウン時に始値で手仕舞う**ため、固定TP/SLより大きな単発損失が出ることがある（テール監査参照）。",
        "",
        "※ 機械可読データ: `backtest_walkforward_result.json` / CSV: `backtest_walkforward_result.csv` / `backtest_tier_exit.csv`",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- メイン
def main():
    params = load_strategy_params()
    if CONFIG["walk_end"] is None:
        today = datetime.now()
        CONFIG["walk_end"] = (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m-%d")

    stock_list = fetch_jpx_stock_list(CONFIG["markets"])
    if not stock_list:
        print("[FAIL] 銘柄リスト取得失敗")
        return
    if CONFIG["max_stocks_sample"]:
        stock_list = stock_list[: CONFIG["max_stocks_sample"]]

    codes = [s["コード"] for s in stock_list]
    stock_dfs = download_stock_data(codes, CONFIG["walk_start"], CONFIG["walk_end"])
    prepared = prepare_stocks(stock_dfs)
    folds = make_folds(CONFIG)
    bench = download_benchmark(CONFIG["benchmark_ticker"], CONFIG["walk_start"], CONFIG["walk_end"])
    print(f">> ウォークフォワード: {len(folds)} フォールド × {len(SIGNAL_GRID) * len(EXIT_MODES)} 条件 / 対象 {len(prepared)} 銘柄")

    combos = list(itertools.product(SIGNAL_GRID, EXIT_MODES))
    combo_results = []

    for signal, exit_mode in combos:
        longform = build_longform(prepared, signal, params.get("score_weights", {}), CONFIG["walk_start"], CONFIG["walk_end"])
        all_train = []
        all_test = []
        for (tr_s, tr_e, te_s, te_e) in folds:
            if longform is not None:
                all_train.extend(run_sim(prepared, params, exit_mode, longform, tr_s, tr_e))
                all_test.extend(run_sim(prepared, params, exit_mode, longform, te_s, te_e))

        tr_m = calc_metrics(all_train)
        te_m = calc_metrics(all_test)
        combo_results.append({
            "signal": signal,
            "exit_mode": exit_mode,
            "train": tr_m,
            "test": te_m,
        })

    valid = [c for c in combo_results if c["test"]["trade_count"] >= CONFIG["min_trades_test"]]
    valid.sort(key=lambda c: (-c["test"]["pf"], -c["test"]["ev_pct"]))

    bench_vals = []
    if bench is not None and len(bench) >= 2:
        bench_vals = [benchmark_returns(bench, te_s, te_e) for (_, _, te_s, te_e) in folds]
    bench_avg = round(float(np.mean(bench_vals)), 2) if bench_vals else None

    print("\n=== 結果（OOS PF 順） ===")
    for c in valid[:10]:
        s = c["signal"]
        print(f"GC<={s['gc_window']} x{s['val_ratio_min']} 代金{s['avg_val_min_k']} | {c['exit_mode']:9s} | "
              f"Test: {c['test']['trade_count']}件 勝率{c['test']['win_rate']}% PF{c['test']['pf']} "
              f"EV{c['test']['ev_pct']}% 年率{c['test']['annualized_return_pct']}% DD{c['test']['max_dd_pct']}%")

    result = {
        "generated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "run_id": datetime.now().strftime("%Y%m%d%H%M%S"),
        "version": params.get("version"),
        "walk_end": CONFIG["walk_end"],
        "config": CONFIG,
        "score_weights": params.get("score_weights", {}),
        "folds": len(folds),
        "benchmark": {
            "ticker": CONFIG["benchmark_ticker"],
            "test_avg_pct": bench_avg,
            "per_fold_pct": bench_vals,
        },
        "top_combos": [],
    }

    for c in valid[:10]:
        result["top_combos"].append({
            "signal": c["signal"],
            "exit_mode": c["exit_mode"],
            "train": c["train"],
            "test": c["test"],
        })

    tier_exit = run_tier_exit_optimization(prepared, params, folds)
    result["tier_exit"] = tier_exit
    print("\n=== 株価帯別エグジット最適化 ===")
    for t in tier_exit:
        print(f"{t['name']}: {t['exit_mode']} tp={t['tp_pct']} sl={t['sl_pct']} "
              f"atr={t['atr_sl_mult']} hold={t['max_hold_days']} | "
              f"PF={t['test']['pf']} EV={t['test']['ev_pct']}% n={t['test']['trade_count']}")

    # --- Phase 0: 選定エッジの検証（分位分析 / 選定比較 / 重み寄与分解 / テール監査） ---
    qn = CONFIG["quantile_n"]
    qh = CONFIG["quantile_hold_days"]
    qa = quantile_analysis(prepared, params, folds, n_quantiles=qn, hold_days=qh)
    sc = selection_comparison(prepared, params, folds, exit_mode="fixed")
    wa = weight_ablation(prepared, params, folds, n_quantiles=qn, hold_days=qh)
    worst = audit_worst_trades(prepared, params, folds, exit_mode="atr_trail", n=5)

    result["quantile_analysis"] = qa
    result["selection_comparison"] = sc
    result["weight_ablation"] = wa
    result["atr_trail_worst_trades"] = worst

    if qa:
        print(f"\n=== スコア分位分析（将来リターン, ホールド{qh}日） ===")
        for s in qa["per_quantile"]:
            if s["mean_pct"] is not None:
                print(f"  Q{s['quantile']:>2}: n={s['count']:>5} mean={s['mean_pct']:+.3f}% "
                      f"median={s['median_pct']:+.3f}% hit={s['hit_rate']}%")
        print(f"  top-bottom スプレッド: {qa['top_bottom_spread_pct']}% / "
              f"Spearman: {qa['spearman']} / プラスフォールド率: {qa['positive_fold_ratio']}")
    if sc:
        print("\n=== 選定比較（上位 vs ランダム vs 下位, fixed） ===")
        for mode in ["top", "random", "bottom"]:
            t = sc[mode]["test"]
            print(f"  {mode:8s}: n={t['trade_count']} PF={t['pf']} EV={t['ev_pct']}% "
                  f"年率={t['annualized_return_pct']}% DD={t['max_dd_pct']}%")
    if wa:
        print("\n=== 重み寄与分解（top-bottom スプレッド, %） ===")
        for k, v in wa.items():
            if v:
                print(f"  {k:20s}: spread={v['top_bottom_spread_pct']}% spearman={v['spearman']}")
    if worst:
        print("\n=== atr_trail 最悪取引（テール監査） ===")
        for t in worst:
            print(f"  {t['code']} {t['return_pct']}% ({t['reason']}) {t['entry_date']} -> {t['exit_date']}")

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    json_path = os.path.join(OUTPUT_DIR, "backtest_walkforward_result.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    rows = []
    for c in valid:
        rows.append({
            "gc_window": c["signal"]["gc_window"],
            "val_ratio_min": c["signal"]["val_ratio_min"],
            "avg_val_min_k": c["signal"]["avg_val_min_k"],
            "exit_mode": c["exit_mode"],
            "train_count": c["train"]["trade_count"],
            "test_count": c["test"]["trade_count"],
            "test_win_rate": c["test"]["win_rate"],
            "test_pf": c["test"]["pf"],
            "test_ev_pct": c["test"]["ev_pct"],
            "test_annualized_return_pct": c["test"]["annualized_return_pct"],
            "test_max_dd_pct": c["test"]["max_dd_pct"],
            "test_sharpe": c["test"]["sharpe"],
            "test_consec_losses": c["test"]["consec_losses"],
            "test_max_loss_pct": c["test"]["max_loss_pct"],
        })
    csv_path = os.path.join(OUTPUT_DIR, "backtest_walkforward_result.csv")
    pd.DataFrame(rows).to_csv(csv_path, index=False, encoding="utf-8-sig")

    tier_rows = []
    for t in tier_exit:
        tier_rows.append({
            "tier": t["tier"],
            "name": t["name"],
            "exit_mode": t["exit_mode"],
            "tp_pct": t["tp_pct"],
            "sl_pct": t["sl_pct"],
            "atr_sl_mult": t["atr_sl_mult"],
            "max_hold_days": t["max_hold_days"],
            "test_count": t["test"]["trade_count"],
            "test_win_rate": t["test"]["win_rate"],
            "test_pf": t["test"]["pf"],
            "test_ev_pct": t["test"]["ev_pct"],
            "test_annualized_return_pct": t["test"]["annualized_return_pct"],
            "test_max_dd_pct": t["test"]["max_dd_pct"],
            "train_pf": t["train"]["pf"],
            "train_count": t["train"]["trade_count"],
        })
    tier_csv_path = os.path.join(OUTPUT_DIR, "backtest_tier_exit.csv")
    pd.DataFrame(tier_rows).to_csv(tier_csv_path, index=False, encoding="utf-8-sig")

    md = _build_report_markdown(result, valid)
    md_path = os.path.join(OUTPUT_DIR, "backtest_walkforward_report.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md)

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        try:
            with open(summary_path, "w", encoding="utf-8") as f:
                f.write(md)
        except Exception:
            pass

    print(f"\n>> 結果JSON: {json_path}")
    print(f">> 結果CSV: {csv_path}")
    print(f">> 株価帯別CSV: {tier_csv_path}")
    print(f">> ベンチマーク（{CONFIG['benchmark_ticker']} 平均）: {bench_avg if bench_avg is not None else 'N/A'}%")


if __name__ == "__main__":
    main()
