"""
Stock_app/main8.py
割安優良株＆スイング 統合スクリーナー（一本化版）

main6（全銘柄ファンダメンタル網羅・バリュー+スイング）と
main7（技術スコア + AI戦略提言）を統合した単一エントリポイント。

出力（docs/ 配下を単一情報源に統合）:
  - docs/history/{date}.json / latest.json   統合レコード（全銘柄、スコア付き）
  - docs/history/dates.json / meta.json      日付一覧 / データ鮮度メタ
  - docs/recommendations.json                AI付きおすすめ（--ai 実行時）
  - docs/recommendations_technical.json      技術スコアのみのおすすめ
  - docs/ai_analysis/{date}.json             AI応答生キャッシュ
  - docs/ai_strategy_latest.json             銘柄別最新AI戦略インデックス
  - data/stocks.db                           ファンダメンタル・キャッシュ
"""

import argparse
import io
import json
import math
import os
import re
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

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
HISTORY_DIR = os.path.join(DOCS_DIR, "history")
DB_PATH = os.path.join(DATA_DIR, "stocks.db")
RECOMMENDATIONS_PATH = os.path.join(DOCS_DIR, "recommendations.json")
TECHNICAL_REC_PATH = os.path.join(DOCS_DIR, "recommendations_technical.json")
AI_LATEST_PATH = os.path.join(DOCS_DIR, "ai_strategy_latest.json")
AI_ANALYSIS_DIR = os.path.join(DOCS_DIR, "ai_analysis")

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")

JST = timezone(timedelta(hours=9))
# SMA200（200営業日）を機能させるため、余裕を持って約400暦日（≈270営業日）を取得する
PRICE_DAYS = 400


def _jst_now():
    return datetime.now(JST)


def get_target_date_str():
    """実行日付（トレーディングデー）を日本時間基準で返す。JST 9:00 前は前営業日扱い。"""
    now = _jst_now()
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

    print(">> ⚠️ JPX障害のため、緊急用としてローカルDBから銘柄リストを復元します...")
    if os.path.exists(DB_PATH):
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("SELECT DISTINCT code, name, market, sector FROM daily_stocks")
            rows = c.fetchall()
            conn.close()
            if rows:
                records = [{"コード": r[0], "銘柄名": r[1], "市場・商品区分": r[2], "33業種区分": r[3]} for r in rows]
                print(f">> 緊急用DB復元完了: {len(records)} 銘柄")
                return records
        except Exception:
            pass
    return []


def market_label(s_info):
    m = s_info.get("市場・商品区分", "")
    if "プライム" in m:
        return "プライム"
    if "スタンダード" in m:
        return "スタンダード"
    return m


# ---------------------------------------------------------------- 価格データ（増分キャッシュ）
def download_ohlcv_batch(codes, stock_dfs, start_date):
    print(f">> {len(codes)} 銘柄の日足を一括取得中...")
    batch_size = 100
    downloaded = 0
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
                            downloaded += 1
                except Exception:
                    pass
        except Exception as e:
            print(f"Batch download error: {e}")
    print(f">> 一括取得完了: {downloaded} 銘柄")
    return downloaded


def fetch_ohlcv_all(codes):
    """価格データをキャッシュ付きで取得。1日1回だけ全更新、残りはキャッシュ。"""
    os.makedirs(PRICE_CACHE, exist_ok=True)
    today = get_target_date_str()
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

    min_expected = max(10, int(len(codes) * 0.5))
    # 過去キャッシュが短いと SMA200（200営業日）が計算できないため、十分な履歴が無ければ再取得する
    has_long_history = any(len(df) >= 200 for df in stock_dfs.values())
    start_date = (_jst_now() - timedelta(days=PRICE_DAYS)).strftime("%Y-%m-%d")
    if last_update != today or len(stock_dfs) < min_expected or not has_long_history:
        print(f">> 価格キャッシュを更新します（前回更新: {last_update or 'なし'} / 既存 {len(stock_dfs)} 銘柄）")
        downloaded = download_ohlcv_batch(codes, stock_dfs, start_date)
        if downloaded > 0:
            with open(stamp, "w", encoding="utf-8") as f:
                f.write(today)
            print(f">> 価格キャッシュ更新完了: {downloaded} 銘柄取得")
        else:
            print(">> ⚠️ 日足ダウンロードが0件のため、更新スタンプは書き込みません（次回再取得されます）")
    else:
        print(f">> 本日分の価格キャッシュを使用します（{len(stock_dfs)} 銘柄）")

    return stock_dfs


