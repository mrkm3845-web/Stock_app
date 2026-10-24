#!/usr/bin/env python3
"""作戦台帳のトリガーをチェックするスクリプト。

docs/strategy/active.json を読み、保有のOCO・押し目/ブレイク・
セクター/地合い条件を判定して、docs/strategy/status.json と
docs/strategy/events.json を更新します。

Discord Webhook があれば、状態が変わったときだけ通知します。
発注は行いません。通知と記録だけです。

使い方:
  python tools/check_strategy_plan.py --mode eod
  python tools/check_strategy_plan.py --mode intraday --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[1]
PLAN_PATH = ROOT / "docs" / "strategy" / "active.json"
STATUS_PATH = ROOT / "docs" / "strategy" / "status.json"
EVENTS_PATH = ROOT / "docs" / "strategy" / "events.json"
DISCORD_URL = os.environ.get("DISCORD_WEBHOOK_URL") or ""
WEB_URL = "https://mrkm3845-web.github.io/Stock_app/strategy.html"

ACTIONABLE = {
    "stop_hit",
    "tp_hit",
    "triggered",
    "triggered_then_stop",
    "blocked",
    "expired",
    "max_hold",
}
NEAR = {"near", "near_stop", "near_tp"}
ALERT_ON_CHANGE = ACTIONABLE | NEAR | {"blocked_waiting"}
STATUS_PRIORITY = {
    "stop_hit": 0,
    "tp_hit": 1,
    "triggered": 2,
    "triggered_then_stop": 3,
    "near_stop": 4,
    "near_tp": 5,
    "blocked": 6,
    "blocked_waiting": 7,
    "near": 8,
    "max_hold": 9,
    "expired": 10,
    "hold": 11,
    "waiting": 12,
    "avoid": 13,
}


def load_json(path: Path, default=None):
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:  # noqa: BLE001
        print(f"警告: JSONを読めません: {path} ({exc})", file=sys.stderr)
        return default


def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    tmp.replace(path)


def read_gzip_json(path: Path):
    if not path.exists():
        return None
    try:
        import gzip

        with gzip.open(path, "rt", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:  # noqa: BLE001
        print(f"警告: 履歴を読めません: {path} ({exc})", file=sys.stderr)
        return None


def now_jst() -> datetime:
    return datetime.now(JST)


def ticker(code: str) -> str:
    code = str(code).strip()
    if code.endswith(".T") or "." in code:
        return code
    return f"{code}.T"


def market_time(now: datetime, mode: str) -> bool:
    if now.weekday() >= 5:
        return False
    if mode == "eod":
        return now.hour >= 15
    return (now.hour == 9 and now.minute >= 0) or (10 <= now.hour <= 14) or (
        now.hour == 15 and now.minute <= 30
    )


def fetch_prices(codes: list[str], mode: str):
    """yfinance から現在値と当日の高値/安値を取る。失敗時は空。"""
    try:
        import yfinance as yf
    except Exception:  # noqa: BLE001
        print("警告: yfinance がありません。履歴の終値で代替します。", file=sys.stderr)
        return {}, "yfinance未導入"

    tickers = [ticker(code) for code in codes]
    if not tickers:
        return {}, "対象なし"
    period = "5d"
    interval = "1d" if mode == "eod" else "5m"
    try:
        data = yf.download(
            tickers,
            period=period,
            interval=interval,
            group_by="ticker",
            auto_adjust=True,
            progress=False,
            threads=True,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"警告: 価格取得に失敗: {exc}", file=sys.stderr)
        return {}, "yfinance失敗"

    result = {}
    for code, t in zip(codes, tickers):
        try:
            if len(tickers) == 1:
                df = data
            else:
                df = data[t] if t in data.columns.get_level_values(0) else None
            if df is None:
                continue
            df = df.dropna(subset=["Close"])
            if df.empty:
                continue
            last_date = df.index[-1].date()
            day = df[df.index.date == last_date]
            if day.empty:
                day = df.tail(1)
            price = float(day["Close"].iloc[-1])
            high = float(day["High"].max()) if "High" in day else price
            low = float(day["Low"].min()) if "Low" in day else price
            asof = df.index[-1].isoformat()
            result[code] = {
                "price": price,
                "high": high,
                "low": low,
                "asof": asof,
                "source": f"yfinance-{interval}",
            }
        except Exception as exc:  # noqa: BLE001
            print(f"警告: {code} の価格を読めません: {exc}", file=sys.stderr)
    return result, "yfinance"


def history_fallback(codes: list[str], hist, data_date: str | None = None):
    if not hist:
        return {}
    by_code = {str(item.get("code")): item for item in hist}
    result = {}
    for code in codes:
        item = by_code.get(str(code))
        if not item:
            continue
        price = float(item.get("price") or 0)
        if price <= 0:
            continue
        result[code] = {
            "price": price,
            "high": price,
            "low": price,
            "asof": data_date or "history",
            "source": "history-close",
        }
    return result


MIN_BREADTH_N = 10


def breadth_for(hist, sector: str | None = None):
    """セクター（Noneなら全体）の広がり。

    週足が下向きの銘柄（excluded）も含めて数える。除外してから
    「25日線の上比率」を測ると、上昇中の銘柄だけを集めることになり
    数値が上振れする（銀行のように母数が消える）ため。
    """
    if not hist:
        return None
    items = [item for item in hist if float(item.get("price") or 0) > 0]
    if sector:
        items = [item for item in items if str(item.get("sector")) == sector]
    if not items:
        return None
    above25 = sum(1 for item in items if float(item.get("ctx", {}).get("dist_sma25_pct") or 0) > 0)
    above200 = sum(1 for item in items if float(item.get("ctx", {}).get("dist_sma200_pct") or 0) > 0)
    ret5 = [float(item.get("ctx", {}).get("ret_5d_pct") or 0) for item in items]
    return {
        "n": len(items),
        "low_sample": len(items) < MIN_BREADTH_N,
        "above_sma25_pct": round(above25 / len(items) * 100, 1),
        "above_sma200_pct": round(above200 / len(items) * 100, 1),
        "avg_ret5_pct": round(sum(ret5) / len(ret5), 2) if ret5 else None,
    }


def sector_of(hist, code: str):
    if not hist:
        return None
    for item in hist:
        if str(item.get("code")) == str(code):
            return str(item.get("sector") or "")
    return None


def parse_date(value):
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(str(value), fmt).date()
        except ValueError:
            continue
    return None


def check_conditions(item, hist, meta, earnings):
    reasons = []
    ok = True
    for condition in item.get("conditions") or []:
        if condition == "market_risk_on":
            regime = (meta or {}).get("regime") or {}
            if not regime.get("risk_on"):
                ok = False
                reasons.append("地合いがリスクオフ")
        elif condition in ("sector_ok", "bank_sector_ok"):
            sector = "銀行業" if condition == "bank_sector_ok" else sector_of(hist, item.get("code"))
            b = breadth_for(hist, sector) if sector else None
            if not b:
                ok = False
                reasons.append(f"{sector or 'セクター'}のデータ不足")
            elif b["n"] < MIN_BREADTH_N:
                ok = False
                reasons.append(f"{sector}の銘柄数が少ない（{b['n']}銘柄・参考外）")
            elif b["above_sma25_pct"] < 30:
                ok = False
                reasons.append(f"{sector}の25日線上{b['above_sma25_pct']}%（{b['n']}銘柄）")
        elif condition == "earnings_ok":
            info = (earnings or {}).get("items", {}).get(str(item.get("code"))) or {}
            days = info.get("days_until")
            if info.get("soon") or (isinstance(days, (int, float)) and 0 <= days <= 5):
                ok = False
                reasons.append(f"決算接近（{info.get('date') or '日付不明'}）")
    return ok, reasons


def distance_pct(price, target):
    if not price or not target:
        return None
    return abs(float(target) - float(price)) / float(price) * 100


def evaluate_position(item, px, now, near_pct):
    price = float(px["price"])
    high = float(px.get("high") or price)
    low = float(px.get("low") or price)
    stop = float(item.get("stop") or 0)
    tp = float(item.get("take_profit") or 0)
    stop_hit = bool(stop and low <= stop)
    tp_hit = bool(tp and high >= tp)
    max_hold = parse_date(item.get("max_hold_until"))
    if stop_hit and tp_hit:
        return "stop_hit", "同日に損切・利確。損切り優先で確認", price, high, low
    if stop_hit:
        return "stop_hit", f"損切り{stop:,.0f}に到達。OCO約定を確認", price, high, low
    if tp_hit:
        return "tp_hit", f"利確{tp:,.0f}に到達。利確/トレールを確認", price, high, low
    if max_hold and now.date() >= max_hold:
        return "max_hold", f"最大保有日{max_hold}。引けで手仕舞いを検討", price, high, low
    if stop and price <= stop * (1 + near_pct / 100):
        return "near_stop", f"損切り{stop:,.0f}まであと{distance_pct(price, stop):.1f}%", price, high, low
    if tp and price >= tp * (1 - near_pct / 100):
        return "near_tp", f"利確{tp:,.0f}まであと{distance_pct(price, tp):.1f}%", price, high, low
    return "hold", "保有継続", price, high, low


def evaluate_watch(item, px, now, near_pct, hist, meta, earnings):
    price = float(px["price"])
    high = float(px.get("high") or price)
    low = float(px.get("low") or price)
    entry = float(item.get("entry_price") or 0)
    stop = float(item.get("stop") or 0)
    wait_until = parse_date(item.get("wait_until"))
    trigger_type = item.get("trigger_type") or "pullback"
    cond_ok, cond_reasons = check_conditions(item, hist, meta, earnings)

    if trigger_type == "breakout":
        price_hit = bool(entry and high >= entry)
    else:
        price_hit = bool(entry and low <= entry)
    same_day_stop = bool(price_hit and stop and low <= stop)

    if price_hit:
        if same_day_stop:
            return "triggered_then_stop", f"入口{entry:,.0f}と損切{stop:,.0f}に同日到達。見送り", price, high, low, cond_ok, cond_reasons
        if cond_ok:
            return "triggered", f"買い入口{entry:,.0f}に到達。約定したらOCO {stop:,.0f}/{item.get('take_profit', 0):,.0f}", price, high, low, cond_ok, cond_reasons
        return "blocked", f"入口{entry:,.0f}に到達したが条件NG（{' / '.join(cond_reasons)}）", price, high, low, cond_ok, cond_reasons

    if wait_until and now.date() > wait_until:
        return "expired", f"押し目待ちの期限{wait_until}を過ぎた。注文を取消", price, high, low, cond_ok, cond_reasons
    if not cond_ok:
        return "blocked_waiting", f"条件NG（{' / '.join(cond_reasons)}）。価格が来ても買わない", price, high, low, cond_ok, cond_reasons
    if entry and distance_pct(price, entry) is not None and distance_pct(price, entry) <= near_pct:
        return "near", f"買い入口{entry:,.0f}まであと{distance_pct(price, entry):.1f}%", price, high, low, cond_ok, cond_reasons
    return "waiting", f"買い入口{entry:,.0f}待ち", price, high, low, cond_ok, cond_reasons


def send_discord(webhook: str, content: str):
    if not webhook:
        return
    data = json.dumps({"content": content}).encode("utf-8")
    req = urllib.request.Request(
        webhook,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": "StockApp/1.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
    except Exception as exc:  # noqa: BLE001
        print(f"警告: Discord通知に失敗: {exc}", file=sys.stderr)


def fmt_action(item, status, price, action):
    emoji = {
        "stop_hit": "🛑",
        "tp_hit": "🎯",
        "triggered": "🟢",
        "triggered_then_stop": "⚠️",
        "blocked": "⛔",
        "blocked_waiting": "⛔",
        "near": "👀",
        "near_stop": "👀",
        "near_tp": "👀",
        "max_hold": "⏰",
        "expired": "⌛",
    }.get(status, "・")
    code = item.get("code")
    name = item.get("name")
    return f"{emoji} {name}（{code}）：{action}（現在 {price:,.0f}）"


def fmt_plan_update(plan, status):
    """作戦台帳が更新されたときの通知文（push時にも使う）。"""
    items = status.get("items") or []
    plan_id = plan.get("plan_id") or "-"
    updated = plan.get("updated_at") or plan.get("created_at") or "-"
    n_pos = len(plan.get("positions") or [])
    n_watch = len(plan.get("watchlist") or [])
    n_avoid = len(plan.get("avoid") or [])
    lines = [
        f"【作戦更新】{plan_id}",
        f"基準日 {plan.get('data_date') or '-'} / 更新 {str(updated).replace('T', ' ')[:16]}",
        f"保有 {n_pos} / 監視 {n_watch} / 見送り {n_avoid}",
    ]
    for item in items[:10]:
        lines.append(fmt_action(item, item.get("status"), item.get("current_price") or 0, item.get("action") or ""))
    if len(items) > 10:
        lines.append(f"…ほか {len(items) - 10} 件")
    lines.append(WEB_URL)
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="作戦台帳のトリガーをチェック")
    parser.add_argument("--mode", choices=["eod", "intraday"], default="eod")
    parser.add_argument("--dry-run", action="store_true", help="通知もファイル更新もしない")
    parser.add_argument("--no-write", action="store_true", help="ファイル更新しない")
    parser.add_argument("--force", action="store_true", help="時間外・状態変化なしでも通知")
    parser.add_argument("--summary", action="store_true", help="今日の作戦サマリも送る")
    parser.add_argument("--notify-plan", action="store_true", help="作戦台帳の更新通知を送る（push時用）")
    args = parser.parse_args()

    now = now_jst()
    if not args.force and not market_time(now, args.mode):
        print(f"市場時間外のためスキップ（{now:%Y-%m-%d %H:%M} JST / {args.mode}）")
        return 0

    plan = load_json(PLAN_PATH)
    if not plan:
        print(f"作戦台帳がありません: {PLAN_PATH}")
        return 0

    hist = read_gzip_json(ROOT / "docs" / "history" / "latest.json.gz")
    meta = load_json(ROOT / "docs" / "history" / "meta.json", {})
    earnings = load_json(ROOT / "docs" / "earnings.json", {})
    previous = load_json(STATUS_PATH, {}) or {}
    prev_items = {str(item.get("code")): item for item in previous.get("items", [])}

    positions = plan.get("positions") or []
    watchlist = plan.get("watchlist") or []
    codes = [str(item.get("code")) for item in positions + watchlist if item.get("code")]
    prices, source = fetch_prices(codes, args.mode)
    fallback = history_fallback(codes, hist, (meta or {}).get("date"))
    for code, px in fallback.items():
        prices.setdefault(code, px)

    # 日中チェックは、当日の新しいデータが取れない日（休場・データ遅延）に
    # 古い値で通知しない。yfinance のデータだけを「当日」とみなす。
    if args.mode == "intraday":
        fresh_dates = []
        for px in prices.values():
            if not str(px.get("source", "")).startswith("yfinance"):
                continue
            asof = px.get("asof")
            if not asof:
                continue
            try:
                fresh_dates.append(datetime.fromisoformat(asof).date())
            except ValueError:
                continue
        if not fresh_dates or max(fresh_dates) < now.date():
            print("当日の新しい価格が取れないためスキップ（休場/データ遅延）")
            return 0

    items = []
    events = []
    alerts = []
    near_pct = float((plan.get("policy") or {}).get("near_pct") or 1.0)

    for item in positions:
        code = str(item.get("code"))
        px = prices.get(code)
        if not px:
            continue
        status, action, price, high, low = evaluate_position(item, px, now, near_pct)
        items.append(
            {
                "code": code,
                "name": item.get("name"),
                "kind": "position",
                "status": status,
                "current_price": round(price, 1),
                "high": round(high, 1),
                "low": round(low, 1),
                "entry_price": item.get("entry_price"),
                "stop": item.get("stop"),
                "take_profit": item.get("take_profit"),
                "distance_pct": distance_pct(price, item.get("stop")),
                "action": action,
                "source": px.get("source"),
                "asof": px.get("asof"),
            }
        )

    for item in watchlist:
        code = str(item.get("code"))
        px = prices.get(code)
        if not px:
            continue
        status, action, price, high, low, cond_ok, cond_reasons = evaluate_watch(
            item, px, now, near_pct, hist, meta, earnings
        )
        items.append(
            {
                "code": code,
                "name": item.get("name"),
                "kind": "watch",
                "status": status,
                "current_price": round(price, 1),
                "high": round(high, 1),
                "low": round(low, 1),
                "entry_price": item.get("entry_price"),
                "stop": item.get("stop"),
                "take_profit": item.get("take_profit"),
                "distance_pct": distance_pct(price, item.get("entry_price")),
                "conditions_ok": cond_ok,
                "condition_reasons": cond_reasons,
                "action": action,
                "source": px.get("source"),
                "asof": px.get("asof"),
            }
        )

    items.sort(key=lambda x: STATUS_PRIORITY.get(x["status"], 99))

    for item in items:
        code = item["code"]
        prev = prev_items.get(code) or {}
        prev_status = prev.get("status")
        changed = prev_status != item["status"]
        last_near_date = prev.get("last_near_date")
        last_notified_status = prev.get("last_notified_status")
        notify = bool(args.force)
        if changed and item["status"] in ALERT_ON_CHANGE:
            notify = True
        if item["status"] in NEAR and last_near_date != now.date().isoformat():
            notify = True
        if changed:
            events.append(
                {
                    "at": now.isoformat(),
                    "plan_id": plan.get("plan_id"),
                    "code": code,
                    "name": item.get("name"),
                    "status": item["status"],
                    "price": item["current_price"],
                    "action": item["action"],
                }
            )
        if notify:
            alerts.append(fmt_action(item, item["status"], item["current_price"], item["action"]))
            item["last_notified_status"] = item["status"]
            item["last_notified_at"] = now.isoformat()
        else:
            item["last_notified_status"] = last_notified_status
            item["last_notified_at"] = prev.get("last_notified_at")
        if item["status"] in NEAR:
            item["last_near_date"] = now.date().isoformat()
        else:
            item["last_near_date"] = None

    data_asof = next((item.get("asof") for item in items if item.get("asof")), None)
    status = {
        "plan_id": plan.get("plan_id"),
        "checked_at": now.isoformat(),
        "source": args.mode,
        "data_asof": data_asof,
        "market": {
            "regime": (meta or {}).get("regime"),
            "breadth": breadth_for(hist),
            "bank_sector": breadth_for(hist, "銀行業"),
        },
        "items": items,
        "today_actions": [
            f"{item['code']} {item['name']}：{item['action']}" for item in items
        ],
    }

    if not args.dry_run and not args.no_write:
        save_json(STATUS_PATH, status)
        event_doc = load_json(EVENTS_PATH, {"plan_id": plan.get("plan_id"), "events": []})
        event_doc["plan_id"] = plan.get("plan_id")
        event_doc["events"] = (event_doc.get("events") or []) + events
        event_doc["events"] = event_doc["events"][-500:]
        save_json(EVENTS_PATH, event_doc)

    print(f"作戦チェック: {plan.get('plan_id')} / {args.mode} / {source}")
    for line in status["today_actions"]:
        print(f"  - {line}")

    if args.dry_run:
        print("dry-run のため通知・保存はしていません。")
        return 0

    if alerts:
        header = f"【作戦トリガー】{plan.get('plan_id')}\n"
        send_discord(DISCORD_URL, header + "\n".join(alerts) + f"\n{WEB_URL}")
    if args.summary:
        lines = status["today_actions"][:12]
        send_discord(
            DISCORD_URL,
            f"【今日の作戦】{plan.get('plan_id')}\n" + "\n".join(lines) + f"\n{WEB_URL}",
        )
    if args.notify_plan:
        send_discord(DISCORD_URL, fmt_plan_update(plan, status))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
