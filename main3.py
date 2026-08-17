import io
import math
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import pandas as pd
import requests
import yfinance as yf

# ==========================================
# 設定エリア
# ==========================================
# 環境変数から取得（GitHub Actions対応）
DISCORD_WEBHOOK_URL = os.environ.get(
    "DISCORD_WEBHOOK_URL",
    "https://discord.com/api/webhooks/1538221097592553562/NF2KemUR1ezI2PcK9R1qSpElwbybasUqh1b8zu6aJZ8rd_B3Fyli013d38RUuCaC5Mr2",
)
TARGET_MARKET = "プライム（内国株式）"
MAX_STOCKS_TO_CHECK = 100


# ==========================================
# 1. JPXから最新銘柄一覧を取得
# ==========================================
def fetch_jpx_stock_list(market_filter=TARGET_MARKET):
    print(">> 東証（JPX）から上場銘柄リストを自動ダウンロード中...")
    url = "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xls"
    headers = {"User-Agent": "Mozilla/5.0"}

    res = requests.get(url, headers=headers)
    df = pd.read_excel(io.BytesIO(res.content))

    df = df[["コード", "銘柄名", "市場・商品区分", "33業種区分"]]
    df["コード"] = df["コード"].astype(str)

    if market_filter:
        df = df[df["市場・商品区分"] == market_filter]

    stocks = df.to_dict("records")
    print(f">> 対象銘柄数: {len(stocks)} 件 ({market_filter})")
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

        # EPS・BPS・PER・PBRの基本チェック
        if not (eps and bps and pe and pb and eps > 0 and bps > 0):
            return None

        # 異常値ガード1：EPS/BPSのデータ狂いを除外
        if bps > current_price * 10 or eps > current_price * 2:
            return None

        # 異常値ガード2：PER/PBRの異常値を除外
        if pe <= 1.0 or pe > 100.0 or pb <= 0.1 or pb > 20.0:
            return None

        # ★ ミックス係数 = PER * PBR
        mix_index = pe * pb

        # グレアム理論株価 (22.5基準)
        graham_price = math.sqrt(22.5 * eps * bps)
        discount_rate = ((graham_price - current_price) / graham_price) * 100

        # 異常値ガード3：理論株価が現在値から乖離しすぎているものを除外
        if discount_rate > 75.0:
            return None

        # ROE・利益率の正規化
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

        # 基本フィルター：ミックス係数 22.5未満 かつ ROE 7%以上 かつ 営業利益率 6%以上
        if mix_index < 22.5 and roe_pct >= 7.0 and op_margin_pct >= 6.0:
            return {
                "コード": code,
                "社名": name,
                "業種": stock_info.get("33業種区分", "N/A"),
                "現在値": round(current_price, 1),
                "理論株価": round(graham_price, 1),
                "ミックス係数": round(mix_index, 2),
                "割安度(%)": round(discount_rate, 1),
                "PER": round(pe, 1),
                "PBR": round(pb, 2),
                "ROE(%)": round(roe_pct, 1),
                "配当利回り(%)": round(div_yield_pct, 2),
            }
    except Exception:
        return None
    return None


# ==========================================
# 3. 並列処理でランキング生成
# ==========================================
def generate_ranking(stocks_list, max_workers=10):
    results = []
    print(
        f">> 並列スクリーニング開始（最大 {len(stocks_list)} 銘柄をスキャン）..."
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
        # ミックス係数が小さい順（＝割安度が高い順）にソート
        df = df.sort_values(by="ミックス係数", ascending=True).reset_index(
            drop=True
        )
        df.index = df.index + 1
        df.index.name = "順位"
    return df


# ==========================================
# 4. Discordへの通知機能
# ==========================================
def send_to_discord(df, title_label, webhook_url):
    if not webhook_url or "discord.com" not in webhook_url or df.empty:
        return

    message = f"📊 **【{title_label}】** ({time.strftime('%Y-%m-%d %H:%M')})\n"
    message += "```\n"
    message += f"{'順位':<3} {'コード':<5} {'社名':<10} {'係数':<6} {'PER':<5} {'PBR':<5} {'利回り'}\n"
    message += "-" * 48 + "\n"

    for rank, row in df.iterrows():
        short_name = (
            (row["社名"][:8] + "..") if len(row["社名"]) > 8 else row["社名"]
        )
        message += f"{rank:<4} {row['コード']:<6} {short_name:<10} {row['ミックス係数']:<6.2f} {row['PER']:<5.1f} {row['PBR']:<5.2f} {row['配当利回り(%)']}%\n"
    message += "```"

    payload = {"content": message}
    res = requests.post(webhook_url, json=payload)
    if res.status_code == 204:
        print(f">> Discordへ通知送信成功: {title_label}")
    else:
        print(f">> Discord通知失敗（ステータス: {res.status_code}）")


# ==========================================
# 実行部
# ==========================================
if __name__ == "__main__":
    all_stocks = fetch_jpx_stock_list()
    target_list = all_stocks[:MAX_STOCKS_TO_CHECK]
    ranking_df = generate_ranking(target_list)

    if not ranking_df.empty:
        # ① 【超割安】ミックス係数 5.625 未満
        ultra_cheap_df = ranking_df[ranking_df["ミックス係数"] < 5.625]
        if not ultra_cheap_df.empty:
            send_to_discord(
                ultra_cheap_df.head(5),
                "🔥 係数5.625未満 超・割安株",
                DISCORD_WEBHOOK_URL,
            )

        # ② 【みきまる基準】ミックス係数 11.25 未満
        strict_cheap_df = ranking_df[ranking_df["ミックス係数"] < 11.25]
        if not strict_cheap_df.empty:
            send_to_discord(
                strict_cheap_df.head(5),
                "🎯 係数11.25未満 厳選割安株",
                DISCORD_WEBHOOK_URL,
            )

        # ③ 【標準】グレアム基準 22.5 未満（TOP 10）
        send_to_discord(
            ranking_df.head(10),
            "📊 割安優良株ランキング TOP10 (係数22.5未満)",
            DISCORD_WEBHOOK_URL,
        )

        print(">> 処理がすべて完了しました。")
    else:
        print(">> 該当する銘柄がありませんでした。")