# ---------------------------------------------------------------- ファンダメンタル
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
                "div_yield": r[5], "graham_price": r[6],
            }
        conn.close()
        return cache
    except Exception:
        conn.close()
        return {}


def fetch_single_fundamental(code):
    try:
        ticker = yf.Ticker(f"{code}.T")
        info = ticker.info
        if not info:
            return code, None
        return code, info
    except Exception:
        return code, None


def _as_float(v):
    """yfinance はごく稀に文字列（"N/A" 等）を返すため、安全に float へ変換する。"""
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        s = str(v).replace(",", "").replace("%", "").strip()
        return float(s)
    except (TypeError, ValueError):
        return None


def _fundamentals_from_info(info, current_price):
    eps = _as_float(info.get("trailingEps"))
    bps = _as_float(info.get("bookValue"))
    pe = _as_float(info.get("trailingPE")) or _as_float(info.get("forwardPE"))
    pb = _as_float(info.get("priceToBook"))
    roe = _as_float(info.get("returnOnEquity"))
    op_margin = _as_float(info.get("operatingMargins"))
    div_yield = _as_float(info.get("dividendYield"))
    div_rate = _as_float(info.get("dividendRate"))

    if (pe is None or pe <= 0) and (eps is not None and eps > 0):
        pe = current_price / eps
    if (pb is None or pb <= 0) and (bps is not None and bps > 0):
        pb = current_price / bps
    if (eps is None or eps <= 0) and (pe is not None and pe > 0):
        eps = current_price / pe
    if (bps is None or bps <= 0) and (pb is not None and pb > 0):
        bps = current_price / pb

    roe_pct = (roe * 100) if (roe is not None and roe < 1.0) else (roe if roe else 0.0)
    op_margin_pct = (op_margin * 100) if (op_margin is not None and op_margin < 1.0) else (op_margin if op_margin else 0.0)
    if div_yield is not None:
        div_yield_pct = (div_yield * 100) if div_yield < 0.20 else (div_yield if div_yield <= 20.0 else 0.0)
    elif div_rate is not None and div_rate > 0 and current_price > 0:
        div_yield_pct = min(20.0, (div_rate / current_price) * 100)
    else:
        div_yield_pct = 0.0

    return eps, bps, pe, pb, roe_pct, op_margin_pct, div_yield_pct


