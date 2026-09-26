"""
weekly_review.py
週次スイングトレード戦略の「答え合わせ」スクリプト。

目的:
  日々スクリーナー（main8.py）が提示した推奨と技術スコア上位を、
  1週間（月〜金）単位で実際の値動きと突き合わせて採点する。
  「どの銘柄が、どういう状況で、どう上がって／下がって、どれだけ
  よかったか／悪かったか」を人間にわかりやすく提示する。

評価ルール（ユーザー合意済み）:
  対象     : AI推奨(recommend) と 技術スコア上位 の両方
  約定     : entry_plan 通り（breakout は翌日以降の到達、pullback は
             entry_zone_high への押し目到達。未到達は「見送り」）
  手仕舞い : 約定日を含む週の金曜 11:30（前場引け）に成行
  コスト   : 手数料 0.05% ＋ スリッページ 0.1%（既存バックテストと同率）
  参考指標 : アプリのルール出口（TP/SL/最大保有日数）を適用した場合の結果も併記

入力:
  - docs/picks/{date}.json    日次ピックのスナップショット（main8.py が保存）
  - docs/history/{date}.json  全銘柄レコード（スナップショットが無い日の復元用）
  - docs/ai_analysis/{date}.json  AI 応答キャッシュ（AI判定の復元用）
  - docs/strategy_params.json パラメータ（price_tiers / entry_guard / portfolio）

出力:
  - docs/weekly/{YYYY-Www}.json   週次の詳細結果
  - docs/weekly/latest.json       最新週（フロント用）
  - docs/weekly/index.json        週一覧（アーカイブ）
  - back_tester/results/weekly_review_{YYYY-Www}.md  人間向けレポート
"""

import argparse
import gzip
import json
import math
import os
import re
import sys
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import yfinance as yf

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _REPO_ROOT)  # リポジトリ直下（common/ と docs/）
from common.config import load_strategy_params  # noqa: E402
from common import features as F  # noqa: E402
from common.persona import PERSONA_JA  # noqa: E402

DOCS_DIR = os.path.join(_REPO_ROOT, "docs")
HISTORY_DIR = os.path.join(DOCS_DIR, "history")
PICKS_DIR = os.path.join(DOCS_DIR, "picks")
AI_ANALYSIS_DIR = os.path.join(DOCS_DIR, "ai_analysis")
RECOMMENDATIONS_PATH = os.path.join(DOCS_DIR, "recommendations.json")
AI_LATEST_PATH = os.path.join(DOCS_DIR, "ai_strategy_latest.json")
WEEKLY_DIR = os.path.join(DOCS_DIR, "weekly")
OUTPUT_DIR = os.path.join(_HERE, "results")
CACHE_DIR = os.path.join(_HERE, "data", "weekly_cache")

BENCH_TICKER = "1306.T"

# 過熱度の日本語ラベル
OVERHEAT_LABELS = {
    "low": "低",
    "moderate": "中",
    "strong": "強",
    "extreme": "極",
}
# エントリー方式の日本語ラベル
ENTRY_TYPE_LABELS = {
    "breakout_chase": "追いかけ",
    "pullback_wait": "押し目待ち",
    "probe_only": "打診のみ",
    "wait": "見送り",
}
VERDICT_LABELS = {
    "recommend": "推奨",
    "watch": "様子見",
    "neutral": "中立",
    "hold": "保有継続",
    "caution": "注意",
    "avoid": "回避",
    "sell": "売却",
    "technical_only": "スコア順",
    "technical_reconstructed": "スコア順(復元)",
    "ai_reconstructed": "AI(復元)",
}


# --------------------------------------------------------------------------- ユーティリティ
def _json_safe(obj):
    """NaN / inf を None に置換して JSON として安全にする。"""
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        obj = float(obj)
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    return obj


def _to_num(v):
    try:
        if v is None:
            return None
        f = float(v)
        if math.isnan(f):
            return None
        return f
    except (TypeError, ValueError):
        return None


def _ticker(code):
    code = str(code)
    return code if "." in code else f"{code}.T"


def _iso_week_label(d):
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def _parse_iso_week(label):
    y, w = label.split("-W")
    monday = datetime.fromisocalendar(int(y), int(w), 1).date()
    return monday


def _week_dates(monday):
    return [monday + timedelta(days=i) for i in range(5)]


def _friday_of(d):
    return d + timedelta(days=(4 - d.weekday()))


def _clean(x):
    """数値を丸めるユーティリティ。"""
    x = _to_num(x)
    return round(x, 4) if x is not None else None


# --------------------------------------------------------------------------- データ読込
def _read_json_maybe_gz(base_path):
    """`<base>.json.gz` を優先し、無ければ `<base>.json` を読む（二重対応）。"""
    for p in (base_path + ".json.gz", base_path + ".json"):
        if os.path.exists(p):
            try:
                with open(p, "rb") as f:
                    raw = f.read()
                if p.endswith(".gz"):
                    raw = gzip.decompress(raw)
                return json.loads(raw.decode("utf-8"))
            except Exception:
                return None
    return None


def load_history(date_str):
    return _read_json_maybe_gz(os.path.join(HISTORY_DIR, date_str))


