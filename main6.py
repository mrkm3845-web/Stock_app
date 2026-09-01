"""
割安優良株 ＆ スイング 高速・高精度自動スクリーナー (プロ仕様・完全網羅版)
"""

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
import numpy as np
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


# 1. JPX銘柄リスト取得
def fetch_jpx_stock_list(keyword):
    url = "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xls"
    try:
        res = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
        df = pd.read_excel(io.BytesIO(res.content))
        df = df[["コード", "銘柄名", "市場・商品区分", "33業種区分"]]
        df["コード"] = df["コード"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
        return df[df["市場・商品区分"].str.contains(keyword, na=False)].to_dict("records")
    except Exception as e:
        print(f">> JPX銘柄リスト取得エラー: {e}")
        return []


# 2. SQLiteからの財務データキャッシュ読み込み
def load_cached_fundamentals():
    if not os.path.exists(DB_PATH):
        return {}
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        c.execute("SELECT code, per, pbr, roe, op_margin, div_yield, graham_price FROM daily_stocks")
        rows = c.fetchall()
        cache = {}
        for r in rows:
            cache[r[0]] = {
                "per": r[1], "pbr": r[2], "roe": r[3], "op_margin": r[4],
                "div_yield": r[5], "graham_price": r[6]
            }
        conn.close()
        return cache
    except Exception:
        conn.close()
        return {}


# 3. 日足OHLCVのバッチ一括ダウンロード（取りこぼしゼロ）
def fetch_ohlcv_batch(codes):
    print(f">> 全 {len(codes)} 銘柄の日足株価データをバッチ一括取得中...")
    stock_dfs = {}
    batch_size = 100
    
    # 直近3ヶ月分
    start_date = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
    
    for i in range(0, len(codes), batch_size):
        batch = codes[i:i+batch_size]
        tickers = [f"{c}.T" for c in batch]
        try:
            data = yf.download(tickers, start=start_date, group_by='ticker', auto_adjust=True, progress=False, threads=True)
            for c in batch:
                try:
                    ticker_sym = f"{c}.T"
                    if ticker_sym in data.columns.levels[0]:
                        df = data[ticker_sym].dropna(how='all')[["Open", "High", "Low", "Close", "Volume"]].dropna()
                        if len(df) >= 25:
                            stock_dfs[c] = df
                except Exception:
                    pass
        except Exception as e:
            print(f"Batch download error: {e}")
            
    print(f">> 株価データ取得完了: {len(stock_dfs)} / {len(codes)} 銘柄")
    return stock_dfs


# 4. 個別銘柄のファンダメンタルズ取得（軽量並列）
def fetch_single_fundamental(stock):
    code = stock["コード"]
    try:
        ticker = yf.Ticker(f"{code}.T")
        info = ticker.info
        if not info:
            return code, None
        return code, info
    except Exception:
        return code, None


# 5. 市場全体のスキャン＆高精度テクニカル・ファンダメンタルズ統合
def scan_market(keyword, label):
    stocks = fetch_jpx_stock_list(keyword)
    print(f">> 【{label}】{len(stocks)} 件の本格スキャンを開始します...")
    
    codes = [s["コード"] for s in stocks]
    stock_map = {s["コード"]: s for s in stocks}
    
    # 1. 株価OHLCVを一括取得
    ohlcv_dict = fetch_ohlcv_batch(codes)
    
    # 2. 過去の財務キャッシュをロード
    cached_fund = load_cached_fundamentals()
    
    # 3. 財務データを並列取得（取得成功分で更新）
    print(f">> 【{label}】最新ファンダメンタルズ情報を取得中...")
    latest_fund = {}
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(fetch_single_fundamental, s): s["コード"] for s in stocks if s["コード"] in ohlcv_dict}
        for future in as_completed(futures):
            code, info = future.result()
            if info:
                latest_fund[code] = info
                
    results = []
    
    for code, df in ohlcv_dict.items():
        try:
            s_info = stock_map[code]
            name = s_info["銘柄名"]
            sector = s_info.get("33業種区分", "その他")
            
            closes = df["Close"]
            lows = df["Low"]
            highs = df["High"]
            volumes = df["Volume"]
            
            current_price = int(round(closes.iloc[-1]))
            if current_price <= 0:
                continue
                
            # --- テクニカル計算 ---
            trading_values = (closes * volumes) / 1000.0  # 千円単位
            sma5_series = closes.rolling(window=5).mean()
            sma25_series = closes.rolling(window=25).mean()
            
            # GC判定 (過去10営業日以内)
            gc_days = None
            for d in range(min(10, len(closes) - 2)):
                idx_today = len(closes) - 1 - d
                idx_yesterday = idx_today - 1
                if sma5_series.iloc[idx_today] > sma25_series.iloc[idx_today] and sma5_series.iloc[idx_yesterday] <= sma25_series.iloc[idx_yesterday]:
                    gc_days = d
                    break
                    
            avg_val_5d = int(trading_values.iloc[-6:-1].mean()) if len(trading_values) >= 6 else int(trading_values.mean())
            val_today = trading_values.iloc[-1]
            val_5d_ago = trading_values.iloc[-6] if len(trading_values) >= 6 else trading_values.iloc[0]
            val_ratio_5d = round(float(val_today / val_5d_ago), 2) if val_5d_ago > 0 else 1.0
            
            latest_sma5 = int(round(sma5_series.iloc[-1]))
            latest_sma25 = int(round(sma25_series.iloc[-1]))
            low_5d = int(round(lows.iloc[-5:].min()))
            high_20d = int(round(highs.iloc[-20:].max()))
            
            # --- ファンダメンタルズ計算（キャッシュ＋最新のハイブリッド） ---
            info = latest_fund.get(code)
            
            eps, bps, pe, pb, roe_pct, op_margin_pct, div_yield_pct = None, None, None, None, 0.0, 0.0, 0.0
            
            if info:
                eps = info.get("trailingEps")
                bps = info.get("bookValue")
                pe = info.get("trailingPE") or info.get("forwardPE")
                pb = info.get("priceToBook")
                roe = info.get("returnOnEquity")
                op_margin = info.get("operatingMargins")
                div_yield = info.get("dividendYield")
                div_rate = info.get("dividendRate")
                
                # 自動補完
                if (not pe or pe <= 0) and (eps and eps > 0): pe = current_price / eps
                if (not pb or pb <= 0) and (bps and bps > 0): pb = current_price / bps
                if (not eps or eps <= 0) and (pe and pe > 0): eps = current_price / pe
                if (not bps or bps <= 0) and (pb and pb > 0): bps = current_price / pb
                
                roe_pct = (roe * 100) if (roe is not None and roe < 1.0) else (roe if roe else 0.0)
                op_margin_pct = (op_margin * 100) if (op_margin is not None and op_margin < 1.0) else (op_margin if op_margin else 0.0)
                if div_yield is not None:
                    div_yield_pct = (div_yield * 100) if div_yield < 0.20 else (div_yield if div_yield <= 20.0 else 0.0)
                elif div_rate and current_price > 0:
                    div_yield_pct = min(20.0, (div_rate / current_price) * 100)
            
            # キャッシュからの救出
            if not (eps and bps and eps > 0 and bps > 0) and (code in cached_fund):
                c_item = cached_fund[code]
                if c_item.get("per") and c_item.get("pbr"):
                    pe = c_item["per"]
                    pb = c_item["pbr"]
                    eps = current_price / pe if pe > 0 else 0
                    bps = current_price / pb if pb > 0 else 0
                    roe_pct = c_item.get("roe", 0.0)
                    op_margin_pct = c_item.get("op_margin", 0.0)
                    div_yield_pct = c_item.get("div_yield", 0.0)
                    
            if not (eps and bps and pe and pb and eps > 0 and bps > 0):
                continue
                
            mix_index = round(pe * pb, 2)
            graham_price = int(round(math.sqrt(22.5 * eps * bps)))
            discount_rate = round(((graham_price - current_price) / graham_price) * 100, 1)
            
            is_kabumini = label == "プライム" or (300 <= current_price <= 50000)
            
            results.append({
                "code": code,
                "name": name,
                "market": label,
                "sector": sector,
                "is_mini": is_kabumini,
                "price": current_price,
                "graham_price": graham_price,
                "discount_rate": discount_rate,
                "mix_index": mix_index,
                "per": round(pe, 1),
                "pbr": round(pb, 2),
                "roe": round(roe_pct, 1),
                "op_margin": round(op_margin_pct, 1),
                "div_yield": round(div_yield_pct, 2),
                "gc_days": gc_days,
                "avg_val_5d": avg_val_5d,
                "val_ratio_5d": val_ratio_5d,
                "sma5": latest_sma5,
                "sma25": latest_sma25,
                "low_5d": low_5d,
                "high_20d": high_20d
            })
        except Exception:
            continue
            
    print(f">> 【{label}】有効銘柄の抽出完了: {len(results)} 件")
    return results