# ---------------------------------------------------------------- 統合スキャン
def scan_market_merged(stock_list, params):
    codes = [s["コード"] for s in stock_list]
    stock_map = {s["コード"]: s for s in stock_list}
    ohlcv = fetch_ohlcv_all(codes)
    cached_fund = load_cached_fundamentals()

    print(f">> 最新ファンダメンタルズ情報を取得中...（{len(ohlcv)} 銘柄）")
    latest_fund = {}
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(fetch_single_fundamental, c): c for c in ohlcv.keys()}
        for future in as_completed(futures):
            code = futures[future]
            try:
                _, info = future.result()
                if info:
                    latest_fund[code] = info
            except Exception:
                pass

    results = []
    errors = []
    for code, df in ohlcv.items():
        try:
            s_info = stock_map.get(code)
            if not s_info:
                continue
            name = s_info["銘柄名"]
            sector = s_info.get("33業種区分", "その他")
            market = market_label(s_info)

            feat = F.compute_daily_features(df)
            weekly = F.compute_weekly_features(df)
            price = float(feat["close"][-1])
            if price <= 0:
                continue

            val_ratio = round(float(feat["val_ratio_5d"][-1]), 2)
            tier = F.tier_for_price(price, params["price_tiers"])
            warnings = F.evaluate_warnings(price, val_ratio, params)
            score = F.compute_technical_score(feat, params, weekly["trend_up"])
            ctx = F.compute_stock_context(df, feat, weekly)

            w_down = bool(
                len(weekly["close"]) >= 13
                and not np.isnan(weekly["sma13"][-1])
                and weekly["close"][-1] < weekly["sma13"][-1]
            )

            gc_raw = int(feat["gc_days"][-1])
            gc_days = gc_raw if gc_raw < 900 else None

            info = latest_fund.get(code)
            eps = bps = pe = pb = None
            roe_pct = op_margin_pct = div_yield_pct = 0.0
            if info:
                eps, bps, pe, pb, roe_pct, op_margin_pct, div_yield_pct = _fundamentals_from_info(info, price)

            if not (eps and bps and eps > 0 and bps > 0) and (code in cached_fund):
                c_item = cached_fund[code]
                if c_item.get("per") and c_item.get("pbr"):
                    pe = c_item["per"]
                    pb = c_item["pbr"]
                    eps = price / pe if pe > 0 else 0
                    bps = price / pb if pb > 0 else 0
                    roe_pct = c_item.get("roe", 0.0)
                    op_margin_pct = c_item.get("op_margin", 0.0)
                    div_yield_pct = c_item.get("div_yield", 0.0)

            if not (eps and bps and pe and pb and eps > 0 and bps > 0):
                continue

            mix_index = round(pe * pb, 2)
            graham_price = int(round(math.sqrt(22.5 * eps * bps)))
            discount_rate = round(((graham_price - price) / graham_price) * 100, 1)
            is_mini = (market == "プライム") or (300 <= price <= 50000)

            sma5 = int(round(float(feat["sma5"][-1]))) if not np.isnan(feat["sma5"][-1]) else None
            sma25 = int(round(float(feat["sma25"][-1]))) if not np.isnan(feat["sma25"][-1]) else None
            atr14 = round(float(feat["atr14"][-1]), 1) if not np.isnan(feat["atr14"][-1]) else None
            low_5d = int(round(float(np.min(feat["low"][-5:]))))
            high_20d = int(round(float(np.max(feat["high"][-20:]))))

            results.append({
                "code": code,
                "name": name,
                "market": market,
                "sector": sector,
                "is_mini": is_mini,
                "price": int(round(price)),
                "graham_price": graham_price,
                "discount_rate": discount_rate,
                "mix_index": mix_index,
                "per": round(pe, 1),
                "pbr": round(pb, 2),
                "roe": round(roe_pct, 1),
                "op_margin": round(op_margin_pct, 1),
                "div_yield": round(div_yield_pct, 2),
                "gc_days": gc_days,
                "avg_val_5d": int(round(float(feat["avg_val_5d"][-1]))),
                "val_ratio_5d": val_ratio,
                "sma5": sma5,
                "sma25": sma25,
                "low_5d": low_5d,
                "high_20d": high_20d,
                "score": score,
                "tier": tier["name"] if tier else "",
                "weekly_trend_up": bool(weekly["trend_up"]),
                "atr14": atr14,
                "warnings": [w["id"] for w in warnings],
                "ctx": ctx,
                "excluded": w_down,
            })
        except Exception as e:
            errors.append((code, type(e).__name__, str(e)))
            continue

    if errors:
        print(f">> ⚠️ スキャンで {len(errors)} 銘柄がエラー（先頭5件を表示）:")
        for code, err_type, err_msg in errors[:5]:
            print(f"   - {code}: {err_type}: {err_msg}")
    print(f">> 統合スキャン有効銘柄: {len(results)} 件")
    return results


def build_pool(results, params):
    ai = params.get("ai", {})
    pool_max = ai.get("stage1_pool_max", 40)
    eligible = [r for r in results if not r["excluded"]]
    pool = sorted(eligible, key=lambda r: -r["score"])[:pool_max]
    return pool


# ---------------------------------------------------------------- ニュース（プール分のみ）
def fetch_news_for_pool(pool):
    news_map = {}
    total = len(pool)
    for i, r in enumerate(pool):
        try:
            t = yf.Ticker(f"{r['code']}.T")
            items = t.news or []
            headlines = []
            for n in items[:5]:
                title = n.get("title")
                if title:
                    headlines.append(title)
            if headlines:
                news_map[r["code"]] = headlines
        except Exception:
            pass
        if i < total - 1:
            time.sleep(0.25)
    return news_map


