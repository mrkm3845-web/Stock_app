import argparse
import io
import json
import math
import os
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import pandas as pd
import requests
import yfinance as yf

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
DOCS_DIR = "docs"
DATA_DIR = "data"
HISTORY_DIR = os.path.join(DOCS_DIR, "history")
DB_PATH = os.path.join(DATA_DIR, "stocks.db")


# 1. JPX銘柄取得
def fetch_jpx_stock_list(keyword):
    url = "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xls"
    try:
        res = requests.get(
            url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30
        )
        df = pd.read_excel(io.BytesIO(res.content))
        df = df[["コード", "銘柄名", "市場・商品区分", "33業種区分"]]
        df["コード"] = (
            df["コード"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
        )
        df_filtered = df[df["市場・商品区分"].str.contains(keyword, na=False)]
        return df_filtered.to_dict("records")
    except Exception:
        return []


# 2. 個別銘柄のデータ取得
def analyze_single_stock(stock_info, market_label):
    code = stock_info["コード"]
    name = stock_info["銘柄名"]
    sector = stock_info.get("33業種区分", "その他")

    info = None
    for attempt in range(2):
        try:
            ticker = yf.Ticker(f"{code}.T")
            info = ticker.info
            if info and (
                info.get("currentPrice") or info.get("regularMarketPrice")
            ):
                break
        except Exception:
            time.sleep(0.3)

    if not info:
        return None

    try:
        current_price = info.get("currentPrice") or info.get(
            "regularMarketPrice"
        )
        if not current_price or current_price <= 0:
            return None

        eps = info.get("trailingEps")
        bps = info.get("bookValue")
        pe = info.get("trailingPE") or info.get("forwardPE")
        pb = info.get("priceToBook")
        roe = info.get("returnOnEquity")
        op_margin = info.get("operatingMargins")
        div_yield = info.get("dividendYield")
        div_rate = info.get("dividendRate")

        if not (eps and bps and pe and pb and eps > 0 and bps > 0):
            return None
        if bps > current_price * 10 or eps > current_price * 2:
            return None
        if pe <= 0.5 or pe > 200.0 or pb <= 0.05 or pb > 30.0:
            return None

        mix_index = pe * pb
        graham_price = math.sqrt(22.5 * eps * bps)
        discount_rate = ((graham_price - current_price) / graham_price) * 100
        if discount_rate > 85.0 or discount_rate < -300.0:
            return None

        roe_pct = (
            (roe * 100)
            if (roe is not None and roe < 1.0)
            else (roe if roe else 0.0)
        )
        op_margin_pct = (
            (op_margin * 100)
            if (op_margin is not None and op_margin < 1.0)
            else (op_margin if op_margin else 0.0)
        )

        div_yield_pct = 0.0
        if div_yield is not None:
            div_yield_pct = (
                (div_yield * 100)
                if div_yield < 0.20
                else (div_yield if div_yield <= 15.0 else 0.0)
            )
        elif div_rate and current_price > 0:
            calc_yield = (div_rate / current_price) * 100
            div_yield_pct = calc_yield if calc_yield <= 15.0 else 0.0

        return {
            "code": code,
            "name": name,
            "market": market_label,
            "sector": sector,
            "price": int(round(current_price)),
            "graham_price": int(round(graham_price)),
            "discount_rate": round(discount_rate, 1),
            "mix_index": round(mix_index, 2),
            "per": round(pe, 1),
            "pbr": round(pb, 2),
            "roe": round(roe_pct, 1),
            "op_margin": round(op_margin_pct, 1),
            "div_yield": round(div_yield_pct, 2),
        }
    except Exception:
        return None


# 3. 並列スキャン
def scan_market(keyword, label):
    stocks = fetch_jpx_stock_list(keyword)
    print(f">> 【{label}】{len(stocks)} 件スキャン開始...")
    results = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(analyze_single_stock, s, label): s for s in stocks
        }
        for future in as_completed(futures):
            res = future.result()
            if res:
                results.append(res)
    print(f">> 【{label}】抽出完了: {len(results)} 件")
    return results


