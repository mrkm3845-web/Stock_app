import argparse
import io
import json
import math
import os
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
import pandas as pd
import requests
import yfinance as yf

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
DOCS_DIR = "docs"
DATA_DIR = "data"
HISTORY_DIR = os.path.join(DOCS_DIR, "history")
DB_PATH = os.path.join(DATA_DIR, "stocks.db")


# 対象日付の取得（朝9:00前なら前日営業日扱い）
def get_target_date_str():
    now = datetime.now()
    if now.hour < 9:
        return (now - timedelta(days=1)).strftime("%Y-%m-%d")
    return now.strftime("%Y-%m-%d")


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


# 2. 個別銘柄のデータ取得＆テクニカル計算
def analyze_single_stock(stock_info, market_label):
    code = stock_info["コード"]
    name = stock_info["銘柄名"]
    sector = stock_info.get("33業種区分", "その他")

    ticker = yf.Ticker(f"{code}.T")
    info = None
    hist = None

    for attempt in range(2):
        try:
            info = ticker.info
            hist = ticker.history(period="2mo")
            if info and not hist.empty and len(hist) >= 26:
                break
        except Exception:
            time.sleep(0.3)

    if not info or hist is None or hist.empty or len(hist) < 26:
        return None

    try:
        current_price = info.get("currentPrice") or info.get("regularMarketPrice") or hist["Close"].iloc[-1]
        if not current_price or current_price <= 0:
            return None

        # ファンダメンタルズ
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

        roe_pct = (roe * 100) if (roe is not None and roe < 1.0) else (roe if roe else 0.0)
        op_margin_pct = (op_margin * 100) if (op_margin is not None and op_margin < 1.0) else (op_margin if op_margin else 0.0)

        div_yield_pct = 0.0
        if div_yield is not None:
            div_yield_pct = (div_yield * 100) if div_yield < 0.20 else (div_yield if div_yield <= 15.0 else 0.0)
        elif div_rate and current_price > 0:
            calc_yield = (div_rate / current_price) * 100
            div_yield_pct = calc_yield if calc_yield <= 15.0 else 0.0

        is_kabumini = market_label == "プライム" or (current_price >= 300 and current_price <= 50000)

        # スイング用テクニカル＆売買代金計算
        closes = hist["Close"]
        lows = hist["Low"]
        highs = hist["High"]
        volumes = hist["Volume"]
        trading_values = (closes * volumes) / 1000  # 千円単位

        sma5_series = closes.rolling(window=5).mean()
        sma25_series = closes.rolling(window=25).mean()

        # ゴールデンクロス判定（過去10営業日以内）
        gc_days = None
        for d in range(min(10, len(closes) - 26)):
            idx_today = len(closes) - 1 - d
            idx_yesterday = idx_today - 1
            if sma5_series.iloc[idx_today] > sma25_series.iloc[idx_today] and sma5_series.iloc[idx_yesterday] <= sma25_series.iloc[idx_yesterday]:
                gc_days = d
                break

        # 前日までの5日平均売買代金（千円）
        avg_val_5d = int(trading_values.iloc[-6:-1].mean()) if len(trading_values) >= 6 else int(trading_values.mean())

        # 5営業日前からの売買代金増加率
        val_today = trading_values.iloc[-1]
        val_5d_ago = trading_values.iloc[-6] if len(trading_values) >= 6 else trading_values.iloc[0]
        val_ratio_5d = round(val_today / val_5d_ago, 2) if val_5d_ago > 0 else 1.0

        # ★ スイング戦略用のサポート＆レジスタンス指標
        latest_sma5 = int(round(sma5_series.iloc[-1]))
        latest_sma25 = int(round(sma25_series.iloc[-1]))
        low_5d = int(round(lows.iloc[-5:].min()))      # 直近5日の最安値（直近サポート）
        high_20d = int(round(highs.iloc[-20:].max()))  # 直近20日の最高値（ターゲット）

        return {
            "code": code,
            "name": name,
            "market": market_label,
            "sector": sector,
            "is_mini": is_kabumini,
            "price": int(round(current_price)),
            "graham_price": int(round(graham_price)),
            "discount_rate": round(discount_rate, 1),
            "mix_index": round(mix_index, 2),
            "per": round(pe, 1),
            "pbr": round(pb, 2),
            "roe": round(roe_pct, 1),
            "op_margin": round(op_margin_pct, 1),
            "div_yield": round(div_yield_pct, 2),
            "gc_days": gc_days,
            "avg_val_5d": avg_val_5d,
            "val_ratio_5d": val_ratio_5d,
            # ★ 追加データ
            "sma5": latest_sma5,
            "sma25": latest_sma25,
            "low_5d": low_5d,
            "high_20d": high_20d
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
def save_to_sqlite(all_stocks, target_date):
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS daily_stocks (
            date TEXT, code TEXT, name TEXT, market TEXT, sector TEXT,
            price REAL, graham_price REAL, discount_rate REAL, mix_index REAL,
            per REAL, pbr REAL, roe REAL, op_margin REAL, div_yield REAL,
            is_mini INTEGER DEFAULT 1,
            gc_days INTEGER, avg_val_5d INTEGER, val_ratio_5d REAL,
            sma5 INTEGER, sma25 INTEGER, low_5d INTEGER, high_20d INTEGER,
            PRIMARY KEY (date, code)
        )
    """)

    for col, col_type in [("gc_days", "INTEGER"), ("avg_val_5d", "INTEGER"), ("val_ratio_5d", "REAL"),
                          ("sma5", "INTEGER"), ("sma25", "INTEGER"), ("low_5d", "INTEGER"), ("high_20d", "INTEGER")]:
        try:
            c.execute(f"ALTER TABLE daily_stocks ADD COLUMN {col} {col_type}")
        except Exception:
            pass

    for s in all_stocks:
        c.execute(
            """
            INSERT OR REPLACE INTO daily_stocks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
            (
                target_date,
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
                1 if s.get("is_mini") else 0,
                s.get("gc_days"),
                s.get("avg_val_5d"),
                s.get("val_ratio_5d"),
                s.get("sma5"),
                s.get("sma25"),
                s.get("low_5d"),
                s.get("high_20d"),
            ),
        )
    conn.commit()
    conn.close()
    print(f">> SQLite DB ({DB_PATH}) に [{target_date}] 分 {len(all_stocks)} 件保存しました。")


