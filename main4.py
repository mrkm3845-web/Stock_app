import io
import json
import math
import os
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from curl_cffi import requests as cffi_requests
import pandas as pd
import requests
import yfinance as yf

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
DOCS_DIR = "docs"
DATA_DIR = "data"
HISTORY_DIR = os.path.join(DOCS_DIR, "history")
DB_PATH = os.path.join(DATA_DIR, "stocks.db")

SHARED_SESSION = cffi_requests.Session(impersonate="chrome")


# 1. JPX銘柄取得
def fetch_jpx_stock_list(market_name):
    url = "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xls"
    try:
        res = requests.get(
            url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30
        )
        df = pd.read_excel(io.BytesIO(res.content))
        df = df[["コード", "銘柄名", "市場・商品区分", "33業種区分"]]
        df["コード"] = df["コード"].astype(str)
        df_filtered = df[df["市場・商品区分"] == market_name]
        return df_filtered.to_dict("records")
    except Exception:
        return []


# 2. 個別銘柄のデータ取得（異常値・分割バグ完全ガード）
def analyze_single_stock(stock_info, market_name):
    code = stock_info["コード"]
    name = stock_info["銘柄名"]
    sector = stock_info.get("33業種区分", "その他")
    try:
        ticker = yf.Ticker(f"{code}.T", session=SHARED_SESSION)
        info = ticker.info
        if not info:
            return None

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
            "market": "プライム" if "プライム" in market_name else "スタンダード",
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
def scan_market(stocks_list, market_name, max_workers=6):
    results = []
    print(
        f">> 【{market_name}】全 {len(stocks_list)} 銘柄のスキャン開始..."
    )
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(analyze_single_stock, s, market_name): s
            for s in stocks_list
        }
        for future in as_completed(futures):
            res = future.result()
            if res:
                results.append(res)
    print(f">> 【{market_name}】抽出完了: {len(results)} 件")
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


# 6. 同日内での変更チェック関数（同日の2回目以降のみ差分チェック）
def check_is_data_changed(new_stocks):
    today_str = datetime.now().strftime("%Y-%m-%d")
    today_file = os.path.join(HISTORY_DIR, f"{today_str}.json")

    # 今日のファイルがまだ無い ＝ 「今日初めての実行（夕方）」なので必ずフル通知！
    if not os.path.exists(today_file):
        return True

    # 今日のファイルが既にある ＝ 「今日2回目の実行（夜など）」なので内容を比較
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
    if not webhook_url or not all_stocks:
        return

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    # 同日内でデータに変更がなかった場合
    if not is_changed:
        msg = f"✅ **【データ確認】** ({now_str})\n前回取得時からデータに変更はありませんでした（夕方の確定値と一致・正常稼働中）。\n👉 Webスクリーナー: https://mrkm3845-web.github.io/Stock_app/"
        try:
            requests.post(webhook_url, json={"content": msg}, timeout=10)
        except Exception:
            pass
        return

    # 日付が変わった初回、またはデータに差分（補完）があった場合はフル通知
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
        requests.post(webhook_url, json={"content": msg}, timeout=10)
    except Exception:
        pass


if __name__ == "__main__":
    prime_stocks = fetch_jpx_stock_list("プライム（内国株式）")
    prime_results = scan_market(prime_stocks, "プライム")
    time.sleep(3)
    standard_stocks = fetch_jpx_stock_list("スタンダード（内国株式）")
    standard_results = scan_market(standard_stocks, "スタンダード")

    all_results = prime_results + standard_results
    if all_results:
        # 1. 保存前に同日の変更有無を判定
        is_changed = check_is_data_changed(all_results)

        # 2. SQLite & 日別JSON保存
        save_to_sqlite(all_results)
        save_history_json(all_results)

        # 3. Discord通知（同日2回目で一致ならスッキリ確認通知）
        send_to_discord(all_results, is_changed, DISCORD_WEBHOOK_URL)
        print(">> 全ての処理が完了しました！")
