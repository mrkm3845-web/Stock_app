#!/usr/bin/env python3
"""作戦台帳を作り直す必要があるかを判定する（読み取りのみ・書き込みなし）。

基準は「直近の確定終値」。
- 期待する取引日: 15:30（大引け）より前は前営業日
- 価格: docs/history/meta.json の date が期待日と一致し、その日の15:30以降に生成されている
- 売買: 取引履歴CSVの最終約定日と active.json の trade_history_last_date を比較

判定と終了コード:
  0  FRESH         更新不要（価格は最新・売買の変化なし・台帳も最新）
  2  STALE_PRICE   価格が古い（更新してから台帳を作り直す）
  3  STALE_TRADES  価格は最新だが売買が新しい（台帳だけ更新する）
  4  PARTIAL       当日の途中データが入っている（引け後に作り直す。今は作らない）
  5  NO_PLAN       作戦台帳がない
  6  NO_DATA       価格データがない

使い方:
  python tools/plan_staleness.py
  python tools/plan_staleness.py --json
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from refresh_data_if_stale import expected_trading_date  # noqa: E402

JST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[1]
META_PATH = ROOT / "docs" / "history" / "meta.json"
PLAN_PATH = ROOT / "docs" / "strategy" / "active.json"
REC_PATH = ROOT / "docs" / "recommendations.json"

CODES = {
    "FRESH": 0,
    "STALE_PRICE": 2,
    "STALE_TRADES": 3,
    "PARTIAL": 4,
    "NO_PLAN": 5,
    "NO_DATA": 6,
}


def read_json(path: Path, default=None):
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:  # noqa: BLE001
        print(f"警告: 読めません: {path} ({exc})", file=sys.stderr)
        return default


def parse_date(value):
    if not value:
        return None
    text = str(value).strip()[:10]
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def parse_datetime(value):
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text).replace(tzinfo=None)
    except ValueError:
        return None


def latest_trade_history_path():
    files = [p for p in ROOT.glob("tradehistory(JP)_*.csv") if p.is_file()]
    if not files:
        return None
    return max(files, key=lambda p: (p.stat().st_mtime, p.name))


def trade_history_last_date(path: Path | None):
    """取引履歴CSVの最終約定日を返す（読めなければ None）。"""
    if path is None:
        return None
    rows = []
    for encoding in ("cp932", "utf-8-sig"):
        try:
            with path.open("r", encoding=encoding, newline="") as f:
                rows = list(csv.DictReader(f))
            break
        except UnicodeDecodeError:
            continue
        except Exception:  # noqa: BLE001
            return None
    dates = [parse_date(row.get("約定日")) for row in rows]
    dates = [d for d in dates if d]
    return max(dates) if dates else None


def inspect(now: datetime | None = None):
    now = now or datetime.now(JST)
    expected = expected_trading_date(now)

    meta = read_json(META_PATH, {}) or {}
    rec = read_json(REC_PATH, {}) or {}
    plan = read_json(PLAN_PATH, {}) or {}

    meta_date = parse_date(meta.get("date"))
    generated_at = parse_datetime(meta.get("generated_at"))

    price_confirmed = bool(
        meta_date and generated_at
        and generated_at >= datetime.combine(meta_date, time(15, 30))
    )

    if not meta_date:
        price_state = "NO_DATA"
    elif meta_date > expected:
        price_state = "PARTIAL"
    elif meta_date < expected:
        price_state = "STALE_PRICE"
    elif not price_confirmed:
        price_state = "PARTIAL"
    else:
        price_state = "OK"

    csv_path = latest_trade_history_path()
    csv_last = trade_history_last_date(csv_path)
    plan_trades = parse_date(plan.get("trade_history_last_date"))
    trades_state = "UNKNOWN"
    if csv_last and plan_trades:
        trades_state = "NEW" if csv_last > plan_trades else "OK"
    elif csv_last and not plan_trades:
        trades_state = "NEW"

    plan_date = parse_date(plan.get("data_date"))

    if not plan:
        code = "NO_PLAN"
        reason = "作戦台帳（docs/strategy/active.json）がありません。"
    elif price_state == "NO_DATA":
        code = "NO_DATA"
        reason = "価格データ（docs/history）がありません。データ更新が必要です。"
    elif price_state == "PARTIAL":
        code = "PARTIAL"
        reason = "当日の途中データが入っています。引け後（15:30以降）に作り直してください。"
    elif price_state == "STALE_PRICE":
        code = "STALE_PRICE"
        reason = f"価格データが {meta_date} で古い（期待は {expected}）。更新してから作り直します。"
    elif not plan_date or plan_date < expected:
        code = "STALE_PRICE"
        reason = f"台帳の基準日が {plan_date or '未設定'} で古い（期待は {expected}）。作り直します。"
    elif trades_state == "NEW":
        code = "STALE_TRADES"
        reason = f"取引履歴が新しい（CSV {csv_last} / 台帳 {plan_trades}）。台帳を更新します。"
    else:
        code = "FRESH"
        reason = f"{expected} の確定終値で最新。売買の変化もなし。更新不要です。"

    return {
        "code": code,
        "exit_code": CODES[code],
        "reason": reason,
        "now": now.strftime("%Y-%m-%dT%H:%M:%S"),
        "expected_date": expected.isoformat(),
        "price_date": meta_date.isoformat() if meta_date else None,
        "price_generated_at": meta.get("generated_at"),
        "price_confirmed": price_confirmed,
        "price_state": price_state,
        "recommendations_date": rec.get("date"),
        "plan_id": plan.get("plan_id"),
        "plan_data_date": plan_date.isoformat() if plan_date else None,
        "plan_trade_history_last_date": plan_trades.isoformat() if plan_trades else None,
        "trade_history_csv": csv_path.name if csv_path else None,
        "trade_history_last_date": csv_last.isoformat() if csv_last else None,
        "trades_state": trades_state,
    }


def print_report(info):
    print(f"期待する取引日: {info['expected_date']}（15:30前は前営業日）")
    price = info["price_date"] or "なし"
    gen = info["price_generated_at"] or "-"
    print(f"価格データ:     {price}（生成 {gen}）→ {info['price_state']}")
    print(f"台帳の基準日:   {info['plan_data_date'] or 'なし'}（{info['plan_id'] or '-'}）")
    if info["trade_history_csv"]:
        print(
            f"取引履歴:       {info['trade_history_last_date'] or '不明'}"
            f"（台帳 {info['plan_trade_history_last_date'] or '未設定'}）→ {info['trades_state']}"
        )
    else:
        print("取引履歴:       CSVなし（判定は価格のみ）")
    print(f"判定: {info['code']}（終了コード {info['exit_code']}）")
    print(f"  {info['reason']}")


def main() -> int:
    parser = argparse.ArgumentParser(description="作戦台帳を作り直す必要があるかを判定")
    parser.add_argument("--json", action="store_true", help="JSONで出力")
    parser.add_argument(
        "--date",
        default=None,
        help="基準日時を上書き（テスト用・JSTの YYYY-MM-DDTHH:MM）",
    )
    args = parser.parse_args()

    now = None
    if args.date:
        now = datetime.strptime(args.date, "%Y-%m-%dT%H:%M").replace(tzinfo=JST)

    info = inspect(now)
    if args.json:
        print(json.dumps(info, ensure_ascii=False, indent=2))
    else:
        print_report(info)
    return info["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
