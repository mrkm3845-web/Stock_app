#!/usr/bin/env python3
"""手動の作戦作成前に、価格・スコア・AIが最新かを確認する。

- 最新の取引日（平日の夜なら当日、週末なら金曜）を求める
- docs/history の日付、recommendations の日付・価格ズレを確認
- 古ければ main8.py を実行して更新する
  - DEEPSEEK_API_KEY / GEMINI_API_KEY があれば AI も更新
  - なければテクニカルのみ更新し、AIは古いまま警告
- ローカル専用。コミットはしない。

使い方:
  python tools/refresh_data_if_stale.py --check
  python tools/refresh_data_if_stale.py --refresh
  python tools/refresh_data_if_stale.py --refresh --no-ai
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[1]
PYTHON_VERSION = "3.11"
DEPS = [
    "pandas",
    "numpy",
    "requests",
    "yfinance",
    "openpyxl",
    "xlrd",
    "pandas_market_calendars",
]


def read_json(path: Path, default=None):
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return default


def read_gzip_json(path: Path):
    if not path.exists():
        return None
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return None


def expected_trading_date(now: datetime):
    """直近の「確定した」取引日。15:30より前は前日扱い。"""
    days = []
    try:
        import pandas_market_calendars as mcal

        cal = mcal.get_calendar("JPX")
        start = (now.date() - timedelta(days=21)).isoformat()
        end = now.date().isoformat()
        days = [d.date() for d in cal.valid_days(start_date=start, end_date=end)]
        days = [d for d in days if d <= now.date()]
    except Exception:  # noqa: BLE001
        # カレンダーが無い場合は平日だけの簡易判定
        day = now.date()
        while day.weekday() >= 5:
            day -= timedelta(days=1)
        days = [day]

    if not days:
        return now.date()

    last = max(days)
    if last == now.date() and now.time() < time(15, 30):
        previous = [d for d in days if d < now.date()]
        if previous:
            last = max(previous)
    return last


def has_ai_key() -> bool:
    return bool(os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("GEMINI_API_KEY"))


def inspect(now: datetime | None = None):
    now = now or datetime.now(JST)
    expected = expected_trading_date(now)
    dates = read_json(ROOT / "docs" / "history" / "dates.json", []) or []
    meta = read_json(ROOT / "docs" / "history" / "meta.json", {}) or {}
    rec = read_json(ROOT / "docs" / "recommendations.json", {}) or {}
    hist = read_gzip_json(ROOT / "docs" / "history" / "latest.json.gz") or []
    hist_by_code = {str(item.get("code")): item for item in hist}

    hist_date = meta.get("date") or (dates[0] if dates else None)
    rec_date = rec.get("date")
    picks = rec.get("picks") or []

    mismatches = []
    for pick in picks:
        code = str(pick.get("code"))
        record = hist_by_code.get(code)
        if not record:
            continue
        ai_price = pick.get("price")
        last_price = record.get("price")
        try:
            ai_price = float(ai_price)
            last_price = float(last_price)
        except (TypeError, ValueError):
            continue
        if last_price <= 0:
            continue
        diff = abs(ai_price - last_price) / last_price
        if diff > 0.01:
            mismatches.append(
                {
                    "code": code,
                    "name": pick.get("name"),
                    "ai_price": ai_price,
                    "last_price": last_price,
                    "diff_pct": round(diff * 100, 1),
                }
            )

    tech_stale = bool(hist_date and str(hist_date) < expected.isoformat())
    if not hist_date:
        tech_stale = True
    ai_stale = bool(not rec) or (bool(rec_date) and str(rec_date) < expected.isoformat())
    if rec_date and str(rec_date) == expected.isoformat() and len(mismatches) >= 2:
        ai_stale = True

    return {
        "expected_date": expected.isoformat(),
        "history_date": hist_date,
        "recommendations_date": rec_date,
        "tech_stale": tech_stale,
        "ai_stale": ai_stale,
        "price_mismatches": mismatches,
        "ai_key": has_ai_key(),
    }


def print_report(info):
    print(f"期待する取引日: {info['expected_date']}")
    print(f"履歴の最新日:   {info['history_date'] or 'なし'}")
    print(f"AI推奨の日付:   {info['recommendations_date'] or 'なし'}")
    if info["price_mismatches"]:
        print("AI価格と最新終値のズレ:")
        for item in info["price_mismatches"][:5]:
            print(
                f"  - {item['code']} {item['name']}: "
                f"AI {item['ai_price']:,.0f} / 最新 {item['last_price']:,.0f} "
                f"({item['diff_pct']:+.1f}%)"
            )
    print(f"テクニカル: {'古い' if info['tech_stale'] else '最新'}")
    print(f"AI:         {'古い' if info['ai_stale'] else '最新'}")
    print(f"AIキー:     {'あり' if info['ai_key'] else 'なし'}")


def build_main8_cmd(use_ai: bool):
    uv = shutil.which("uv")
    if not uv:
        return None
    cmd = [uv, "run", "--no-project", "--python", PYTHON_VERSION]
    for dep in DEPS:
        cmd += ["--with", dep]
    cmd += ["python", "main8.py"]
    if use_ai:
        cmd += ["--ai", "--force-ai"]
    return cmd


def run_main8(use_ai: bool) -> int:
    cmd = build_main8_cmd(use_ai)
    if not cmd:
        print("uv が見つかりません。PROJECT_STATUS.md のコマンドで main8.py を実行してください。")
        return 1
    print("実行:", " ".join(cmd))
    result = subprocess.run(cmd, cwd=ROOT)
    return result.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="価格・スコア・AIの鮮度を確認")
    parser.add_argument("--check", action="store_true", help="確認だけ（既定）")
    parser.add_argument("--refresh", action="store_true", help="古ければ main8.py を実行")
    parser.add_argument("--no-ai", action="store_true", help="AIを更新しない（テクニカルのみ）")
    parser.add_argument("--dry-run", action="store_true", help="実行するコマンドだけ表示")
    args = parser.parse_args()

    info = inspect()
    print_report(info)
    fresh = not info["tech_stale"] and not info["ai_stale"]
    if fresh:
        print("データは最新です。")
        return 0
    if not args.refresh:
        print("古いデータがあります。--refresh を付けると更新します。")
        return 2

    use_ai = not args.no_ai and info["ai_key"]
    if info["ai_stale"] and not use_ai:
        print("AIキーがないため、テクニカルのみ更新します。")
        print("AIも更新するには GitHub Actions の『データ更新（AI）』を使うか、")
        print("DEEPSEEK_API_KEY / GEMINI_API_KEY を設定して再実行してください。")

    if args.dry_run:
        cmd = build_main8_cmd(use_ai)
        print("実行予定:", " ".join(cmd) if cmd else "uv が見つかりません")
        return 0

    code = run_main8(use_ai=use_ai)
    if code != 0:
        print(f"main8.py が失敗しました（終了コード {code}）。")
        return code

    print("\n更新後の状態:")
    after = inspect()
    print_report(after)
    if after["tech_stale"]:
        print("警告: テクニカルデータがまだ古いです（yfinanceの反映待ちの可能性）。")
    if after["ai_stale"]:
        print("警告: AI推奨がまだ古いです。作戦ではスコアと台帳を優先してください。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
