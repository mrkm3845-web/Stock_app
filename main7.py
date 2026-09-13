"""
Stock_app/main7.py
割安優良株＆スイング スクリーナー（刷新版・Stage1 技術スコア + Stage2 DeepSeek 戦略提言）

設計方針:
- 現行 main6.py は無変更で運用継続。本ファイルは新規並行実装。
- 共有パラメータ strategy_params.json を読み込む（単一情報源）。
- Stage1 は OHLCV のみで総合スコアを計算し、候補プールを適応的に絞る。
- ファンダメンタルはプール分のみ取得（実行時間・レート制限対策）。
- Stage2 は DeepSeek（ai.enabled=true かつ DEEPSEEK_API_KEY 設定時のみ）。
- 出力は docs/history7/ と docs/recommendations.json（main6 の docs/history/ と分離）。
"""

import argparse
import io
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import requests
import yfinance as yf

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common.config import load_strategy_params  # noqa: E402
from common import features as F  # noqa: E402

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
PRICE_CACHE = os.path.join(DATA_DIR, "price_cache")
DOCS_DIR = os.path.join(BASE_DIR, "docs")
HISTORY7_DIR = os.path.join(DOCS_DIR, "history7")
RECOMMENDATIONS_PATH = os.path.join(DOCS_DIR, "recommendations.json")


def get_target_date_str():
    now = datetime.now()
    if now.hour < 9:
        return (now - timedelta(days=1)).strftime("%Y-%m-%d")
    return now.strftime("%Y-%m-%d")


