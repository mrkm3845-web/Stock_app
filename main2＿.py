import yfinance as yf
import pandas as pd
import numpy as np
import requests
import math
import io
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

# ==========================================
# 設定エリア
# ==========================================
DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/1538221097592553562/NF2KemUR1ezI2PcK9R1qSpElwbybasUqh1b8zu6aJZ8rd_B3Fyli013d38RUuCaC5Mr2" 
TARGET_MARKET = 'プライム（内国株式）'
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
    
    df = df[['コード', '銘柄名', '市場・商品区分', '33業種区分']]
    df['コード'] = df['コード'].astype(str)
    
    if market_filter:
        df = df[df['市場・商品区分'] == market_filter]
        
    stocks = df.to_dict('records')
    print(f">> 対象銘柄数: {len(stocks)} 件 ({market_filter})")
    return stocks

# ==========================================
# 2. 個別銘柄の分析ロジック
# ==========================================
def analyze_single_stock(stock_info):
    code = stock_info['コード']
    name = stock_info['銘柄名']
    ticker_symbol = f"{code}.T"
    
    try:
        ticker = yf.Ticker(ticker_symbol)
        info = ticker.info
        
        current_price = info.get('currentPrice') or info.get('regularMarketPrice')
        if not current_price:
            return None
        
        eps = info.get('trailingEps')
        bps = info.get('bookValue')
        pe = info.get('trailingPE') or info.get('forwardPE')
        pb = info.get('priceToBook')
        roe = info.get('returnOnEquity')
        op_margin = info.get('operatingMargins')
        div_yield = info.get('dividendYield')
        div_rate = info.get('dividendRate')
        
        if not (eps and bps and eps > 0 and bps > 0):
            return None
        
        # グレアム理論株価 = sqrt(22.5 * EPS * BPS)
        graham_price = math.sqrt(22.5 * eps * bps)
        discount_rate = ((graham_price - current_price) / graham_price) * 100
        
        # ROE・利益率の正規化
        roe_pct = (roe * 100) if (roe is not None and roe < 1.0) else (roe if roe else 0.0)
        op_margin_pct = (op_margin * 100) if (op_margin is not None and op_margin < 1.0) else (op_margin if op_margin else 0.0)
        
        # 配当利回りの安全な計算（バグ修正箇所）
        div_yield_pct = 0.0
        if div_yield is not None:
            div_yield_pct = (div_yield * 100) if div_yield < 1.0 else div_yield
        elif div_rate and current_price:
            div_yield_pct = (div_rate / current_price) * 100
        
        # 条件：割安度10%以上 かつ ROE 7%以上 かつ 営業利益率 6%以上
        if discount_rate > 10.0 and roe_pct >= 7.0 and op_margin_pct >= 6.0:
            return {
                "コード": code,
                "社名": name,
                "業種": stock_info.get('33業種区分', 'N/A'),
                "現在値": round(current_price, 1),
                "理論株価": round(graham_price, 1),
                "割安度(%)": round(discount_rate, 1),
                "PER": round(pe, 1) if pe else "-",
                "PBR": round(pb, 2) if pb else "-",
                "ROE(%)": round(roe_pct, 1),
                "配当利回り(%)": round(div_yield_pct, 2)
            }
    except Exception:
        return None
    return None

# ==========================================
# 3. 並列処理でランキング生成
# ==========================================
def generate_ranking(stocks_list, max_workers=10):
    results = []
    print(f">> 並列スクリーニング開始（最大 {len(stocks_list)} 銘柄をスキャン）...")
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(analyze_single_stock, s): s for s in stocks_list}
        for future in as_completed(futures):
            res = future.result()
            if res:
                results.append(res)
                
    df = pd.DataFrame(results)
    if not df.empty:
        df = df.sort_values(by="割安度(%)", ascending=False).reset_index(drop=True)
        df.index = df.index + 1
        df.index.name = "順位"
    return df

# ==========================================
# 4. HTMLレポートの生成
# ==========================================
def save_html_report(df, filename="ranking_report.html"):
    html_content = f"""
    <!DOCTYPE html>
    <html lang="ja">
    <head>
        <meta charset="UTF-8">
        <title>理論株価 割安優良株ランキング</title>
        <style>
            body {{ font-family: 'Helvetica Neue', Arial, sans-serif; background-color: #f4f7f6; padding: 20px; }}
            h1 {{ color: #2c3e50; text-align: center; }}
            .sub {{ text-align: center; color: #7f8c8d; font-size: 14px; }}
            table {{ width: 95%; margin: 20px auto; border-collapse: collapse; background: #fff; border-radius: 8px; overflow: hidden; box-shadow: 0 4px 6px rgba(0,0,0,0.1); }}
            th {{ background-color: #2c3e50; color: white; padding: 12px; font-size: 14px; text-align: center; }}
            td {{ padding: 10px; border-bottom: 1px solid #eee; text-align: center; font-size: 14px; }}
            tr:hover {{ background-color: #f1f8ff; }}
            .highlight {{ color: #e74c3c; font-weight: bold; }}
            .rank {{ font-weight: bold; background: #edf2f7; width: 50px; }}
        </style>
    </head>
    <body>
        <h1>📊 理論株価 割安優良株ランキング TOP</h1>
        <p class="sub">更新日時: {time.strftime('%Y-%m-%d %H:%M:%S')} | 条件: ROE 7%以上 / 営業利益率 6%以上</p>
        <table>
            <thead>
                <tr>
                    <th>順位</th><th>コード</th><th>社名</th><th>業種</th><th>現在値</th>
                    <th>理論株価</th><th>割安度</th><th>PER</th><th>PBR</th><th>ROE</th><th>配当利回り</th>
                </tr>
            </thead>
            <tbody>
    """
    for rank, row in df.iterrows():
        html_content += f"""
                <tr>
                    <td class="rank">{rank}</td>
                    <td><b>{row['コード']}</b></td>
                    <td>{row['社名']}</td>
                    <td>{row['業種']}</td>
                    <td>{row['現在値']:,}円</td>
                    <td>{row['理論株価']:,}円</td>
                    <td class="highlight">+{row['割安度(%)']}%</td>
                    <td>{row['PER']}倍</td>
                    <td>{row['PBR']}倍</td>
                    <td>{row['ROE(%)']}%</td>
                    <td>{row['配当利回り(%)']}%</td>
                </tr>
        """
    html_content += """
            </tbody>
        </table>
    </body>
    </html>
    """
    with open(filename, "w", encoding="utf-8") as f:
        f.write(html_content)
    print(f">> HTMLレポートを更新しました: {filename}")

if __name__ == "__main__":
    all_stocks = fetch_jpx_stock_list()
    target_list = all_stocks[:MAX_STOCKS_TO_CHECK]
    ranking_df = generate_ranking(target_list)
    
    if not ranking_df.empty:
        save_html_report(ranking_df.head(20))
        print(">> 完了しました。ブラウザをリロードしてご確認ください。")