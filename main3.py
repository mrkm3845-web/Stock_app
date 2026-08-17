import io
import math
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import pandas as pd
import requests
import yfinance as yf

# ==========================================
# 設定エリア
# ==========================================
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
DOCS_DIR = "docs"


# ==========================================
# 1. JPXから銘柄一覧を取得
# ==========================================
def fetch_jpx_stock_list(market_name):
    print(f">> 東証（JPX）から上場銘柄リスト（{market_name}）を取得中...")
    url = "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xls"
    headers = {"User-Agent": "Mozilla/5.0"}

    res = requests.get(url, headers=headers)
    df = pd.read_excel(io.BytesIO(res.content))
    df = df[["コード", "銘柄名", "市場・商品区分", "33業種区分"]]
    df["コード"] = df["コード"].astype(str)

    df_filtered = df[df["市場・商品区分"] == market_name]
    stocks = df_filtered.to_dict("records")
    print(f">> 【{market_name}】対象銘柄数: {len(stocks)} 件")
    return stocks


# ==========================================
# 2. 個別銘柄の分析ロジック
# ==========================================
def analyze_single_stock(stock_info):
    code = stock_info["コード"]
    name = stock_info["銘柄名"]
    ticker_symbol = f"{code}.T"

    try:
        ticker = yf.Ticker(ticker_symbol)
        info = ticker.info

        current_price = info.get("currentPrice") or info.get(
            "regularMarketPrice"
        )
        if not current_price:
            return None

        eps = info.get("trailingEps")
        bps = info.get("bookValue")
        pe = info.get("trailingPE") or info.get("forwardPE")
        pb = info.get("priceToBook")
        roe = info.get("returnOnEquity")
        op_margin = info.get("operatingMargins")
        div_yield = info.get("dividendYield")
        div_rate = info.get("dividendRate")

        # 基本チェック
        if not (eps and bps and pe and pb and eps > 0 and bps > 0):
            return None

        # 異常値ガード1：データ狂いを除外
        if bps > current_price * 10 or eps > current_price * 2:
            return None

        # 異常値ガード2：PER/PBRの異常値を除外
        if pe <= 1.0 or pe > 100.0 or pb <= 0.1 or pb > 20.0:
            return None

        # ミックス係数 = PER * PBR
        mix_index = pe * pb

        # グレアム理論株価 (22.5基準)
        graham_price = math.sqrt(22.5 * eps * bps)
        discount_rate = ((graham_price - current_price) / graham_price) * 100

        # 異常値ガード3：極端な乖離の除外
        if discount_rate > 75.0:
            return None

        # ROE・営業利益率の正規化
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

        # 配当利回りの安全な計算
        div_yield_pct = 0.0
        if div_yield is not None:
            div_yield_pct = (div_yield * 100) if div_yield < 1.0 else div_yield
        elif div_rate and current_price:
            div_yield_pct = (div_rate / current_price) * 100

        # 抽出条件：係数22.5未満、ROE 7%以上、営業利益率 6%以上
        if mix_index < 22.5 and roe_pct >= 7.0 and op_margin_pct >= 6.0:
            return {
                "コード": code,
                "社名": name,
                "現在値": int(round(current_price)),
                "割安度": f"+{round(discount_rate, 1)}%",
                "ミックス係数": round(mix_index, 2),
                "利回り": f"{round(div_yield_pct, 2)}%",
                "PER": round(pe, 1),
                "PBR": round(pb, 2),
                "ROE": f"{round(roe_pct, 1)}%",
                "理論株価": int(round(graham_price)),
                "業種": stock_info.get("33業種区分", "N/A"),
            }
    except Exception:
        return None
    return None


# ==========================================
# 3. 並列処理でランキング生成
# ==========================================
def generate_ranking(stocks_list, market_label, max_workers=10):
    results = []
    print(
        f">> 【{market_label}】全 {len(stocks_list)} 銘柄のスキャン開始..."
    )

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(analyze_single_stock, s): s for s in stocks_list
        }
        for future in as_completed(futures):
            res = future.result()
            if res:
                results.append(res)

    df = pd.DataFrame(results)
    if not df.empty:
        df = df.sort_values(by="ミックス係数", ascending=True).reset_index(
            drop=True
        )
        # 順位列を先頭に追加（インデックスずれを解消）
        df.insert(0, "順位", df.index + 1)
    return df