# ---------------------------------------------------------------- Stage2 (Gemini / DeepSeek)
def _build_ai_prompt(pool, fund_map, news_map=None):
    news_map = news_map or {}
    lines = []
    for r in pool:
        ctx = r.get("ctx") or {}
        fund = fund_map.get(r["code"]) or {}
        parts = [
            f"{r['code']} {r['name']}",
            f"市場:{r['market']} 業種:{r['sector']}",
            f"株価:{ctx.get('price', r['price'])}円 スコア:{r['score']}",
            f"GC:{r['gc_days'] if r['gc_days'] is not None else 'なし'}日前",
            f"5日平均代金:{r['avg_val_5d']}千円 増加率:{r['val_ratio_5d']}倍",
            f"ATR14:{r['atr14']}円",
            f"支持20日:{ctx.get('support_20d')} 抵抗20日:{ctx.get('resistance_20d')}",
            f"上髭ATR比:{ctx.get('upper_shadow_atr')} 下髭:{ctx.get('lower_shadow_atr')} レンジ位置:{ctx.get('range_position')}",
            f"週足↑:{ctx.get('weekly_trend_up')} 月足↑:{ctx.get('monthly_trend_up')}",
            f"5日:{ctx.get('ret_5d_pct')}% 20日:{ctx.get('ret_20d_pct')}%",
            f"SMA5/25/200:{ctx.get('sma5')}/{ctx.get('sma25')}/{ctx.get('sma200')}",
        ]
        if fund:
            parts.append(
                f"PER:{fund.get('per')}倍 PBR:{fund.get('pbr')}倍 ROE:{fund.get('roe')}% "
                f"営利:{fund.get('op_margin')}% 配当:{fund.get('div_yield')}%"
            )
        headlines = news_map.get(r["code"]) or []
        if headlines:
            parts.append("ニュース:" + " / ".join(headlines[:3]))
        lines.append(" | ".join(str(x) for x in parts))
    return "\n".join(lines)


def _ai_system_prompt():
    return (
        "あなたは日本株スイングトレードのプロ。以下の候補銘柄すべてを、上昇期待・リスク・流動性・テクニカル・"
        "ファンダメンタルの観点で1位から順位づけし、上位3〜5銘柄をおすすめに選んでください。"
        "回答は必ず以下のJSONのみを返してください（Markdownやコードフェンスなし）:"
        '{"overall":"市場・テーマの総評（2〜3文）",'
        '"stocks":[{"code":"候補一覧に記載の実際の銘柄コード文字列","rank":1,"verdict":"recommend",'
        '"reason":"おすすめ理由","news_note":"その銘柄の直近の材料・ニュース（与えたニュース見出しや一般知識から判断。不明なら要確認）",'
        '"entry_strategy":"押し目狙い","entry_price":数値,"support":数値,"resistance":数値,'
        '"tp_price":数値,"sl_price":数値,"trailing_plan":"トレーリング計画の説明"}]}'
        "code は必ず候補一覧に記載された実際のコードをそのままコピーし、「...」や省略形は使わないでください。"
        "news_note は直近の決算・ニュース・材料を具体的に記述し、見出しや確度が無い銘柄は『要確認』と付記してください。"
        "全候補銘柄を stocks 配列に含めてください。画像は使用しない。数値は与えられたデータに基づく。最終判断は人間が行う前提。"
    )


def _call_deepseek(user, system, params):
    key = os.environ.get("DEEPSEEK_API_KEY")
    ai = params.get("ai", {})
    if not key:
        return None
    url = "https://api.deepseek.com/chat/completions"
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
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


def _call_gemini(user, system, params):
    key = os.environ.get("GEMINI_API_KEY")
    ai = params.get("ai", {})
    if not key:
        return None
    models = [ai.get("model", "gemini-3.8-flash")]
    for m in ("gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash"):
        if m not in models:
            models.append(m)
    last_err = None
    for model in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        payload = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"},
        }
        for attempt in range(3):
            try:
                resp = requests.post(url, params={"key": key}, json=payload, timeout=120)
                if resp.status_code in (429, 500, 502, 503, 504):
                    last_err = f"{resp.status_code} {resp.reason} ({model})"
                    time.sleep(min(2 ** attempt, 10))
                    continue
                resp.raise_for_status()
                data = resp.json()
                text = data["candidates"][0]["content"]["parts"][0]["text"]
                return _extract_json(text)
            except requests.exceptions.HTTPError as e:
                last_err = f"{e} ({model})"
                break
            except Exception as e:
                last_err = f"{e} ({model})"
                break
        print(f">> Gemini {model} 失敗: {last_err} → 次のモデルへフォールバック")
    print(f">> Gemini呼び出し失敗（技術スコアでフォールバック）: {last_err}")
    return None