# 5. 日別JSON保存
def save_history_json(all_stocks, target_date):
    os.makedirs(HISTORY_DIR, exist_ok=True)

    for filename in [f"{target_date}.json", "latest.json"]:
        with open(os.path.join(HISTORY_DIR, filename), "w", encoding="utf-8") as f:
            json.dump(all_stocks, f, ensure_ascii=False)

    dates_file = os.path.join(HISTORY_DIR, "dates.json")
    existing_dates = []
    if os.path.exists(dates_file):
        try:
            with open(dates_file, "r", encoding="utf-8") as f:
                existing_dates = json.load(f)
        except Exception:
            pass
    if target_date not in existing_dates:
        existing_dates.append(target_date)
    existing_dates.sort(reverse=True)
    with open(dates_file, "w", encoding="utf-8") as f:
        json.dump(existing_dates, f, ensure_ascii=False)
    print(f">> 日別JSON (docs/history/{target_date}.json) を保存しました。")


# 6. Discord通知
def send_to_discord(all_stocks, added_count, updated_count, target_date, webhook_url):
    if not webhook_url or not all_stocks:
        return

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    df = pd.DataFrame(all_stocks)

    if added_count == 0 and updated_count == 0:
        msg = f"✅ **【データ確認】** ({now_str})\n対象日: `{target_date}` ➔ 前回から変更なし（現在 計{len(all_stocks)}社マージ済・正常稼働中）。\n👉 Webスクリーナー: https://mrkm3845-web.github.io/Stock_app/"
        try:
            requests.post(webhook_url, json={"content": msg}, timeout=10)
        except Exception:
            pass
        return

    valid_df = df[(df["roe"] >= 7.0) & (df["op_margin"] >= 6.0)].sort_values(by="mix_index", ascending=True)

    def make_value_section(market_name):
        m_df = valid_df[valid_df["market"] == market_name]
        ultra = m_df[m_df["mix_index"] < 5.625]
        strict = m_df[(m_df["mix_index"] >= 5.625) & (m_df["mix_index"] < 11.25)]
        text = f"\n**【{market_name}市場】** (計 {len(m_df)} 件合致)\n```\n"
        text += f"{'コード':<5} {'社名':<8} {'割安度':<6} {'係数':<5} {'利回り'}\n" + "-" * 38 + "\n"
        if not ultra.empty:
            text += "▼ 🔥 超・割安 (係数 < 5.625)\n"
            for _, r in ultra.head(3).iterrows():
                sname = (r["name"][:6] + "..") if len(r["name"]) > 6 else r["name"]
                text += f"{r['code']:<6} {sname:<8} +{r['discount_rate']}% {r['mix_index']:<5.2f} {r['div_yield']}%\n"
        if not strict.empty:
            text += "▼ 🎯 厳選割安 (係数 < 11.25)\n"
            for _, r in strict.head(3).iterrows():
                sname = (r["name"][:6] + "..") if len(r["name"]) > 6 else r["name"]
                text += f"{r['code']:<6} {sname:<8} +{r['discount_rate']}% {r['mix_index']:<5.2f} {r['div_yield']}%\n"
        return text + "```"

    swing_df = df[
        (df["gc_days"].notna()) & 
        (df["gc_days"] <= 3) & 
        (df["avg_val_5d"] >= 50000) & 
        (df["val_ratio_5d"] >= 1.5)
    ].sort_values(by="val_ratio_5d", ascending=False)

    swing_section = "\n**🚀 【スイング注目】GC×出来高急増** (GC3日以内 / 5日平均5千万↑ / 増加率1.5倍↑)\n"
    if not swing_df.empty:
        swing_section += f"```\n{'コード':<5} {'社名':<8} {'GC':<5} {'売買代金':<7} {'増加率'}\n" + "-" * 38 + "\n"
        for _, r in swing_df.head(5).iterrows():
            sname = (r["name"][:6] + "..") if len(r["name"]) > 6 else r["name"]
            gc_label = "当日GC" if r["gc_days"] == 0 else f"{int(r['gc_days'])}日前"
            val_k = r["avg_val_5d"]
            val_str = f"¥{val_k/100000:.1f}億" if val_k >= 100000 else f"¥{int(val_k/10)}万"
            swing_section += f"{r['code']:<6} {sname:<8} {gc_label:<5} {val_str:<7} {r['val_ratio_5d']}倍\n"
        swing_section += "```"
    else:
        swing_section += "└ ※本日の条件合致銘柄はありません (0件)\n"

    msg = f"📊 **【株式自動スクリーニング速報】** ({now_str})\n"
    msg += f"📅 対象営業日: **`{target_date}`** (新規: +{added_count}件 / 更新: {updated_count}件)\n"
    msg += make_value_section("プライム") + make_value_section("スタンダード")
    msg += swing_section
    msg += "\n👉 Webスクリーナー: https://mrkm3845-web.github.io/Stock_app/"

    try:
        requests.post(webhook_url, json={"content": msg}, timeout=10)
    except Exception:
        pass


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

        for root, dirs, files in os.walk(DATA_DIR):
            for file in files:
                filepath = os.path.join(root, file)
                if "prime" in file.lower() and file.endswith(".json"):
                    try:
                        with open(filepath, "r", encoding="utf-8") as f:
                            prime_data = json.load(f)
                    except Exception:
                        pass
                elif "standard" in file.lower() and file.endswith(".json"):
                    try:
                        with open(filepath, "r", encoding="utf-8") as f:
                            standard_data = json.load(f)
                    except Exception:
                        pass

        new_batch = prime_data + standard_data
        target_date = get_target_date_str()
        target_file = os.path.join(HISTORY_DIR, f"{target_date}.json")

        existing_stocks = []
        if os.path.exists(target_file):
            try:
                with open(target_file, "r", encoding="utf-8") as f:
                    existing_stocks = json.load(f)
            except Exception:
                pass

        merged_dict = {s["code"]: s for s in existing_stocks}
        added_count = 0
        updated_count = 0

        for s in new_batch:
            code = s["code"]
            if code not in merged_dict:
                merged_dict[code] = s
                added_count += 1
            else:
                if merged_dict[code] != s:
                    merged_dict[code] = s
                    updated_count += 1

        final_stocks = list(merged_dict.values())
        print(f">> 【{target_date}】マージ結果: 既存 {len(existing_stocks)} 件 ➔ 合計 {len(final_stocks)} 件 (新規 +{added_count}, 更新 {updated_count})")

        if final_stocks:
            save_to_sqlite(final_stocks, target_date)
            save_history_json(final_stocks, target_date)
            send_to_discord(
                final_stocks,
                added_count,
                updated_count,
                target_date,
                DISCORD_WEBHOOK_URL,
            )
            print(">> 全体の合体＆ORマージ更新が完了しました！")