# ==========================================
# 4. HTML生成関数
# ==========================================
COMMON_CSS = """
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
<style>
    body { background-color: #f4f6f9; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; font-size: 0.9rem; padding-bottom: 60px; }
    .card { border-radius: 12px; box-shadow: 0 2px 8px rgba(0,0,0,0.06); border: none; margin-bottom: 20px; }
    .table-container { overflow-x: auto; -webkit-overflow-scrolling: touch; }
    .table { font-size: 0.85rem; margin-bottom: 0; }
    .table th { background-color: #212529 !important; color: white !important; white-space: nowrap; font-weight: 600; padding: 10px 8px; text-align: center; }
    .table td { vertical-align: middle; white-space: nowrap; padding: 8px; }
    .table-striped tbody tr:nth-of-type(odd) { background-color: #ffffff; }
    .table-striped tbody tr:nth-of-type(even) { background-color: #f8f9fa; }
    .badge-scroll { font-size: 0.75rem; background-color: #e9ecef; color: #495057; padding: 3px 8px; border-radius: 20px; }
</style>
"""


def build_table_html(target_df):
    if target_df.empty:
        return "<p class='p-3 text-muted text-center mb-0'>該当する銘柄はありません。</p>"
    return target_df.to_html(
        classes="table table-hover table-striped text-center",
        border=0,
        index=False,
    )


def generate_market_page(df, market_title, filename):
    os.makedirs(DOCS_DIR, exist_ok=True)
    filepath = os.path.join(DOCS_DIR, filename)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    ultra_table = build_table_html(df[df["ミックス係数"] < 5.625])
    strict_table = build_table_html(
        df[(df["ミックス係数"] >= 5.625) & (df["ミックス係数"] < 11.25)]
    )
    all_table = build_table_html(df)

    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{market_title} スクリーニング</title>
    {COMMON_CSS}
</head>
<body>
    <div class="container py-3 max-w-6xl">
        <div class="d-flex justify-content-between align-items-center mb-3">
            <a href="index.html" class="btn btn-outline-secondary btn-sm">← トップ目次に戻る</a>
            <span class="badge-scroll">👉 左右にスクロールできます</span>
        </div>

        <div class="card p-3 bg-white mb-3">
            <h2 class="h5 mb-1 text-primary fw-bold">📊 {market_title} スクリーニング</h2>
            <p class="text-muted small mb-0">最終更新: {now_str} (JST) / 抽出件数: {len(df)} 件</p>
        </div>

        <div class="card p-3 bg-white">
            <h5 class="fw-bold text-danger mb-2">🔥 超・割安株（係数 5.625未満）</h5>
            <div class="table-container">{ultra_table}</div>
        </div>

        <div class="card p-3 bg-white">
            <h5 class="fw-bold text-success mb-2">🎯 厳選割安株（係数 11.25未満）</h5>
            <div class="table-container">{strict_table}</div>
        </div>

        <div class="card p-3 bg-white">
            <h5 class="fw-bold text-secondary mb-2">📋 全体ランキング（係数 22.5未満）</h5>
            <div class="table-container">{all_table}</div>
        </div>
    </div>