def call_ai(pool, fund_map, news_map, params):
    ai = params.get("ai", {})
    user = _build_ai_prompt(pool[: ai.get("max_calls_per_run", 40)], fund_map, news_map)
    system = _ai_system_prompt()
    if ai.get("provider", "deepseek") == "gemini":
        return _call_gemini(user, system, params)
    return _call_deepseek(user, system, params)


def _extract_json(text):
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except Exception:
            pass
    start = text.find("[")
    end = text.rfind("]")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except Exception:
            pass
    return None


def sanitize_ai_map(ai_map, pool):
    if not isinstance(ai_map, dict):
        return None
    pool_codes = {r["code"] for r in pool}
    stocks = ai_map.get("stocks") or []
    valid = [s for s in stocks if isinstance(s, dict) and s.get("code") in pool_codes]
    if not valid:
        return None
    overall = ai_map.get("overall")
    if not overall or str(overall).strip() in ("", "..."):
        overall = None
    return {"overall": overall, "stocks": valid}


# ---------------------------------------------------------------- 出力
def _to_num(v):
    if v is None or v == "":
        return None
    try:
        f = float(v)
        return int(round(f)) if float(f).is_integer() else round(f, 2)
    except (TypeError, ValueError):
        return v


def _ai_rank(item):
    try:
        return int(item.get("rank"))
    except (TypeError, ValueError):
        return None


def build_recommendations(pool, fund_map, ai_map, news_map, params, date):
    picks = []
    ai = params.get("ai", {})
    top_n = ai.get("weekly_top_picks", 5)

    overall = None
    ai_stocks = []
    if isinstance(ai_map, dict):
        overall = ai_map.get("overall")
        ai_stocks = ai_map.get("stocks") or []
    pool_codes = {r["code"] for r in pool}
    ai_by_code = {s.get("code"): s for s in ai_stocks if s.get("code") in pool_codes}

    def sort_key(r):
        rank = _ai_rank(ai_by_code.get(r["code"])) if ai_by_code.get(r["code"]) else None
        if rank is not None:
            return (0, rank, -r["score"])
        return (1, -r["score"])

    ordered = sorted(pool, key=sort_key)

    for r in ordered[:top_n]:
        item = ai_by_code.get(r["code"]) or {}
        tier = F.tier_for_price(r["price"], params["price_tiers"]) or {}
        ctx = r.get("ctx") or {}

        tp_price = _to_num(item.get("tp_price"))
        if tp_price is None:
            tp_price = int(round(r["price"] * (1 + tier.get("tp_pct", 0.12))))
        sl_price = _to_num(item.get("sl_price"))
        if sl_price is None:
            if r["atr14"]:
                sl_price = int(round(r["price"] - tier.get("atr_sl_mult", 2.0) * r["atr14"]))

        verdict = item.get("verdict") or "technical_only"

        picks.append({
            "code": r["code"],
            "name": r["name"],
            "sector": r["sector"],
            "price": r["price"],
            "score": r["score"],
            "tier": r["tier"],
            "verdict": verdict,
            "rank": _ai_rank(item),
            "advice": item or None,
            "fundamentals": fund_map.get(r["code"]),
            "entry_strategy": item.get("entry_strategy"),
            "entry_price": _to_num(item.get("entry_price")),
            "support": _to_num(item.get("support")) if item.get("support") is not None else ctx.get("support_20d"),
            "resistance": _to_num(item.get("resistance")) if item.get("resistance") is not None else ctx.get("resistance_20d"),
            "tp_price": tp_price,
            "sl_price": sl_price,
            "trailing_plan": item.get("trailing_plan"),
            "news_note": item.get("news_note"),
            "news_headlines": news_map.get(r["code"]) or [],
            "max_hold_days": tier.get("max_hold_days", 14),
            "gc_days": r["gc_days"],
            "val_ratio_5d": r["val_ratio_5d"],
            "avg_val_5d": r["avg_val_5d"],
            "atr14": r["atr14"],
            "warnings": r["warnings"],
        })

    return {
        "generated_at": _jst_now().strftime("%Y-%m-%dT%H:%M:%S"),
        "date": date,
        "version": params.get("version"),
        "overall": overall,
        "picks": picks,
    }