def load_snapshot(date_str):
    path = os.path.join(PICKS_DIR, f"{date_str}.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def load_ai_analysis(date_str):
    path = os.path.join(AI_ANALYSIS_DIR, f"{date_str}.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _tier_for(price, params):
    try:
        return F.tier_for_price(price, params["price_tiers"]) or {}
    except Exception:
        return {}


def reconstruct_from_history(date_str, params):
    """スナップショットが無い日について、history から技術上位ピックを復元する。

    AI 判定は ai_analysis があれば反映する（無ければ技術のみ）。
    """
    records = load_history(date_str)
    if not records:
        return None
    ai = load_ai_analysis(date_str)
    pool_max = params.get("ai", {}).get("stage1_pool_max", 40)
    limit = params.get("ai", {}).get("max_picks", 5)
    portfolio = params.get("portfolio", {})
    max_per_sector = portfolio.get("max_per_sector")

    eligible = [r for r in records if not r.get("excluded")]
    pool = sorted(eligible, key=lambda r: -r.get("score", 0))[:pool_max]

    ai_stocks = []
    ai_by_code = {}
    if isinstance(ai, dict):
        ai_stocks = ai.get("stocks") or []
    elif isinstance(ai, list):
        ai_stocks = [s for s in ai if isinstance(s, dict)]
    ai_by_code = {s.get("code"): s for s in ai_stocks if s.get("code")}

    ai_candidates = [r for r in pool
                     if (ai_by_code.get(r["code"]) or {}).get("verdict") == "recommend"]
    if ai_candidates:
        candidates = ai_candidates
        source = "ai_reconstructed"
    else:
        candidates = pool
        source = "technical_reconstructed"

    selected = []
    sector_counts = {}
    for r in candidates:
        if len(selected) >= limit:
            break
        sec = r.get("sector") or "その他"
        if max_per_sector and sector_counts.get(sec, 0) >= max_per_sector:
            continue
        selected.append(r)
        if max_per_sector:
            sector_counts[sec] = sector_counts.get(sec, 0) + 1

    picks = []
    for r in selected:
        item = ai_by_code.get(r["code"]) or {}
        tier = _tier_for(r.get("price") or 0, params)
        atr_mult = tier.get("atr_sl_mult", 2.0)
        ctx = r.get("ctx") or {}
        plan = F.compute_entry_plan(ctx, params)
        entry_type = item.get("entry_type") or plan["suggested_entry_type"]
        entry_price = _to_num(item.get("entry_price")) or plan["entry_price"]
        price = r.get("price")
        atr14 = r.get("atr14")
        tp_price = int(round(price * (1 + tier.get("tp_pct", 0.12)))) if price else None
        if atr14:
            sl_price = int(round(price - atr_mult * atr14))
        else:
            sl_price = _to_num(item.get("sl_price"))
        picks.append({
            "code": r["code"],
            "name": r.get("name"),
            "sector": r.get("sector"),
            "price": price,
            "score": r.get("score"),
            "verdict": item.get("verdict") or "technical_only",
            "rank": item.get("rank"),
            "entry_type": entry_type,
            "entry_type_label": ENTRY_TYPE_LABELS.get(entry_type, entry_type),
            "entry_price": entry_price,
            "entry_zone_low": plan["entry_zone_low"],
            "entry_zone_high": plan["entry_zone_high"],
            "entry_wait_days": plan.get("wait_days"),
            "overheat_level": plan["overheat_level"],
            "overheat_label": OVERHEAT_LABELS.get(plan["overheat_level"], plan["overheat_level"]),
            "rsi14": ctx.get("rsi14"),
            "dist_sma25_pct": ctx.get("dist_sma25_pct"),
            "atr14": atr14,
            "tp_price": tp_price,
            "sl_price": sl_price,
            "atr_sl_mult": atr_mult,
            "max_hold_days": tier.get("max_hold_days", 14),
            "entry_strategy": item.get("entry_strategy"),
            "reason": item.get("reason"),
            "news_note": item.get("news_note"),
            "reconstructed": True,
        })

    return {
        "date": date_str,
        "source": source,
        "picks": picks,
        "pool": [
            {
                "code": r["code"],
                "name": r.get("name"),
                "sector": r.get("sector"),
                "score": r.get("score"),
                "price": r.get("price"),
            }
            for r in pool
        ],
        "ai_stocks": ai_stocks,
        "reconstructed": True,
    }


def load_week_snapshots(week_dates, params):
    """その週の各日のピック情報を（スナップショット優先で）読み込む。"""
    out = {}
    for d in week_dates:
        ds = d.strftime("%Y-%m-%d")
        snap = load_snapshot(ds)
        if snap and (snap.get("picks") is not None):
            out[ds] = snap
        else:
            rec = reconstruct_from_history(ds, params)
            if rec:
                out[ds] = rec
    return out


# --------------------------------------------------------------------------- 価格取得
def _extract(df, ticker, intraday=False):
    if df is None or len(df) == 0:
        return None
    try:
        if isinstance(df.columns, pd.MultiIndex):
            lvl0 = df.columns.get_level_values(0)
            lvl1 = df.columns.get_level_values(1)
            if ticker in set(lvl0):
                sub = df[ticker].copy()
            elif ticker in set(lvl1):
                sub = df.xs(ticker, axis=1, level=1).copy()
            else:
                return None
        else:
            sub = df.copy()
        sub = sub.dropna(how="all")
        if sub.empty:
            return None
        idx = pd.to_datetime(sub.index)
        if idx.tz is not None:
            idx = idx.tz_convert("Asia/Tokyo").tz_localize(None)
        sub.index = idx
        if not intraday:
            sub.index = sub.index.normalize()
        return sub
    except Exception:
        return None


def _cache_path(kind, ticker):
    d = os.path.join(CACHE_DIR, kind)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{ticker}.csv")


def _read_cache(kind, ticker, need_start=None, need_end=None):
    path = _cache_path(kind, ticker)
    if not os.path.exists(path):
        return None
    try:
        df = pd.read_csv(path, index_col=0, parse_dates=True)
        if df.empty:
            return None
        first = pd.to_datetime(df.index.min()).date()
        last = pd.to_datetime(df.index.max()).date()
        # 末尾が古い
        if need_end is not None and last < need_end:
            return None
        # 先頭が遅く、必要な期間の前半が欠けている
        if need_start is not None and first > need_start:
            return None
        return df
    except Exception:
        return None


def _write_cache(kind, ticker, df):
    try:
        df.to_csv(_cache_path(kind, ticker))
    except Exception:
        pass


def fetch_daily(tickers, start, end, no_fetch=False):
    """日足を取得（ticker -> DataFrame）。ディスクキャッシュ併用。"""
    result = {}
    missing = []
    start_d = datetime.strptime(start, "%Y-%m-%d").date()
    end_d = datetime.strptime(end, "%Y-%m-%d").date()
    expected_end = min(end_d, datetime.now().date())
    for t in tickers:
        df = _read_cache("daily", t, need_start=start_d + timedelta(days=4),
                         need_end=expected_end - timedelta(days=5))
        if df is not None:
            result[t] = df
        else:
            missing.append(t)
    if missing and not no_fetch:
        print(f">> 日足を取得: {len(missing)} 銘柄 ({start}〜{end})")
        try:
            batch = yf.download(missing, start=start, end=end, auto_adjust=True,
                                group_by="ticker", progress=False, threads=True)
        except Exception as e:
            print(f">> ⚠️ 日足取得失敗: {e}")
            batch = None
        for t in missing:
            sub = _extract(batch, t) if batch is not None else None
            if sub is not None:
                _write_cache("daily", t, sub)
                result[t] = sub
    return result


def fetch_intraday(tickers, period="60d", no_fetch=False, need_start=None, need_end=None):
    """30分足を取得（金曜11:30の価格用）。失敗時は空。"""
    result = {}
    missing = []
    for t in tickers:
        df = _read_cache("intraday", t, need_start=need_start, need_end=need_end)
        if df is not None:
            result[t] = df
        else:
            missing.append(t)
    if missing and not no_fetch:
        print(f">> 30分足を取得: {len(missing)} 銘柄")
        try:
            batch = yf.download(missing, period=period, interval="30m", auto_adjust=True,
                                group_by="ticker", progress=False, threads=True)
        except Exception as e:
            print(f">> ⚠️ 30分足取得失敗（日足近似へフォールバック）: {e}")
            batch = None
        for t in missing:
            sub = _extract(batch, t, intraday=True) if batch is not None else None
            if sub is not None:
                _write_cache("intraday", t, sub)
                result[t] = sub
    return result


# --------------------------------------------------------------------------- 株価アクセス補助
def _row_on(df, date):
    """指定日の行を返す（無ければ None）。"""
    if df is None or len(df) == 0:
        return None
    key = pd.Timestamp(date)
    try:
        if key in df.index:
            return df.loc[key]
    except Exception:
        pass
    return None


def _morning_close(intr, date):
    """指定日の前場引け（11:30 まで）の終値を返す。取れなければ None。"""
    if intr is None or len(intr) == 0:
        return None
    try:
        day = intr[intr.index.normalize() == pd.Timestamp(date)]
        if day.empty:
            return None
        hm = day.index.hour * 60 + day.index.minute
        morning = day[(hm >= 9 * 60) & (hm <= 11 * 60 + 30)]
        if morning.empty:
            return None
        return float(morning["Close"].iloc[-1])
    except Exception:
        return None


def _next_trading_day(dates, d):
    for x in dates:
        if x > d:
            return x
    return None


def _exit_morning(daily, intraday, ticker, exit_date):
    """指定日の前場（11:30）出口価格を返す。(price, source)

    30分足が取れれば前場引け終値、無ければ日足の(始値+終値)/2で近似。
    """
    price = _morning_close((intraday or {}).get(ticker), exit_date)
    if price is not None:
        return price, "intraday_1130"
    row = _row_on((daily or {}).get(ticker), exit_date)
    if row is not None:
        op = _to_num(row.get("Open"))
        cl = _to_num(row.get("Close"))
        if op is not None and cl is not None:
            return (op + cl) / 2.0, "daily_proxy"
        if cl is not None:
            return cl, "daily_close"
    return None, None


# --------------------------------------------------------------------------- シミュレーション
def _entry_fill(pick, df, signal_date):
    """entry_plan に従って約定を判定する。

    戻り値: (fill_date, fill_price, status)
      status: "filled" / "not_filled" / "no_data" / "wait"
    """
    if df is None or len(df) == 0:
        return None, None, "no_data"
    dates = list(df.index.normalize())
    signal_ts = pd.Timestamp(signal_date)
    future = [d for d in dates if d > signal_ts]
    if not future:
        return None, None, "no_data"

    etype = pick.get("entry_type") or "breakout_chase"
    if etype == "wait":
        return None, None, "wait"

    signal_close = _to_num(pick.get("price"))
    wait_days = int(pick.get("entry_wait_days") or 10)
    window = future[:max(1, wait_days)]

    if etype in ("pullback_wait", "probe_only"):
        # 押し目・打診は指値（下値到達で約定）。打診はより深い水準。
        limit = _to_num(pick.get("entry_price"))
        if limit is None:
            limit = _to_num(pick.get("entry_zone_high") if etype == "pullback_wait"
                            else pick.get("entry_zone_low"))
        if limit is None:
            return None, None, "no_data"
        for d in window:
            row = _row_on(df, d)
            if row is None:
                continue
            low = _to_num(row.get("Low"))
            op = _to_num(row.get("Open"))
            if low is not None and low <= limit:
                fill = min(op, limit) if op is not None else limit
                return d, fill, "filled"
        return None, None, "not_filled"

    # breakout_chase（現在値近辺なら翌日寄成、上抜けなら買いストップ）
    trigger = _to_num(pick.get("entry_price"))
    if trigger is None:
        d = window[0]
        row = _row_on(df, d)
        op = _to_num(row.get("Open")) if row is not None else None
        return (d, op, "filled") if op is not None else (None, None, "no_data")

    if signal_close is not None and trigger <= signal_close * 1.001:
        # 現在値近辺＝成行扱い（翌営業日寄り）
        d = window[0]
        row = _row_on(df, d)
        op = _to_num(row.get("Open")) if row is not None else None
        return (d, op, "filled") if op is not None else (None, None, "no_data")

    # 上抜け買いストップ
    for d in window:
        row = _row_on(df, d)
        if row is None:
            continue
        op = _to_num(row.get("Open"))
        high = _to_num(row.get("High"))
        if op is not None and op >= trigger:
            return d, op, "filled"
        if high is not None and high >= trigger:
            return d, trigger, "filled"
    return None, None, "not_filled"


def _market_counterfactual(daily, intraday, ticker, signal_date, fee_rate, slip):
    """「もし翌営業日の寄りで成行買いしていたら」の損益（見送りの機会損益）。

    約定しなかった（押し目未到達）銘柄について、追随買いの機会損失／回避を測る。
    戻り値: (return_net_pct, entry_date, exit_date) or (None, None, None)
    """
    df = (daily or {}).get(ticker)
    if df is None or len(df) == 0:
        return None, None, None
    dates = list(df.index.normalize())
    signal_ts = pd.Timestamp(signal_date)
    future = [d for d in dates if d > signal_ts]
    if not future:
        return None, None, None
    entry_date = future[0]
    row = _row_on(df, entry_date)
    entry = _to_num(row.get("Open")) if row is not None else None
    if not entry:
        return None, None, None
    target_fri = _friday_of(entry_date.date())
    cands = [d for d in dates if d.date() <= target_fri and d >= entry_date]
    exit_date = max(cands) if cands else entry_date
    exit_price, _src = _exit_morning(daily, intraday, ticker, exit_date)
    if not exit_price:
        return None, None, None
    net = _net_return(entry, exit_price, fee_rate, slip)
    if net is None:
        return None, None, None
    return net * 100.0, entry_date, exit_date


def _rule_exit(pick, df, entry_date, entry_price):
    """実行ルール（OCO）の出口: TP/SL 到達、未到達なら最大保有日数の引け。

    データが最大保有日数に届いていない場合は None（＝未確定）を返す。
    呼び出し側で「持ち越し（pending）」として扱い、翌週以降に再採点する。
    """
    tp = _to_num(pick.get("tp_price"))
    sl = _to_num(pick.get("sl_price"))
    max_hold = int(pick.get("max_hold_days") or 14)
    if df is None or len(df) == 0:
        return None
    dates = [d for d in df.index.normalize() if d >= pd.Timestamp(entry_date)]
    if not dates:
        return None
    hold_dates = dates[:max_hold]
    last_row = None
    for i, d in enumerate(hold_dates):
        row = _row_on(df, d)
        if row is None:
            continue
        last_row = row
        op = _to_num(row.get("Open"))
        high = _to_num(row.get("High"))
        low = _to_num(row.get("Low"))
        close = _to_num(row.get("Close"))
        # 同日両到達は損切り優先（既存バックテスト踏襲）
        if sl is not None:
            if op is not None and op <= sl:
                return {"reason": "損切り", "date": d.strftime("%Y-%m-%d"), "price": op}
            if low is not None and low <= sl:
                return {"reason": "損切り", "date": d.strftime("%Y-%m-%d"), "price": sl}
        if tp is not None:
            if op is not None and op >= tp:
                return {"reason": "利確", "date": d.strftime("%Y-%m-%d"), "price": op}
            if high is not None and high >= tp:
                return {"reason": "利確", "date": d.strftime("%Y-%m-%d"), "price": tp}
        # 最大保有日数まで到達し、TP/SL 未到達 → 期間終了の引けで決済
        if i == len(hold_dates) - 1 and len(hold_dates) >= max_hold and close is not None:
            return {"reason": "保有期限", "date": d.strftime("%Y-%m-%d"), "price": close}
    # データが最大保有日数に届いていない → 未確定（持ち越し）
    return None


def _net_return(fill, exit_price, fee_rate, slip):
    if fill is None or exit_price is None or fill <= 0:
        return None
    buy = fill * (1 + slip)
    buy_fee = buy * fee_rate
    cost = buy + buy_fee
    sell = exit_price * (1 - slip)
    sell_fee = sell * fee_rate
    return (sell - sell_fee - cost) / cost


def simulate_pick(pick, daily, intraday, signal_date, bench_daily, bench_intr, params):
    """1ピックを採点して結果 dict を返す。"""
    cfg = params.get("weekly_review", {})
    fee_rate = cfg.get("fee_rate", 0.0005)
    slip = cfg.get("slippage_rate", 0.001)

    code = pick.get("code")
    ticker = _ticker(code)
    df = daily.get(ticker)
    advice = pick.get("advice") or {}
    reason = pick.get("reason") or advice.get("reason")
    news_note = pick.get("news_note") or advice.get("news_note")

    result = {
        "code": code,
        "name": pick.get("name"),
        "sector": pick.get("sector"),
        "signal_date": signal_date,
        "source": pick.get("source"),
        "score": _to_num(pick.get("score")),
        "verdict": pick.get("verdict"),
        "verdict_label": VERDICT_LABELS.get(pick.get("verdict"), pick.get("verdict")),
        "rank": pick.get("rank"),
        "entry_type": pick.get("entry_type"),
        "entry_type_label": pick.get("entry_type_label") or ENTRY_TYPE_LABELS.get(pick.get("entry_type")),
        "overheat_level": pick.get("overheat_level"),
        "overheat_label": pick.get("overheat_label") or OVERHEAT_LABELS.get(pick.get("overheat_level")),
        "reconstructed": bool(pick.get("reconstructed")),
        "day_rank": pick.get("day_rank"),
        "day_pick_count": pick.get("day_pick_count"),
        "price": _to_num(pick.get("price")),
        "reason": reason,
        "news_note": news_note,
        "entry_plan": {
            "entry_type": pick.get("entry_type"),
            "entry_price": _to_num(pick.get("entry_price")),
            "entry_zone_low": _to_num(pick.get("entry_zone_low")),
            "entry_zone_high": _to_num(pick.get("entry_zone_high")),
            "wait_days": pick.get("entry_wait_days"),
        },
        "fill_date": None,
        "fill_price": None,
        "status": "no_data",
        "exit_oco": None,
        "exit_weekly": None,
        "exit_rule": None,
        "return_gross_pct": None,
        "return_net_pct": None,
        "return_rule_pct": None,
        "return_weekly_pct": None,
        "benchmark_pct": None,
        "excess_pct": None,
        "exit_price_source": None,
        "missed_return_pct": None,
        "missed_entry_date": None,
        "missed_exit_date": None,
    }

    fill_date, fill_price, status = _entry_fill(pick, df, signal_date)
    result["status"] = status
    if status != "filled" or fill_date is None or fill_price is None:
        # 見送り（押し目未到達など）は「成行追随した場合」の機会損益を記録する
        if status in ("not_filled", "wait"):
            mret, m_entry, m_exit = _market_counterfactual(
                daily, intraday, ticker, signal_date, fee_rate, slip)
            if mret is not None:
                result["missed_return_pct"] = _clean(mret)
                result["missed_entry_date"] = m_entry.strftime("%Y-%m-%d")
                result["missed_exit_date"] = m_exit.strftime("%Y-%m-%d")
        return result
    result["fill_date"] = fill_date.strftime("%Y-%m-%d")
    result["fill_price"] = _clean(fill_price)

    # --- 参考: 約定週の金曜11:30で手仕舞い（短期の参考値。主指標ではない） ---
    dates = list(df.index.normalize()) if df is not None else []
    target_fri = _friday_of(fill_date.date() if hasattr(fill_date, "date") else fill_date)
    weekly_cands = [d for d in dates if d.date() <= target_fri and d >= fill_date]
    weekly_exit_date = max(weekly_cands) if weekly_cands else fill_date
    w_price, w_src = _exit_morning(daily, intraday, ticker, weekly_exit_date)
    if w_price is not None:
        w_net = _net_return(fill_price, w_price, fee_rate, slip)
        result["exit_weekly"] = {
            "date": weekly_exit_date.strftime("%Y-%m-%d"),
            "price": _clean(w_price),
            "return_net_pct": _clean(w_net * 100 if w_net is not None else None),
            "source": w_src,
        }
        result["return_weekly_pct"] = _clean(w_net * 100 if w_net is not None else None)

    # --- 主ルール（実行ルール）: OCO（利確=指値／損切=逆指値）→ 未到達は最大保有日数の引け ---
    rule = _rule_exit(pick, df, fill_date, fill_price)
    if rule is None or rule.get("price") is None:
        # データが最大保有日数に届いていない → 未確定（翌週以降に再採点）
        result["status"] = "pending"
        return result
    o_net = _net_return(fill_price, rule["price"], fee_rate, slip)
    result["exit_oco"] = {
        "date": rule["date"],
        "price": _clean(rule["price"]),
        "reason": rule["reason"],
        "return_net_pct": _clean(o_net * 100 if o_net is not None else None),
    }
    result["exit_rule"] = result["exit_oco"]  # 後方互換
    result["return_net_pct"] = _clean(o_net * 100 if o_net is not None else None)
    result["return_rule_pct"] = result["return_net_pct"]
    if fill_price:
        result["return_gross_pct"] = _clean((rule["price"] - fill_price) / fill_price * 100)

    # --- ベンチマーク（約定日寄り→OCO決済日の終値） ---
    bdf = (bench_daily or {}).get(BENCH_TICKER)
    b_entry_row = _row_on(bdf, fill_date)
    b_entry = _to_num(b_entry_row.get("Open")) if b_entry_row is not None else None
    b_exit_row = _row_on(bdf, pd.Timestamp(rule["date"]))
    b_exit = None
    if b_exit_row is not None:
        b_exit = _to_num(b_exit_row.get("Close"))
        if b_exit is None:
            b_exit = _to_num(b_exit_row.get("Open"))
    if b_entry and b_exit:
        b_net = _net_return(b_entry, b_exit, fee_rate, slip)
        result["benchmark_pct"] = _clean(b_net * 100 if b_net is not None else None)
        if result["return_net_pct"] is not None and b_net is not None:
            result["excess_pct"] = _clean(result["return_net_pct"] - b_net * 100)

    return result


# --------------------------------------------------------------------------- 分析
def _stats(values):
    vals = [v for v in values if v is not None]
    if not vals:
        return {"n": 0, "avg": None, "median": None, "win_rate": None}
    wins = sum(1 for v in vals if v > 0)
    return {
        "n": len(vals),
        "avg": round(float(np.mean(vals)), 3),
        "median": round(float(np.median(vals)), 3),
        "win_rate": round(wins / len(vals), 3),
    }


def _spearman(xs, ys):
    pairs = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    if len(pairs) < 3:
        return None
    x = pd.Series([p[0] for p in pairs]).rank()
    y = pd.Series([p[1] for p in pairs]).rank()
    if x.std() == 0 or y.std() == 0:
        return None
    return round(float(np.corrcoef(x, y)[0, 1]), 3)


def analyze_ranking(pool_by_date, daily, intraday, bench_daily, params):
    """技術スコア上位の「順位−翌営業日寄り→週内金曜11:30リターン」を検証。

    スコアは満点（100）で並ぶことがあるため、上位Kではなく候補プール全件を使う。
    """
    cfg = params.get("weekly_review", {})
    fee_rate = cfg.get("fee_rate", 0.0005)
    slip = cfg.get("slippage_rate", 0.001)

    rows = []
    for ds, snap in sorted(pool_by_date.items()):
        pool = sorted(snap.get("pool") or [], key=lambda r: -(r.get("score") or 0))
        for r in pool:
            ticker = _ticker(r["code"])
            df = daily.get(ticker)
            if df is None:
                continue
            dates = list(df.index.normalize())
            nxt = _next_trading_day(dates, pd.Timestamp(ds))
            if nxt is None:
                continue
            row = _row_on(df, nxt)
            op = _to_num(row.get("Open")) if row is not None else None
            if not op:
                continue
            target_fri = _friday_of(nxt.date())
            cands = [d for d in dates if d.date() <= target_fri and d >= nxt]
            ex_date = max(cands) if cands else nxt
            morning, _src = _exit_morning(daily, intraday, ticker, ex_date)
            if not morning:
                continue
            net = _net_return(op, morning, fee_rate, slip)
            bdf = bench_daily.get(BENCH_TICKER)
            b_entry = _to_num(_row_on(bdf, nxt).get("Open")) if _row_on(bdf, nxt) is not None else None
            b_morning, _b = _exit_morning(bench_daily, intraday, BENCH_TICKER, ex_date)
            b_net = _net_return(b_entry, b_morning, fee_rate, slip) if (b_entry and b_morning) else None
            rows.append({
                "signal_date": ds,
                "code": r["code"],
                "name": r.get("name"),
                "sector": r.get("sector"),
                "score": _to_num(r.get("score")),
                "entry_date": nxt.strftime("%Y-%m-%d"),
                "entry_price": _clean(op),
                "exit_date": ex_date.strftime("%Y-%m-%d"),
                "exit_price": _clean(morning),
                "return_net_pct": _clean(net * 100 if net is not None else None),
                "benchmark_pct": _clean(b_net * 100 if b_net is not None else None),
                "excess_pct": _clean((net - b_net) * 100 if (net is not None and b_net is not None) else None),
            })

    scores = [r["score"] for r in rows]
    rets = [r["return_net_pct"] for r in rows]
    spread = None
    top_avg = bottom_avg = None
    if rows:
        ordered = sorted(rows, key=lambda r: -(r["score"] or 0))
        half = max(1, len(ordered) // 2)
        top = [r["return_net_pct"] for r in ordered[:half] if r["return_net_pct"] is not None]
        bot = [r["return_net_pct"] for r in ordered[-half:] if r["return_net_pct"] is not None]
        top_avg = round(float(np.mean(top)), 3) if top else None
        bottom_avg = round(float(np.mean(bot)), 3) if bot else None
        if top_avg is not None and bottom_avg is not None:
            spread = round(top_avg - bottom_avg, 3)

    return {
        "k": None,
        "n": len(rows),
        "spearman": _spearman(scores, rets),
        "top_avg_pct": top_avg,
        "bottom_avg_pct": bottom_avg,
        "spread_pct": spread,
        "rows": rows,
    }


def analyze_ai_calibration(pool_by_date, daily, intraday, bench_daily, params):
    """AI判定（recommend/watch/neutral…）別の翌日寄り→週内金曜リターン平均。"""
    cfg = params.get("weekly_review", {})
    fee_rate = cfg.get("fee_rate", 0.0005)
    slip = cfg.get("slippage_rate", 0.001)
    by_verdict = {}
    rows = []
    for ds, snap in sorted(pool_by_date.items()):
        ai_stocks = snap.get("ai_stocks") or []
        price_by_code = {r["code"]: r.get("price") for r in (snap.get("pool") or [])}
        for s in ai_stocks:
            code = s.get("code")
            if not code:
                continue
            ticker = _ticker(code)
            df = daily.get(ticker)
            if df is None:
                continue
            dates = list(df.index.normalize())
            nxt = _next_trading_day(dates, pd.Timestamp(ds))
            if nxt is None:
                continue
            row = _row_on(df, nxt)
            op = _to_num(row.get("Open")) if row is not None else None
            if not op:
                continue
            target_fri = _friday_of(nxt.date())
            cands = [d for d in dates if d.date() <= target_fri and d >= nxt]
            ex_date = max(cands) if cands else nxt
            morning, _src = _exit_morning(daily, intraday, ticker, ex_date)
            if not morning:
                continue
            net = _net_return(op, morning, fee_rate, slip)
            if net is None:
                continue
            verdict = s.get("verdict") or "unknown"
            by_verdict.setdefault(verdict, []).append(net * 100)
            rows.append({
                "signal_date": ds, "code": code,
                "verdict": verdict,
                "rank": s.get("rank"),
                "return_net_pct": _clean(net * 100),
            })
    summary = {v: _stats(vals) for v, vals in by_verdict.items()}
    rec = summary.get("recommend", {}).get("avg")
    watch = summary.get("watch", {}).get("avg")
    delta = round(rec - watch, 3) if (rec is not None and watch is not None) else None
    return {"by_verdict": summary, "recommend_minus_watch_pct": delta, "rows": rows}


def _breakdown(trades, key, label_map=None):
    groups = {}
    for t in trades:
        if t.get("status") != "filled":
            continue
        v = t.get(key)
        if v is None:
            v = "unknown"
        label = (label_map or {}).get(v, v) if label_map else v
        groups.setdefault(label, []).append(t.get("return_net_pct"))
    out = {}
    for g, vals in groups.items():
        out[g] = _stats(vals)
    return out


def analyze_top_n(trades, ns):
    """その日の推奨上位N件だけを買った場合の成績（実行可能性を踏まえた検証）。"""
    out = {}
    for n in ns:
        sel = [t for t in trades if (t.get("day_rank") or 99) <= n]
        filled = [t for t in sel if t.get("status") == "filled"]
        not_filled = [t for t in sel if t.get("status") == "not_filled"]
        pending = [t for t in sel if t.get("status") == "pending"]
        evaluated = len(filled) + len(not_filled)
        rets = [t["return_net_pct"] for t in filled if t.get("return_net_pct") is not None]
        excess = [t["excess_pct"] for t in filled if t.get("excess_pct") is not None]
        st = _stats(rets)
        best = max(filled, key=lambda t: t["return_net_pct"]) if filled else None
        worst = min(filled, key=lambda t: t["return_net_pct"]) if filled else None
        out[str(n)] = {
            "n_per_day": n,
            "picks": len(sel),
            "n_filled": len(filled),
            "n_pending": len(pending),
            "fill_rate": round(len(filled) / evaluated, 3) if evaluated else None,
            "avg_return_pct": st["avg"],
            "median_return_pct": st["median"],
            "win_rate": st["win_rate"],
            "avg_excess_pct": round(float(np.mean(excess)), 3) if excess else None,
            "best": None if not best else {"code": best["code"], "name": best["name"], "return_net_pct": best["return_net_pct"]},
            "worst": None if not worst else {"code": worst["code"], "name": worst["name"], "return_net_pct": worst["return_net_pct"]},
        }
    return out


def build_notes(summary, ranking, calib, breakdown):
    notes = []
    # 未確定（OCOの最大保有日数にデータが届かず来週以降に確定）
    n_pending = summary.get("n_pending") or 0
    if n_pending:
        notes.append(f"未確定（データ不足で来週確定）が {n_pending}件あります。翌週のレビューで確定します。")
    # 見送り（押し目未到達など）の機会損益
    n_missed = summary.get("n_missed_evaluated") or 0
    if n_missed > 0:
        m_avg = summary.get("missed_avg_pct")
        up = summary.get("missed_up_count") or 0
        down = summary.get("missed_down_count") or 0
        m_up = summary.get("missed_up_avg_pct")
        m_dn = summary.get("missed_down_avg_pct")
        if m_avg is not None:
            tag = "機会損失" if m_avg > 0 else "回避できた"
            notes.append(
                f"見送り {n_missed}件を成行追随していたら平均 {m_avg:+.2f}%（{tag}）。"
                f"うち上昇 {up}件（平均 {(m_up or 0.0):+.2f}%）/ 下落 {down}件（平均 {(m_dn or 0.0):+.2f}%）。"
            )
    if summary.get("n_filled", 0) == 0:
        if (summary.get("n_pending") or 0) > 0:
            notes.append("OCO決済が未確定です（最大保有日数に未到達）。翌週以降のレビューで確定します。")
        elif (summary.get("n_entered") or 0) > 0:
            notes.append("建玉はありますが、まだ確定した決済がありません。")
        else:
            notes.append("今週は約定した推奨がありませんでした（押し目・ブレイク未到達、または相場急変）。")
        return notes
    avg = summary.get("avg_return_pct")
    bench = summary.get("avg_benchmark_pct")
    if avg is not None and bench is not None:
        if avg > bench:
            notes.append(f"推奨の平均リターンは {avg:+.2f}% で、TOPIX（{bench:+.2f}%）を上回りました。")
        else:
            notes.append(f"推奨の平均リターンは {avg:+.2f}% で、TOPIX（{bench:+.2f}%）を下回りました。")
    sp = ranking.get("spearman")
    if sp is not None:
        if sp >= 0.2:
            notes.append(f"スコア上位ほどよく上がる関係が確認できました（順位相関 {sp:+.2f}）。")
        elif sp <= -0.2:
            notes.append(f"スコア上位ほど弱い逆相関でした（順位相関 {sp:+.2f}）。ランキングの見直し候補です。")
        else:
            notes.append(f"スコアとリターンの関係はほぼ中立でした（順位相関 {sp:+.2f}）。")
    delta = calib.get("recommend_minus_watch_pct")
    if delta is not None:
        if delta > 0:
            notes.append(f"AIの「推奨」は「様子見」を平均 {delta:+.2f}% 上回りました。")
        else:
            notes.append(f"AIの「推奨」は「様子見」に平均 {abs(delta):.2f}% 劣後しました。")
    et = breakdown.get("by_entry_type", {})
    for key in ("追いかけ", "押し目待ち", "打診のみ"):
        st = et.get(key)
        if st and st.get("n", 0) > 0 and st.get("avg") is not None:
            notes.append(f"エントリー「{key}」は {st['n']}件・平均 {st['avg']:+.2f}%。")
    oh = breakdown.get("by_overheat", {})
    for key in ("低", "中", "強", "極"):
        st = oh.get(key)
        if st and st.get("n", 0) > 0 and st.get("avg") is not None:
            notes.append(f"過熱度「{key}」は {st['n']}件・平均 {st['avg']:+.2f}%。")
    return notes


# --------------------------------------------------------------------------- フィードバック（結果をランキングへ還元）
def compute_weekly_feedback(params):
    """直近N週の実績から、過熱度別の「スコア補正量」を計算する（縮小推定つき）。

    - baseline からの超過リターンを、サンプル数で縮小（n/(n+k)）して安定化。
    - グループの取引数が min_group_trades 未満なら補正しない（0）。
    - 補正量は ±max_delta にクランプし、小さなサンプルでの過学習を防ぐ。
    """
    import glob
    cfg = params.get("weekly_review", {})
    window = int(cfg.get("feedback_window_weeks", 6))
    min_weeks = int(cfg.get("feedback_min_weeks", 3))
    min_group = int(cfg.get("feedback_min_group_trades", 8))
    k = float(cfg.get("feedback_shrinkage_k", 10.0))
    max_delta = float(cfg.get("feedback_max_delta", 3.0))
    enabled = bool(cfg.get("feedback_enabled", False))

    weeks = []
    for path in glob.glob(os.path.join(WEEKLY_DIR, "*.json")):
        name = os.path.basename(path)
        if name in ("index.json", "latest.json", "feedback.json"):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                j = json.load(f)
            if j.get("week"):
                weeks.append(j)
        except Exception:
            continue
    weeks.sort(key=lambda j: j.get("week", ""), reverse=True)
    use = weeks[:window]

    filled = []
    for j in use:
        for t in j.get("trades") or []:
            if t.get("status") == "filled" and t.get("return_net_pct") is not None:
                filled.append(t)

    fb = {
        "enabled": False,
        "generated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "window_weeks": len(use),
        "weeks": [j.get("week") for j in use],
        "n_filled": len(filled),
        "baseline_avg_pct": None,
        "overheat_delta": {},
        "overheat_stats": {},
        "max_delta": max_delta,
        "shrinkage_k": k,
        "min_group_trades": min_group,
        "note": "",
    }
    if len(use) < min_weeks or len(filled) < min_group:
        fb["note"] = f"サンプル不足のため補正なし（週{len(use)}/{min_weeks}・取引{len(filled)}/{min_group}）"
        return fb

    baseline = float(np.mean([t["return_net_pct"] for t in filled]))
    groups = {}
    for t in filled:
        lvl = t.get("overheat_level") or "unknown"
        groups.setdefault(lvl, []).append(t["return_net_pct"])

    deltas, stats = {}, {}
    for lvl, vals in groups.items():
        nn = len(vals)
        m = float(np.mean(vals))
        stats[lvl] = {"n": nn, "avg": round(m, 3)}
        if nn >= min_group:
            shrink = nn / (nn + k)
            d = shrink * (m - baseline)
            deltas[lvl] = round(max(-max_delta, min(max_delta, d)), 2)

    fb["baseline_avg_pct"] = round(baseline, 3)
    fb["overheat_delta"] = deltas
    fb["overheat_stats"] = stats
    fb["enabled"] = bool(enabled and deltas)
    if not fb["enabled"]:
        fb["note"] = "feedback_enabled=false のため補正は適用しません（観測のみ）。"
    else:
        fb["note"] = "実績に基づき過熱度別のスコア補正を適用（縮小推定・上限±%.1f）" % max_delta
    return fb


def build_actions(fb):
    """フィードバックから「来週の作戦」を生成する。"""
    acts = []
    stats = fb.get("overheat_stats") or {}
    for lvl, d in (fb.get("overheat_delta") or {}).items():
        st = stats.get(lvl) or {}
        label = OVERHEAT_LABELS.get(lvl, lvl)
        neg = "加点" if d > 0 else "減点"
        acts.append(
            f"過熱度『{label}』（{st.get('n','?')}件・平均{st.get('avg')}%）は {neg} {d:+.2f}。"
        )
    if not acts:
        acts.append(fb.get("note") or "有効なサンプルが不足のため補正なし（観測継続）。")
    return acts


def write_feedback(fb):
    """フィードバックを docs/weekly/feedback.json と strategy_params に反映する。"""
    os.makedirs(WEEKLY_DIR, exist_ok=True)
    with open(os.path.join(WEEKLY_DIR, "feedback.json"), "w", encoding="utf-8") as f:
        json.dump(_json_safe(fb), f, ensure_ascii=False, indent=2)

    params_path = os.path.join(DOCS_DIR, "strategy_params.json")
    try:
        with open(params_path, "r", encoding="utf-8") as f:
            sp = json.load(f)
    except Exception:
        return
    # 手動のキーを壊さないよう、weekly_feedback だけを更新
    try:
        sp["weekly_feedback"] = _json_safe(fb)
        with open(params_path, "w", encoding="utf-8") as f:
            json.dump(sp, f, ensure_ascii=False, indent=2)
        print(f">> weekly_feedback を更新: enabled={fb.get('enabled')} deltas={fb.get('overheat_delta')}")
    except Exception as e:
        print(f">> ⚠️ weekly_feedback の反映に失敗: {e}")


# --------------------------------------------------------------------------- 来週の作戦（AI深掘り）
def _next_week_label(based_date):
    try:
        d = datetime.strptime(based_date, "%Y-%m-%d").date()
    except Exception:
        return None
    monday = d - timedelta(days=d.weekday())
    return _iso_week_label(monday + timedelta(days=7))


def _load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _cand_from_pick(p, hist):
    h = hist or {}
    ctx = h.get("ctx") or {}
    return {
        "code": p.get("code"),
        "name": p.get("name") or h.get("name"),
        "sector": p.get("sector") or h.get("sector"),
        "price": _to_num(p.get("price")) or _to_num(h.get("price")),
        "score": _to_num(p.get("score")) or _to_num(h.get("score")),
        "verdict": p.get("verdict"),
        "entry_type": p.get("entry_type"),
        "entry_type_label": p.get("entry_type_label"),
        "entry_price": _to_num(p.get("entry_price")),
        "entry_zone_low": _to_num(p.get("entry_zone_low")),
        "entry_zone_high": _to_num(p.get("entry_zone_high")),
        "overheat_label": p.get("overheat_label"),
        "rsi14": _to_num(p.get("rsi14")),
        "dist_sma25_pct": _to_num(p.get("dist_sma25_pct")),
        "atr_pct": _to_num(p.get("atr_pct")),
        "pos_52w": _to_num(p.get("pos_52w")),
        "tp_price": _to_num(p.get("tp_price")),
        "sl_price": _to_num(p.get("sl_price")),
        "fundamentals": p.get("fundamentals"),
        "earnings_date": p.get("earnings_date"),
        "news_note": p.get("news_note"),
        "news_headlines": p.get("news_headlines") or [],
        "reason": p.get("reason") or (p.get("advice") or {}).get("reason"),
    }


def _load_latest_candidates(params, limit):
    """金曜時点の最新候補を返す（AI推奨 → スコア上位プールの順、重複除去）。"""
    rec = _load_json(RECOMMENDATIONS_PATH)
    hist = _read_json_maybe_gz(os.path.join(HISTORY_DIR, "latest")) or []
    hist_by_code = {r.get("code"): r for r in hist if r.get("code")}
    based = (rec or {}).get("date")
    if not based:
        meta = _load_json(os.path.join(HISTORY_DIR, "meta.json")) or {}
        based = meta.get("date")

    cands = []
    seen = set()
    for p in (rec or {}).get("picks") or []:
        code = p.get("code")
        if not code or code in seen:
            continue
        cands.append(_cand_from_pick(p, hist_by_code.get(code)))
        seen.add(code)

    pool = sorted([r for r in hist if not r.get("excluded")], key=lambda r: -(r.get("score") or 0))
    for r in pool:
        if len(cands) >= limit:
            break
        code = r.get("code")
        if not code or code in seen:
            continue
        ctx = r.get("ctx") or {}
        cands.append({
            "code": code, "name": r.get("name"), "sector": r.get("sector"),
            "price": _to_num(r.get("price")), "score": _to_num(r.get("score")),
            "verdict": None, "entry_type": None, "entry_type_label": None,
            "entry_price": None, "entry_zone_low": None, "entry_zone_high": None,
            "overheat_label": None,
            "rsi14": _to_num(ctx.get("rsi14")), "dist_sma25_pct": _to_num(ctx.get("dist_sma25_pct")),
            "atr_pct": _to_num(ctx.get("atr_pct")), "pos_52w": _to_num(ctx.get("pos_52w")),
            "tp_price": None, "sl_price": None,
            "fundamentals": {"per": r.get("per"), "pbr": r.get("pbr"), "roe": r.get("roe"),
                             "op_margin": r.get("op_margin"), "div_yield": r.get("div_yield")},
            "earnings_date": r.get("earnings_date"), "news_note": None, "news_headlines": [],
            "reason": None,
        })
        seen.add(code)
    return based, cands[:limit]


def _plan_candidate_lines(cands):
    lines = []
    for c in cands:
        fund = c.get("fundamentals") or {}
        parts = [
            f"{c['code']} {c.get('name')}",
            f"業種:{c.get('sector')}",
            f"株価:{c.get('price')}円 スコア:{c.get('score')}",
            f"AI判定:{c.get('verdict')} 入口:{c.get('entry_type_label') or c.get('entry_type') or '-'}",
            f"入口価格:{c.get('entry_price')}(押し目候補{c.get('entry_zone_low')}〜{c.get('entry_zone_high')})",
            f"過熱:{c.get('overheat_label')} RSI:{c.get('rsi14')} 25日乖離:{c.get('dist_sma25_pct')}% ATR比:{c.get('atr_pct')}% 52週位置:{c.get('pos_52w')}",
            f"PER:{fund.get('per')} PBR:{fund.get('pbr')} ROE:{fund.get('roe')} 配当:{fund.get('div_yield')}",
            f"利確候補:{c.get('tp_price')} 損切候補:{c.get('sl_price')}",
        ]
        if c.get("earnings_date"):
            parts.append(f"決算:{c['earnings_date']}")
        if c.get("reason"):
            parts.append(f"AI理由:{c['reason']}")
        if c.get("news_note"):
            parts.append(f"材料:{c['news_note']}")
        lines.append(" | ".join(str(x) for x in parts))
    return "\n".join(lines)


def _plan_system_prompt():
    return (
        PERSONA_JA + "\n"
        "あなたは日本株の週次スイング戦略の責任者である。金曜大引け後の最新候補と、直近の答え合わせ結果を踏まえて"
        "『来週の作戦』を練る。毎日のAI順位より踏み込み、候補を深掘りして『来週一番のおすすめ（top_pick）』を1つ選び、"
        "入口・OCO（利確指値/損切逆指値）・想定シナリオ・リスクを具体的に示す。"
        "イナゴ買い・高値掴みの防止を最優先し、過熱が強い銘柄は追いかけず押し目・打診に留める。"
        "答え合わせ結果には必ずしも従わなくてよいが、同じ失敗を繰り返さない工夫を示す。"
        "回答は必ず次のJSONのみ（Markdown/コードフェンスなし）:"
        '{"market_view":"来週の地合い・テーマの見立て(2〜3文)",'
        '"top_pick":{"code":"候補一覧の実コード","name":"銘柄名","reason":"なぜ一番か","entry_strategy":"買い方(寄成/押し目指値と価格)",'
        '"entry_price":数値,"tp_price":数値,"sl_price":数値,"scenario":"想定シナリオ","confidence":"高/中/低"},'
        '"backups":[{"code":"実コード","name":"銘柄名","reason":"補欠理由","entry_strategy":"買い方","entry_price":数値}],'
        '"avoid":[{"code":"実コード","reason":"避ける理由"}],'
        '"risk_notes":"リスク・注意(2〜3文)"}'
        " codeは必ず候補一覧に記載の実際のコードをコピーすること。reason 等は各80文字以内で簡潔に。"
        "confidence は 高/中/低 のいずれか。最終判断は人間が行う前提。"
    )


def build_next_week_plan(params, summary=None, feedback=None):
    """来週の作戦をAIで深掘り生成する。キー未設定/失敗時は None（呼び出し側でフォールバック）。"""
    cfg = params.get("weekly_review", {})
    if not cfg.get("plan_enabled", True):
        return None
    limit = int(cfg.get("plan_candidates", 15) or 15)
    based, cands = _load_latest_candidates(params, limit)
    if not cands:
        print(">> 来週の作戦: 候補が取得できないためスキップします。")
        return None

    plan_path = os.path.join(WEEKLY_DIR, "plan.json")
    cached = _load_json(plan_path)
    if cached and cached.get("based_on_date") == based and cached.get("plan"):
        print(f">> 来週の作戦: 既存プランを再利用（{based}）")
        return cached

    ai = params.get("ai", {})
    provider = ai.get("provider", "deepseek")
    if provider == "gemini":
        if not os.environ.get("GEMINI_API_KEY"):
            print(">> 来週の作戦: GEMINI_API_KEY 未設定のためスキップ（テンプレ作戦を表示）")
            return None
    elif not os.environ.get("DEEPSEEK_API_KEY"):
        print(">> 来週の作戦: DEEPSEEK_API_KEY 未設定のためスキップ（テンプレ作戦を表示）")
        return None

    ctx_lines = ["【直近の答え合わせ（参考）】"]
    if summary:
        ctx_lines.append(
            f"建玉{summary.get('n_entered')} / OCO確定{summary.get('n_filled')} / 未確定{summary.get('n_pending')} / 見送り{summary.get('n_not_filled')}"
        )
        ctx_lines.append(
            f"平均(OCO){summary.get('avg_return_pct')}% 勝率{summary.get('win_rate')} TOPIX超過{summary.get('avg_excess_pct')}% "
            f"（参考:金曜{summary.get('avg_weekly_return_pct')}%）"
        )
    if feedback:
        ctx_lines.append(f"過熱度別スコア補正: {feedback.get('overheat_delta')}")
    ctx_lines.append("")
    ctx_lines.append("【候補銘柄】")
    user = "\n".join(ctx_lines) + "\n" + _plan_candidate_lines(cands)

    try:
        from main8 import _call_deepseek, _call_gemini  # 遅延import（重い依存を避ける）
    except Exception as e:
        print(f">> 来週の作戦: AIモジュールの読み込みに失敗: {e}")
        return None

    print(f">> 来週の作戦: AIで生成中（候補 {len(cands)} 銘柄 / provider={provider}）")
    system = _plan_system_prompt()
    res = _call_gemini(user, system, params) if provider == "gemini" else _call_deepseek(user, system, params)
    if not res or not isinstance(res, dict):
        print(">> 来週の作戦: AI応答が得られませんでした（テンプレ作戦を表示）")
        return None

    plan = {
        "generated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "week": _next_week_label(based),
        "based_on_date": based,
        "provider": provider,
        "model": ai.get("model"),
        "candidates": [{"code": c["code"], "name": c.get("name"), "score": c.get("score"),
                        "verdict": c.get("verdict")} for c in cands],
        "plan": _json_safe(res),
    }
    return plan


def write_plan(plan):
    if not plan:
        return
    os.makedirs(WEEKLY_DIR, exist_ok=True)
    with open(os.path.join(WEEKLY_DIR, "plan.json"), "w", encoding="utf-8") as f:
        json.dump(_json_safe(plan), f, ensure_ascii=False, indent=2)
    print(f">> 来週の作戦を保存: docs/weekly/plan.json（{plan.get('week')}）")


# --------------------------------------------------------------------------- 本体
def review_week(monday, params, no_fetch=False):
    week_dates = _week_dates(monday)
    week_label = _iso_week_label(monday)
    start = week_dates[0].strftime("%Y-%m-%d")
    end = week_dates[-1].strftime("%Y-%m-%d")
    print(f"\n===== 週次レビュー {week_label} ({start} 〜 {end}) =====")

    pool_by_date = load_week_snapshots(week_dates, params)
    if not pool_by_date:
        print(">> 対象週のデータがありません（history / picks 未生成）。")
        return None

    # 取得対象の銘柄を集約
    codes = set()
    for snap in pool_by_date.values():
        for p in snap.get("picks") or []:
            codes.add(str(p.get("code")))
        for r in snap.get("pool") or []:
            codes.add(str(r.get("code")))
        for s in snap.get("ai_stocks") or []:
            codes.add(str(s.get("code")))
    codes = {c for c in codes if c and c != "None"}
    tickers = {_ticker(c) for c in codes}
    tickers.add(BENCH_TICKER)

    # OCO の最大保有日数は週をまたぐため、その分の将来データも取得対象にする
    carryover = int(params.get("weekly_review", {}).get("carryover_weeks", 2) or 2)
    tiers = params.get("price_tiers") or []
    max_hold = max([int(t.get("max_hold_days") or 14) for t in tiers] or [14])
    horizon_days = 7 * (carryover + 1) + int(max_hold * 1.6) + 10
    fetch_start = (monday - timedelta(days=7)).strftime("%Y-%m-%d")
    fetch_end = (monday + timedelta(days=horizon_days)).strftime("%Y-%m-%d")
    need_start = monday - timedelta(days=3)
    need_end = min(monday + timedelta(days=horizon_days), datetime.now().date()) - timedelta(days=3)
    daily = fetch_daily(sorted(tickers), fetch_start, fetch_end, no_fetch=no_fetch)
    intraday = fetch_intraday(sorted(tickers), no_fetch=no_fetch,
                              need_start=need_start, need_end=need_end)

    # 非営業日（土日祝・取引所休場）のシグナルを除外する。
    # ベンチマークの実際の営業日を基準にするため、データに裏付けられた判定になる。
    bench_df = daily.get(BENCH_TICKER)
    if bench_df is not None and len(bench_df) > 0:
        bench_days = {pd.Timestamp(x).date() for x in bench_df.index}
        removed = []
        for ds in list(pool_by_date.keys()):
            try:
                dd = datetime.strptime(ds, "%Y-%m-%d").date()
            except Exception:
                continue
            if dd not in bench_days:
                removed.append(ds)
                pool_by_date.pop(ds)
        if removed:
            print(f">> 非営業日のシグナルを除外: {', '.join(sorted(removed))}")

    if not pool_by_date:
        print(">> 対象週に営業日のデータがありません（全休場など）。スキップします。")
        return None

    # 各ピックを採点（day_rank はその日の推奨順位＝買う優先順）
    trades = []
    for ds, snap in sorted(pool_by_date.items()):
        src = snap.get("source")
        picks = snap.get("picks") or []
        for rank_in_day, p in enumerate(picks, start=1):
            p = dict(p)
            p["source"] = src
            p["day_rank"] = rank_in_day
            p["day_pick_count"] = len(picks)
            trades.append(simulate_pick(p, daily, intraday, ds, daily, intraday, params))

    filled = [t for t in trades if t["status"] == "filled"]
    not_filled = [t for t in trades if t["status"] == "not_filled"]
    pending = [t for t in trades if t["status"] == "pending"]
    no_data = [t for t in trades if t["status"] in ("no_data", "wait")]
    entered = [t for t in trades if t.get("fill_date")]
    evaluated = len(filled) + len(not_filled)
    # 主指標 = OCO（実行ルール）リターン（確定分のみ）
    primary_rets = [t["return_net_pct"] for t in filled if t.get("return_net_pct") is not None]
    # 参考 = 金曜11:30手仕舞い（未確定分も金曜時点の値が取れれば含める）
    weekly_ref_rets = [t["return_weekly_pct"] for t in trades
                       if t.get("return_weekly_pct") is not None]
    bench_rets = [t["benchmark_pct"] for t in filled if t.get("benchmark_pct") is not None]
    excess = [t["excess_pct"] for t in filled if t.get("excess_pct") is not None]

    best = max(filled, key=lambda t: t["return_net_pct"]) if filled else None
    worst = min(filled, key=lambda t: t["return_net_pct"]) if filled else None

    # 見送り（押し目未到達など）を成行追随していた場合の機会損益
    missed_vals = [t["missed_return_pct"] for t in trades if t.get("missed_return_pct") is not None]
    missed_up = [v for v in missed_vals if v > 0]
    missed_down = [v for v in missed_vals if v <= 0]

    summary = {
        "picks_total": len(trades),
        "n_filled": len(filled),
        "n_not_filled": len(not_filled),
        "n_pending": len(pending),
        "n_no_data": len(no_data),
        "n_entered": len(entered),
        "fill_rate": round(len(filled) / evaluated, 3) if evaluated else None,
        "avg_return_pct": _stats(primary_rets)["avg"],
        "median_return_pct": _stats(primary_rets)["median"],
        "win_rate": _stats(primary_rets)["win_rate"],
        "avg_benchmark_pct": round(float(np.mean(bench_rets)), 3) if bench_rets else None,
        "avg_excess_pct": round(float(np.mean(excess)), 3) if excess else None,
        "beat_benchmark_rate": round(sum(1 for e in excess if e > 0) / len(excess), 3) if excess else None,
        "rule_avg_return_pct": _stats(primary_rets)["avg"],
        "rule_win_rate": _stats(primary_rets)["win_rate"],
        "avg_weekly_return_pct": _stats(weekly_ref_rets)["avg"],
        "weekly_win_rate": _stats(weekly_ref_rets)["win_rate"],
        "n_missed_evaluated": len(missed_vals),
        "missed_avg_pct": round(float(np.mean(missed_vals)), 3) if missed_vals else None,
        "missed_up_count": len(missed_up),
        "missed_down_count": len(missed_down),
        "missed_up_avg_pct": round(float(np.mean(missed_up)), 3) if missed_up else None,
        "missed_down_avg_pct": round(float(np.mean(missed_down)), 3) if missed_down else None,
        "best": None if not best else {
            "code": best["code"], "name": best["name"], "return_net_pct": best["return_net_pct"],
        },
        "worst": None if not worst else {
            "code": worst["code"], "name": worst["name"], "return_net_pct": worst["return_net_pct"],
        },
    }

    ranking = analyze_ranking(pool_by_date, daily, intraday, daily, params)
    calib = analyze_ai_calibration(pool_by_date, daily, intraday, daily, params)
    top_n_ns = params.get("weekly_review", {}).get("top_n_review") or [3]
    try:
        top_n_ns = [int(x) for x in top_n_ns if int(x) > 0]
    except Exception:
        top_n_ns = [3]
    top_n = analyze_top_n(trades, top_n_ns)
    breakdown = {
        "by_entry_type": _breakdown(filled, "entry_type", ENTRY_TYPE_LABELS),
        "by_overheat": _breakdown(filled, "overheat_level", OVERHEAT_LABELS),
        "by_sector": _breakdown(filled, "sector"),
        "by_verdict": _breakdown(filled, "verdict", VERDICT_LABELS),
    }
    notes = build_notes(summary, ranking, calib, breakdown)

    result = {
        "week": week_label,
        "start": start,
        "end": end,
        "generated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "review_rule": {
            "entry": "entry_plan通り（翌営業日寄成 or 押し目指値。未到達は見送り）",
            "exit": "OCO（利確=指値／損切=逆指値）。同日両到達は損切優先。未到達は最大保有日数の引け",
            "exit_reference": "参考: 約定週の金曜11:30（前場引け）に成行（短期メトリクス）",
            "cost": f"手数料{params.get('weekly_review', {}).get('fee_rate', 0.0005)*100:.3f}%+スリッページ{params.get('weekly_review', {}).get('slippage_rate', 0.001)*100:.1f}%",
            "benchmark": "1306.T（TOPIX ETF。約定日寄り→OCO決済日終値）",
            "note": "OCO主指標は最大保有日数（7〜20営業日）で確定するため、直近週は未確定が多くなります（翌週以降に順次確定）。短期的な参考として金曜11:30手仕舞いを併記。機械的な答え合わせであり、特定日の売買を強制するものではありません。",
        },
        "summary": summary,
        "trades": trades,
        "ranking": ranking,
        "ai_calibration": calib,
        "top_n": top_n,
        "breakdown": breakdown,
        "notes": notes,
    }
    return _json_safe(result)


def write_outputs(result):
    os.makedirs(WEEKLY_DIR, exist_ok=True)
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    week = result["week"]

    with open(os.path.join(WEEKLY_DIR, f"{week}.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    with open(os.path.join(WEEKLY_DIR, "latest.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    index_path = os.path.join(WEEKLY_DIR, "index.json")
    weeks = []
    if os.path.exists(index_path):
        try:
            with open(index_path, "r", encoding="utf-8") as f:
                weeks = json.load(f)
        except Exception:
            weeks = []
    weeks = [w for w in weeks if w.get("week") != week]
    weeks.append({
        "week": week,
        "start": result["start"],
        "end": result["end"],
        "avg_return_pct": result["summary"].get("avg_return_pct"),
        "win_rate": result["summary"].get("win_rate"),
        "n_filled": result["summary"].get("n_filled"),
    })
    weeks.sort(key=lambda w: w.get("week", ""), reverse=True)
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(weeks, f, ensure_ascii=False, indent=2)

    md = render_markdown(result)
    with open(os.path.join(OUTPUT_DIR, f"weekly_review_{week}.md"), "w", encoding="utf-8") as f:
        f.write(md)

    print(f">> 出力: docs/weekly/{week}.json / latest.json / index.json")
    print(f">> レポート: back_tester/results/weekly_review_{week}.md")


def render_markdown(r):
    def m(v, suffix="%"):
        return "-" if v is None else f"{v}{suffix}"
    s = r["summary"]
    lines = [f"# 週次答え合わせレポート {r['week']}（{r['start']} 〜 {r['end']}）", ""]
    lines.append(f"※ ルール: {r['review_rule']['entry']} / {r['review_rule']['exit']}")
    lines.append(f"※ {r['review_rule'].get('exit_reference','')} / コスト {r['review_rule']['cost']} / ベンチマーク {r['review_rule'].get('benchmark','')}")
    lines.append("")
    lines.append("## サマリ（主指標＝OCO／参考＝金曜11:30）")
    lines.append("")
    lines.append("| 指標 | 値 |")
    lines.append("| :--- | ---: |")
    lines.append(f"| 対象ピック数 | {s['picks_total']} |")
    lines.append(f"| 建玉 / OCO確定 / 未確定 / 見送り | {s.get('n_entered',0)} / {s['n_filled']} / {s.get('n_pending', 0)} / {s['n_not_filled']}（約定率 {m(s['fill_rate'])}) |")
    lines.append(f"| 平均リターン（OCO） | {m(s['avg_return_pct'])} |")
    lines.append(f"| 中央値（OCO） | {m(s['median_return_pct'])} |")
    lines.append(f"| 勝率（OCO） | {m(s['win_rate'],'')} |")
    lines.append(f"| 平均 TOPIX比（OCO） | {m(s['avg_excess_pct'])} |")
    lines.append(f"| 参考: 金曜11:30 平均 | {m(s.get('avg_weekly_return_pct'))} |")
    lines.append("")
    lines.append("## 今週の気づき")
    for n in r.get("notes") or []:
        lines.append(f"- {n}")
    lines.append("")
    lines.append("## 銘柄ごとの結果（損益はOCO＝実行ルール）")
    lines.append("")
    lines.append("| シグナル日 | コード | 銘柄 | 判定 | 入口 | 過熱 | 約定日 | 約定値 | 決済日 | 決済値 | 出口理由 | 損益(OCO) | TOPIX比 | 参考:金曜 | 状況・材料 |")
    lines.append("| :--- | :--- | :--- | :--- | :--- | :--- | :--- | ---: | :--- | ---: | :--- | ---: | ---: | ---: | :--- |")
    for t in r.get("trades") or []:
        if t.get("status") != "filled":
            continue
        note = (t.get("news_note") or t.get("reason") or "").replace("|", "/")
        if len(note) > 40:
            note = note[:40] + "…"
        oco = t.get("exit_oco") or {}
        wk = t.get("exit_weekly") or {}
        lines.append(
            f"| {t['signal_date']} | {t['code']} | {t.get('name','')} | {t.get('verdict_label','')} | "
            f"{t.get('entry_type_label','')} | {t.get('overheat_label','')} | {t.get('fill_date','')} | "
            f"{t.get('fill_price')} | {oco.get('date','')} | {oco.get('price')} | {oco.get('reason','')} | "
            f"{t.get('return_net_pct')}% | {t.get('excess_pct')}% | {t.get('return_weekly_pct')}% | {note} |"
        )
    lines.append("")
    tn = r.get("top_n") or {}
    if tn:
        lines.append("## 上位N件だけ買った場合（実行可能性の検証）")
        lines.append("")
        lines.append("| 件数/日 | 対象 | 約定 | 約定率 | 平均 | 中央値 | 勝率 | TOPIX超過 |")
        lines.append("| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
        for key in sorted(tn, key=lambda x: int(x)):
            ts = tn[key]
            lines.append(
                f"| {ts.get('n_per_day')} | {ts.get('picks')} | {ts.get('n_filled')} | {m(ts.get('fill_rate'),'')} | "
                f"{m(ts.get('avg_return_pct'))} | {m(ts.get('median_return_pct'))} | {m(ts.get('win_rate'),'')} | {m(ts.get('avg_excess_pct'))} |"
            )
        lines.append("")
    rk = r.get("ranking") or {}
    lines.append("## スコア順位の効き（技術候補プール）")
    lines.append("")
    lines.append(f"- 順位相関(Spearman): {rk.get('spearman')}")
    lines.append(f"- 上位群 平均: {rk.get('top_avg_pct')}% / 下位群 平均: {rk.get('bottom_avg_pct')}% / スプレッド: {rk.get('spread_pct')}%")
    lines.append("")
    if r.get("actions"):
        lines.append("## 実績からの自動補正")
        lines.append("")
        for a in r["actions"]:
            lines.append(f"- {a}")
        lines.append("")
    np_plan = r.get("next_week_plan") or {}
    p = np_plan.get("plan") or {}
    if p:
        lines.append(f"## 来週の作戦（AI深掘り / 対象: {np_plan.get('week')}）")
        lines.append("")
        if p.get("market_view"):
            lines.append(f"**見立て**: {p['market_view']}")
            lines.append("")
        top = p.get("top_pick") or {}
        if top:
            lines.append(f"### 一番のおすすめ: {top.get('code')} {top.get('name')}（自信度: {top.get('confidence')}）")
            lines.append(f"- 理由: {top.get('reason')}")
            lines.append(f"- 買い方: {top.get('entry_strategy')}（入口 {top.get('entry_price')} / 利確 {top.get('tp_price')} / 損切 {top.get('sl_price')}）")
            lines.append(f"- シナリオ: {top.get('scenario')}")
            lines.append("")
        for b in p.get("backups") or []:
            lines.append(f"- 補欠: {b.get('code')} {b.get('name')} — {b.get('reason')}（{b.get('entry_strategy')} {b.get('entry_price')}）")
        for a in p.get("avoid") or []:
            lines.append(f"- 回避: {a.get('code')} — {a.get('reason')}")
        if p.get("risk_notes"):
            lines.append("")
            lines.append(f"**リスク・注意**: {p['risk_notes']}")
        lines.append("")
    return "\n".join(lines)


def resolve_weeks(args):
    if args.week:
        return [_parse_iso_week(args.week)]
    if args.all:
        dates_path = os.path.join(HISTORY_DIR, "dates.json")
        mondays = set()
        if os.path.exists(dates_path):
            with open(dates_path, "r", encoding="utf-8") as f:
                for ds in json.load(f):
                    d = datetime.strptime(ds, "%Y-%m-%d").date()
                    mondays.add(d - timedelta(days=d.weekday()))
        return sorted(mondays)
    # 直近の完了週（土曜実行想定）。古い週→新しい週の順で返す
    # （複数週を処理するとき、latest.json が最新週で終わるようにするため）。
    today = datetime.now().date()
    last_friday = today - timedelta(days=(today.weekday() - 4) % 7)
    if last_friday > today:
        last_friday -= timedelta(days=7)
    monday = last_friday - timedelta(days=4)
    weeks = [monday - timedelta(days=7 * i) for i in range(args.weeks)]
    return sorted(weeks)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    parser = argparse.ArgumentParser(description="週次スイング戦略の答え合わせ")
    parser.add_argument("--week", help="対象週 YYYY-Www（例 2026-W39）")
    parser.add_argument("--weeks", type=int, default=None,
                        help="直近N週を生成（既定は設定 carryover_weeks。先週分を翌週に再採点して確定させる）")
    parser.add_argument("--all", action="store_true", help="historyにある全週を生成（遡及）")
    parser.add_argument("--no-fetch", action="store_true", help="ネット取得をせずキャッシュのみ使用")
    parser.add_argument("--no-plan", action="store_true", help="来週の作戦(AI深掘り)を生成しない")
    args = parser.parse_args()

    params = load_strategy_params()
    if not params.get("weekly_review", {}).get("enabled", True):
        print(">> weekly_review は無効化されています。")
        return

    # 既定は「先週＋今週」の2週を毎回採点する。金曜シグナルや週をまたぐ
    # 押し目約定を、翌週のデータが揃った時点で確定させるため。
    if args.weeks is None:
        args.weeks = int(params.get("weekly_review", {}).get("carryover_weeks", 2))

    mondays = resolve_weeks(args)
    print(f">> 対象週: {len(mondays)} 週")
    results = []
    for monday in mondays:
        try:
            result = review_week(monday, params, no_fetch=args.no_fetch)
        except Exception as e:
            print(f">> ⚠️ {monday} の週でエラー: {e}")
            import traceback
            traceback.print_exc()
            continue
        if result:
            write_outputs(result)
            results.append(result)

    # 結果を今後に反映: 直近N週の実績からスコア補正を計算し、
    # strategy_params の weekly_feedback を更新する（main8 が次回から参照）。
    try:
        params = load_strategy_params()
        fb = compute_weekly_feedback(params)
        actions = build_actions(fb)
        fb_summary = {
            "enabled": fb.get("enabled"),
            "window_weeks": fb.get("window_weeks"),
            "n_filled": fb.get("n_filled"),
            "baseline_avg_pct": fb.get("baseline_avg_pct"),
            "overheat_delta": fb.get("overheat_delta"),
            "overheat_stats": fb.get("overheat_stats"),
            "note": fb.get("note"),
        }
        # 来週の作戦（AI深掘り）。最新週のレポート最下段に添付する。
        plan = None
        if not args.no_plan:
            latest_summary = results[-1]["summary"] if results else None
            plan = build_next_week_plan(params, latest_summary, fb_summary)
            write_plan(plan)
        for r in results:
            r["actions"] = actions
            r["feedback"] = fb_summary
            r["next_week_plan"] = plan if (r is results[-1]) else None
            write_outputs(r)
        write_feedback(fb)
    except Exception as e:
        print(f">> ⚠️ フィードバック計算に失敗（結果は出力済み）: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
