"""
research_signals.py
スコア再設計のための候補シグナル研究（Phase 1）

目的:
- 現行スコアは「上位ほど上がる」関係を示せていない（Phase 0 の分位分析）。
- そこで候補となる指標を1つずつ、実際に将来リターンを分位別に測り、
  「上位分位ほどよく上がる」ものを探す。

方法:
- 各営業日、適格銘柄をその指標で横断的に分位（デシル）し、
  将来リターン（翌日寄り→H日後引け、TP/SLなし・コスト込み）を分位別に集計。
- 上位−下位スプレッド、Spearman相関、プラスだったフォールド率で評価。

出力:
- results/research_signals.json / .csv （指標ごとのスプレッド等のランキング）

注意:
- サバイバーシップバイアスあり（現在上場銘柄のみ）。
- ファンダメンタル（PER/PBR等）は先読みになるため対象外（価格・出来高のみ）。
- 過熱系（RSI14・5日線/25日線乖離・5日リターン・寄付ギャップ・連騰日数）は
  「値が高いほど将来リターンが低い」ならスプレッドがマイナスになる。
  これは高値掴み回避（押し目待ち）の根拠になる（マイナスほど過熱の逆効果が強い）。
"""

import json
import os
import sys
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))  # リポジトリ直下
import backtest_rolling_walkforward as B  # noqa: E402

OUTPUT_DIR = os.path.join(_HERE, "results")


# --------------------------------------------------------------------------- 候補シグナル
def _pct_change(a, n):
    prev = pd.Series(a).shift(n).values
    return np.where(np.asarray(prev, dtype=float) > 0, np.asarray(a, dtype=float) / prev - 1.0, np.nan)


def _ratio(num, den):
    num = np.asarray(num, dtype=float)
    den = np.asarray(den, dtype=float)
    return np.where(den > 0, num / den, np.nan)


def _roll(a, n, how):
    s = pd.Series(np.asarray(a, dtype=float))
    return getattr(s.rolling(n, min_periods=1), how)().values


def _gc_recent(p):
    gc = np.asarray(p["gc_days"], dtype=float)
    return np.where((gc >= 0) & (gc <= 3), 1.0, 0.0)


def _gap_pct(open_, close):
    """寄付ギャップ（当日寄値 / 前日終値 − 1）。窓開け急騰の過熱度。"""
    o = np.asarray(open_, dtype=float)
    prev = pd.Series(np.asarray(close, dtype=float)).shift(1).values
    prev = np.asarray(prev, dtype=float)
    return np.where(prev > 0, o / prev - 1.0, np.nan)


def _run_up_days(close):
    """連騰日数（直近終値ベースで何日連続して前日比プラスか）。"""
    c = np.asarray(close, dtype=float)
    out = np.zeros(len(c), dtype=float)
    run = 0
    for i in range(1, len(c)):
        run = run + 1 if c[i] > c[i - 1] else 0
        out[i] = run
    return out


def _sector_relative(prepared, sector_map, base_values):
    """各営業日・各業種の平均からの超過（業種相対強度）を {code: 配列} で返す。

    base_values は {code: 配列}（正の値が強いほど良い指標を想定）。
    """
    buckets = {}
    for code, p in prepared.items():
        sector = sector_map.get(code)
        base = base_values.get(code)
        if sector is None or base is None:
            continue
        dates = p["dates"]
        elig = p["eligible"]
        for i in range(len(dates)):
            if not elig[i]:
                continue
            v = base[i]
            if v is None or not np.isfinite(v):
                continue
            buckets.setdefault((dates[i], sector), []).append(float(v))
    means = {k: float(np.mean(v)) for k, v in buckets.items()}

    out = {}
    for code, p in prepared.items():
        dates = p["dates"]
        arr = np.full(len(dates), np.nan)
        sector = sector_map.get(code)
        base = base_values.get(code)
        if sector is not None and base is not None:
            for i in range(len(dates)):
                if not p["eligible"][i]:
                    continue
                v = base[i]
                if v is None or not np.isfinite(v):
                    continue
                m = means.get((dates[i], sector))
                if m is not None:
                    arr[i] = float(v) - m
        out[code] = arr
    return out