def update_ai_latest(ai_map, date):
    if not isinstance(ai_map, dict):
        return
    stocks = ai_map.get("stocks") or []
    if not stocks:
        return
    latest = {}
    if os.path.exists(AI_LATEST_PATH):
        try:
            with open(AI_LATEST_PATH, "r", encoding="utf-8") as f:
                latest = json.load(f)
        except Exception:
            latest = {}
    now = _jst_now().strftime("%Y-%m-%dT%H:%M:%S")
    for s in stocks:
        code = s.get("code")
        if code:
            latest[code] = {**s, "date": date, "updated_at": now}
    with open(AI_LATEST_PATH, "w", encoding="utf-8") as f:
        json.dump(latest, f, ensure_ascii=False, indent=2)
    print(f">> 最新AI戦略インデックスを更新: {AI_LATEST_PATH}（{len(latest)} 銘柄）")


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
            score REAL, PRIMARY KEY (date, code)
        )
    """)

    for col, col_type in [("gc_days", "INTEGER"), ("avg_val_5d", "INTEGER"), ("val_ratio_5d", "REAL"),
                          ("sma5", "INTEGER"), ("sma25", "INTEGER"), ("low_5d", "INTEGER"),
                          ("high_20d", "INTEGER"), ("score", "REAL")]:
        try:
            c.execute(f"ALTER TABLE daily_stocks ADD COLUMN {col} {col_type}")
        except Exception:
            pass

    for s in all_stocks:
        c.execute(
            """
            INSERT OR REPLACE INTO daily_stocks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
            (
                target_date, s["code"], s["name"], s["market"], s["sector"],
                s["price"], s["graham_price"], s["discount_rate"], s["mix_index"],
                s["per"], s["pbr"], s["roe"], s["op_margin"], s["div_yield"],
                1 if s.get("is_mini") else 0, s.get("gc_days"), s.get("avg_val_5d"),
                s.get("val_ratio_5d"), s.get("sma5"), s.get("sma25"), s.get("low_5d"),
                s.get("high_20d"), s.get("score"),
            ),
        )
    conn.commit()
    conn.close()
    print(f">> SQLite DB ({DB_PATH}) に [{target_date}] 分 {len(all_stocks)} 件保存しました。")