# 4. SQLite保存
def save_to_sqlite(all_stocks):
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS daily_stocks (
            date TEXT, code TEXT, name TEXT, market TEXT, sector TEXT,
            price REAL, graham_price REAL, discount_rate REAL, mix_index REAL,
            per REAL, pbr REAL, roe REAL, op_margin REAL, div_yield REAL,
            PRIMARY KEY (date, code)
        )
    """)
    today_str = datetime.now().strftime("%Y-%m-%d")
    for s in all_stocks:
        c.execute(
            """
            INSERT OR REPLACE INTO daily_stocks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
            (
                today_str,
                s["code"],
                s["name"],
                s["market"],
                s["sector"],
                s["price"],
                s["graham_price"],
                s["discount_rate"],
                s["mix_index"],
                s["per"],
                s["pbr"],
                s["roe"],
                s["op_margin"],
                s["div_yield"],
            ),
        )
    conn.commit()
    conn.close()
    print(
        f">> SQLite DB ({DB_PATH}) に {len(all_stocks)} 件保存しました。"
    )


# 5. 日別JSON保存
def save_history_json(all_stocks):
    os.makedirs(HISTORY_DIR, exist_ok=True)
    today_str = datetime.now().strftime("%Y-%m-%d")
    for filename in [f"{today_str}.json", "latest.json"]:
        with open(
            os.path.join(HISTORY_DIR, filename), "w", encoding="utf-8"
        ) as f:
            json.dump(all_stocks, f, ensure_ascii=False)

    dates_file = os.path.join(HISTORY_DIR, "dates.json")
    existing_dates = []
    if os.path.exists(dates_file):
        try:
            with open(dates_file, "r", encoding="utf-8") as f:
                existing_dates = json.load(f)
        except Exception:
            pass
    if today_str not in existing_dates:
        existing_dates.append(today_str)
    existing_dates.sort(reverse=True)
    with open(dates_file, "w", encoding="utf-8") as f:
        json.dump(existing_dates, f, ensure_ascii=False)
    print(
        f">> 日別JSON (docs/history/{today_str}.json) を保存しました。"
    )


# 6. 同日内での変更チェック関数
def check_is_data_changed(new_stocks):
    today_str = datetime.now().strftime("%Y-%m-%d")
    today_file = os.path.join(HISTORY_DIR, f"{today_str}.json")
    if not os.path.exists(today_file):
        return True
    try:
        with open(today_file, "r", encoding="utf-8") as f:
            old_stocks = json.load(f)
        if len(new_stocks) != len(old_stocks):
            return True
        new_summary = {s["code"]: s["price"] for s in new_stocks}
        old_summary = {s["code"]: s["price"] for s in old_stocks}
        return new_summary != old_summary
    except Exception:
        return True


