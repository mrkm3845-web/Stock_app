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
import json
import math
import os
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

DOCS_DIR = os.path.join(_REPO_ROOT, "docs")
HISTORY_DIR = os.path.join(DOCS_DIR, "history")
PICKS_DIR = os.path.join(DOCS_DIR, "picks")
AI_ANALYSIS_DIR = os.path.join(DOCS_DIR, "ai_analysis")
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
def load_history(date_str):
    path = os.path.join(HISTORY_DIR, f"{date_str}.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


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
        # トリガ未設定なら翌日寄成
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


def _rule_exit(pick, df, entry_date, entry_price):
    """アプリのルール出口（TP/SL/最大保有日数）を適用した場合の結果。"""
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
        if i == len(hold_dates) - 1 and close is not None:
            return {"reason": "保有期限", "date": d.strftime("%Y-%m-%d"), "price": close}
    if last_row is not None:
        close = _to_num(last_row.get("Close"))
        d = hold_dates[-1]
        return {"reason": "保有期限", "date": d.strftime("%Y-%m-%d"), "price": close}
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
        "exit_weekly": None,
        "exit_rule": None,
        "return_gross_pct": None,
        "return_net_pct": None,
        "return_rule_pct": None,
        "benchmark_pct": None,
        "excess_pct": None,
        "exit_price_source": None,
    }

    fill_date, fill_price, status = _entry_fill(pick, df, signal_date)
    result["status"] = status
    if status != "filled" or fill_date is None or fill_price is None:
        return result
    result["fill_date"] = fill_date.strftime("%Y-%m-%d")
    result["fill_price"] = _clean(fill_price)

    # --- 主ルール: 約定週の金曜 11:30 に手仕舞い ---
    dates = list(df.index.normalize()) if df is not None else []
    target_fri = _friday_of(fill_date.date() if hasattr(fill_date, "date") else fill_date)
    exit_candidates = [d for d in dates if d.date() <= target_fri and d >= fill_date]
    exit_date = max(exit_candidates) if exit_candidates else fill_date

    morning, src = _exit_morning(daily, intraday, ticker, exit_date)
    if morning is not None:
        gross = (morning - fill_price) / fill_price if fill_price else None
        net = _net_return(fill_price, morning, fee_rate, slip)
        result["exit_weekly"] = {
            "date": exit_date.strftime("%Y-%m-%d"),
            "price": _clean(morning),
            "return_gross_pct": _clean(gross * 100 if gross is not None else None),
            "return_net_pct": _clean(net * 100 if net is not None else None),
        }
        result["exit_price_source"] = src
        result["return_gross_pct"] = _clean(gross * 100 if gross is not None else None)
        result["return_net_pct"] = _clean(net * 100 if net is not None else None)

    # --- 参考: アプリのルール出口 ---
    rule = _rule_exit(pick, df, fill_date, fill_price)
    if rule and rule.get("price") is not None:
        net = _net_return(fill_price, rule["price"], fee_rate, slip)
        result["exit_rule"] = {
            "date": rule["date"],
            "price": _clean(rule["price"]),
            "reason": rule["reason"],
            "return_net_pct": _clean(net * 100 if net is not None else None),
        }
        result["return_rule_pct"] = _clean(net * 100 if net is not None else None)

    # --- ベンチマーク（約定日寄り→同じ出口日・同時刻） ---
    bdf = (bench_daily or {}).get(BENCH_TICKER)
    b_entry_row = _row_on(bdf, fill_date)
    b_entry = _to_num(b_entry_row.get("Open")) if b_entry_row is not None else None
    b_exit, _b_src = _exit_morning(bench_daily, bench_intr, BENCH_TICKER, exit_date)
    if b_entry and b_exit:
        b_net = _net_return(b_entry, b_exit, fee_rate, slip)
        result["benchmark_pct"] = _clean(b_net * 100 if b_net is not None else None)
        if result["return_net_pct"] is not None and b_net is not None:
            result["excess_pct"] = _clean((result["return_net_pct"]) - b_net * 100)

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


