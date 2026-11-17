#!/usr/bin/env python3
"""朝・昼・引け前・夜の自律調査＆Discordブリーフィング生成スクリプト。

GAS などの外部スケジューラから GitHub Actions（workflow_dispatch）経由で
朝(morning)・昼(midday)・引け前(preclose)・夜(evening)に実行されることを想定。
最新データ（台帳・推奨・履歴）とリアルタイム市場/5分足・Web検索（Gemini Grounding）を
統合し、具体的な売買判断と取引履歴CSVの更新催促を Discord へ通知する。

使い方:
  python tools/daily_briefing.py --slot morning --dry-run
  python tools/daily_briefing.py --slot preclose
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.persona import PERSONA_JA  # noqa: E402
from tools import check_strategy_plan as csp  # noqa: E402

JST = timezone(timedelta(hours=9))
WEB_STRATEGY_URL = "https://mrkm3845-web.github.io/Stock_app/strategy.html"
WEB_HOME_URL = "https://mrkm3845-web.github.io/Stock_app/"

SLOT_LABELS = {
    "morning": "☀️ 朝の作戦ブリーフィング",
    "midday": "🕛 昼（前場引け）状況チェック",
    "preclose": "🔔 引け前（大引け前）最終判断",
    "evening": "🌙 夜の確定まとめ＆明日への備え",
}


def now_jst() -> datetime:
    return datetime.now(JST)


def is_tse_business_day(target_date: date) -> bool:
    """東証（XTKS）の営業日かを判定する。ライブラリ未導入時は平日判定。"""
    if target_date.weekday() >= 5:
        return False
    try:
        import pandas_market_calendars as mcal

        cal = mcal.get_calendar("XTKS")
        sched = cal.schedule(
            start_date=target_date.isoformat(),
            end_date=target_date.isoformat(),
        )
        return not sched.empty
    except Exception:  # noqa: BLE001
        return True


def _extract_ticker_df(data, symbol: str, multi_requested: bool):
    """yfinance.download の戻り値から指定ティッカーの DataFrame を安全に取り出す。"""
    import pandas as pd

    if data is None or getattr(data, "empty", True):
        return None
    if isinstance(data.columns, pd.MultiIndex):
        lvl0 = data.columns.get_level_values(0)
        lvl1 = data.columns.get_level_values(1)
        if symbol in lvl0:
            return data[symbol]
        if symbol in lvl1:
            return data.xs(symbol, axis=1, level=1)
        return None
    return data if not multi_requested else None


def fetch_daily_snapshots(symbols: list[str]) -> dict[str, dict]:
    """直近5日・日足から現在値・前日比・始値/高値/安値を取得する。"""
    if not symbols:
        return {}
    try:
        import yfinance as yf
    except Exception:  # noqa: BLE001
        return {}

    uniq = list(dict.fromkeys(symbols))
    try:
        data = yf.download(
            uniq,
            period="5d",
            interval="1d",
            group_by="ticker",
            auto_adjust=True,
            progress=False,
            threads=True,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"警告: 日足ダウンロード失敗: {exc}", file=sys.stderr)
        return {}

    out: dict[str, dict] = {}
    multi = len(uniq) > 1
    for sym in uniq:
        try:
            df = _extract_ticker_df(data, sym, multi)
            if df is None:
                continue
            df = df.dropna(subset=["Close"])
            if df.empty:
                continue
            last_row = df.iloc[-1]
            close = float(last_row["Close"])
            prev_close = float(df["Close"].iloc[-2]) if len(df) >= 2 else None
            chg_pct = ((close / prev_close) - 1.0) * 100.0 if prev_close else None
            out[sym] = {
                "date": df.index[-1].date().isoformat(),
                "open": round(float(last_row["Open"]), 2) if "Open" in last_row else close,
                "high": round(float(last_row["High"]), 2) if "High" in last_row else close,
                "low": round(float(last_row["Low"]), 2) if "Low" in last_row else close,
                "close": round(close, 2),
                "prev_close": round(prev_close, 2) if prev_close is not None else None,
                "chg_pct": round(chg_pct, 2) if chg_pct is not None else None,
                "volume": int(last_row["Volume"]) if "Volume" in last_row and last_row["Volume"] == last_row["Volume"] else None,
            }
        except Exception as exc:  # noqa: BLE001
            print(f"警告: {sym} 日足解析失敗: {exc}", file=sys.stderr)
    return out


def analyze_intraday_5m(codes: list[str], target_date: date) -> dict[str, dict]:
    """当日5分足から「落ちるナイフ vs 下げ止まり」を判定するための定量指標を算出する。"""
    if not codes:
        return {}
    try:
        import yfinance as yf
    except Exception:  # noqa: BLE001
        return {}

    tickers = [csp.ticker(c) for c in codes]
    try:
        data = yf.download(
            tickers,
            period="2d",
            interval="5m",
            group_by="ticker",
            auto_adjust=True,
            progress=False,
            threads=True,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"警告: 5分足ダウンロード失敗: {exc}", file=sys.stderr)
        return {}

    out: dict[str, dict] = {}
    multi = len(tickers) > 1
    for code, sym in zip(codes, tickers):
        try:
            df = _extract_ticker_df(data, sym, multi)
            if df is None:
                continue
            df = df.dropna(subset=["Close"])
            if df.empty:
                continue
            # タイムゾーンをJSTに揃えて対象日（または最新日）を切り出す
            idx = df.index
            if idx.tzinfo is not None:
                idx_jst = idx.tz_convert(JST)
            else:
                idx_jst = idx.tz_localize(JST)
            df = df.copy()
            df.index = idx_jst
            day_df = df[df.index.date == target_date]
            if day_df.empty:
                latest_d = df.index[-1].date()
                day_df = df[df.index.date == latest_d]
            if day_df.empty:
                continue

            day_open = float(day_df["Open"].iloc[0])
            day_high = float(day_df["High"].max())
            day_low = float(day_df["Low"].min())
            last_close = float(day_df["Close"].iloc[-1])
            rng = day_high - day_low
            intraday_pos = (last_close - day_low) / rng if rng > 0 else 0.5

            # 直近1時間（最大12本）の動きを見る
            recent = day_df.tail(12)
            rec_high = float(recent["High"].max())
            rec_low = float(recent["Low"].min())
            rec_rng_pct = ((rec_high - rec_low) / last_close * 100.0) if last_close > 0 else 0.0
            rec_start = float(recent["Open"].iloc[0])
            rec_chg_pct = ((last_close / rec_start) - 1.0) * 100.0 if rec_start > 0 else 0.0
            bull_bars = int((recent["Close"] >= recent["Open"]).sum())
            sma_25bar = float(day_df["Close"].tail(25).mean())
            above_5m_ma = last_close >= sma_25bar

            # 簡易シグナル判定
            if rec_chg_pct <= -0.8 and not above_5m_ma and intraday_pos < 0.25 and bull_bars <= 4:
                knife_state = "falling_knife"
                knife_label = "下落継続（落ちるナイフ警戒）"
            elif rec_chg_pct >= 0.4 and above_5m_ma and intraday_pos >= 0.45:
                knife_state = "rebounding"
                knife_label = "反発基調（5分足短期線回復）"
            elif abs(rec_chg_pct) < 0.6 and rec_rng_pct < 1.5:
                knife_state = "stabilizing"
                knife_label = "下げ止まり・横ばい圏"
            else:
                knife_state = "mixed"
                knife_label = "方向感探り（もみ合い）"

            out[code] = {
                "asof": day_df.index[-1].strftime("%Y-%m-%d %H:%M"),
                "bars": len(day_df),
                "day_open": round(day_open, 1),
                "day_high": round(day_high, 1),
                "day_low": round(day_low, 1),
                "last_close": round(last_close, 1),
                "intraday_pos": round(intraday_pos, 2),
                "recent_1h_chg_pct": round(rec_chg_pct, 2),
                "recent_1h_range_pct": round(rec_rng_pct, 2),
                "recent_1h_bull_bars": f"{bull_bars}/{len(recent)}",
                "above_5m_sma25": above_5m_ma,
                "knife_state": knife_state,
                "knife_label": knife_label,
            }
        except Exception as exc:  # noqa: BLE001
            print(f"警告: {code} 5分足解析失敗: {exc}", file=sys.stderr)
    return out


def pick_sector_peers(hist: list[dict] | None, target_sectors: list[str], exclude_codes: set[str], limit_per_sector: int = 4) -> dict[str, list[dict]]:
    """対象セクターの流動性上位銘柄（比較用）を履歴から抽出する。"""
    if not hist or not target_sectors:
        return {}
    out: dict[str, list[dict]] = {}
    for sec in dict.fromkeys(s for s in target_sectors if s):
        peers = [
            item for item in hist
            if str(item.get("sector")) == sec
            and str(item.get("code")) not in exclude_codes
            and float(item.get("price") or 0) > 0
        ]
        peers.sort(key=lambda x: float(x.get("avg_val_5d") or 0), reverse=True)
        out[sec] = [
            {"code": str(p.get("code")), "name": str(p.get("name") or "")}
            for p in peers[:limit_per_sector]
        ]
    return out


def evaluate_csv_reminder(
    plan: dict,
    status_doc: dict | None,
    events_doc: dict | None,
    recs_doc: dict | None,
    slot: str,
    today: date,
    watch_evals: list[dict],
) -> list[str]:
    """取引履歴CSVのダウンロード＆/strategy-note実行を促すべき条件を判定する。"""
    reasons: list[str] = []
    trade_last = csp.parse_date(plan.get("trade_history_last_date"))
    data_date = csp.parse_date(plan.get("data_date"))
    rec_date = csp.parse_date((recs_doc or {}).get("date"))

    # 1. 直近2日以内に重要トリガーイベントが発生
    recent_actionable = {"triggered", "stop_hit", "tp_hit", "max_hold", "expired"}
    for ev in reversed((events_doc or {}).get("events") or []):
        at_str = str(ev.get("at") or "")[:10]
        ev_date = csp.parse_date(at_str)
        if ev_date and (today - ev_date).days <= 2 and ev.get("status") in recent_actionable:
            reasons.append(
                f"直近（{at_str}）に {ev.get('name')}（{ev.get('code')}）で「{ev.get('status')}」が発生"
            )
            break

    # 2. 本日の監視評価で到達・決済イベントが出た、または引け前に買い条件クリア見込み
    for w in watch_evals:
        st = w.get("live_status")
        if st in recent_actionable:
            reasons.append(f"本日 {w.get('name')}（{w.get('code')}）が「{st}」に到達（約定確認が必要）")
        elif slot == "preclose" and w.get("in_entry_zone") and w.get("conditions_ok") and w.get("knife_state") != "falling_knife":
            reasons.append(
                f"引け前に {w.get('name')}（{w.get('code')}）が買い条件を満たす見込み（注文/約定した場合は引け後にCSV更新推奨）"
            )

    # 3. 台帳基準日が推奨データより2日以上古い
    if data_date and rec_date and (rec_date - data_date).days >= 2:
        reasons.append(f"作戦台帳の基準日（{data_date}）が最新データ（{rec_date}）より古い")

    # 4. 取引履歴CSVの最終取引日から日数が空いている（週末・週明けチェック）
    if trade_last:
        gap_days = (today - trade_last).days
        if gap_days >= 14 and (slot in ("morning", "evening") or today.weekday() in (0, 4)):
            reasons.append(
                f"取引履歴CSVの最終約定日（{trade_last}）から{gap_days}日経過（最新の建玉・資金確認のため）"
            )

    return list(dict.fromkeys(reasons))


def collect_context(slot: str, today: date) -> dict:
    """台帳・推奨・履歴・リアルタイム株価・5分足・セクター・CSV催促を集約する。"""
    plan = csp.load_json(csp.PLAN_PATH, {}) or {}
    status_doc = csp.load_json(csp.STATUS_PATH, {}) or {}
    events_doc = csp.load_json(csp.EVENTS_PATH, {}) or {}
    recs_doc = csp.load_json(ROOT / "docs" / "recommendations.json", {}) or {}
    hist = csp.read_gzip_json(ROOT / "docs" / "history" / "latest.json.gz")
    meta = csp.load_json(ROOT / "docs" / "history" / "meta.json", {}) or {}
    earnings = csp.load_json(ROOT / "docs" / "earnings.json", {}) or {}

    hist_by_code = {str(x.get("code")): x for x in (hist or [])}

    positions = plan.get("positions") or []
    watchlist = plan.get("watchlist") or []
    avoid_list = [a for a in (plan.get("avoid") or []) if a.get("reentry_condition")]

    # 推奨銘柄から買いやすいレンジ上位を追加
    rec_candidates = []
    seen_codes = {str(x.get("code")) for x in positions + watchlist + avoid_list if x.get("code")}
    for p in (recs_doc.get("picks_in_range") or []) + (recs_doc.get("picks") or []):
        c = str(p.get("code") or "")
        if c and c not in seen_codes:
            seen_codes.add(c)
            rec_candidates.append(p)
        if len(rec_candidates) >= 3:
            break

    target_codes = [str(x.get("code")) for x in positions + watchlist if x.get("code")]
    extra_codes = [str(x.get("code")) for x in avoid_list[:2] + rec_candidates if x.get("code")]
    all_watch_codes = list(dict.fromkeys(target_codes + extra_codes))

    # セクター比較銘柄抽出
    target_sectors = [csp.sector_of(hist, c) for c in target_codes]
    peers_by_sector = pick_sector_peers(hist, target_sectors, set(all_watch_codes), limit_per_sector=4)
    peer_codes = [p["code"] for plist in peers_by_sector.values() for p in plist]

    # 日足スナップショット（指数・為替＋対象銘柄＋セクター比較銘柄）
    index_syms = ["1306.T", "1305.T", "^N225", "JPY=X"]
    stock_syms = [csp.ticker(c) for c in list(dict.fromkeys(all_watch_codes + peer_codes))]
    daily_px = fetch_daily_snapshots(index_syms + stock_syms)

    # 日中（昼・引け前）または日中データ取得可能時は5分足解析も実行
    intraday_5m = analyze_intraday_5m(target_codes, today) if slot in ("midday", "preclose", "evening") else {}

    # 指数・地合いサマリ
    regime_meta = meta.get("regime") or {}
    etf_1306 = daily_px.get("1306.T") or {}
    etf_1305 = daily_px.get("1305.T") or {}
    n225 = daily_px.get("^N225") or {}
    usdjpy = daily_px.get("JPY=X") or {}
    sma200_1306 = regime_meta.get("sma")
    dist_1306_sma200_pct = None
    if etf_1306.get("close") and sma200_1306:
        dist_1306_sma200_pct = round((etf_1306["close"] / float(sma200_1306) - 1.0) * 100.0, 2)

    breadth_all = csp.breadth_for(hist)
    breadth_bank = csp.breadth_for(hist, "銀行業")

    # 保有・監視銘柄の詳細評価
    now_dt = now_jst()
    near_pct = float((plan.get("policy") or {}).get("near_pct") or 1.0)
    pos_evals = []
    for item in positions:
        c = str(item.get("code"))
        snap = daily_px.get(csp.ticker(c)) or {}
        m5 = intraday_5m.get(c) or {}
        cur = (snap.get("close") if slot == "evening" else None) or m5.get("last_close") or snap.get("close") or (hist_by_code.get(c) or {}).get("price")
        px_obj = {
            "price": cur or 0,
            "high": m5.get("day_high") or snap.get("high") or cur or 0,
            "low": m5.get("day_low") or snap.get("low") or cur or 0,
        }
        st, act, _, _, _ = csp.evaluate_position(item, px_obj, now_dt, near_pct) if cur else ("unknown", "価格未取得", 0, 0, 0)
        pos_evals.append({
            "code": c,
            "name": item.get("name"),
            "entry_price": item.get("entry_price"),
            "stop": item.get("stop"),
            "take_profit": item.get("take_profit"),
            "current_price": cur,
            "chg_pct": snap.get("chg_pct"),
            "live_status": st,
            "action": act,
            "intraday_5m": m5 or None,
        })

    watch_evals = []
    for item in watchlist:
        c = str(item.get("code"))
        h = hist_by_code.get(c) or {}
        ctx = h.get("ctx") or {}
        snap = daily_px.get(csp.ticker(c)) or {}
        m5 = intraday_5m.get(c) or {}
        cur = (snap.get("close") if slot == "evening" else None) or m5.get("last_close") or snap.get("close") or h.get("price")
        px_obj = {
            "price": cur or 0,
            "high": m5.get("day_high") or snap.get("high") or cur or 0,
            "low": m5.get("day_low") or snap.get("low") or cur or 0,
        }
        if cur:
            st, act, _, _, _, cond_ok, cond_reasons = csp.evaluate_watch(item, px_obj, now_dt, near_pct, hist, meta, earnings)
        else:
            st, act, cond_ok, cond_reasons = "unknown", "価格未取得", False, ["価格未取得"]

        sec = csp.sector_of(hist, c)
        sec_b = csp.breadth_for(hist, sec) if sec else None
        z_lo = item.get("entry_zone_low")
        z_hi = item.get("entry_zone_high")
        in_zone = bool(cur and z_lo and z_hi and float(z_lo) <= float(cur) <= float(z_hi))
        peers_info = []
        for p in peers_by_sector.get(sec or "", []):
            ps = daily_px.get(csp.ticker(p["code"])) or {}
            if ps:
                peers_info.append({
                    "code": p["code"],
                    "name": p["name"],
                    "close": ps.get("close"),
                    "chg_pct": ps.get("chg_pct"),
                })

        watch_evals.append({
            "code": c,
            "name": item.get("name"),
            "priority": item.get("priority"),
            "trigger_type": item.get("trigger_type"),
            "entry_price": item.get("entry_price"),
            "entry_zone": [z_lo, z_hi],
            "in_entry_zone": in_zone,
            "stop": item.get("stop"),
            "take_profit": item.get("take_profit"),
            "current_price": cur,
            "chg_pct": snap.get("chg_pct"),
            "sma5": ctx.get("sma5"),
            "sma25": ctx.get("sma25"),
            "sma200": ctx.get("sma200"),
            "rsi14": ctx.get("rsi14"),
            "sector": sec,
            "sector_color": csp.sector_color_of(sec_b),
            "sector_above_sma25_pct": (sec_b or {}).get("above_sma25_pct"),
            "sector_peers_today": peers_info,
            "conditions_ok": cond_ok,
            "condition_reasons": cond_reasons,
            "live_status": st,
            "action": act,
            "intraday_5m": m5 or None,
            "knife_state": (m5 or {}).get("knife_state"),
            "thesis": item.get("thesis"),
        })

    avoid_reentry = []
    for item in avoid_list[:2]:
        c = str(item.get("code"))
        snap = daily_px.get(csp.ticker(c)) or {}
        avoid_reentry.append({
            "code": c,
            "name": item.get("name"),
            "current_price": snap.get("close"),
            "chg_pct": snap.get("chg_pct"),
            "reason": item.get("reason"),
            "reentry_condition": item.get("reentry_condition"),
        })

    rec_summary = []
    for p in rec_candidates:
        c = str(p.get("code"))
        snap = daily_px.get(csp.ticker(c)) or {}
        rec_summary.append({
            "code": c,
            "name": p.get("name"),
            "sector": p.get("sector"),
            "sector_color": p.get("sector_color"),
            "verdict": p.get("verdict"),
            "score": p.get("score"),
            "hist_price": p.get("price"),
            "live_price": snap.get("close"),
            "chg_pct": snap.get("chg_pct"),
            "entry_strategy": p.get("entry_strategy"),
            "news_note": p.get("news_note"),
        })

    csv_reminders = evaluate_csv_reminder(plan, status_doc, events_doc, recs_doc, slot, today, watch_evals)

    return {
        "slot": slot,
        "slot_label": SLOT_LABELS[slot],
        "generated_at": now_dt.strftime("%Y-%m-%d %H:%M JST"),
        "data_dates": {
            "recommendations_date": recs_doc.get("date"),
            "history_meta_date": meta.get("date"),
            "plan_id": plan.get("plan_id"),
            "plan_data_date": plan.get("data_date"),
            "trade_history_last_date": plan.get("trade_history_last_date"),
        },
        "policy": plan.get("policy"),
        "rules": plan.get("rules"),
        "market": {
            "1306_T": etf_1306,
            "1306_sma200_prev": sma200_1306,
            "1306_dist_sma200_pct": dist_1306_sma200_pct,
            "1305_T_ref": etf_1305,
            "N225": n225,
            "USDJPY": usdjpy,
            "breadth_all_prev": breadth_all,
            "breadth_bank_prev": breadth_bank,
            "recs_overall": recs_doc.get("overall"),
        },
        "positions": pos_evals,
        "watchlist": watch_evals,
        "avoid_reentry": avoid_reentry,
        "top_recommendations": rec_summary,
        "csv_reminders": csv_reminders,
    }


def _slot_mission(slot: str) -> str:
    if slot == "morning":
        return (
            "今は【朝】のブリーフィングです。Web検索で直近の米国株（ダウ・NASDAQ・SOX）・為替・"
            "保有/監視/注目銘柄の最新ニュース（悪材料や決算修正など）を確認したうえで、"
            "今日の寄り付き方針・見るべき価格ライン・注文点検ポイントをまとめてください。"
        )
    if slot == "midday":
        return (
            "今は【昼（前場引け後）】の状況チェックです。Web検索で前場の東京市場の動きや監視銘柄の新着ニュースがないか確認し、"
            "5分足の状況（落ちるナイフか下げ止まりか）とセクター比較を踏まえて、後場に向けた警戒ラインと待機方針をまとめてください。"
        )
    if slot == "preclose":
        return (
            "今は【大引け前（15時前）】の最終判断です。Web検索で本日中の新規悪材料がないか確認し、"
            "5分足の下げ止まり状況・押し目ゾーン一致・1306.Tの200日線維持・セクター動向・DDブレーキ（口座ルール）を総合して、"
            "『本日エントリーするか／見送るか』を明確に判断し、行動レベル（100株・指値/見送り・約定当日OCO）を提案してください。"
        )
    return (
        "今は【夜（確定後）】のまとめです。本日の大引け結果、最新のAI推奨（top_recommendations）、および台帳の状態を突き合わせ、"
        "今日の振り返り、明日以降の注目候補（資金12万円以内/リスク5,000円以内で買えるか含む）、"
        "台帳や監視銘柄の見直しが必要かを簡潔にまとめてください。"
    )


def generate_ai_briefing(ctx: dict) -> tuple[str, str]:
    """Gemini API + Google Search Grounding でブリーフィング本文を生成する。"""
    api_key = os.environ.get("GEMINI_API_KEY") or ""
    if not api_key:
        return build_fallback_briefing(ctx, "GEMINI_API_KEY 未設定"), "fallback-no-key"

    system_prompt = (
        f"{PERSONA_JA}\n\n"
        "あなたはDiscordで個人投資家に寄り添って報告する日本株スイングの専属アナリストです。\n"
        "出力ルール：\n"
        "1. 日本語のみ（中国語は絶対に使わない）。\n"
        "2. 銘柄は必ず『銘柄名（コード）』の形式にする（例：豊田合成（7282））。\n"
        "3. 最初に『■ 結論』を1〜3行で書き、続けて『■ 地合い・外部環境』『■ 監視・保有の判定』『■ 不確実性・注意点』『■ 次の行動』の順で簡潔にまとめる。\n"
        "4. 全体は1,300文字以内で読みやすく箇条書き中心にする（Discord通知用）。\n"
        "5. 数字は率(%)・金額(円)・件数を混ぜずに書く。儲かると断定しない。\n"
        "6. policyにある資金上限・DDブレーキ・決算5日前ルールを必ず尊重する。\n"
        "7. 入力JSONの事実とWeb検索で確認した最新事実のみを書く。推測で数値を補わない。"
    )

    user_prompt = (
        f"{_slot_mission(ctx['slot'])}\n\n"
        "以下が現在の最新コンテキストJSONです：\n"
        f"{json.dumps(ctx, ensure_ascii=False, indent=2)}"
    )

    models = ["gemini-3-flash-preview", "gemini-3.1-pro-preview"]
    last_err = ""
    for model in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
        payload = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "tools": [{"google_search": {}}],
            "generationConfig": {"temperature": 0.2},
        }
        body = json.dumps(payload).encode("utf-8")
        for attempt in range(3):
            req = urllib.request.Request(
                url,
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=90) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                cand = (data.get("candidates") or [{}])[0]
                parts = (cand.get("content") or {}).get("parts") or []
                text = "\n".join(p.get("text", "") for p in parts if isinstance(p, dict) and p.get("text")).strip()
                if text:
                    return text, model
                last_err = f"{model}: empty text"
            except urllib.error.HTTPError as exc:
                err_txt = exc.read().decode("utf-8", errors="ignore")[:200]
                last_err = f"{model} HTTP {exc.code}: {err_txt}"
                if exc.code in (500, 502, 503, 504):
                    time.sleep(min(2 ** attempt, 8))
                    continue
                break
            except Exception as exc:  # noqa: BLE001
                last_err = f"{model}: {exc}"
                break
        print(f"警告: {last_err}", file=sys.stderr)

    return build_fallback_briefing(ctx, last_err), "fallback-error"


def build_fallback_briefing(ctx: dict, reason: str) -> str:
    """API未設定・制限時でも定量データだけは必ず届けるフォールバック文面。"""
    m = ctx.get("market") or {}
    e1306 = m.get("1306_T") or {}
    n225 = m.get("N225") or {}
    fx = m.get("USDJPY") or {}
    lines = [
        f"※AI要約スキップ（{reason}）のため定量サマリを送信します。",
        f"• 日経平均: {n225.get('close', '-')} ({n225.get('chg_pct', '-')}%)",
        f"• 1306.T: {e1306.get('close', '-')} ({e1306.get('chg_pct', '-')}%) / 200日線比 {m.get('1306_dist_sma200_pct', '-')}%",
        f"• ドル円: {fx.get('close', '-')} ({fx.get('chg_pct', '-')}%)",
    ]
    for w in ctx.get("watchlist") or []:
        m5 = w.get("intraday_5m") or {}
        knife = m5.get("knife_label") or "-"
        lines.append(
            f"• 監視 {w.get('name')}（{w.get('code')}）: {w.get('current_price')}円 ({w.get('chg_pct', '-')}%) / "
            f"入口 {w.get('entry_price')} / {w.get('action')} / 5分足: {knife}"
        )
    for p in ctx.get("positions") or []:
        lines.append(
            f"• 保有 {p.get('name')}（{p.get('code')}）: {p.get('current_price')}円 / {p.get('action')}"
        )
    return "\n".join(lines)


def format_discord_message(ctx: dict, body: str, model_used: str) -> str:
    dd = ctx["data_dates"]
    header = (
        f"**{ctx['slot_label']}**（{ctx['generated_at']}）\n"
        f"`台帳:{dd.get('plan_id') or '-'} (基準日:{dd.get('plan_data_date') or '-'}) | "
        f"推奨:{dd.get('recommendations_date') or '-'} | CSV最終:{dd.get('trade_history_last_date') or '-'} | "
        f"判定:{model_used}`"
    )
    parts = [header, body]
    reminders = ctx.get("csv_reminders") or []
    if reminders:
        rem_lines = ["**📩 取引履歴CSV更新のお願い**"]
        for r in reminders:
            rem_lines.append(f"- {r}")
        rem_lines.append("→ 証券会社から最新CSVをダウンロードし、リポジトリ直下に置いて `/strategy-note` を実行してください。")
        parts.append("\n".join(rem_lines))
    parts.append(f"🔗 今日の作戦: {WEB_STRATEGY_URL}\n🔗 スクリーナー: {WEB_HOME_URL}")
    return "\n\n".join(parts)


def send_discord_chunks(webhook: str, text: str, max_len: int = 1900) -> None:
    if not webhook:
        print("情報: DISCORD_WEBHOOK_URL が未設定のため送信をスキップしました。")
        return
    chunks: list[str] = []
    cur = ""
    for line in text.splitlines(keepends=True):
        if len(cur) + len(line) > max_len:
            if cur:
                chunks.append(cur.rstrip())
                cur = ""
            while len(line) > max_len:
                chunks.append(line[:max_len])
                line = line[max_len:]
        cur += line
    if cur.strip():
        chunks.append(cur.rstrip())

    for i, chunk in enumerate(chunks):
        payload = json.dumps({"content": chunk}).encode("utf-8")
        req = urllib.request.Request(
            webhook,
            data=payload,
            headers={"Content-Type": "application/json", "User-Agent": "StockApp/1.0"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                resp.read()
        except Exception as exc:  # noqa: BLE001
            print(f"警告: Discord送信失敗 ({i + 1}/{len(chunks)}): {exc}", file=sys.stderr)
        if i + 1 < len(chunks):
            time.sleep(0.5)


def main() -> int:
    parser = argparse.ArgumentParser(description="時間帯別 自律調査＆Discordブリーフィング")
    parser.add_argument(
        "--slot",
        choices=["morning", "midday", "preclose", "evening"],
        required=True,
        help="実行時間帯スロット",
    )
    parser.add_argument("--dry-run", action="store_true", help="Discord送信せず標準出力のみ表示")
    parser.add_argument("--force", action="store_true", help="休場日でも強制実行")
    args = parser.parse_args()

    now = now_jst()
    if not args.force and not is_tse_business_day(now.date()):
        print(f"東証休場日のためスキップします（{now:%Y-%m-%d %H:%M} JST / {args.slot}）")
        return 0

    ctx = collect_context(args.slot, now.date())
    body, model_used = generate_ai_briefing(ctx)
    message = format_discord_message(ctx, body, model_used)

    print(message)
    if args.dry_run:
        print("\n[dry-run] Discord送信はスキップしました。")
        return 0

    webhook = os.environ.get("DISCORD_WEBHOOK_URL") or ""
    send_discord_chunks(webhook, message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