def build_candidates(params):
    sig = params.get("signals", {})
    weights = params.get("score_weights", {})
    return {
        "現行スコア(score)": lambda p: B.score_series(p, p["weekly_up"], sig, weights),
        "ret_20d(20日リターン)": lambda p: _pct_change(p["close"], 20),
        "ret_60d(60日リターン)": lambda p: _pct_change(p["close"], 60),
        "ret_120d(120日リターン)": lambda p: _pct_change(p["close"], 120),
        "ret_5d(5日リターン)": lambda p: _pct_change(p["close"], 5),
        "dist_sma5(5日線乖離)": lambda p: _ratio(p["close"], p["sma5"]) - 1.0,
        "dist_sma25(25日線乖離)": lambda p: _ratio(p["close"], p["sma25"]) - 1.0,
        "dist_sma200(200日線乖離)": lambda p: _ratio(p["close"], p["sma200"]) - 1.0,
        "rsi14(RSI14)": lambda p: np.asarray(p["rsi14"], dtype=float),
        "gap_pct(寄付ギャップ)": lambda p: _gap_pct(p["open"], p["close"]),
        "run_up_days(連騰日数)": lambda p: _run_up_days(p["close"]),
        "sma200_slope(200日線傾き)": lambda p: _pct_change(p["sma200"], 20),
        "pos_52w(52週高値位置)": lambda p: _ratio(p["close"], _roll(p["close"], 252, "max")),
        "atr_pct(ボラティリティ)": lambda p: _ratio(p["atr14"], p["close"]),
        "val_ratio_5d(出来高増加率)": lambda p: np.asarray(p["val_ratio_5d"], dtype=float),
        "log_avg_val(流動性)": lambda p: np.log1p(np.asarray(p["avg_val_5d"], dtype=float)),
        "gc_recent(GC直後)": _gc_recent,
        "weekly_trend(週足上昇)": lambda p: np.asarray(p["weekly_up"], dtype=float),
    }