def build_notes(summary, ranking, calib, breakdown):
    notes = []
    if summary.get("n_filled", 0) == 0:
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

    fetch_start = (monday - timedelta(days=7)).strftime("%Y-%m-%d")
    fetch_end = (monday + timedelta(days=14)).strftime("%Y-%m-%d")
    need_start = monday - timedelta(days=3)
    need_end = min(monday + timedelta(days=14), datetime.now().date()) - timedelta(days=3)
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

    # 各ピックを採点
    trades = []
    for ds, snap in sorted(pool_by_date.items()):
        src = snap.get("source")
        for p in snap.get("picks") or []:
            p = dict(p)
            p["source"] = src
            trades.append(simulate_pick(p, daily, intraday, ds, daily, intraday, params))

    filled = [t for t in trades if t["status"] == "filled"]
    not_filled = [t for t in trades if t["status"] == "not_filled"]
    no_data = [t for t in trades if t["status"] in ("no_data", "wait")]
    evaluated = len(filled) + len(not_filled)
    weekly_rets = [t["return_net_pct"] for t in filled]
    rule_rets = [t["return_rule_pct"] for t in filled if t.get("return_rule_pct") is not None]
    bench_rets = [t["benchmark_pct"] for t in filled if t.get("benchmark_pct") is not None]
    excess = [t["excess_pct"] for t in filled if t.get("excess_pct") is not None]

    best = max(filled, key=lambda t: t["return_net_pct"]) if filled else None
    worst = min(filled, key=lambda t: t["return_net_pct"]) if filled else None

    summary = {
        "picks_total": len(trades),
        "n_filled": len(filled),
        "n_not_filled": len(not_filled),
        "n_no_data": len(no_data),
        "fill_rate": round(len(filled) / evaluated, 3) if evaluated else None,
        "avg_return_pct": _stats(weekly_rets)["avg"],
        "median_return_pct": _stats(weekly_rets)["median"],
        "win_rate": _stats(weekly_rets)["win_rate"],
        "avg_benchmark_pct": round(float(np.mean(bench_rets)), 3) if bench_rets else None,
        "avg_excess_pct": round(float(np.mean(excess)), 3) if excess else None,
        "beat_benchmark_rate": round(sum(1 for e in excess if e > 0) / len(excess), 3) if excess else None,
        "rule_avg_return_pct": _stats(rule_rets)["avg"],
        "rule_win_rate": _stats(rule_rets)["win_rate"],
        "best": None if not best else {
            "code": best["code"], "name": best["name"], "return_net_pct": best["return_net_pct"],
        },
        "worst": None if not worst else {
            "code": worst["code"], "name": worst["name"], "return_net_pct": worst["return_net_pct"],
        },
    }

    ranking = analyze_ranking(pool_by_date, daily, intraday, daily, params)
    calib = analyze_ai_calibration(pool_by_date, daily, intraday, daily, params)
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
            "entry": "entry_plan通り（未到達は見送り）",
            "exit": "約定週の金曜11:30（前場引け）に成行",
            "cost": f"手数料{params.get('weekly_review', {}).get('fee_rate', 0.0005)*100:.3f}%+スリッページ{params.get('weekly_review', {}).get('slippage_rate', 0.001)*100:.1f}%",
            "benchmark": BENCH_TICKER,
        },
        "summary": summary,
        "trades": trades,
        "ranking": ranking,
        "ai_calibration": calib,
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
    s = r["summary"]
    lines = [f"# 週次答え合わせレポート {r['week']}（{r['start']} 〜 {r['end']}）", ""]
    lines.append(f"※ ルール: {r['review_rule']['entry']} / {r['review_rule']['exit']} / コスト {r['review_rule']['cost']}")
    lines.append("")
    lines.append("## サマリ")
    lines.append("")
    lines.append("| 指標 | 値 |")
    lines.append("| :--- | ---: |")
    lines.append(f"| 対象ピック数 | {s['picks_total']} |")
    lines.append(f"| 約定 / 見送り | {s['n_filled']} / {s['n_not_filled']}（約定率 {s['fill_rate']}） |")
    lines.append(f"| 平均リターン | {s['avg_return_pct']}% |")
    lines.append(f"| 中央値 | {s['median_return_pct']}% |")
    lines.append(f"| 勝率 | {s['win_rate']} |")
    lines.append(f"| 平均 TOPIX比 | {s['avg_excess_pct']}% |")
    lines.append(f"| ルール出口の平均 | {s['rule_avg_return_pct']}% |")
    lines.append("")
    lines.append("## 今週の気づき")
    for n in r.get("notes") or []:
        lines.append(f"- {n}")
    lines.append("")
    lines.append("## 銘柄ごとの結果")
    lines.append("")
    lines.append("| シグナル日 | コード | 銘柄 | 判定 | 入口 | 過熱 | 約定日 | 約定値 | 手仕舞い | 損益(ネット) | TOPIX比 | 状況・材料 |")
    lines.append("| :--- | :--- | :--- | :--- | :--- | :--- | :--- | ---: | ---: | ---: | ---: | :--- |")
    for t in r.get("trades") or []:
        if t.get("status") != "filled":
            continue
        note = (t.get("news_note") or t.get("reason") or "").replace("|", "/")
        if len(note) > 40:
            note = note[:40] + "…"
        lines.append(
            f"| {t['signal_date']} | {t['code']} | {t.get('name','')} | {t.get('verdict_label','')} | "
            f"{t.get('entry_type_label','')} | {t.get('overheat_label','')} | {t.get('fill_date','')} | "
            f"{t.get('fill_price')} | {t.get('exit_weekly',{}).get('price')} | "
            f"{t.get('return_net_pct')}% | {t.get('excess_pct')}% | {note} |"
        )
    lines.append("")
    rk = r.get("ranking") or {}
    lines.append("## スコア順位の効き（技術候補プール）")
    lines.append("")
    lines.append(f"- 順位相関(Spearman): {rk.get('spearman')}")
    lines.append(f"- 上位群 平均: {rk.get('top_avg_pct')}% / 下位群 平均: {rk.get('bottom_avg_pct')}% / スプレッド: {rk.get('spread_pct')}%")
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
    # 直近の完了週（土曜実行想定）
    today = datetime.now().date()
    last_friday = today - timedelta(days=(today.weekday() - 4) % 7)
    if last_friday > today:
        last_friday -= timedelta(days=7)
    monday = last_friday - timedelta(days=4)
    return [monday - timedelta(days=7 * i) for i in range(args.weeks)]


def main():
    parser = argparse.ArgumentParser(description="週次スイング戦略の答え合わせ")
    parser.add_argument("--week", help="対象週 YYYY-Www（例 2026-W39）")
    parser.add_argument("--weeks", type=int, default=1, help="直近N週を生成（既定1）")
    parser.add_argument("--all", action="store_true", help="historyにある全週を生成（遡及）")
    parser.add_argument("--no-fetch", action="store_true", help="ネット取得をせずキャッシュのみ使用")
    args = parser.parse_args()

    params = load_strategy_params()
    if not params.get("weekly_review", {}).get("enabled", True):
        print(">> weekly_review は無効化されています。")
        return

    mondays = resolve_weeks(args)
    print(f">> 対象週: {len(mondays)} 週")
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


if __name__ == "__main__":
    main()
