"""research_patterns.py
テクニカル「型（チャートパターン）」のイベントスタディ研究（新規・既存BTは無変更）

目的:
- ダブルボトム/ゴールデンクロス等の「型」が、実際に将来リターンと関係するかを測る。
- 単一スカラーの分位分析（research_signals.py）ではなく、発生イベントの前向きリターンを
  ベースライン（全適格日平均）・ベンチマーク（TOPIX ETF）と比較する。
- 保有期間 {5,10,20,60} 日を測り、短期（〜1週間）と中長期で型の効き方が違うかを確認する。

方法（先読み排除）:
- シグナルは当日終値確定値のみ。エントリーは翌営業日寄り（backtest と同一）。
- 将来リターンは `B._fwd_return`（手数料0.05%＋スリッページ0.1%・TP/SLなし）で測定。
- OOS はウォークフォワードの検証期間（make_folds の test 区間）のみを使用。

出力:
- results/research_patterns.json / .csv / research_patterns_report.md

注意（解釈）:
- サバイバーシップバイアスあり（現在上場銘柄のみ）。
- 多重検定: 型×保有期間×定義グリッドを同時に試すため、偶然の好成績に注意。定義感度で符号の
  安定を必ず確認する。
- 本スクリプトは研究専用。スコア・strategy_params.json への反映は行わない。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))  # リポジトリ直下（common/ を参照）
import backtest_rolling_walkforward as B  # noqa: E402
from common import patterns as P  # noqa: E402

OUTPUT_DIR = os.path.join(_HERE, "results")
DEFAULT_HOLDS = [5, 10, 20, 60]

# データ異常ガード（研究用）。yfinance の分割調整ミス等で 1 点だけ異常値が入ると
# 平均が破壊されるため、1取引の |リターン| 上限と価格フロアで外れ値を除外する。
# ※ 既存 B._fwd_return は変更せず、本スクリプト内でのみ適用する。
MAX_ABS_RET = 5.0   # 1取引 |リターン| 上限（+500% / -99% 超は除外）
MIN_PRICE = 1.0     # 円未満の価格（調整ミス・異常値）を除外
BENCH_MAX_ABS_RET = 0.5  # ベンチマークの |リターン| 上限（TOPIXで±50%は異常）


def _safe_fwd_return(p, idx, hold):
    """外れ値を除いた前向きリターン。異常値は None（集計から除外）。"""
    n = len(p["close"])
    entry_i = idx + 1
    if entry_i >= n:
        return None
    e_raw = float(p["open"][entry_i])
    exit_i = min(entry_i + hold, n - 1)
    x_raw = float(p["close"][exit_i])
    if not (np.isfinite(e_raw) and np.isfinite(x_raw)):
        return None
    if e_raw < MIN_PRICE or x_raw < MIN_PRICE:
        return None
    r = B._fwd_return(p, int(idx), hold)
    if r is None or not np.isfinite(r) or abs(r) > MAX_ABS_RET:
        return None
    return r

# 定義感度（頑健性）チェック用のバリエーション。符号が安定するかを見る。
SENSITIVITY = {
    "ダブルボトム": ("double_bottom", [
        dict(tol=0.03, min_gap=15, max_gap=60),
        dict(tol=0.05, min_gap=10, max_gap=60),
        dict(tol=0.06, min_gap=20, max_gap=90),
    ]),
    "ダブルトップ": ("double_top", [
        dict(tol=0.03, min_gap=15, max_gap=60),
        dict(tol=0.05, min_gap=10, max_gap=60),
        dict(tol=0.06, min_gap=20, max_gap=90),
    ]),
    "レンジブレイク": ("range_breakout", [
        dict(lookback=15, width_atr_max=2.0, vol_mult=1.5),
        dict(lookback=20, width_atr_max=3.0, vol_mult=1.5),
        dict(lookback=30, width_atr_max=3.5, vol_mult=1.2),
    ]),
    "三角持ち合い上放れ": ("triangle_breakout", [
        dict(lookback=30, sub=10, vol_mult=1.0),
        dict(lookback=40, sub=10, vol_mult=1.0),
        dict(lookback=50, sub=15, vol_mult=1.0),
    ]),
    "フラッグ/ペナント": ("flag_pennant", [
        dict(run_days=8, run_min=0.12, flag_days=8),
        dict(run_days=10, run_min=0.15, flag_days=10),
        dict(run_days=15, run_min=0.20, flag_days=12),
    ]),
    "カップウィズハンドル": ("cup_with_handle", [
        dict(depth_min=0.10, depth_max=0.40, handle_max=10),
        dict(depth_min=0.12, depth_max=0.45, handle_max=15),
        dict(depth_min=0.15, depth_max=0.50, handle_max=20),
    ]),
}


# --------------------------------------------------------------------------- ユーティリティ
def _fold_periods(folds):
    return [(np.datetime64(f[2], "D"), np.datetime64(f[3], "D")) for f in folds]


def _day_key(d):
    return np.datetime64(d, "D")


def _pct(x):
    return None if x is None else round(float(x) * 100, 3)


def _stats(rets):
    arr = np.asarray(rets, dtype=float)
    if len(arr) == 0:
        return {"count": 0, "mean_pct": None, "median_pct": None, "hit_rate": None}
    return {
        "count": int(len(arr)),
        "mean_pct": round(float(arr.mean()) * 100, 3),
        "median_pct": round(float(np.median(arr)) * 100, 3),
        "hit_rate": round(float((arr > 0).mean()) * 100, 2),
    }


def _overheat_level(dist25_pct, rsi):
    """entry_guard に近い簡易分類（低/中/強）。"""
    d = dist25_pct if dist25_pct is not None and np.isfinite(dist25_pct) else 0.0
    r = rsi if rsi is not None and np.isfinite(rsi) else 0.0
    if d >= 45.0 or (d >= 30.0 and r >= 80.0):
        return "extreme"
    if d >= 30.0 or r >= 80.0:
        return "strong"
    if d >= 18.0 or r >= 75.0:
        return "moderate"
    return "low"


def _bench_series(bench):
    s = bench.iloc[:, 0] if isinstance(bench, pd.DataFrame) else bench
    s = pd.Series(s).dropna()
    s.index = pd.to_datetime(s.index).tz_localize(None)
    return s


# --------------------------------------------------------------------------- データ準備
def prepare(cfg, max_stocks, walk_end):
    B.CONFIG["walk_end"] = walk_end
    stock_list = B.fetch_jpx_stock_list(B.CONFIG["markets"])
    if not stock_list:
        print("[FAIL] 銘柄リスト取得失敗")
        return None
    if max_stocks:
        stock_list = stock_list[:max_stocks]

    codes = [s["コード"] for s in stock_list]
    stock_dfs = B.download_stock_data(codes, B.CONFIG["walk_start"], B.CONFIG["walk_end"])
    prepared = B.prepare_stocks(stock_dfs)
    folds = B.make_folds(B.CONFIG)
    bench = B.download_benchmark(B.CONFIG["benchmark_ticker"], B.CONFIG["walk_start"], B.CONFIG["walk_end"])
    return {"prepared": prepared, "folds": folds, "bench": bench, "stock_list": stock_list}


def compute_patterns_store(prepared):
    """{code: {pattern_name: bool配列}} を作る（日付軸は prepared と一致）。"""
    store = {}
    for code, p in prepared.items():
        try:
            pats = P.compute_patterns(p)
        except Exception:
            continue
        n = len(p["dates"])
        clean = {name: arr for name, arr in pats.items() if len(arr) == n}
        if clean:
            store[code] = clean
    return store


# --------------------------------------------------------------------------- ベースライン
def compute_baselines(prepared, fold_periods, holds):
    """各フォールド・各保有期間の『全適格日』平均リターン（無条件ベースライン）。"""
    base = {h: [[] for _ in fold_periods] for h in holds}
    for fi, (ts, te) in enumerate(fold_periods):
        for p in prepared.values():
            dates = p["dates"]
            mask = p["eligible"] & (dates >= ts) & (dates <= te)
            for i in np.where(mask)[0]:
                for h in holds:
                    r = _safe_fwd_return(p, int(i), h)
                    if r is not None:
                        base[h][fi].append(r)
        print(f"    baseline fold {fi + 1}/{len(fold_periods)} 完了")
    return base


def bench_fold_means(bench, fold_periods, holds):
    """フォールドごとの TOPIX 平均前向きリターン（終値→終値・コストなしの近似）。"""
    s = _bench_series(bench) if bench is not None else pd.Series(dtype=float)
    days = np.asarray(s.index.values, dtype="datetime64[D]")
    close = np.asarray(s.values, dtype=float)
    pos = {days[i]: i for i in range(len(days))}
    out = {h: [None] * len(fold_periods) for h in holds}
    for fi, (ts, te) in enumerate(fold_periods):
        sel = [i for i in range(len(days)) if ts <= days[i] <= te]
        for h in holds:
            rets = []
            for i in sel:
                j = pos.get(days[i])
                if j is None or j + h >= len(close) or close[j] <= 0:
                    continue
                r = close[j + h] / close[j] - 1.0
                if not np.isfinite(r) or abs(r) > BENCH_MAX_ABS_RET:
                    continue
                rets.append(r)
            out[h][fi] = float(np.mean(rets)) if rets else None
    return out


# --------------------------------------------------------------------------- イベント評価
def evaluate_flags(flags_store, prepared, fold_periods, holds):
    """型の発生イベントを保有期間ごとに集計する。"""
    result = {}
    for h in holds:
        fold_rets = [[] for _ in fold_periods]
        ev_regime = {"on": [], "off": []}
        ev_over = {"low": [], "moderate": [], "high": []}
        for code, pats in flags_store.items():
            p = prepared.get(code)
            if p is None:
                continue
            flags = pats.get("__flags__") if "__flags__" in pats else None
            if flags is None:
                continue
            dates = p["dates"]
            elig = p["eligible"]
            close = np.asarray(p["close"], dtype=float)
            sma25 = np.asarray(p.get("sma25"), dtype=float) if p.get("sma25") is not None else None
            rsi = np.asarray(p.get("rsi14"), dtype=float) if p.get("rsi14") is not None else None
            for fi, (ts, te) in enumerate(fold_periods):
                mask = flags & elig & (dates >= ts) & (dates <= te)
                for i in np.where(mask)[0]:
                    r = _safe_fwd_return(p, int(i), h)
                    if r is None:
                        continue
                    fold_rets[fi].append(r)
                    day = _day_key(dates[i])
                    on = REGIME.get(day, True)
                    ev_regime["on" if on else "off"].append(r)
                    d25 = None
                    if sma25 is not None and np.isfinite(sma25[i]) and sma25[i] > 0:
                        d25 = (close[i] / sma25[i] - 1.0) * 100.0
                    rv = None
                    if rsi is not None and np.isfinite(rsi[i]):
                        rv = float(rsi[i])
                    lv = _overheat_level(d25, rv)
                    key = "high" if lv in ("strong", "extreme") else lv
                    ev_over[key].append(r)

        all_rets = [r for fr in fold_rets for r in fr]
        result[h] = {
            "overall": _stats(all_rets),
            "by_regime": {k: _stats(v) for k, v in ev_regime.items()},
            "by_overheat": {k: _stats(v) for k, v in ev_over.items()},
            "_fold_rets": fold_rets,
        }
    return result


def attach_metrics(ev, baseline, bench_means, fold_periods, holds):
    """イベント結果にベースライン超過・ベンチマーク超過・プラスフォールド率を付す。"""
    out = {}
    for h in holds:
        fold_rets = ev[h].pop("_fold_rets")
        base_folds = baseline[h]
        base_all = [r for fr in base_folds for r in fr]
        base_mean = float(np.mean(base_all)) if base_all else None
        ev_mean = ev[h]["overall"]["mean_pct"]
        ev_mean_frac = (ev_mean / 100.0) if ev_mean is not None else None
        excess = None if (ev_mean_frac is None or base_mean is None) else (ev_mean_frac - base_mean)
        bmeans = [m for m in bench_means[h] if m is not None]
        bench_mean = float(np.mean(bmeans)) if bmeans else None
        bench_excess = None if (ev_mean_frac is None or bench_mean is None) else (ev_mean_frac - bench_mean)

        n_same = 0
        n_folds = 0
        for fi in range(len(fold_periods)):
            er = fold_rets[fi]
            br = base_folds[fi]
            if not er or not br:
                continue
            n_folds += 1
            if float(np.mean(er)) > float(np.mean(br)):
                n_same += 1

        ev[h].update({
            "baseline_mean_pct": _pct(base_mean),
            "excess_pct": _pct(excess),
            "bench_mean_pct": _pct(bench_mean),
            "bench_excess_pct": _pct(bench_excess),
            "positive_fold_ratio": round(n_same / n_folds, 3) if n_folds else None,
            "n_folds_with_event": n_folds,
        })
        out[h] = ev[h]
    return out


# --------------------------------------------------------------------------- メイン
REGIME = {}


def run(args):
    cfg = B.CONFIG
    walk_end = args.walk_end
    if walk_end is None:
        today = datetime.now()
        walk_end = (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m-%d")
    holds = [int(x) for x in args.hold_days.split(",")] if args.hold_days else DEFAULT_HOLDS

    data = prepare(cfg, args.max_stocks, walk_end)
    if data is None:
        return
    prepared = data["prepared"]
    fold_periods = _fold_periods(data["folds"])
    print(f">> 型の研究: {len(prepared)} 銘柄 / {len(fold_periods)} フォールド / 保有 {holds}日 / walk_end {walk_end}")

    global REGIME
    REGIME = B.build_regime(data["bench"], 200) or {}

    print(">> 型を検出中...")
    pats = compute_patterns_store(prepared)
    names = sorted({n for c in pats.values() for n in c})
    if args.pattern:
        names = [n for n in names if any(a in n for a in args.pattern)]
    print(f">> 対象の型: {len(names)} 種")

    print(">> ベースライン集計中...")
    baseline = compute_baselines(prepared, fold_periods, holds)
    bench_means = bench_fold_means(data["bench"], fold_periods, holds)

    results = []
    for name in names:
        flags_store = {code: {"__flags__": c[name]} for code, c in pats.items() if name in c}
        n_events = int(sum(int(c[name].sum()) for c in pats.values() if name in c))
        print(f"   - {name} ... イベント数 {n_events}")
        ev = evaluate_flags(flags_store, prepared, fold_periods, holds)
        ev = attach_metrics(ev, baseline, bench_means, fold_periods, holds)
        results.append({"pattern": name, "n_events": n_events, "holds": ev})

    sens = run_sensitivity(pats, prepared, fold_periods, baseline, bench_means, holds, args)

    write_outputs(results, baseline, bench_means, holds, walk_end, len(prepared), len(fold_periods), sens)
    return results


def run_sensitivity(pats, prepared, fold_periods, baseline, bench_means, holds, args):
    """定義を少し変えても超過リターンの符号が安定するかを確認する。"""
    tag = "（定義感度）"
    print(f">> 定義感度チェック{tag}...")
    out = {}
    for name, (fn, variants) in SENSITIVITY.items():
        if args.pattern and not any(a in name for a in args.pattern):
            continue
        if name not in {n for c in pats.values() for n in c}:
            continue
        rows = []
        for vi, kw in enumerate(variants):
            store = {}
            for code, p in prepared.items():
                try:
                    arr = np.asarray(P.pattern_functions()[name][0](p, **kw), dtype=bool)
                except Exception:
                    continue
                if len(arr) == len(p["dates"]):
                    store[code] = {"__flags__": arr}
            if not store:
                continue
            ev = evaluate_flags(store, prepared, fold_periods, holds)
            ev = attach_metrics(ev, baseline, bench_means, fold_periods, holds)
            rows.append({
                "variant": vi,
                "params": kw,
                "excess_pct": {str(h): ev[h].get("excess_pct") for h in holds},
            })
        out[name] = rows
    return out


# --------------------------------------------------------------------------- 出力
def write_outputs(results, baseline, bench_means, holds, walk_end, n_stocks, n_folds, sens):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    doc = {
        "generated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "walk_end": walk_end,
        "hold_days": holds,
        "n_stocks": n_stocks,
        "n_folds": n_folds,
        "baseline_mean_pct": {
            str(h): _pct(float(np.mean([r for fr in baseline[h] for r in fr])))
            if any(fr for fr in baseline[h]) else None for h in holds
        },
        "bench_mean_pct": {str(h): _pct(float(np.mean([m for m in bench_means[h] if m is not None])))
                           if any(m is not None for m in bench_means[h]) else None for h in holds},
        "patterns": [
            {
                "pattern": r["pattern"],
                "n_events": r["n_events"],
                "holds": {str(h): r["holds"][h] for h in holds},
            }
            for r in results
        ],
        "sensitivity": sens,
        "notes": [
            "将来リターンは翌営業日寄り→H日後引け（手数料0.05%＋スリッページ0.1%・TP/SLなし）。",
            "ベースラインは同一フォールド・同一適格日の全銘柄平均。",
            f"benchmark は 1306.T の終値→終値（コストなし）の近似。|{BENCH_MAX_ABS_RET:.0%}|超の異常値は除外。",
            "excess_pct はイベント平均 − ベースライン平均。プラスほど型が有効。",
            "positive_fold_ratio はフォールド単位でイベント平均 > ベースライン平均だった割合。",
            f"データ異常ガード: 1取引 |リターン|>{MAX_ABS_RET:.0f}（+500%超）と価格<{MIN_PRICE}円は除外。",
            "サバイバーシップバイアスあり。型×保有期間×定義グリッドの多重検定に注意。",
            "研究専用。スコア・strategy_params.json には反映しない。",
        ],
    }
    path_json = os.path.join(OUTPUT_DIR, "research_patterns.json")
    with open(path_json, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)

    rows = []
    for r in results:
        for h in holds:
            m = r["holds"][h]
            ov = m["overall"]
            rows.append({
                "pattern": r["pattern"],
                "hold_days": h,
                "count": ov["count"],
                "mean_pct": ov["mean_pct"],
                "median_pct": ov["median_pct"],
                "hit_rate": ov["hit_rate"],
                "baseline_mean_pct": m["baseline_mean_pct"],
                "excess_pct": m["excess_pct"],
                "bench_excess_pct": m["bench_excess_pct"],
                "positive_fold_ratio": m["positive_fold_ratio"],
            })
    pd.DataFrame(rows).to_csv(os.path.join(OUTPUT_DIR, "research_patterns.csv"),
                              index=False, encoding="utf-8-sig")

    with open(os.path.join(OUTPUT_DIR, "research_patterns_report.md"), "w", encoding="utf-8") as f:
        f.write(render_report(doc))
    print(f"\n>> 出力: {path_json} / research_patterns.csv / research_patterns_report.md")


def render_report(doc):
    lines = [
        "# テクニカル『型』イベントスタディ（研究・スコア未反映）",
        "",
        f"- **実行日時**: {doc['generated_at']}",
        f"- **対象期間の終端 (walk_end)**: {doc['walk_end']}",
        f"- **銘柄数**: {doc['n_stocks']} / **フォールド数**: {doc['n_folds']}",
        f"- **保有期間**: {doc['hold_days']} 営業日",
        f"- **ベースライン平均**: {doc['baseline_mean_pct']}",
        f"- **ベンチマーク(1306.T)平均**: {doc['bench_mean_pct']}",
        "",
        "## 型ごとの成績（OOS・翌日寄り→H日後引け・コスト込み）",
        "",
    ]
    for r in doc["patterns"]:
        lines.append(f"### {r['pattern']}（イベント数 {r['n_events']}）")
        lines.append("")
        lines.append("| 保有 | 件数 | 平均 | 中央値 | 勝率 | ベースライン超過 | TOPIX超過 | プラス期間率 |")
        lines.append("| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
        for h in doc["hold_days"]:
            m = r["holds"].get(str(h))
            if not m:
                continue
            ov = m["overall"]
            lines.append(
                f"| {h}日 | {ov['count']} | {ov['mean_pct']}% | {ov['median_pct']}% | {ov['hit_rate']}% | "
                f"{m['excess_pct']}% | {m['bench_excess_pct']}% | {m['positive_fold_ratio']} |"
            )
        lines.append("")
        lines.append("| 保有 | 地合いON | 地合いOFF | 過熱低 | 過熱中 | 過熱高 |")
        lines.append("| ---: | ---: | ---: | ---: | ---: | ---: |")
        for h in doc["hold_days"]:
            m = r["holds"].get(str(h))
            if not m:
                continue
            rg = m["by_regime"]
            ov = m["by_overheat"]
            lines.append(
                f"| {h}日 | {rg['on']['mean_pct']}%({rg['on']['count']}) | {rg['off']['mean_pct']}%({rg['off']['count']}) | "
                f"{ov['low']['mean_pct']}%({ov['low']['count']}) | {ov['moderate']['mean_pct']}%({ov['moderate']['count']}) | "
                f"{ov['high']['mean_pct']}%({ov['high']['count']}) |"
            )
        lines.append("")
    lines.append("## 定義感度（パラメータを変えても超過の符号が安定するか）")
    lines.append("")
    lines.append("| 型 | 保有 | 変種1 | 変種2 | 変種3 |")
    lines.append("| --- | ---: | ---: | ---: | ---: |")
    for name, rows in doc["sensitivity"].items():
        for h in doc["hold_days"]:
            vals = [str(r["excess_pct"].get(str(h))) for r in rows]
            while len(vals) < 3:
                vals.append("—")
            lines.append(f"| {name} | {h}日 | {vals[0]}% | {vals[1]}% | {vals[2]}% |")
    lines.append("")
    lines.append("## 注意")
    lines.append("")
    for n in doc["notes"]:
        lines.append(f"- {n}")
    lines.append("")
    return "\n".join(lines)


def parse_args():
    ap = argparse.ArgumentParser(description="テクニカル型のイベントスタディ研究（研究専用）")
    ap.add_argument("--max-stocks", type=int, default=None, help="テスト用に銘柄数を絞る")
    ap.add_argument("--hold-days", type=str, default=None, help="カンマ区切り（既定 5,10,20,60）")
    ap.add_argument("--pattern", type=str, default=None,
                    help="対象の型名をカンマ区切りで指定（部分一致）")
    ap.add_argument("--walk-end", type=str, default=None, help="既定は実行日の前月末")
    return ap.parse_args()


def main():
    args = parse_args()
    if args.pattern:
        args.pattern = [p.strip() for p in args.pattern.split(",") if p.strip()]
    run(args)


if __name__ == "__main__":
    main()
