"""research_upside.py
スクリーナ目的（＝たまの大上昇＝右裾の捕捉）に特化した指標研究（新規・既存BTは無変更）

背景:
- 平均（期待値）最適化と「大勝ち（右裾）の捕捉」は別問題。
- 平均を引き上げる特徴と、右裾を引き上げる特徴は異なりうる（例: ボラは両裾を太らせる）。
- そこで指標ごとに「大勝ち確率のリフト」と「上位Kの期待値」を測り、両裾を併記する。

評価（OOS = ウォークフォワードの検証期間のみ・翌日寄り→H日後引け・コスト込み）:
- 大勝ちリフト = P(リターン ≥ T | 上位分位) / P(リターン ≥ T | 全体)
- 上位K期待値   = 毎日、指標上位K（既定 1/3/5）を買った場合の1トレード平均リターン
- 併記: 大負け確率 P(リターン ≤ -T)、中央値、右裾平均、フォールド別符号
- 時系列ホールドアウト: 学習想定期(〜split) / 検証期(split〜) / 全期間

主=上位K期待値、副=大勝ちリフト、制約=大負け確率。研究専用（スコア/params未反映）。

性能設計（重要）:
- サンプル構造・指標値・前向きリターンを「サンプル位置」で一度だけ作り、ディスクにキャッシュ
  （back_tester/data/upside_cache、gitignore済）。2回目以降は計算せずロードのみ。
- サンプル構造は日付順のフラット配列（指標ごとの再集約・再ソートを排除）。
- era は1回評価で付与し、集計時に era 別／全期間へ分ける（3回評価を廃止）。
- 型検出はベクトル化（pandas rolling / sliding_window_view）。

出力: results/research_upside.json / .csv / research_upside_report.md
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))  # リポジトリ直下（common/ を参照）
import backtest_rolling_walkforward as B  # noqa: E402
from common import patterns as P  # noqa: E402
from common import features as F  # noqa: E402

OUTPUT_DIR = os.path.join(_HERE, "results")
CACHE_DIR = os.path.join(_HERE, "data", "upside_cache")
CACHE_VERSION = "u2"

NQ = 10                     # 分位（十分位）
TOPK_LIST = [1, 3, 5]       # 上位K（5が主指標）
PRIMARY_TOPK = 5

# 保有期間ごとの「大勝ち」閾値（主）と感度セット
THRESHOLDS = {5: 0.10, 10: 0.15, 20: 0.20, 60: 0.30}
THRESHOLD_SENS = {
    5: [0.08, 0.10, 0.15],
    10: [0.12, 0.15, 0.20],
    20: [0.15, 0.20, 0.30],
    60: [0.20, 0.30, 0.40],
}
DEFAULT_HOLDS = [5, 10, 20, 60]

# データ異常ガード（既存 B._fwd_return は変更しない）
MAX_ABS_RET = 5.0
MIN_PRICE = 1.0

LIST_CACHE = os.path.join(CACHE_DIR, "jpx_stock_list.json")


# --------------------------------------------------------------------------- 進捗
class TimeLimitExceeded(Exception):
    pass


class Progress:
    """軽量な進捗表示。

    - 全体バー: フェーズ推定所要で重み付けし、完了時に実測秒で自己補正。
    - 現フェーズバー: 件数ベースで正確（総数既知）。
    - 描画は interval 秒に1回のみ。更新はカウンタ加算だけで軽い。
    - 10%刻みの節目だけログへ追記（描画負荷を増やさない）。
    """
    _ORDER = ["list", "load", "features", "patterns", "indicators", "returns", "arrange", "engine"]
    _FRAC = {"list": 0.01, "load": 0.08, "features": 0.22, "patterns": 0.40,
             "indicators": 0.12, "returns": 0.02, "arrange": 0.05, "engine": 0.10}

    def __init__(self, total_guess=150.0, interval=0.5, deadline=None, log_path=None):
        self.est = {k: max(0.5, v * total_guess) for k, v in self._FRAC.items()}
        self.frac = {k: 0.0 for k in self._ORDER}
        self.done_map = {k: False for k in self._ORDER}
        self.t0 = time.monotonic()
        self.last = 0.0
        self.interval = interval
        self.deadline = deadline
        self.log_path = log_path
        self.phase = None
        self.label = ""
        self.total = 1
        self.done = 0
        self.phase_start = self.t0
        self._next_log = 10
        self._log = None

    def _logline(self, msg):
        if not self.log_path:
            return
        try:
            if self._log is None:
                os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
                self._log = open(self.log_path, "a", encoding="utf-8")
            self._log.write(msg + "\n")
            self._log.flush()
        except Exception:
            pass

    def skip_pre_engine(self):
        for p in self._ORDER:
            if p == "engine":
                break
            self.done_map[p] = True
            self.frac[p] = 1.0
            self.est[p] = 0.0
        self.phase = None

    def _finish_current(self):
        if self.phase is None:
            return
        self.est[self.phase] = max(0.05, time.monotonic() - self.phase_start)
        self.frac[self.phase] = 1.0
        self.done_map[self.phase] = True
        self.done = self.total
        self._render()
        self.phase = None

    def set_phase(self, name, label, total):
        self._finish_current()
        if self.deadline is not None and time.monotonic() > self.deadline:
            raise TimeLimitExceeded()
        self.phase = name
        self.label = label
        self.total = max(1, int(total))
        self.done = 0
        self.phase_start = time.monotonic()
        self._next_log = 10
        self._render()

    def update(self, n=1):
        if self.phase is None:
            return
        self.done += n
        f = self.done / self.total
        self.frac[self.phase] = 1.0 if f > 1.0 else f
        now = time.monotonic()
        if self.deadline is not None and now > self.deadline:
            raise TimeLimitExceeded()
        if now - self.last < self.interval and self.done < self.total:
            return
        self.last = now
        self._render()

    @staticmethod
    def _bar(pct, width=12):
        f = int(pct) * width // 100
        return "#" * f + "-" * (width - f)

    def _render(self):
        now = time.monotonic()
        elapsed = now - self.t0
        remaining = 0.0
        for p in self._ORDER:
            if self.done_map[p]:
                continue
            remaining += self.est[p] * (1.0 - self.frac[p])
        total = elapsed + remaining
        pct = int(100.0 * elapsed / total) if total > 0 else 0
        pct = 0 if pct < 0 else (100 if pct > 100 else pct)
        if self.phase is not None:
            pf = int(100.0 * self.done / self.total)
            pf = 0 if pf < 0 else (100 if pf > 100 else pf)
            line = (f"\r全体[{self._bar(pct)}]{pct:3d}% | {self.label}[{self._bar(pf)}]"
                    f"{self.done}/{self.total}({pf:3d}%) 経過{elapsed:5.1f}s 残り約{remaining:4.0f}s")
            if pf >= self._next_log:
                self._logline(f"{self.label}: {pf}% ({self.done}/{self.total}) elapsed={elapsed:.1f}s")
                self._next_log += 10
        else:
            line = f"\r全体[{self._bar(pct)}]{pct:3d}% 経過{elapsed:5.1f}s 残り約{remaining:4.0f}s"
        pad = 96 - len(line)
        sys.stdout.write(line + (" " * pad if pad > 0 else ""))
        sys.stdout.flush()

    def close(self):
        self._finish_current()
        sys.stdout.write("\n")
        sys.stdout.flush()
        if self._log is not None:
            try:
                self._log.close()
            except Exception:
                pass


# --------------------------------------------------------------------------- 基本
def _fold_periods(folds):
    return [(np.datetime64(f[2], "D"), np.datetime64(f[3], "D")) for f in folds]


def _safe_ret_series(p, hold):
    """1銘柄・1保有期間の『安全な前向きリターン』配列（不正値は NaN）。ベクトル化。"""
    close = np.asarray(p["close"], dtype=float)
    op = np.asarray(p["open"], dtype=float)
    n = len(close)
    out = np.full(n, np.nan, dtype=np.float32)
    if n < 2:
        return out
    idx = np.arange(n)
    entry_i = idx + 1
    valid = entry_i < n
    e = np.full(n, np.nan)
    x = np.full(n, np.nan)
    ei = entry_i[valid]
    e[valid] = op[ei]
    xi = np.minimum(ei + hold, n - 1)
    x[valid] = close[xi]
    fee = B.CONFIG["fee_rate"]
    slip = B.CONFIG["slippage_rate"]
    with np.errstate(divide="ignore", invalid="ignore"):
        entry_p = e * (1.0 + slip) * (1.0 + fee)
        exit_p = x * (1.0 - slip) * (1.0 - fee)
        r = (exit_p - entry_p) / entry_p
    ok = (valid & np.isfinite(e) & np.isfinite(x) & (e >= MIN_PRICE) & (x >= MIN_PRICE)
          & np.isfinite(r) & (np.abs(r) <= MAX_ABS_RET))
    out[ok] = r[ok].astype(np.float32)
    return out


def _pct(x):
    return None if x is None else round(float(x) * 100, 3)


def _recent(flags, window=3):
    """直近 window 本以内にフラグが立ったか（ベクトル化）。"""
    f = np.asarray(flags, dtype=float)
    if len(f) == 0:
        return f
    return pd.Series(f).rolling(window, min_periods=1).max().values


def _pct_change(a, n):
    a = np.asarray(a, dtype=float)
    prev = np.asarray(pd.Series(a).shift(n).values, dtype=float)
    return np.where(prev > 0, a / prev - 1.0, np.nan)


def _gap(p):
    close = np.asarray(p["close"], float)
    op = np.asarray(p["open"], float)
    prev = np.asarray(pd.Series(close).shift(1).values, dtype=float)
    return np.where(prev > 0, op / prev - 1.0, np.nan)


def _run_up(close):
    """連騰日数（ベクトル化）。"""
    c = np.asarray(close, float)
    n = len(c)
    if n < 2:
        return np.zeros(n)
    up = np.concatenate(([False], c[1:] > c[:-1]))
    grp = (~up).cumsum()
    return pd.Series(up.astype(np.int32)).groupby(grp).cumsum().values.astype(float)


# --------------------------------------------------------------------------- 指標
def _patterns_needed(p):
    """型のうち必要なものだけを計算（16型全部は不要）。"""
    gc, dc = P.ma_cross(p, 5, 25)
    return {
        "GC(5,25)": np.asarray(gc, dtype=bool),
        "DC(5,25)": np.asarray(dc, dtype=bool),
        "ダブルボトム": P.double_bottom(p),
        "レンジブレイク": P.range_breakout(p),
        "カップウィズハンドル": P.cup_with_handle(p),
        "フラッグ/ペナント": P.flag_pennant(p),
    }


def build_indicators(prepared, params, pat_cache, with_patterns=True, progress=None):
    """指標名 -> {code: 配列} を返す（配列は prepared の日付軸に整列）。"""
    weights = params.get("score_weights", {})
    sig = params.get("signals", {})
    ind = {}

    def set_arr(name, fn):
        d = {}
        for code, p in prepared.items():
            if progress is not None:
                progress.update(1)
            try:
                v = fn(p)
            except Exception:
                continue
            if v is None:
                continue
            arr = np.asarray(v, dtype=float)
            if len(arr) == len(p["dates"]):
                d[code] = arr
        ind[name] = d

    set_arr("現行スコア(score)", lambda p: B.score_series(p, p["weekly_up"], sig, weights))
    set_arr("pos_52w(52週高値位置)", lambda p: p["pos_52w"])
    set_arr("dist_sma200(200日線乖離)", lambda p: p["dist_sma200"])
    set_arr("sma200_slope(200日線傾き)", lambda p: p["sma200_slope"])
    set_arr("ret_120d(120日リターン)", lambda p: p["ret_120d"])
    set_arr("ret_60d(60日リターン)", lambda p: _pct_change(p["close"], 60))
    set_arr("ret_20d(20日リターン)", lambda p: _pct_change(p["close"], 20))
    set_arr("ret_5d(5日リターン)", lambda p: _pct_change(p["close"], 5))
    set_arr("dist_sma25(25日線乖離)", lambda p: np.asarray(p["close"], float) / np.asarray(p["sma25"], float) - 1.0)
    set_arr("dist_sma5(5日線乖離)", lambda p: np.asarray(p["close"], float) / np.asarray(p["sma5"], float) - 1.0)
    set_arr("rsi14(RSI14)", lambda p: p["rsi14"])
    set_arr("atr_pct(ボラティリティ)", lambda p: p["atr_pct"])
    set_arr("log_avg_val(流動性)", lambda p: np.log1p(np.maximum(np.asarray(p["avg_val_5d"], float), 0.0)))
    set_arr("val_ratio_5d(出来高増加率)", lambda p: p["val_ratio_5d"])
    set_arr("gap_pct(寄付ギャップ)", lambda p: _gap(p))
    set_arr("run_up_days(連騰日数)", lambda p: _run_up(p["close"]))

    if with_patterns:
        def pat(p, name):
            return pat_cache.get(_CMAP.get(id(p)), {}).get(name, np.zeros(len(p["dates"])))

        set_arr("GC(5,25)直後", lambda p: _recent(pat(p, "GC(5,25)"), 3))
        set_arr("DC(5,25)直後", lambda p: _recent(pat(p, "DC(5,25)"), 3))
        set_arr("ダブルボトム", lambda p: pat(p, "ダブルボトム").astype(float))
        set_arr("レンジブレイク", lambda p: pat(p, "レンジブレイク").astype(float))
        set_arr("カップウィズハンドル", lambda p: pat(p, "カップウィズハンドル").astype(float))
        set_arr("フラッグ/ペナント", lambda p: pat(p, "フラッグ/ペナント").astype(float))
    return ind


_CMAP = {}


# --------------------------------------------------------------------------- 統計
def _stats_arr(arr, thresholds, primary):
    a = np.asarray(arr, dtype=float)
    a = a[np.isfinite(a)]
    if len(a) == 0:
        return {"count": 0, "mean_pct": None, "median_pct": None, "hit_rate": None,
                "big_win": {}, "big_loss_pct": None, "tail_mean_pct": None}
    big = {str(t): round(float((a >= t).mean()) * 100, 2) for t in thresholds}
    pos = a[a >= primary]
    return {
        "count": int(len(a)),
        "mean_pct": _pct(a.mean()),
        "median_pct": _pct(np.median(a)),
        "hit_rate": round(float((a > 0).mean()) * 100, 2),
        "big_win": big,
        "big_loss_pct": round(float((a <= -primary).mean()) * 100, 2),
        "tail_mean_pct": _pct(pos.mean()) if len(pos) else None,
    }


def _cat(chunks):
    if not chunks:
        return np.array([], dtype=np.float32)
    if len(chunks) == 1:
        return chunks[0]
    return np.concatenate(chunks)


# --------------------------------------------------------------------------- サンプル構造
def build_sample_structure(prepared, fold_periods, split, progress=None):
    """全フォールドの適格サンプルを日付順のフラット配列で構築する（1回だけ）。"""
    code_list = list(prepared.keys())
    off = {}
    o = 0
    for c in code_list:
        off[c] = o
        o += len(prepared[c]["dates_day"])
    n_total = o
    sp = np.datetime64(split, "D") if split else None

    fp_l, day_l, era_l, fold_l = [], [], [], []
    fold_era = []
    for fi, (ts, te) in enumerate(fold_periods):
        era = 0
        if sp is not None:
            era = 0 if te < sp else 1
        fold_era.append(era)
        for c, p in prepared.items():
            d = p["dates_day"]
            ii = np.where(p["eligible"] & (d >= ts) & (d <= te))[0]
            if len(ii) == 0:
                continue
            fp_l.append(off[c] + ii.astype(np.int64))
            day_l.append(d[ii])
            era_l.append(np.full(len(ii), era, dtype=np.int8))
            fold_l.append(np.full(len(ii), fi, dtype=np.int32))
        if progress is not None:
            progress.update(1)
    if not fp_l:
        return None
    fp = np.concatenate(fp_l)
    day = np.concatenate(day_l)
    era = np.concatenate(era_l)
    fold = np.concatenate(fold_l)
    order = np.argsort(day, kind="mergesort")
    fp, day, era, fold = fp[order], day[order], era[order], fold[order]
    _uniq, first = np.unique(day, return_index=True)
    bounds = list(zip(first.tolist(), (list(first[1:]) + [len(day)])))
    return {
        "code_list": code_list, "off": off, "n_total": n_total,
        "fp": fp, "day": day, "era": era, "fold": fold,
        "bounds": bounds,
        "fold_era": np.asarray(fold_era, dtype=np.int8), "split": sp,
    }


def flatten_ret(ret_series, code_list, off, n_total):
    out = {}
    for h, d in ret_series.items():
        buf = np.full(n_total, np.nan, dtype=np.float32)
        for c in code_list:
            a = d.get(c)
            if a is None:
                continue
            o = off[c]
            buf[o:o + len(a)] = a
        out[h] = buf
    return out


def flat_indicator(ind, code_list, off, n_total):
    fv = np.full(n_total, np.nan, dtype=np.float32)
    for c in code_list:
        a = ind.get(c)
        if a is None:
            continue
        o = off[c]
        fv[o:o + len(a)] = a
    return fv


# --------------------------------------------------------------------------- キャッシュ
def _cache_key(walk_end, cache_holds, max_stocks, split_date, with_patterns, offline):
    hh = "-".join(str(x) for x in sorted(cache_holds))
    return (f"{walk_end}_{hh}_{max_stocks or 'all'}_{split_date or 'nosplit'}"
            f"_{int(with_patterns)}_{int(offline)}_{CACHE_VERSION}")


def _save_cache(path, inputs):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    S = inputs["struct"]
    arrs = {
        "day": S["day"], "era": S["era"], "fold": S["fold"],
        "bstart": np.array([b[0] for b in S["bounds"]], dtype=np.int32),
        "bend": np.array([b[1] for b in S["bounds"]], dtype=np.int32),
        "fold_era": S["fold_era"],
    }
    for h, v in inputs["rets_all"].items():
        arrs[f"ret_{h}"] = v
    names = inputs["names"]
    for i, name in enumerate(names):
        arrs[f"v_{i}"] = inputs["vals_all"][name]
    np.savez(path, **arrs)
    meta = {k: inputs[k] for k in ("walk_end", "split_date", "n_stocks", "n_folds")}
    meta["names"] = names
    with open(path + ".json", "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)


def _load_cache(path):
    with open(path + ".json", "r", encoding="utf-8") as f:
        meta = json.load(f)
    names = meta["names"]
    with np.load(path) as z:
        struct = {
            "day": z["day"], "era": z["era"], "fold": z["fold"],
            "bounds": list(zip(z["bstart"].tolist(), z["bend"].tolist())),
            "fold_era": z["fold_era"],
        }
        rets_all = {int(k[4:]): z[k] for k in z.files if k.startswith("ret_")}
        vals_all = {name: z[f"v_{i}"] for i, name in enumerate(names)}
    return {
        "struct": struct, "rets_all": rets_all, "vals_all": vals_all,
        "names": names, "walk_end": meta["walk_end"], "split_date": meta["split_date"],
        "n_stocks": meta["n_stocks"], "n_folds": meta["n_folds"],
    }


# --------------------------------------------------------------------------- 実行
def _local_backtest_codes():
    out = []
    try:
        for fn in os.listdir(B.CACHE_DIR):
            if fn.endswith(".csv") and not fn.startswith("benchmark_"):
                out.append(fn[:-4])
    except Exception:
        pass
    return sorted(out)


def load_stock_list(offline):
    if os.path.exists(LIST_CACHE):
        try:
            with open(LIST_CACHE, "r", encoding="utf-8") as f:
                lst = json.load(f)
            if lst:
                return lst
        except Exception:
            pass
    if offline:
        return [{"コード": c, "銘柄名": c, "33業種区分": ""} for c in _local_backtest_codes()]
    lst = B.fetch_jpx_stock_list(B.CONFIG["markets"])
    if lst:
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            with open(LIST_CACHE, "w", encoding="utf-8") as f:
                json.dump(lst, f, ensure_ascii=False)
        except Exception:
            pass
    return lst


def load_dfs_offline(codes, progress=None):
    dfs = {}
    for c in codes:
        f = os.path.join(B.CACHE_DIR, f"{c}.csv")
        if os.path.exists(f):
            df = B._read_cached(f)
            if df is not None and len(df) >= 30:
                dfs[c] = df
        if progress is not None:
            progress.update(1)
    return dfs


def prepare_stocks_local(dfs, progress=None):
    prepared = {}
    for code, df in dfs.items():
        try:
            feat = F.compute_daily_features(df)
            if len(feat["close"]) < 35:
                continue
            weekly_up = B.weekly_trend_daily(df)
            eligible = (weekly_up
                        & (feat["avg_val_5d"] >= B.CONFIG["min_avg_val_k"])
                        & (~np.isnan(feat["sma200"]))
                        & (feat["close"] > 0))
            prepared[code] = {**feat, "weekly_up": weekly_up, "eligible": eligible}
        except Exception:
            pass
        if progress is not None:
            progress.update(1)
    return prepared


def prepare(cfg, max_stocks, walk_end, offline, progress):
    B.CONFIG["walk_end"] = walk_end
    progress.set_phase("list", "銘柄リスト", 1)
    stock_list = load_stock_list(offline)
    if not stock_list:
        print("[FAIL] 銘柄リスト取得失敗")
        return None
    if max_stocks:
        stock_list = stock_list[:max_stocks]
    codes = [s["コード"] for s in stock_list]
    progress.update(1)

    progress.set_phase("load", "CSV読込", len(codes))
    if offline:
        stock_dfs = load_dfs_offline(codes, progress)
    else:
        stock_dfs = B.download_stock_data(codes, B.CONFIG["walk_start"], B.CONFIG["walk_end"])
        progress.done = progress.total
        progress.update(0)

    progress.set_phase("features", "特徴量", len(stock_dfs))
    prepared = prepare_stocks_local(stock_dfs, progress)
    folds = B.make_folds(B.CONFIG)
    return {"prepared": prepared, "folds": folds}


def compute_inputs(args, walk_end, cache_holds, progress):
    t0 = time.perf_counter()
    data = prepare(B.CONFIG, args.max_stocks, walk_end, args.offline, progress)
    if data is None:
        return None
    prepared = data["prepared"]
    fold_periods = _fold_periods(data["folds"])

    for code, p in prepared.items():
        _CMAP[id(p)] = code
        p["dates_day"] = np.asarray(p["dates"]).astype("datetime64[D]")

    pat_cache = {}
    if args.with_patterns:
        progress.set_phase("patterns", "型検出", len(prepared))
        for code, p in prepared.items():
            try:
                pat_cache[code] = _patterns_needed(p)
            except Exception:
                pat_cache[code] = {}
            progress.update(1)

    params = B.load_strategy_params()
    n_ind = 16 + (6 if args.with_patterns else 0)
    progress.set_phase("indicators", "指標計算", n_ind * max(1, len(prepared)))
    ind = build_indicators(prepared, params, pat_cache,
                           with_patterns=args.with_patterns, progress=progress)
    names = list(ind.keys())

    progress.set_phase("returns", "リターン",
                       len(fold_periods) + len(cache_holds) * max(1, len(prepared)))
    S = build_sample_structure(prepared, fold_periods, args.split_date, progress=progress)
    if S is None:
        print("[FAIL] 適格サンプルがありません")
        return None
    fp = S["fp"]
    ret_series = {h: {} for h in cache_holds}
    for h in cache_holds:
        for code, p in prepared.items():
            ret_series[h][code] = _safe_ret_series(p, h)
            progress.update(1)
    flat_ret = flatten_ret(ret_series, S["code_list"], S["off"], S["n_total"])
    rets_all = {h: flat_ret[h][fp] for h in cache_holds}

    progress.set_phase("arrange", "値配置", max(1, len(names) + len(cache_holds)))
    vals_all = {}
    for name in names:
        vals_all[name] = flat_indicator(ind[name], S["code_list"], S["off"], S["n_total"])[fp]
        progress.update(1)
    for _ in cache_holds:
        progress.update(1)

    return {
        "struct": S, "rets_all": rets_all, "vals_all": vals_all, "names": names,
        "walk_end": walk_end, "split_date": args.split_date,
        "n_stocks": len(prepared), "n_folds": len(fold_periods),
    }


def load_or_compute(args, walk_end, holds, progress):
    cache_holds = sorted(set(DEFAULT_HOLDS) | set(holds))
    path = os.path.join(CACHE_DIR, _cache_key(walk_end, cache_holds, args.max_stocks,
                                               args.split_date, args.with_patterns,
                                               args.offline) + ".npz")
    if args.use_cache and not args.refresh_cache and os.path.exists(path):
        try:
            inputs = _load_cache(path)
            print(f">> キャッシュ読込: {path}")
            return inputs, cache_holds, True
        except Exception as e:
            print(f">> キャッシュ読込失敗（再計算します）: {e}")

    inputs = compute_inputs(args, walk_end, cache_holds, progress)
    if inputs is None:
        return None, cache_holds, False
    if args.use_cache:
        try:
            _save_cache(path, inputs)
            print(f">> キャッシュ保存: {path}")
        except Exception as e:
            print(f">> キャッシュ保存失敗（続行）: {e}")
    return inputs, cache_holds, False


def _run(args, progress):
    t0 = time.perf_counter()
    walk_end = args.walk_end
    if walk_end is None:
        today = datetime.now()
        walk_end = (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m-%d")
    holds = [int(x) for x in args.hold_days.split(",")] if args.hold_days else DEFAULT_HOLDS
    split_date = args.split_date

    inputs, cache_holds, from_cache = load_or_compute(args, walk_end, holds, progress)
    if inputs is None:
        progress.close()
        return
    if from_cache:
        progress.skip_pre_engine()

    S = inputs["struct"]
    rets_all = inputs["rets_all"]
    vals_all = inputs["vals_all"]
    names = list(inputs["names"])
    if args.indicator:
        names = [n for n in names if any(a in n for a in args.indicator)]

    if split_date:
        acc_groups = {f"〜{split_date}": [0], f"{split_date}〜": [1], "全期間": [0, 1]}
    else:
        acc_groups = {"全期間": [0]}
    labels = list(acc_groups.keys())
    era_to_labels = {ed: [lab for lab, mem in acc_groups.items() if ed in mem] for ed in (0, 1)}
    label_folds = {lab: [fi for fi, e in enumerate(S["fold_era"]) if int(e) in mem]
                   for lab, mem in acc_groups.items()}

    bstart = np.array([b[0] for b in S["bounds"]], dtype=np.int64)
    bend = np.array([b[1] for b in S["bounds"]], dtype=np.int64)
    day_era = S["era"][bstart]
    day_fold = S["fold"][bstart]

    # ベースライン（ラベル×ホライズン）
    base = {lab: {} for lab in labels}
    for h in holds:
        r_all = rets_all[h]
        for lab, mem in acc_groups.items():
            sel = np.isin(S["era"], mem) & np.isfinite(r_all)
            base[lab][h] = r_all[sel]
    progress.set_phase("engine", "集計", max(1, len(names) * len(holds)))

    group_results = {lab: [] for lab in labels}
    for ni, name in enumerate(names, 1):
        vals_all_arr = vals_all[name]
        holds_out = {}
        for h in holds:
            thresholds = THRESHOLD_SENS.get(h, [THRESHOLDS.get(h, 0.1)])
            primary = THRESHOLDS.get(h, thresholds[len(thresholds) // 2])
            rets_h = rets_all[h]
            dec = {lab: {q: [] for q in range(NQ)} for lab in labels}
            top = {lab: {k: [] for k in TOPK_LIST} for lab in labels}
            top_fold = {k: {} for k in TOPK_LIST}
            for di in range(len(bstart)):
                s, e = int(bstart[di]), int(bend[di])
                v = vals_all_arr[s:e]
                r = rets_h[s:e]
                ok = np.isfinite(v) & np.isfinite(r)
                cnt = int(ok.sum())
                if cnt == 0:
                    continue
                v = v[ok]
                r = r[ok]
                glabels = era_to_labels[int(day_era[di])]
                fi = int(day_fold[di])
                asc = np.argsort(v, kind="mergesort")
                rs_sorted = r[asc]
                if cnt >= NQ:
                    q = (np.arange(cnt) * NQ) // cnt
                    edges = np.searchsorted(q, np.arange(1, NQ))
                    starts = np.concatenate(([0], edges))
                    ends = np.concatenate((edges, [cnt]))
                    for lab in glabels:
                        dlab = dec[lab]
                        for qq in range(NQ):
                            a, z = int(starts[qq]), int(ends[qq])
                            if z > a:
                                dlab[qq].append(rs_sorted[a:z])
                if cnt >= max(TOPK_LIST):
                    desc = asc[::-1]
                    for k in TOPK_LIST:
                        sel = r[desc[:k]]
                        for lab in glabels:
                            top[lab][k].append(sel)
                        top_fold[k].setdefault(fi, []).append(sel)

            def _era_metrics(lab):
                ba = base[lab][h]
                ba_f = ba[np.isfinite(ba)]
                base_big = {str(t): (round(float((ba_f >= t).mean()) * 100, 2) if len(ba_f) else None)
                            for t in thresholds}
                per_q = {q: _stats_arr(_cat(dec[lab][q]), thresholds, primary) for q in range(NQ)}
                top_q = per_q[NQ - 1]
                lifts = {}
                for t in thresholds:
                    pb = base_big.get(str(t))
                    pt = top_q["big_win"].get(str(t))
                    lifts[str(t)] = round(pt / pb, 2) if (pb and pt is not None and pb > 0) else None
                tk = {}
                for k in TOPK_LIST:
                    st = _stats_arr(_cat(top[lab][k]), thresholds, primary)
                    fold_means = []
                    for fi in label_folds[lab]:
                        chunks = top_fold[k].get(fi)
                        if chunks:
                            fold_means.append(float(np.mean(_cat(chunks))) * 100)
                    st["positive_fold_ratio"] = (round(sum(1 for x in fold_means if x > 0) / len(fold_means), 3)
                                                 if fold_means else None)
                    tk[str(k)] = st
                return {
                    "threshold": primary, "thresholds": thresholds,
                    "base": {"count": int(len(ba_f)),
                             "mean_pct": _pct(ba_f.mean()) if len(ba_f) else None,
                             "big_win": base_big},
                    "top_decile": {**top_q, "lift": lifts},
                    "topk": tk,
                    "per_quantile": {str(q): per_q[q] for q in range(NQ)},
                }

            for lab in labels:
                holds_out.setdefault(lab, {})[h] = _era_metrics(lab)
            progress.update(1)
        for lab in labels:
            group_results[lab].append({"indicator": name, "holds": holds_out[lab]})

    era_outputs = {}
    for lab in labels:
        era_outputs[lab] = {"n_folds": len(label_folds[lab]), "base": base[lab], "results": group_results[lab]}
    write_outputs(era_outputs, holds, walk_end, inputs["n_stocks"], inputs["n_folds"], split_date)
    progress.close()
    print(f">> 総所要時間 {time.perf_counter() - t0:.1f}s")
    return era_outputs


def run(args):
    deadline = None
    if getattr(args, "max_minutes", 0) and args.max_minutes > 0:
        deadline = time.monotonic() + float(args.max_minutes) * 60.0
    progress = Progress(
        total_guess=150.0,
        interval=max(0.2, float(getattr(args, "progress_every", 0.5))),
        deadline=deadline,
        log_path=os.path.join(OUTPUT_DIR, "research_upside_progress.log"),
    )
    try:
        return _run(args, progress)
    except TimeLimitExceeded:
        progress.close()
        print(f"\n!! 制限時間 {args.max_minutes}分を超えたため中断しました。"
              "同じコマンドで再実行してください（完了済みフェーズはキャッシュがあれば再利用されます）。")
        return None
    except KeyboardInterrupt:
        progress.close()
        print("\n!! 中断しました。")
        return None


# --------------------------------------------------------------------------- 出力
def _era_json(eo, holds):
    return {
        "n_folds": eo["n_folds"],
        "baseline": {str(h): {"n": int(np.isfinite(eo["base"][h]).sum()),
                              "mean_pct": _pct(eo["base"][h][np.isfinite(eo["base"][h])].mean())
                              if np.isfinite(eo["base"][h]).any() else None}
                     for h in holds},
        "indicators": [
            {"indicator": r["indicator"], "holds": {str(h): r["holds"][h] for h in holds}}
            for r in eo["results"]
        ],
    }


def write_outputs(era_outputs, holds, walk_end, n_stocks, n_folds, split_date):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    doc = {
        "generated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "walk_end": walk_end,
        "split_date": split_date,
        "hold_days": holds,
        "n_stocks": n_stocks,
        "n_folds": n_folds,
        "topk_list": TOPK_LIST,
        "thresholds": {str(h): THRESHOLD_SENS.get(h, [THRESHOLDS.get(h)]) for h in holds},
        "eras": {label: _era_json(eo, holds) for label, eo in era_outputs.items()},
        "notes": [
            "OOS=検証期間のみ。翌日寄り→H日後引け（手数料0.05%＋スリッページ0.1%）。",
            "大勝ちリフト=P(ret>=T|上位十分位)/P(ret>=T|全体)。大きさ・負け側は無視。",
            "上位K期待値=毎日 指標上位K を買った場合の1トレード平均（主指標=K5）。",
            "時系列ホールドアウト: 学習想定期(〜split)で挙動を確認し、検証期(split〜)で符号が残るかを見る。",
            "大負け確率 P(ret<=-T) を必ず併記（左裾の制約）。",
            f"データ異常ガード: |ret|>{MAX_ABS_RET:.0f} と価格<{MIN_PRICE}円は除外。",
            "サバイバーシップバイアスは右裾を過大化しやすい。",
            "研究専用。スコア・strategy_params.json には反映しない。",
        ],
    }
    with open(os.path.join(OUTPUT_DIR, "research_upside.json"), "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2, default=str)

    rows = []
    for label, eo in era_outputs.items():
        for r in eo["results"]:
            for h in holds:
                m = r["holds"][h]
                td = m["top_decile"]
                tk5 = m["topk"][str(PRIMARY_TOPK)]
                rows.append({
                    "era": label,
                    "indicator": r["indicator"],
                    "hold_days": h,
                    "threshold_pct": round(m["threshold"] * 100, 1),
                    "top_decile_mean_pct": td["mean_pct"],
                    "top_decile_median_pct": td["median_pct"],
                    "top_decile_bigwin_pct": td["big_win"].get(str(m["threshold"])),
                    "bigwin_lift": td["lift"].get(str(m["threshold"])),
                    "top_decile_bigloss_pct": td["big_loss_pct"],
                    f"topK{PRIMARY_TOPK}_expectancy_pct": tk5["mean_pct"],
                    f"topK{PRIMARY_TOPK}_bigwin_pct": tk5["big_win"].get(str(m["threshold"])),
                    f"topK{PRIMARY_TOPK}_positive_fold_ratio": tk5["positive_fold_ratio"],
                })
    pd.DataFrame(rows).to_csv(os.path.join(OUTPUT_DIR, "research_upside.csv"),
                              index=False, encoding="utf-8-sig")

    with open(os.path.join(OUTPUT_DIR, "research_upside_report.md"), "w", encoding="utf-8") as f:
        f.write(render_report(doc, era_outputs))
    print(f"\n>> 出力: {os.path.join(OUTPUT_DIR, 'research_upside.json')} / research_upside.csv / research_upside_report.md")


def _fmt(x, unit="%"):
    return "—" if x is None else f"{x}{unit}"


def _lookup(era, name):
    for r in era["results"]:
        if r["indicator"] == name:
            return r
    return None


def render_report(doc, era_outputs):
    holds = doc["hold_days"]
    labels = list(era_outputs.keys())
    full = "全期間" if "全期間" in era_outputs else labels[-1]
    lines = [
        "# 右裾（大勝ち）捕捉の指標研究（研究・スコア未反映）",
        "",
        f"- **実行日時**: {doc['generated_at']}",
        f"- **対象期間の終端**: {doc['walk_end']} / 銘柄 {doc['n_stocks']} / フォールド {doc['n_folds']}",
        f"- **保有期間**: {holds} 営業日 / **大勝ち閾値 T**: {doc['thresholds']}",
        f"- **時系列分割**: {doc.get('split_date') or 'なし'}",
        "",
    ]
    ph = 5 if 5 in holds else holds[0]
    if doc.get("split_date") and len(labels) >= 3:
        train_label, test_label = labels[0], labels[1]
        lines += [
            "## 時系列ホールドアウト（主要指標・5日・上位K5期待値とリフト）",
            "",
            f"- 学習想定期 = {train_label} / 検証期 = {test_label}",
            "- 符号一致=検証期でも学習期と同じ向きか。リフト>1一致=両期で1超か。",
            "",
            "| 指標 | 学習 期待値 | 検証 期待値 | 符号一致 | 学習 リフト | 検証 リフト | リフト>1一致 |",
            "| :-- | --: | --: | :--: | --: | --: | :--: |",
        ]
        tr, te = era_outputs[train_label], era_outputs[test_label]
        for r0 in tr["results"]:
            name = r0["indicator"]
            rt = _lookup(te, name)
            if rt is None:
                continue
            m0 = r0["holds"][ph]
            m1 = rt["holds"][ph]
            e0 = m0["topk"][str(PRIMARY_TOPK)]["mean_pct"]
            e1 = m1["topk"][str(PRIMARY_TOPK)]["mean_pct"]
            l0 = m0["top_decile"]["lift"].get(str(m0["threshold"]))
            l1 = m1["top_decile"]["lift"].get(str(m1["threshold"]))
            sign_ok = "○" if (e0 is not None and e1 is not None and np.sign(e0) == np.sign(e1)) else "×"
            lift_ok = "○" if (l0 is not None and l1 is not None and (l0 > 1) == (l1 > 1)) else "×"
            lines.append(
                f"| {name} | {_fmt(e0)} | {_fmt(e1)} | {sign_ok} | "
                f"{_fmt(l0, 'x')} | {_fmt(l1, 'x')} | {lift_ok} |"
            )
        lines.append("")

    lines += [
        f"## 指標別（{full}・主=上位K5期待値／副=リフト／制約=大負け）",
        "",
        "| 指標 | 保有 | 上位5期待値 | 上位5大勝ち | リフト | 上分位大負け | 上分位中央値 | プラス期間率 |",
        "| :-- | --: | --: | --: | --: | --: | --: | --: |",
    ]
    for r in era_outputs[full]["results"]:
        for h in holds:
            m = r["holds"][h]
            td = m["top_decile"]
            tk5 = m["topk"][str(PRIMARY_TOPK)]
            T = str(m["threshold"])
            lines.append(
                f"| {r['indicator']} | {h}日 | {_fmt(tk5['mean_pct'])} | "
                f"{_fmt(tk5['big_win'].get(T))} | {_fmt(td['lift'].get(T), 'x')} | "
                f"{_fmt(td['big_loss_pct'])} | {_fmt(td['median_pct'])} | "
                f"{_fmt(tk5['positive_fold_ratio'], '')} |"
            )
    lines.append("")
    lines.append(f"## 上位1/3/5の期待値（{full}・K比較）")
    lines.append("")
    lines.append("| 指標 | 保有 | K=1 | K=3 | K=5 |")
    lines.append("| :-- | --: | --: | --: | --: |")
    for r in era_outputs[full]["results"]:
        for h in holds:
            m = r["holds"][h]
            vals = [_fmt(m["topk"][str(k)]["mean_pct"]) for k in TOPK_LIST]
            lines.append(f"| {r['indicator']} | {h}日 | {vals[0]} | {vals[1]} | {vals[2]} |")
    lines.append("")
    lines.append("## 注意")
    lines.append("")
    for n in doc["notes"]:
        lines.append(f"- {n}")
    return "\n".join(lines)


def parse_args():
    ap = argparse.ArgumentParser(description="右裾（大勝ち）捕捉の指標研究（研究専用）")
    ap.add_argument("--max-stocks", type=int, default=None)
    ap.add_argument("--hold-days", type=str, default=None)
    ap.add_argument("--indicator", type=str, default=None, help="指標名をカンマ区切り（部分一致）")
    ap.add_argument("--walk-end", type=str, default=None)
    ap.add_argument("--split-date", type=str, default=None, help="時系列ホールドアウトの分割日（例 2025-01-01）")
    ap.add_argument("--no-cache", action="store_true", help="ディスクキャッシュを使わない")
    ap.add_argument("--refresh-cache", action="store_true", help="キャッシュを無視して再計算し上書き")
    ap.add_argument("--no-patterns", action="store_true", help="型指標を除外（初回を高速化）")
    ap.add_argument("--online", action="store_true", help="yfinanceから再取得（既定はローカルCSVのみ）")
    ap.add_argument("--max-minutes", type=float, default=5.0, help="この時間を超えたら安全に中断（0で無効）")
    ap.add_argument("--progress-every", type=float, default=0.5, help="進捗表示の更新間隔（秒）")
    return ap.parse_args()


def main():
    args = parse_args()
    args.offline = not args.online
    args.use_cache = not args.no_cache
    args.with_patterns = not args.no_patterns
    if args.indicator:
        args.indicator = [s.strip() for s in args.indicator.split(",") if s.strip()]
    run(args)


if __name__ == "__main__":
    main()