# 7. Discord通知
def send_to_discord(all_stocks, is_changed, webhook_url):
    if not webhook_url:
        print(">> ⚠️ DISCORD_WEBHOOK_URL が設定されていません。")
        return
    if not all_stocks:
        print(">> ⚠️ 送信する銘柄データが空です。")
        return

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    if not is_changed:
        msg = f"✅ **【データ確認】** ({now_str})\n前回取得時からデータに変更はありませんでした（正常稼働中）。\n👉 Webスクリーナー: https://mrkm3845-web.github.io/Stock_app/"
        try:
            res = requests.post(
                webhook_url, json={"content": msg}, timeout=10
            )
            print(f">> Discord送信完了 (変更なし): ステータス {res.status_code}")
        except Exception as e:
            print(f">> Discord送信エラー: {e}")
        return

    df = pd.DataFrame(all_stocks).sort_values(by="mix_index", ascending=True)
    valid_df = df[(df["roe"] >= 7.0) & (df["op_margin"] >= 6.0)]

    def make_section(market_name):
        m_df = valid_df[valid_df["market"] == market_name]
        ultra = m_df[m_df["mix_index"] < 5.625]
        strict = m_df[
            (m_df["mix_index"] >= 5.625) & (m_df["mix_index"] < 11.25)
        ]
        text = f"\n**【{market_name}市場】** (計 {len(m_df)} 件合致)\n```\n"
        text += f"{'コード':<5} {'社名':<8} {'割安度':<6} {'係数':<5} {'利回り'}\n" + "-" * 38 + "\n"
        if not ultra.empty:
            text += "▼ 🔥 超・割安 (係数 < 5.625)\n"
            for _, r in ultra.head(3).iterrows():
                sname = (
                    (r["name"][:6] + "..")
                    if len(r["name"]) > 6
                    else r["name"]
                )
                text += f"{r['code']:<6} {sname:<8} +{r['discount_rate']}% {r['mix_index']:<5.2f} {r['div_yield']}%\n"
        if not strict.empty:
            text += "▼ 🎯 厳選割安 (係数 < 11.25)\n"
            for _, r in strict.head(3).iterrows():
                sname = (
                    (r["name"][:6] + "..")
                    if len(r["name"]) > 6
                    else r["name"]
                )
                text += f"{r['code']:<6} {sname:<8} +{r['discount_rate']}% {r['mix_index']:<5.2f} {r['div_yield']}%\n"
        return text + "```"

    msg = f"📊 **【割安優良株スクリーニング速報】** ({now_str})\n"
    msg += make_section("プライム") + make_section("スタンダード")
    msg += "\n👉 Web簡易スクリーナー: https://mrkm3845-web.github.io/Stock_app/"
    try:
        res = requests.post(webhook_url, json={"content": msg}, timeout=10)
        print(f">> Discord送信完了 (フル通知): ステータス {res.status_code}")
    except Exception as e:
        print(f">> Discord送信エラー: {e}")


# ==========================================
# 実行部
# ==========================================
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--market", choices=["prime", "standard"])
    parser.add_argument("--aggregate", action="store_true")
    args = parser.parse_args()

    os.makedirs(DATA_DIR, exist_ok=True)

    if args.market == "prime":
        res = scan_market("プライム", "プライム")
        with open("data/prime.json", "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False)

    elif args.market == "standard":
        res = scan_market("スタンダード", "スタンダード")
        with open("data/standard.json", "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False)

    elif args.aggregate:
        print(">> data フォルダ内のJSONファイルを自動探索中...")
        prime_data = []
        standard_data = []

        # ★ data フォルダ配下の全階層から自動でファイルを発見して読み込む
        for root, dirs, files in os.walk(DATA_DIR):
            for file in files:
                filepath = os.path.join(root, file)
                if "prime" in file.lower() and file.endswith(".json"):
                    try:
                        with open(filepath, "r", encoding="utf-8") as f:
                            prime_data = json.load(f)
                            print(
                                f">> 読み込み成功: {filepath} ({len(prime_data)}件)"
                            )
                    except Exception as e:
                        print(f">> エラー {filepath}: {e}")

                elif "standard" in file.lower() and file.endswith(".json"):
                    try:
                        with open(filepath, "r", encoding="utf-8") as f:
                            standard_data = json.load(f)
                            print(
                                f">> 読み込み成功: {filepath} ({len(standard_data)}件)"
                            )
                    except Exception as e:
                        print(f">> エラー {filepath}: {e}")

        new_results = prime_data + standard_data
        print(f">> 合計読み込み件数: {len(new_results)} 件")

        today_str = datetime.now().strftime("%Y-%m-%d")
        today_file = os.path.join(HISTORY_DIR, f"{today_str}.json")

        existing_stocks = []
        if os.path.exists(today_file):
            try:
                with open(today_file, "r", encoding="utf-8") as f:
                    existing_stocks = json.load(f)
            except Exception:
                pass

        if existing_stocks and len(new_results) < len(existing_stocks):
            print(
                f">> ⚠️ 前回の件数({len(existing_stocks)}件)より今回の件数({len(new_results)}件)が少ないため、前回の完全データをキープします。"
            )
            send_to_discord(existing_stocks, False, DISCORD_WEBHOOK_URL)
        else:
            is_changed = check_is_data_changed(new_results)
            save_to_sqlite(new_results)
            save_history_json(new_results)
            send_to_discord(new_results, is_changed, DISCORD_WEBHOOK_URL)
            print(">> 全体の合体＆更新が完了しました！")