</body>
</html>
"""
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(html)


def generate_index_page(prime_df, standard_df):
    os.makedirs(DOCS_DIR, exist_ok=True)
    filepath = os.path.join(DOCS_DIR, "index.html")
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    prime_ultra = build_table_html(prime_df[prime_df["ミックス係数"] < 5.625])
    standard_ultra = build_table_html(
        standard_df[standard_df["ミックス係数"] < 5.625]
    )

    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>割安優良株ポータル</title>
    {COMMON_CSS}
    <style>
        .nav-btn {{ border-radius: 12px; text-decoration: none; display: block; padding: 20px; transition: transform 0.2s; }}
        .nav-btn:hover {{ transform: translateY(-3px); }}
    </style>
</head>
<body>
    <div class="container py-3 max-w-6xl">
        <div class="card p-4 bg-white mb-3 text-center">
            <h1 class="h4 mb-1 text-primary fw-bold">📈 割安優良株スクリーニング ポータル</h1>
            <p class="text-muted small mb-0">最終更新: {now_str} (JST)</p>
        </div>

        <!-- リンクボタン -->
        <div class="row g-3 mb-4">
            <div class="col-6">
                <a href="prime.html" class="nav-btn bg-primary text-white text-center shadow-sm">
                    <h3 class="h5 fw-bold mb-1">🏛️ プライム市場</h3>
                    <p class="small mb-0 opacity-75">{len(prime_df)} 件の詳細 →</p>
                </a>
            </div>
            <div class="col-6">
                <a href="standard.html" class="nav-btn bg-dark text-white text-center shadow-sm">
                    <h3 class="h5 fw-bold mb-1">🏢 スタンダード市場</h3>
                    <p class="small mb-0 opacity-75">{len(standard_df)} 件の詳細 →</p>
                </a>
            </div>
        </div>

        <div class="d-flex justify-content-end mb-2">
            <span class="badge-scroll">👉 左右にスクロールできます</span>
        </div>

        <!-- プライム超割安 速報 -->
        <div class="card p-3 bg-white">
            <div class="d-flex justify-content-between align-items-center mb-2">
                <h5 class="fw-bold text-danger mb-0">🔥 プライム：超・割安株（係数 < 5.625）</h5>
                <a href="prime.html" class="btn btn-outline-primary btn-sm py-0">全て見る →</a>
            </div>
            <div class="table-container">{prime_ultra}</div>
        </div>

        <!-- スタンダード超割安 速報 -->
        <div class="card p-3 bg-white">
            <div class="d-flex justify-content-between align-items-center mb-2">
                <h5 class="fw-bold text-danger mb-0">🔥 スタンダード：超・割安株（係数 < 5.625）</h5>
                <a href="standard.html" class="btn btn-outline-dark btn-sm py-0">全て見る →</a>
            </div>
            <div class="table-container">{standard_ultra}</div>
        </div>
    </div>
</body>
</html>
"""
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(html)


# ==========================================
# 5. Discord通知
# ==========================================
def send_to_discord(df, title_label, webhook_url):
    if not webhook_url or "discord.com" not in webhook_url or df.empty:
        return

    message = f"📊 **【{title_label}】** ({time.strftime('%Y-%m-%d %H:%M')})\n"
    message += "```\n"
    message += f"{'順位':<3} {'コード':<5} {'社名':<10} {'割安度':<6} {'係数':<6} {'利回り'}\n"
    message += "-" * 48 + "\n"

    for rank, row in df.iterrows():
        short_name = (
            (row["社名"][:8] + "..") if len(row["社名"]) > 8 else row["社名"]
        )
        message += f"{row['順位']:<4} {row['コード']:<6} {short_name:<10} {row['割安度']:<7} {row['ミックス係数']:<6.2f} {row['利回り']}\n"
    message += "```"

    try:
        requests.post(webhook_url, json={"content": message})
    except Exception:
        pass


# ==========================================
# 実行部
# ==========================================
if __name__ == "__main__":
    # 1. プライム全件
    prime_stocks = fetch_jpx_stock_list("プライム（内国株式）")
    prime_df = generate_ranking(prime_stocks, "プライム")

    # 2. スタンダード全件
    standard_stocks = fetch_jpx_stock_list("スタンダード（内国株式）")
    standard_df = generate_ranking(standard_stocks, "スタンダード")

    # 3. HTMLファイル出力
    generate_market_page(prime_df, "プライム市場", "prime.html")
    generate_market_page(standard_df, "スタンダード市場", "standard.html")
    generate_index_page(prime_df, standard_df)

    # 4. Discord通知
    p_ultra = prime_df[prime_df["ミックス係数"] < 5.625]
    if not p_ultra.empty:
        send_to_discord(
            p_ultra.head(5),
            "🔥【プライム】係数5.625未満 超・割安株",
            DISCORD_WEBHOOK_URL,
        )

    s_ultra = standard_df[standard_df["ミックス係数"] < 5.625]
    if not s_ultra.empty:
        send_to_discord(
            s_ultra.head(5),
            "🔥【スタンダード】係数5.625未満 超・割安株",
            DISCORD_WEBHOOK_URL,
        )

    print(">> 全ての処理が完了しました！")