# ---------------------------------------------------------------- JPX銘柄リスト
def fetch_jpx_stock_list(markets):
    page_url = "https://www.jpx.co.jp/markets/statistics-equities/misc/01.html"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    excel_url = None
    try:
        res = requests.get(page_url, headers=headers, timeout=30)
        if res.status_code == 200:
            match = re.search(r'href="([^"]+data_j\.xls[x]?)"', res.text)
            if match:
                rel_url = match.group(1)
                excel_url = "https://www.jpx.co.jp" + rel_url if rel_url.startswith("/") else rel_url
    except Exception as e:
        print(f">> JPX案内ページ取得エラー: {e}")

    if not excel_url:
        excel_url = "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xls"

    try:
        res = requests.get(excel_url, headers=headers, timeout=30)
        if res.status_code == 200 and not res.content.startswith(b"<!DOCTYPE") and not res.content.startswith(b"<html"):
            try:
                df = pd.read_excel(io.BytesIO(res.content), engine="xlrd")
            except Exception:
                df = pd.read_excel(io.BytesIO(res.content), engine="openpyxl")

            df = df[["コード", "銘柄名", "市場・商品区分", "33業種区分"]]
            df["コード"] = df["コード"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
            pattern = "|".join(markets)
            records = df[df["市場・商品区分"].str.contains(pattern, na=False)].to_dict("records")
            if records:
                print(f">> JPX銘柄リスト取得成功: {len(records)} 銘柄")
                return records
    except Exception as e:
        print(f">> 最新Excelダウンロード/解析エラー: {e}")

    return []


# ---------------------------------------------------------------- 価格データ（増分キャッシュ）
def download_ohlcv_batch(codes, stock_dfs):
    print(f">> {len(codes)} 銘柄の日足を一括取得中...")
    start_date = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
    batch_size = 100
    for i in range(0, len(codes), batch_size):
        batch = codes[i:i + batch_size]
        tickers = [f"{c}.T" for c in batch]
        try:
            data = yf.download(tickers, start=start_date, group_by="ticker", auto_adjust=True, progress=False, threads=True)
            for c in batch:
                try:
                    sym = f"{c}.T"
                    if sym in data.columns.levels[0]:
                        df = data[sym].dropna(how="all")[["Open", "High", "Low", "Close", "Volume"]].dropna()
                        if len(df) >= 25:
                            df.index = pd.to_datetime(df.index).tz_localize(None)
                            df.to_csv(os.path.join(PRICE_CACHE, f"{c}.csv"))
                            stock_dfs[c] = df
                except Exception:
                    pass
        except Exception as e:
            print(f"Batch download error: {e}")


def fetch_ohlcv_all(codes):
    """価格データをキャッシュ付きで取得。1日1回だけ全更新、残りはキャッシュ。"""
    os.makedirs(PRICE_CACHE, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    stamp = os.path.join(PRICE_CACHE, "_updated.txt")
    last_update = ""
    if os.path.exists(stamp):
        try:
            with open(stamp, "r", encoding="utf-8") as f:
                last_update = f.read().strip()
        except Exception:
            pass

    stock_dfs = {}
    for code in codes:
        f = os.path.join(PRICE_CACHE, f"{code}.csv")
        if os.path.exists(f):
            try:
                df = pd.read_csv(f, index_col=0, parse_dates=True)
                df.index = pd.to_datetime(df.index).tz_localize(None)
                if len(df) >= 25:
                    stock_dfs[code] = df
            except Exception:
                pass

    if last_update != today:
        print(f">> 価格キャッシュを更新します（前回更新: {last_update or 'なし'}）")
        download_ohlcv_batch(codes, stock_dfs)
        with open(stamp, "w", encoding="utf-8") as f:
            f.write(today)
    else:
        print(f">> 本日分の価格キャッシュを使用します（{len(stock_dfs)} 銘柄）")

    return stock_dfs


# ---------------------------------------------------------------- Stage1
def scan_stage1(stock_list, params):
    codes = [s["コード"] for s in stock_list]
    stock_map = {s["コード"]: s for s in stock_list}
    ohlcv = fetch_ohlcv_all(codes)

    results = []
    feats = {}
    for code, df in ohlcv.items():
        try:
            s_info = stock_map.get(code)
            if not s_info:
                continue
            feat = F.compute_daily_features(df)
            weekly = F.compute_weekly_features(df)
            price = float(feat["close"][-1])
            if price <= 0:
                continue

            val_ratio = float(feat["val_ratio_5d"][-1])
            tier = F.tier_for_price(price, params["price_tiers"])
            warnings = F.evaluate_warnings(price, val_ratio, params)
            score = F.compute_technical_score(feat, params, weekly["trend_up"])

            # 安全装置: 週足が明確に悪化している銘柄は除外
            w_down = bool(
                len(weekly["close"]) >= 13
                and not np.isnan(weekly["sma13"][-1])
                and weekly["close"][-1] < weekly["sma13"][-1]
            )
            excluded = w_down

            feats[code] = {"feat": feat, "weekly_trend_up": weekly["trend_up"]}

            results.append({
                "code": code,
                "name": s_info["銘柄名"],
                "market": s_info.get("市場・商品区分", ""),
                "sector": s_info.get("33業種区分", "その他"),
                "price": int(round(price)),
                "tier": tier["name"] if tier else "",
                "score": score,
                "weekly_trend_up": bool(weekly["trend_up"]),
                "gc_days": int(feat["gc_days"][-1]) if int(feat["gc_days"][-1]) < 900 else None,
                "avg_val_5d": int(round(float(feat["avg_val_5d"][-1]))),
                "val_ratio_5d": round(val_ratio, 2),
                "sma5": int(round(float(feat["sma5"][-1]))) if not np.isnan(feat["sma5"][-1]) else None,
                "sma25": int(round(float(feat["sma25"][-1]))) if not np.isnan(feat["sma25"][-1]) else None,
                "atr14": round(float(feat["atr14"][-1]), 1) if not np.isnan(feat["atr14"][-1]) else None,
                "warnings": [w["id"] for w in warnings],
                "excluded": excluded,
            })
        except Exception:
            continue

    print(f">> Stage1 有効銘柄: {len(results)} 件")
    return results, feats


def build_pool(results, feats, params):
    """適応的に候補プールを構築する。過少なら条件緩和、過多なら上位のみ。"""
    ai = params.get("ai", {})
    pool_min = ai.get("stage1_pool_min", 20)
    pool_max = ai.get("stage1_pool_max", 40)

    def rescore(relax):
        for r in results:
            entry = feats.get(r["code"])
            if entry:
                r["score"] = F.compute_technical_score(entry["feat"], params, entry["weekly_trend_up"], relax)

    relax = {}
    eligible = [r for r in results if not r["excluded"]]
    for _ in range(6):
        pool = sorted(eligible, key=lambda r: -r["score"])[:pool_max]
        if len(pool) >= pool_min:
            return pool, relax

        relax = {
            "val_ratio_min": round(max(1.0, (relax.get("val_ratio_min", params["signals"]["val_ratio_min"]) - 0.2)), 2),
            "avg_val_min_k": max(10000, (relax.get("avg_val_min_k", params["signals"]["avg_val_min_k"]) // 2)),
            "gc_window": min(10, relax.get("gc_window", params["signals"]["gc_window"]) + 1),
        }
        rescore(relax)
        eligible = [r for r in results if not r["excluded"]]

    return sorted(eligible, key=lambda r: -r["score"])[:pool_max], relax


# ---------------------------------------------------------------- ファンダメンタル（プール分のみ）
def _fetch_one_fund(code):
    try:
        info = yf.Ticker(f"{code}.T").info
        if not info:
            return code, None
        return code, info
    except Exception:
        return code, None


def fetch_fundamentals(pool):
    codes = [r["code"] for r in pool]
    if not codes:
        return {}
    print(f">> 候補プール {len(codes)} 銘柄のみファンダメンタルを取得中...")
    out = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(_fetch_one_fund, c): c for c in codes}
        for fu in as_completed(futs):
            code = futs[fu]
            try:
                _, info = fu.result()
                if info:
                    out[code] = info
            except Exception:
                pass
    return out


def normalize_fund(info, price):
    pe = info.get("trailingPE") or info.get("forwardPE")
    pb = info.get("priceToBook")
    roe = info.get("returnOnEquity")
    op_margin = info.get("operatingMargins")
    div_yield = info.get("dividendYield")

    roe_pct = round((roe * 100) if (roe is not None and roe < 1.0) else (roe or 0.0), 1)
    op_margin_pct = round((op_margin * 100) if (op_margin is not None and op_margin < 1.0) else (op_margin or 0.0), 1)
    if div_yield is not None:
        div_pct = round((div_yield * 100) if div_yield < 0.20 else (div_yield if div_yield <= 20.0 else 0.0), 2)
    else:
        div_pct = 0.0

    return {
        "per": round(pe, 1) if pe else None,
        "pbr": round(pb, 2) if pb else None,
        "roe": roe_pct,
        "op_margin": op_margin_pct,
        "div_yield": div_pct,
    }


# ---------------------------------------------------------------- Stage2 (DeepSeek)
def call_deepseek(pool, params):
    """DeepSeek に構造化データを渡し、戦略提言 JSON を返す。"""
    key = os.environ.get("DEEPSEEK_API_KEY")
    ai = params.get("ai", {})
    if not key:
        return None

    url = "https://api.deepseek.com/chat/completions"
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    lines = []
    for r in pool[: ai.get("max_calls_per_run", 40)]:
        lines.append(
            f"{r['code']} {r['name']} | 市場:{r['market']} 業種:{r['sector']} 株価:{r['price']}円 | "
            f"スコア:{r['score']} | GC:{r['gc_days']}日前 | 5日平均代金:{r['avg_val_5d']}千円 | "
            f"増加率:{r['val_ratio_5d']}倍 | ATR14:{r['atr14']}"
        )
    user = "\n".join(lines)

    system = (
        "あなたは日本株スイングトレードのプロ。以下の候補銘柄それぞれについて、"
        "与えられた数値のみから戦略提言をJSON配列で返してください。"
        "各要素: {\"code\":..., \"verdict\":\"recommend\"|\"neutral\"|\"avoid\", "
        "\"reason\":\"根拠\", \"entry_timing\":\"...\", \"support\":数値, \"resistance\":数値, \"risk\":\"...\"}。"
        "画像は使用しない。最終判断は人間が行う前提。"
    )
    payload = {
        "model": ai.get("model", "deepseek-chat"),
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": 0.2,
    }
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=120)
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        return _extract_json(content)
    except Exception as e:
        print(f">> DeepSeek呼び出し失敗（技術スコアでフォールバック）: {e}")
        return None


def _extract_json(text):
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    start = text.find("[")
    end = text.rfind("]")
    if start >= 0 and end > start:
        return json.loads(text[start:end + 1])
    return None


# ---------------------------------------------------------------- 出力
def build_recommendations(pool, fund_map, ai_map, params, date):
    picks = []
    ai = params.get("ai", {})
    top_n = ai.get("weekly_top_picks", 5)

    rank = []
    for r in pool:
        verdict = None
        if ai_map:
            item = next((x for x in ai_map if x.get("code") == r["code"]), None)
            verdict = item.get("verdict") if item else None
        adj = {"recommend": 2, "neutral": 0, "avoid": -2}.get(verdict, 0)
        rank.append((r["score"] + adj, r, verdict, ai_map and next((x for x in ai_map if x.get("code") == r["code"]), None)))

    rank.sort(key=lambda x: -x[0])
    for combined, r, verdict, advice in rank[:top_n]:
        tier = F.tier_for_price(r["price"], params["price_tiers"]) or {}
        sl_price = None
        if r["atr14"]:
            sl_price = int(round(r["price"] - tier.get("atr_sl_mult", 2.0) * r["atr14"]))
        tp_price = int(round(r["price"] * (1 + tier.get("tp_pct", 0.12))))
        picks.append({
            "code": r["code"],
            "name": r["name"],
            "sector": r["sector"],
            "price": r["price"],
            "score": r["score"],
            "tier": r["tier"],
            "verdict": verdict or "technical_only",
            "advice": advice,
            "fundamentals": fund_map.get(r["code"]),
            "tp_price": tp_price,
            "sl_price": sl_price,
            "max_hold_days": tier.get("max_hold_days", 14),
            "warnings": r["warnings"],
        })

    return {
        "generated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "date": date,
        "version": params.get("version"),
        "picks": picks,
    }


def write_outputs(all_results, recommendations, date):
    os.makedirs(HISTORY7_DIR, exist_ok=True)
    serializable = []
    for r in all_results:
        item = {k: v for k, v in r.items()}
        serializable.append(item)

    for fname in (f"{date}.json", "latest.json"):
        with open(os.path.join(HISTORY7_DIR, fname), "w", encoding="utf-8") as f:
            json.dump(serializable, f, ensure_ascii=False)
    with open(RECOMMENDATIONS_PATH, "w", encoding="utf-8") as f:
        json.dump(recommendations, f, ensure_ascii=False)
    print(f">> 出力完了: {HISTORY7_DIR}/{date}.json, {RECOMMENDATIONS_PATH}")


# ---------------------------------------------------------------- main
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--markets", nargs="+", default=["プライム", "スタンダード"])
    parser.add_argument("--max-stocks", type=int, default=None, help="テスト用に銘柄数を制限")
    parser.add_argument("--ai", action="store_true", help="DeepSeek分析を実行する（AI専用実行でのみ指定）")
    args = parser.parse_args()

    params = load_strategy_params()
    stock_list = fetch_jpx_stock_list(args.markets)
    if not stock_list:
        print("❌ 銘柄リストが取得できませんでした。")
        return
    if args.max_stocks:
        stock_list = stock_list[: args.max_stocks]

    results, feats = scan_stage1(stock_list, params)
    pool, relax = build_pool(results, feats, params)
    print(f">> 候補プール: {len(pool)} 銘柄（緩和設定: {relax or 'なし'}）")

    fund_map = {}
    fund_raw = fetch_fundamentals(pool)
    for code, info in fund_raw.items():
        row = next((r for r in pool if r["code"] == code), None)
        if row:
            fund_map[code] = normalize_fund(info, row["price"])

    date = get_target_date_str()
    ai_map = None
    ai_file = os.path.join(DOCS_DIR, f"ai_analysis_{date}.json")
    if args.ai and params.get("ai", {}).get("enabled"):
        if os.path.exists(ai_file):
            try:
                with open(ai_file, "r", encoding="utf-8") as f:
                    ai_map = json.load(f)
                print(f">> 当日のAI分析キャッシュを再利用: {ai_file}")
            except Exception:
                ai_map = None
        else:
            ai_map = call_deepseek(pool, params)
            if ai_map:
                with open(ai_file, "w", encoding="utf-8") as f:
                    json.dump(ai_map, f, ensure_ascii=False)
                print(f">> 当日のAI分析結果を保存: {ai_file}")

    recommendations = build_recommendations(pool, fund_map, ai_map, params, date)
    write_outputs(results, recommendations, date)

    print(">> スクリーニング（main7）完了")
    for p in recommendations["picks"]:
        print(f"  - {p['code']} {p['name']} score={p['score']} verdict={p['verdict']} tp={p['tp_price']} sl={p['sl_price']}")


if __name__ == "__main__":
    main()