def save_history_json(all_stocks, target_date):
    os.makedirs(HISTORY_DIR, exist_ok=True)

    serializable = []
    for s in all_stocks:
        item = {k: v for k, v in s.items()}
        serializable.append(item)

    for filename in [f"{target_date}.json", "latest.json"]:
        with open(os.path.join(HISTORY_DIR, filename), "w", encoding="utf-8") as f:
            json.dump(serializable, f, ensure_ascii=False)

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

    meta = {"date": target_date, "generated_at": _jst_now().strftime("%Y-%m-%dT%H:%M:%S")}
    with open(os.path.join(HISTORY_DIR, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)

    print(f">> 日別JSON (docs/history/{target_date}.json) と meta.json を保存しました。")


def send_to_discord(all_stocks, added_count, updated_count, target_date, webhook_url):
    if not webhook_url or not all_stocks:
        return
    now_str = _jst_now().strftime("%Y-%m-%d %H:%M")
    df = pd.DataFrame(all_stocks)

    if added_count == 0 and updated_count == 0:
        msg = f"☕ **【株価データ変更なし】** ({now_str})\n対象日: `{target_date}` ➔ 本日の更新はすでに完了済み、または市場データ更新待ちです。\n👉 Webスクリーナー: https://mrkm3845-web.github.io/Stock_app/"
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

    msg = f"📊 **【株式自動スクリーニング速報 (統合・高精度版)】** ({now_str})\n"
    msg += f"📅 対象営業日: **`{target_date}`** (総登録: {len(all_stocks)}社 / 新規: +{added_count} / 更新: {updated_count})\n"
    msg += make_value_section("プライム") + make_value_section("スタンダード")
    msg += "\n👉 Webスクリーナー: https://mrkm3845-web.github.io/Stock_app/"

    try:
        requests.post(webhook_url, json={"content": msg}, timeout=10)
    except Exception:
        pass


def _diff_counts(target_date, new_batch):
    """前回データとの差分（新規数・更新数）を通知用に算出する（データは全件上書き保存）。"""
    target_file = os.path.join(HISTORY_DIR, f"{target_date}.json")
    latest_file = os.path.join(HISTORY_DIR, "latest.json")

    existing = []
    if os.path.exists(target_file):
        try:
            with open(target_file, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            pass
    elif os.path.exists(latest_file):
        try:
            with open(latest_file, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            pass

    prev = {s["code"]: s for s in existing}
    added = sum(1 for s in new_batch if s["code"] not in prev)
    updated = sum(1 for s in new_batch if s["code"] in prev and prev[s["code"]] != s)
    return added, updated


# ---------------------------------------------------------------- main
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--markets", nargs="+", default=["プライム", "スタンダード"])
    parser.add_argument("--max-stocks", type=int, default=None, help="テスト用に銘柄数を制限")
    parser.add_argument("--ai", action="store_true", help="AI分析を実行する（AI専用実行でのみ指定）")
    parser.add_argument("--force-ai", action="store_true", help="既存の当日AI分析を無視して再実行・上書きする")
    parser.add_argument("--no-discord", action="store_true", help="Discord通知をスキップ")
    args = parser.parse_args()

    params = load_strategy_params()
    stock_list = fetch_jpx_stock_list(args.markets)
    if not stock_list:
        print("❌ 銘柄リストが取得できませんでした。")
        return
    if args.max_stocks:
        stock_list = stock_list[: args.max_stocks]

    results = scan_market_merged(stock_list, params)
    if not results:
        print(">> ⚠️ 有効銘柄が0件のため、既存データを上書きしません。")
        return

    date = get_target_date_str()
    added, updated = _diff_counts(date, results)
    print(f">> 【{date}】全件スキャン: {len(results)} 件 (新規 +{added}, 更新 {updated})")

    save_to_sqlite(results, date)
    save_history_json(results, date)
    if not args.no_discord:
        send_to_discord(results, added, updated, date, DISCORD_WEBHOOK_URL)

    # ---- AI ステージ ----
    pool = build_pool(results, params)
    print(f">> AI候補プール: {len(pool)} 銘柄")

    fund_map = {
        r["code"]: {"per": r["per"], "pbr": r["pbr"], "roe": r["roe"],
                    "op_margin": r["op_margin"], "div_yield": r["div_yield"]}
        for r in pool
    }

    ai_map = None
    news_map = {}
    ai_file = os.path.join(AI_ANALYSIS_DIR, f"{date}.json")
    if args.ai and params.get("ai", {}).get("enabled"):
        if os.path.exists(ai_file) and not args.force_ai:
            try:
                with open(ai_file, "r", encoding="utf-8") as f:
                    ai_map = json.load(f)
                ai_map = sanitize_ai_map(ai_map, pool)
                if ai_map:
                    print(f">> 当日のAI分析キャッシュを再利用: {ai_file}")
                else:
                    print(f">> 当日のAI分析キャッシュが無効のため再取得します: {ai_file}")
            except Exception:
                ai_map = None

        if ai_map is None:
            news_map = fetch_news_for_pool(pool)
            ai_map = call_ai(pool, fund_map, news_map, params)
            ai_map = sanitize_ai_map(ai_map, pool)
            if ai_map:
                os.makedirs(AI_ANALYSIS_DIR, exist_ok=True)
                with open(ai_file, "w", encoding="utf-8") as f:
                    json.dump(ai_map, f, ensure_ascii=False)
                print(f">> 当日のAI分析結果を保存: {ai_file}")
                update_ai_latest(ai_map, date)
            else:
                print(">> ⚠️ AI応答が無効（プレースホルダ等）のため技術スコアでフォールバックします")

    recommendations = build_recommendations(pool, fund_map, ai_map, news_map, params, date)
    rec_path = RECOMMENDATIONS_PATH if args.ai else TECHNICAL_REC_PATH
    if recommendations.get("picks"):
        with open(rec_path, "w", encoding="utf-8") as f:
            json.dump(recommendations, f, ensure_ascii=False)
        print(f">> おすすめ出力: {rec_path}")

    print(">> スクリーニング（main8）完了")
    for p in recommendations["picks"]:
        print(f"  - {p['code']} {p['name']} score={p['score']} rank={p.get('rank')} verdict={p['verdict']} tp={p['tp_price']} sl={p['sl_price']}")


if __name__ == "__main__":
    main()