# 6. SQLite保存
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
                target_date, s["code"], s["name"], s["market"], s["sector"],
                s["price"], s["graham_price"], s["discount_rate"], s["mix_index"],
                s["per"], s["pbr"], s["roe"], s["op_margin"], s["div_yield"],
                1 if s.get("is_mini") else 0, s.get("gc_days"), s.get("avg_val_5d"),
                s.get("val_ratio_5d"), s.get("sma5"), s.get("sma25"), s.get("low_5d"), s.get("high_20d")
            ),
        )
    conn.commit()
    conn.close()
    print(f">> SQLite DB ({DB_PATH}) に [{target_date}] 分 {len(all_stocks)} 件保存しました。")


# 7. 日別JSON保存
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


# 8. Discord通知 (バックテスト最高勝率ルール適用)
def send_to_discord(all_stocks, added_count, updated_count, target_date, webhook_url):
    if not webhook_url or not all_stocks:
        return

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    df = pd.DataFrame(all_stocks)

    if added_count == 0 and updated_count == 0:
        msg = f"✅ **【データ確認】** ({now_str})\n対象日: `{target_date}` ➔ 現在 計{len(all_stocks)}社マージ済・正常稼働中。\n👉 Webスクリーナー: https://mrkm3845-web.github.io/Stock_app/"
        try:
            requests.post(webhook_url, json={"content": msg}, timeout=10)
        except Exception:
            pass
        return

    # 割安セクション
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

    # ★ バックテスト実証スイング（株価500~2000円 / 初動GC0〜1日 / 出来高1.2倍↑）
    swing_df = df[
        (df["price"] >= 500) & (df["price"] <= 2000) &
        (df["gc_days"].notna()) & 
        (df["gc_days"] <= 1) & 
        (df["avg_val_5d"] >= 30000) & 
        (df["val_ratio_5d"] >= 1.2)
    ].sort_values(by="val_ratio_5d", ascending=False)

    swing_section = "\n**🚀 【実証スイング注目】初動GC×出来高急増 (最高勝率ゾーン)**\n"
    swing_section += "└ 条件: 株価500~2000円 / 初動GC1日以内 / 代金3千万↑ / 増加率1.2倍↑\n"
    if not swing_df.empty:
        swing_section += f"```\n{'コード':<5} {'社名':<8} {'株価':<6} {'GC':<5} {'増加率'}\n" + "-" * 38 + "\n"
        for _, r in swing_df.head(5).iterrows():
            sname = (r["name"][:6] + "..") if len(r["name"]) > 6 else r["name"]
            gc_label = "当日GC" if r["gc_days"] == 0 else f"{int(r['gc_days'])}日前"
            swing_section += f"{r['code']:<6} {sname:<8} ¥{r['price']:<5} {gc_label:<5} {r['val_ratio_5d']}倍\n"
        swing_section += "```"
    else:
        swing_section += "└ ※本日の初動条件合致銘柄はありません (0件)\n"

    msg = f"📊 **【株式自動スクリーニング速報 (高精度版)】** ({now_str})\n"
    msg += f"📅 対象営業日: **`{target_date}`** (総登録: {len(all_stocks)}社 / 新規: +{added_count} / 更新: {updated_count})\n"
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
        latest_file = os.path.join(HISTORY_DIR, "latest.json")

        existing_stocks = []
        if os.path.exists(target_file):
            try:
                with open(target_file, "r", encoding="utf-8") as f:
                    existing_stocks = json.load(f)
            except Exception:
                pass
        elif os.path.exists(latest_file):
            try:
                with open(latest_file, "r", encoding="utf-8") as f:
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
            print(">> 全体の合体＆更新が完了しました！")
