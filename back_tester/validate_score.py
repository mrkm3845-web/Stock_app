"""validate_score.py
スコア v2（現行） vs v3（改善案）を research_upside のキャッシュで高速比較する。

目的:
- 右裾捕捉（上位K5期待値）が改善するか
- 大勝ちリフトが悪化しないか
- 大負け確率が悪化しないか
- 学習期(〜2024)と検証期(2025〜)の両方で向きが保たれるか

v3 の変更:
- sma200_slope を厚め（25→30）
- 流動性（log10 売買代金）を追加（20）
- dist_sma200 / ret_120d を非線形化（行き過ぎは減点）
"""
import argparse
import os
import sys
from datetime import datetime, timedelta

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)
import research_upside as R  # noqa: E402

TH = {5: 0.10, 10: 0.15, 20: 0.20, 60: 0.30}
HOLDS = [5, 20]
LABELS = {0: "学習(〜2024)", 1: "検証(2025〜)", 2: "全期間"}


def clamp01(a):
    a = np.asarray(a, dtype=float)
    a = np.nan_to_num(a, nan=0.0)
    return np.clip(a, 0.0, 1.0)


def taper(x, full, start, end, floor=0.3):
    x = np.nan_to_num(np.asarray(x, dtype=float), nan=0.0)
    out = np.zeros_like(x)
    m = (x > 0) & (x < full)
    out[m] = x[m] / full
    out[(x >= full) & (x <= start)] = 1.0
    m = (x > start) & (x < end)
    out[m] = floor + (1.0 - floor) * (end - x[m]) / (end - start)
    out[x >= end] = floor
    return out


def score_v2(c):
    return (30 * clamp01(c["pos"]) + 25 * clamp01(c["dist"] / 0.30)
            + 25 * clamp01(c["slope"] / 0.10) + 20 * clamp01(c["ret"] / 0.60))


def score_v3(c):
    liq = clamp01((np.log10(np.maximum(np.expm1(c["log_avg"]), 1.0)) - 4.0) / 3.0)
    return (25 * clamp01(c["pos"]) + 30 * clamp01(c["slope"] / 0.10)
            + 15 * taper(c["dist"], 0.20, 0.40, 1.00)
            + 10 * taper(c["ret"], 0.60, 1.20, 3.00)
            + 20 * liq)


def load_components(walk_end, split_date):
    key = R._cache_key(walk_end, sorted(set(R.DEFAULT_HOLDS) | set(HOLDS)),
                       None, split_date, True, True)
    path = os.path.join(R.CACHE_DIR, key + ".npz")
    if not os.path.exists(path):
        raise SystemExit(f"キャッシュがありません: {path}")
    inp = R._load_cache(path)
    v = inp["vals_all"]
    c = {
        "pos": v["pos_52w(52週高値位置)"],
        "dist": v["dist_sma200(200日線乖離)"],
        "slope": v["sma200_slope(200日線傾き)"],
        "ret": v["ret_120d(120日リターン)"],
        "log_avg": v["log_avg_val(流動性)"],
    }
    return inp, c


def evaluate(score, inp, hold):
    st = inp["struct"]
    rets = inp["rets_all"][hold]
    bstart = [b[0] for b in st["bounds"]]
    bend = [b[1] for b in st["bounds"]]
    day_era = st["era"][np.array(bstart)]
    topk = {0: [], 1: [], 2: []}
    base = {0: [], 1: [], 2: []}
    dsum = {0: np.zeros(10), 1: np.zeros(10), 2: np.zeros(10)}
    dcnt = {0: np.zeros(10), 1: np.zeros(10), 2: np.zeros(10)}
    for di, (s, e) in enumerate(zip(bstart, bend)):
        v = score[s:e]
        r = rets[s:e]
        ok = np.isfinite(v) & np.isfinite(r)
        n = int(ok.sum())
        if n == 0:
            continue
        v = v[ok]
        r = r[ok]
        asc = np.argsort(v, kind="mergesort")
        rs = r[asc]
        ed = int(day_era[di])
        for g in (ed, 2):
            base[g].extend(r)
            k = min(5, n)
            topk[g].extend(rs[-k:])
            q = (np.arange(n) * 10) // n
            np.add.at(dsum[g], q, rs)
            np.add.at(dcnt[g], q, 1.0)
    out = {}
    for g in (0, 1, 2):
        tk = np.asarray(topk[g], dtype=float)
        ba = np.asarray(base[g], dtype=float)
        qmean = np.where(dcnt[g] > 0, dsum[g] / np.maximum(dcnt[g], 1), np.nan)
        T = TH[hold]
        if len(tk) == 0 or len(ba) == 0:
            out[g] = None
            continue
        # 上位十分位の大勝ち率（topKではなく分位で測る）
        q9 = qmean[9]
        q0 = qmean[0]
        top_dec_big = None
        # 分位9の大勝ち率は dsum では出せないため、topKの大勝ち率を参考値に使う
        out[g] = {
            "topk_exp_pct": round(float(tk.mean()) * 100, 3),
            "topk_bigwin_pct": round(float((tk >= T).mean()) * 100, 2),
            "topk_bigloss_pct": round(float((tk <= -T).mean()) * 100, 2),
            "spread_pct": round(float(q9 - q0) * 100, 3) if np.isfinite(q9) and np.isfinite(q0) else None,
            "base_mean_pct": round(float(ba.mean()) * 100, 3),
        }
    return out


def main():
    ap = argparse.ArgumentParser(description="スコアv2 vs v3 の高速検証（research_upside キャッシュ使用）")
    ap.add_argument("--walk-end", default=None, help="既定は実行日の前月末")
    ap.add_argument("--split-date", default="2025-01-01")
    args = ap.parse_args()
    walk_end = args.walk_end
    if not walk_end:
        today = datetime.now()
        walk_end = (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m-%d")
    inp, c = load_components(walk_end, args.split_date)
    s2 = score_v2(c)
    s3 = score_v3(c)
    print("スコア検証（OOS・翌日寄り→H日引け・コスト込み）")
    print("保有 | 期間 | v2 上位5期待値 | v3 上位5期待値 | v2 スプレッド | v3 スプレッド | v2 大勝ち | v3 大勝ち | v2 大負け | v3 大負け")
    verdict = []
    for h in HOLDS:
        m2 = evaluate(s2, inp, h)
        m3 = evaluate(s3, inp, h)
        for g in (0, 1, 2):
            a, b = m2[g], m3[g]
            if a is None or b is None:
                continue
            print(f"{h}日 | {LABELS[g]} | {a['topk_exp_pct']}% | {b['topk_exp_pct']}% | "
                  f"{a['spread_pct']}% | {b['spread_pct']}% | {a['topk_bigwin_pct']}% | {b['topk_bigwin_pct']}% | "
                  f"{a['topk_bigloss_pct']}% | {b['topk_bigloss_pct']}%")
        # 判定: 5日は両期間で期待値改善、大負け悪化なし
        if h == 5:
            for g in (0, 1, 2):
                a, b = m2[g], m3[g]
                if a and b:
                    verdict.append(b["topk_exp_pct"] >= a["topk_exp_pct"]
                                   and b["topk_bigloss_pct"] <= a["topk_bigloss_pct"] + 0.5)
    print("\n判定(5日: v3期待値>=v2 かつ 大負け悪化<=0.5pt):", "PASS" if all(verdict) else "FAIL", verdict)


if __name__ == "__main__":
    main()
