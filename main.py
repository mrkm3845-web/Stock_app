import yfinance as yf
import pandas as pd
import numpy as np
import math
import time

def calculate_graham_number(eps, bps):
    """
    グレアムの理論株価 = sqrt(22.5 * EPS * BPS)
    """
    if eps and bps and eps > 0 and bps > 0:
        return math.sqrt(22.5 * eps * bps)
    return None

def analyze_value_stock(ticker_symbol):
    """
    財務健全性と理論株価の割安度を判定
    """
    try:
        ticker = yf.Ticker(ticker_symbol)
        info = ticker.info
        
        current_price = info.get('currentPrice') or info.get('regularMarketPrice')
        if not current_price:
            return None
        
        # 1. ファンダメンタルズ指標の取得
        pe = info.get('trailingPE') or info.get('forwardPE')
        pb = info.get('priceToBook')
        eps = info.get('trailingEps')
        bps = info.get('bookValue')
        roe = info.get('returnOnEquity')
        op_margin = info.get('operatingMargins')
        div_yield = info.get('dividendYield')
        payout_ratio = info.get('payoutRatio')
        
        # パーセンテージの正規化（yfinanceの仕様差対策）
        roe_pct = (roe * 100) if roe is not None else None
        op_margin_pct = (op_margin * 100) if op_margin is not None else None
        div_yield_pct = (div_yield * 100) if div_yield is not None else 0.0
        
        # 2. 理論株価（グレアム数）の算出
        graham_price = calculate_graham_number(eps, bps)
        discount_rate = None
        if graham_price:
            # 理論株価に対して現在株価が何%割安か (安全域: Margin of Safety)
            discount_rate = ((graham_price - current_price) / graham_price) * 100

        # 3. ミックス係数 (PER * PBR)
        mix_factor = (pe * pb) if (pe and pb) else None
        
        # ----------------------------------------------------
        # スコアリング（各優良条件の判定）
        # ----------------------------------------------------
        score = 0
        points_log = []
        
        # ① 理論株価より20%以上割安
        if discount_rate and discount_rate >= 20:
            score += 2
            points_log.append("理論株価割安")
        elif discount_rate and discount_rate > 0:
            score += 1
            
        # ② ミックス係数 22.5以下
        if mix_factor and mix_factor <= 22.5:
            score += 1
            points_log.append("グレアム基準クリア")
            
        # ③ ROE 8%以上（高い資本効率）
        if roe_pct and roe_pct >= 8.0:
            score += 1
            points_log.append("高ROE")
            
        # ④ 営業利益率 8%以上（高い収益性）
        if op_margin_pct and op_margin_pct >= 8.0:
            score += 1
            points_log.append("高収益")
            
        # ⑤ 配当利回り 2.5%以上 かつ 配当性向が健全 (70%以下)
        if div_yield_pct >= 2.5:
            if payout_ratio is None or payout_ratio <= 0.70:
                score += 1
                points_log.append("好配当・健全")

        # スコア3点以上（割安で質が高い銘柄）を抽出
        if score >= 3:
            return {
                "コード": ticker_symbol.replace(".T", ""),
                "社名": info.get('shortName', 'N/A'),
                "現在値": round(current_price, 1),
                "理論株価": round(graham_price, 1) if graham_price else "N/A",
                "割安度(%)": f"+{round(discount_rate, 1)}%" if (discount_rate and discount_rate > 0) else f"{round(discount_rate, 1)}%" if discount_rate else "N/A",
                "PER": round(pe, 1) if pe else "N/A",
                "PBR": round(pb, 2) if pb else "N/A",
                "ROE(%)": round(roe_pct, 1) if roe_pct else "N/A",
                "配当利回り(%)": round(div_yield_pct, 2),
                "優良スコア": score,
                "判定理由": ", ".join(points_log)
            }
            
    except Exception as e:
        return None

    return None


def run_value_screener(stock_list):
    print(f"--- 理論株価・優良バリュースクリーニング開始 (対象: {len(stock_list)}銘柄) ---")
    results = []
    
    for symbol in stock_list:
        ticker = f"{symbol}.T" if not symbol.endswith(".T") else symbol
        data = analyze_value_stock(ticker)
        if data:
            results.append(data)
            print(f"【抽出】 {data['コード']} {data['社名']} | スコア: {data['優良スコア']} / 割安度: {data['割安度(%)']}")
        time.sleep(0.3)
        
    df = pd.DataFrame(results)
    if not df.empty:
        # スコア降順、次に割安度順にソート
        df = df.sort_values(by="優良スコア", ascending=False).reset_index(drop=True)
    return df


if __name__ == "__main__":
    # 日本を代表する主要銘柄・好財務銘柄リスト（例）
    target_stocks = [
        "8058", # 三菱商事
        "8001", # 伊藤忠商事
        "8031", # 三井物産
        "7203", # トヨタ自動車
        "7267", # ホンダ
        "6902", # デンソー
        "8306", # 三菱UFJ
        "8316", # 三井住友FG
        "8411", # みずほFG
        "8591", # オリックス
        "8766", # 東京海上HD
        "9432", # NTT
        "9433", # KDDI
        "4063", # 信越化学
        "5401", # 日本製鉄
        "1925", # 大和ハウス
        "6301", # コマツ
        "7751", # キヤノン
    ]
    
    df_result = run_value_screener(target_stocks)
    
    print("\n" + "="*80)
    print("【スクリーニング結果：理論株価が割安な優良株】")
    print("="*80)
    if not df_result.empty:
        print(df_result.to_string(index=False))
    else:
        print("条件に合致する銘柄が見つかりませんでした。")