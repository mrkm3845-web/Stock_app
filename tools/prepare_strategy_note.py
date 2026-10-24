#!/usr/bin/env python3
"""作戦ノート用の材料を集める汎用スクリプト（Windows/Mac/Linux）。

ルート直下の最新 tradehistory(JP)_*.csv を読み、保有・実現損益・
地合い・セクター・最新ピック・週次・来週プランを
tools/out/strategy_context.json にまとめます。

このファイルはローカル専用です。tools/out/ はコミットしません。
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[1]
OUT_DEFAULT = ROOT / "tools" / "out" / "strategy_context.json"


def read_json(path: Path, default=None):
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:  # noqa: BLE001
        print(f"警告: JSONを読めません: {path} ({exc})")
        return default


def read_history(path: Path):
    if not path.exists():
        return None
    try:
        if path.suffix == ".gz":
            with gzip.open(path, "rt", encoding="utf-8") as f:
                return json.load(f)
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:  # noqa: BLE001
        print(f"警告: 履歴を読めません: {path} ({exc})")
        return None


def to_num(value) -> float:
    if value is None:
        return 0.0
    text = str(value).replace(",", "").strip()
    if not text:
        return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


def to_date(value):
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y/%m/%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def latest_trade_history() -> Path:
    files = [
        p
        for p in ROOT.glob("tradehistory(JP)_*.csv")
        if p.is_file()
    ]
    if not files:
        raise SystemExit("ルートに tradehistory(JP)_*.csv がありません。")
    return max(files, key=lambda p: (p.stat().st_mtime, p.name))


def load_trades(path: Path):
    rows = []
    # 楽天証券のCSVはShift-JIS(cp932)。UTF-8で読めた場合はそちらを使う。
    for encoding in ("cp932", "utf-8-sig"):
        try:
            with path.open("r", encoding=encoding, newline="") as f:
                reader = csv.DictReader(f)
                rows = list(reader)
            break
        except UnicodeDecodeError:
            continue
    if not rows:
        raise SystemExit(f"取引履歴を読めません: {path.name}")
    return rows


def build_positions(rows):
    lots = defaultdict(list)  # code -> [{qty, price, date}]
    realized = {}
    names = {}
    last_trade = None

    for row in rows:
        code = str(row.get("銘柄コード") or "").strip()
        if not code:
            continue
        name = str(row.get("銘柄名") or "").strip()
        names[code] = name
        qty = int(to_num(row.get("数量［株］")))
        price = to_num(row.get("単価［円］"))
        side = str(row.get("売買区分") or "").strip()
        date = to_date(row.get("約定日"))

        if code not in realized:
            realized[code] = {
                "code": code,
                "name": name,
                "qty_bought": 0,
                "qty_sold": 0,
                "realized_pnl": 0.0,
                "last_trade_date": date,
            }
        item = realized[code]
        if date:
            if item["last_trade_date"] is None or date > item["last_trade_date"]:
                item["last_trade_date"] = date
            if last_trade is None or date > last_trade:
                last_trade = date

        if side == "買付":
            lots[code].append({"qty": qty, "price": price, "date": date})
            item["qty_bought"] += qty
        elif side == "売付":
            remaining = qty
            while remaining > 0 and lots[code]:
                lot = lots[code][0]
                take = min(remaining, int(lot["qty"]))
                item["realized_pnl"] += (price - float(lot["price"])) * take
                lot["qty"] = int(lot["qty"]) - take
                remaining -= take
                if int(lot["qty"]) <= 0:
                    lots[code].pop(0)
            if remaining > 0:
                print(f"警告: 売付が買付を超過: {code} {name} / {remaining}株")
            item["qty_sold"] += qty

    positions = []
    for code, code_lots in lots.items():
        open_lots = [lot for lot in code_lots if int(lot["qty"]) > 0]
        if not open_lots:
            continue
        qty = sum(int(lot["qty"]) for lot in open_lots)
        cost = sum(int(lot["qty"]) * float(lot["price"]) for lot in open_lots) / qty
        item = realized[code]
        positions.append(
            {
                "code": code,
                "name": names.get(code, item["name"]),
                "qty": qty,
                "avg_cost": round(cost, 2),
                "last_trade_date": item["last_trade_date"].isoformat()
                if item["last_trade_date"]
                else None,
            }
        )

    realized_list = []
    for item in realized.values():
        if item["qty_sold"] <= 0:
            continue
        realized_list.append(
            {
                "code": item["code"],
                "name": item["name"],
                "qty_bought": item["qty_bought"],
                "qty_sold": item["qty_sold"],
                "realized_pnl": round(item["realized_pnl"], 0),
                "last_trade_date": item["last_trade_date"].isoformat()
                if item["last_trade_date"]
                else None,
            }
        )
    realized_list.sort(key=lambda x: x["last_trade_date"] or "", reverse=True)

    return positions, realized_list, last_trade, names


def enrich_positions(positions, hist):
    price_map = {str(item.get("code")): item for item in (hist or [])}
    for pos in positions:
        record = price_map.get(pos["code"])
        if not record:
            pos.update(
                {
                    "last_price": None,
                    "market_value": None,
                    "unrealized_pnl": None,
                    "unrealized_pct": None,
                }
            )
            continue
        last_price = float(record.get("price") or 0)
        pos["last_price"] = last_price or None
        if last_price:
            pos["market_value"] = round(last_price * pos["qty"], 0)
            pos["unrealized_pnl"] = round((last_price - pos["avg_cost"]) * pos["qty"], 0)
            pos["unrealized_pct"] = round((last_price / pos["avg_cost"] - 1) * 100, 2)
        else:
            pos.update(
                {"market_value": None, "unrealized_pnl": None, "unrealized_pct": None}
            )
    return positions


def compute_breadth_and_sectors(hist):
    if not hist:
        return None, []
    # 週足が下向き（excluded）の銘柄も含めて数える。除外してから広がりを測ると
    # 「上昇中の銘柄だけ」を集めることになり、数値が上振れする（銀行など母数が消える）。
    valid = [item for item in hist if float(item.get("price") or 0) > 0]
    if not valid:
        return None, []

    def pct(values, predicate):
        n = len(values)
        return round(sum(1 for value in values if predicate(value)) / n * 100, 1) if n else None

    ret5 = sorted(float(item.get("ctx", {}).get("ret_5d_pct") or 0) for item in valid)
    ret20 = sorted(float(item.get("ctx", {}).get("ret_20d_pct") or 0) for item in valid)

    def median(values):
        return values[len(values) // 2] if values else None

    breadth = {
        "n": len(valid),
        "above_sma200_pct": pct(
            valid, lambda item: float(item.get("ctx", {}).get("dist_sma200_pct") or 0) > 0
        ),
        "above_sma25_pct": pct(
            valid, lambda item: float(item.get("ctx", {}).get("dist_sma25_pct") or 0) > 0
        ),
        "avg_ret5_pct": round(sum(ret5) / len(ret5), 2) if ret5 else None,
        "median_ret5_pct": round(median(ret5), 2) if ret5 else None,
        "avg_ret20_pct": round(sum(ret20) / len(ret20), 2) if ret20 else None,
        "median_ret20_pct": round(median(ret20), 2) if ret20 else None,
    }

    groups = defaultdict(list)
    for item in valid:
        groups[str(item.get("sector") or "不明")].append(item)

    sectors = []
    for sector, items in groups.items():
        if len(items) < 5:
            continue
        g5 = [float(item.get("ctx", {}).get("ret_5d_pct") or 0) for item in items]
        g20 = [float(item.get("ctx", {}).get("ret_20d_pct") or 0) for item in items]
        sectors.append(
            {
                "sector": sector,
                "n": len(items),
                "low_sample": len(items) < 10,
                "avg_ret5_pct": round(sum(g5) / len(g5), 2),
                "avg_ret20_pct": round(sum(g20) / len(g20), 2),
                "above_sma25_pct": pct(
                    items,
                    lambda item: float(item.get("ctx", {}).get("dist_sma25_pct") or 0) > 0,
                ),
                "above_sma200_pct": pct(
                    items,
                    lambda item: float(item.get("ctx", {}).get("dist_sma200_pct") or 0) > 0,
                ),
            }
        )
    sectors.sort(key=lambda x: x["avg_ret5_pct"] if x["avg_ret5_pct"] is not None else 0)
    return breadth, sectors


def select_fields(items, fields):
    if not items:
        return []
    return [{field: item.get(field) for field in fields} for item in items]


def main() -> int:
    parser = argparse.ArgumentParser(description="作戦ノート用コンテキストを作成")
    parser.add_argument("--out", default=str(OUT_DEFAULT), help="出力JSON")
    parser.add_argument("--recent", type=int, default=30, help="直近の約定件数")
    args = parser.parse_args()

    csv_path = latest_trade_history()
    rows = load_trades(csv_path)
    positions, realized, last_trade, names = build_positions(rows)

    hist = read_history(ROOT / "docs" / "history" / "latest.json.gz")
    if hist is None:
        hist = read_history(ROOT / "docs" / "history" / "latest.json")
    positions = enrich_positions(positions, hist)
    breadth, sectors = compute_breadth_and_sectors(hist)

    rec = read_json(ROOT / "docs" / "recommendations.json", {})
    dates = read_json(ROOT / "docs" / "history" / "dates.json", [])
    meta = read_json(ROOT / "docs" / "history" / "meta.json", {})
    params = read_json(ROOT / "docs" / "strategy_params.json", {})
    weekly = read_json(ROOT / "docs" / "weekly" / "latest.json", {})
    plan = read_json(ROOT / "docs" / "weekly" / "plan.json", {})
    playbook = read_json(ROOT / "docs" / "ai_playbook.json", {})
    earnings = read_json(ROOT / "docs" / "earnings.json", {})

    pick_fields = [
        "code",
        "name",
        "sector",
        "price",
        "score",
        "verdict",
        "rank",
        "entry_type",
        "entry_price",
        "entry_zone_low",
        "entry_zone_high",
        "tp_price",
        "sl_price",
        "suggested_qty",
        "overheat_level",
        "rsi14",
        "dist_sma25_pct",
        "pos_52w",
        "earnings_date",
        "earnings_soon",
        "pattern_summary",
    ]

    earnings_soon = []
    for code, item in (earnings.get("items") or {}).items():
        if item.get("soon"):
            earnings_soon.append(
                {
                    "code": code,
                    "date": item.get("date"),
                    "days_until": item.get("days_until"),
                }
            )
    earnings_soon.sort(key=lambda x: x.get("days_until") or 9999)

    recent_trades = [
        {
            "date": row.get("約定日"),
            "code": row.get("銘柄コード"),
            "name": row.get("銘柄名"),
            "side": row.get("売買区分"),
            "qty": int(to_num(row.get("数量［株］"))),
            "price": to_num(row.get("単価［円］")),
        }
        for row in rows[-max(1, args.recent) :]
    ]

    now = datetime.now(JST)
    context = {
        "generated_at": now.isoformat(),
        "timestamp": now.strftime("%Y-%m-%d-%H%M"),
        "timezone": "JST",
        "trade_history": {
            "file": csv_path.name,
            "last_trade_date": last_trade.isoformat() if last_trade else None,
            "rows": len(rows),
        },
        "positions": positions,
        "realized": realized,
        "total_realized_pnl": round(sum(item["realized_pnl"] for item in realized), 0),
        "recent_trades": recent_trades,
        "market": {
            "data_date": rec.get("date") or (dates[0] if dates else None),
            "score_version": rec.get("score_version"),
            "regime": meta.get("regime"),
            "portfolio": params.get("portfolio"),
            "recommendations": {
                "date": rec.get("date"),
                "generated_at": rec.get("generated_at"),
                "overall": rec.get("overall"),
                "picks": select_fields(rec.get("picks"), pick_fields),
                "picks_in_range": select_fields(rec.get("picks_in_range"), pick_fields),
                "range": rec.get("range"),
            },
            "breadth": breadth,
            "sectors": sectors,
            "weekly": {
                "week": weekly.get("week"),
                "start": weekly.get("start"),
                "end": weekly.get("end"),
                "summary": weekly.get("summary"),
                "ranking": weekly.get("ranking"),
                "ai_calibration": weekly.get("ai_calibration"),
                "top_n": weekly.get("top_n"),
                "notes": weekly.get("notes"),
                "feedback": weekly.get("feedback"),
            }
            if weekly
            else None,
            "plan": plan.get("plan") if plan else None,
            "playbook": {
                "generated_at": playbook.get("generated_at"),
                "stats": playbook.get("stats"),
                "lessons": playbook.get("lessons"),
                "score_ranking": playbook.get("score_ranking"),
            }
            if playbook
            else None,
            "earnings_soon": earnings_soon[:30],
        },
        "warnings": [],
        "note": "このファイルはローカル専用です。tools/out/ はコミットしません。",
    }

    out_path = Path(args.out)
    if not out_path.is_absolute():
        out_path = ROOT / out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(context, f, ensure_ascii=False, indent=2)

    print(f"作戦ノート用コンテキスト: {out_path}")
    print(f"取引履歴: {csv_path.name} / 最終約定: {context['trade_history']['last_trade_date']}")
    print(f"市場データ: {context['market']['data_date']} / スコア: {context['market']['score_version']}")
    if positions:
        pos_text = ", ".join(f"{p['code']} {p['name']} {p['qty']}株" for p in positions)
        print(f"保有: {pos_text}")
    else:
        print("保有: なし")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