def measure_signal(prepared, folds, sig_values, n_quantiles, hold_days):
    """1指標について、日次横断分位の将来リターンを測る。sig_values は {code: 配列}。"""
    bucket = {q: [] for q in range(n_quantiles)}
    fold_top, fold_bottom = [], []

    for (_tr_s, _tr_e, te_s, te_e) in folds:
        ts = np.datetime64(te_s)
        te = np.datetime64(te_e)
        day_map = {}  # day -> [(value, fwd_ret)]
        for code, p in prepared.items():
            dates = p["dates"]
            mask = p["eligible"] & (dates >= ts) & (dates <= te)
            ii = np.where(mask)[0]
            if len(ii) == 0:
                continue
            vals = sig_values.get(code)
            if vals is None:
                continue
            for i in ii:
                v = vals[i]
                if v is None or not np.isfinite(v):
                    continue
                r = B._fwd_return(p, int(i), hold_days)
                if r is None:
                    continue
                day_map.setdefault(dates[i], []).append((float(v), r))

        ft, fb = [], []
        for _day, items in day_map.items():
            if len(items) < n_quantiles:
                continue
            items.sort(key=lambda x: x[0])
            n = len(items)
            for pos, (v, r) in enumerate(items):
                q = min((pos * n_quantiles) // n, n_quantiles - 1)
                bucket[q].append(r)
                if q == n_quantiles - 1:
                    ft.append(r)
                elif q == 0:
                    fb.append(r)
        if ft and fb:
            fold_top.append(float(np.mean(ft)) * 100)
            fold_bottom.append(float(np.mean(fb)) * 100)

    def stats(lst):
        arr = np.array(lst, dtype=float)
        if len(arr) == 0:
            return None
        return {"count": int(len(arr)), "mean_pct": round(float(arr.mean()) * 100, 3),
                "hit_rate": round(float((arr > 0).mean()) * 100, 2)}

    per_q = [stats(bucket[q]) for q in range(n_quantiles)]
    qids = [q for q in range(n_quantiles) if per_q[q] is not None]
    means = [per_q[q]["mean_pct"] for q in qids]
    spearman = B._spearman(qids, means) if len(qids) >= 3 else None

    top_bottom = None
    if per_q[0] is not None and per_q[-1] is not None:
        top_bottom = round(per_q[-1]["mean_pct"] - per_q[0]["mean_pct"], 3)
    n_folds = len(fold_top)
    pos_ratio = round(sum(1 for a, b in zip(fold_top, fold_bottom) if a > b) / n_folds, 3) if n_folds else None

    return {
        "top_bottom_spread_pct": top_bottom,
        "spearman": round(spearman, 3) if spearman is not None else None,
        "positive_fold_ratio": pos_ratio,
        "per_quantile": [{"quantile": q, **(per_q[q] or {"count": 0, "mean_pct": None, "hit_rate": None})} for q in range(n_quantiles)],
    }


def main():
    params = B.load_strategy_params()
    if B.CONFIG["walk_end"] is None:
        today = datetime.now()
        B.CONFIG["walk_end"] = (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m-%d")

    stock_list = B.fetch_jpx_stock_list(B.CONFIG["markets"])
    if not stock_list:
        print("[FAIL] 銘柄リスト取得失敗")
        return
    if B.CONFIG["max_stocks_sample"]:
        stock_list = stock_list[: B.CONFIG["max_stocks_sample"]]

    codes = [s["コード"] for s in stock_list]
    stock_dfs = B.download_stock_data(codes, B.CONFIG["walk_start"], B.CONFIG["walk_end"])
    prepared = B.prepare_stocks(stock_dfs)
    folds = B.make_folds(B.CONFIG)
    nq = B.CONFIG["quantile_n"]
    hd = B.CONFIG["quantile_hold_days"]
    print(f">> 候補シグナル研究: {len(prepared)} 銘柄 / {len(folds)} フォールド / 分位{nq} / ホールド{hd}日")

    candidates = build_candidates(params)
    # 指標値を1度だけ計算して再利用
    sig_values = {name: {code: np.asarray(fn(p), dtype=float) for code, p in prepared.items()}
                  for name, fn in candidates.items()}

    # 業種相対強度（同業種平均からの20日リターン超過）を候補に追加。
    # 採用可否は本スクリプトの分位分析（上位−下位スプレッド）で判断する。
    sector_map = {s["コード"]: s.get("33業種区分") for s in stock_list}
    if any(sector_map.values()):
        rs_name = "sector_rs_20d(業種相対20日)"
        base_ret20 = {code: _pct_change(p["close"], 20) for code, p in prepared.items()}
        sig_values[rs_name] = _sector_relative(prepared, sector_map, base_ret20)
        candidates[rs_name] = None

    results = []
    for name, _fn in candidates.items():
        m = measure_signal(prepared, folds, sig_values[name], nq, hd)
        results.append({"signal": name, **m})
    results.sort(key=lambda r: (r["top_bottom_spread_pct"] if r["top_bottom_spread_pct"] is not None else -9e9), reverse=True)

    print("\n=== 候補シグナル ランキング（上位−下位スプレッド順） ===")
    for r in results:
        print(f"  {r['signal']:28s} spread={r['top_bottom_spread_pct']}%  spearman={r['spearman']}  "
              f"プラス期間率={r['positive_fold_ratio']}")

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out = {
        "generated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "walk_end": B.CONFIG["walk_end"],
        "n_quantiles": nq,
        "hold_days": hd,
        "candidates": results,
    }
    with open(os.path.join(OUTPUT_DIR, "research_signals.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    rows = [{"signal": r["signal"], "top_bottom_spread_pct": r["top_bottom_spread_pct"],
             "spearman": r["spearman"], "positive_fold_ratio": r["positive_fold_ratio"]} for r in results]
    pd.DataFrame(rows).to_csv(os.path.join(OUTPUT_DIR, "research_signals.csv"), index=False, encoding="utf-8-sig")
    print(f"\n>> 出力: {os.path.join(OUTPUT_DIR, 'research_signals.json')} / .csv")


if __name__ == "__main__":
    main()
