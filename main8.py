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
  - docs/picks/{date}.json                   日次ピックのスナップショット（週次答え合わせ用）
  - docs/ai_analysis/{date}.json             AI応答生キャッシュ
  - docs/ai_strategy_latest.json             銘柄別最新AI戦略インデックス
  - data/stocks.db                           ファンダメンタル・キャッシュ
"""

import argparse
import io
import json
import logging
import math
import os
import re
import sqlite3
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import numpy as np
import pandas as pd
import requests
import yfinance as yf

# yfinance 内部の Yahoo Finance 401/429（レート制限等）は想定内のため、ログを抑制してノイズを防ぐ
logging.getLogger("yfinance").setLevel(logging.CRITICAL)

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
PICKS_DIR = os.path.join(DOCS_DIR, "picks")
EARNINGS_PATH = os.path.join(DOCS_DIR, "earnings.json")
# 決算接近の警告とみなす日数（保有上限に合わせる）
EARNINGS_HORIZON_DAYS = 14

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


_MARKET_CAL = None


def is_market_open_day(date_str):
    """date_str が日本の取引所の営業日（土日祝・取引所休場でない）かを判定する。

    判定できない場合（カレンダー未導入・通信不要のローカル判定のみ）は、
    データ欠損を避けるため安全側で True（営業日扱い）を返す。
    """
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
    except Exception:
        return True
    if d.weekday() >= 5:  # 土日
        return False
    global _MARKET_CAL
    try:
        import pandas_market_calendars as mcal
        if _MARKET_CAL is None:
            _MARKET_CAL = mcal.get_calendar("JPX")
        days = _MARKET_CAL.valid_days(start_date=date_str, end_date=date_str)
        return len(days) > 0
    except Exception:
        return True


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


def _earnings_from_info(info):
    """yfinance info から次回決算日（推定）を JST 日付で返す。追加通信なしで使える。"""
    ts = None
    if info:
        ts = info.get("earningsTimestampStart") or info.get("earningsTimestampEnd")
    if not ts:
        return None
    try:
        return (datetime.utcfromtimestamp(float(ts)) + timedelta(hours=9)).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError):
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
            ctx = F.compute_stock_context(df, feat, weekly)
            warnings = F.evaluate_warnings(price, val_ratio, params, ctx)
            score = F.compute_technical_score(feat, params, weekly["trend_up"])

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
            earnings_date = _earnings_from_info(info) if info else None

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
                "earnings_date": earnings_date,
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
_SEARCH_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def _fetch_gnews_rss(name, days, max_items):
    """Google News RSS から直近 days 日の見出しを新しい順に返す（重複除去）。"""
    url = ("https://news.google.com/rss/search?q=" + urllib.parse.quote(name)
           + "&hl=ja&gl=JP&ceid=JP:ja")
    try:
        res = requests.get(url, headers={"User-Agent": _SEARCH_UA}, timeout=20)
        if res.status_code != 200 or not res.content.lstrip().startswith(b"<?xml"):
            return []
        root = ET.fromstring(res.content)
    except Exception:
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    rows = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        if not title:
            continue
        try:
            pdt = parsedate_to_datetime((it.findtext("pubDate") or "").strip())
            if pdt is not None and pdt.tzinfo is None:
                pdt = pdt.replace(tzinfo=timezone.utc)
        except Exception:
            pdt = None
        if pdt is not None and pdt < cutoff:
            continue
        rows.append((pdt, title))
    rows.sort(key=lambda x: (x[0] is None, x[0] if x[0] is not None else datetime.min.replace(tzinfo=timezone.utc)),
              reverse=True)
    out, seen = [], set()
    for _pdt, title in rows:
        key = re.sub(r"\s+", "", title)
        if key in seen:
            continue
        seen.add(key)
        out.append(title)
        if len(out) >= max_items:
            break
    return out


def _fetch_yf_news(code, max_items):
    """yfinance .news の見出し（フォールバック/併用）。新旧の構造差を吸収。"""
    try:
        items = yf.Ticker(f"{code}.T").news or []
    except Exception:
        return []
    heads = []
    for n in items:
        title = n.get("title")
        if not title and isinstance(n.get("content"), dict):
            title = n["content"].get("title")
        if title:
            heads.append(str(title).strip())
        if len(heads) >= max_items:
            break
    return heads


def fetch_news_for_pool(pool, params=None):
    """候補銘柄のニュース見出しを取得する。

    情報源は strategy_params の ai.news_source で切替:
      - "gnews"    : Google News RSS（既定。失敗時は yfinance にフォールバック）
      - "yfinance" : 従来の yfinance .news
      - "hybrid"   : Google News RSS を優先し、yfinance を重複除外して併用
    """
    ai = (params or {}).get("ai", {})
    source = ai.get("news_source", "gnews")
    days = int(ai.get("news_days", 14))
    max_items = int(ai.get("news_max", 5))
    news_map = {}
    total = len(pool)
    for i, r in enumerate(pool):
        code = r["code"]
        name = r.get("name") or ""
        headlines = []
        if source in ("gnews", "hybrid") and name:
            headlines = _fetch_gnews_rss(name, days, max_items)
        use_yf = source == "yfinance" or (source in ("gnews", "hybrid") and not headlines)
        if use_yf:
            yf_heads = _fetch_yf_news(code, max_items)
            headlines = yf_heads or headlines
        elif source == "hybrid":
            seen = set(headlines)
            for h in _fetch_yf_news(code, max_items):
                if h not in seen:
                    headlines.append(h)
                    seen.add(h)
            headlines = headlines[:max_items]
        if headlines:
            news_map[code] = headlines
        if i < total - 1:
            time.sleep(0.2)
    return news_map


# ---------------------------------------------------------------- Stage2 (Gemini / DeepSeek)
def _build_ai_prompt(pool, fund_map, news_map=None, params=None, base_date=None):
    news_map = news_map or {}
    warn_desc = {w.get("id"): w.get("description") for w in (params or {}).get("warnings", [])}
    base_date = base_date or _jst_now().strftime("%Y-%m-%d")
    lines = []
    for r in pool:
        ctx = r.get("ctx") or {}
        plan = F.compute_entry_plan(ctx, params)
        fund = fund_map.get(r["code"]) or {}
        parts = [
            f"{r['code']} {r['name']}",
            f"市場:{r['market']} 業種:{r['sector']}",
            f"株価:{ctx.get('price', r['price'])}円 スコア:{r['score']} 価格帯:{r.get('tier') or '-'}",
            f"グレアム理論株価:{r.get('graham_price')}円 割安度:{r.get('discount_rate')}%",
            f"GC:{r['gc_days'] if r['gc_days'] is not None else 'なし'}日前",
            f"5日平均代金:{r['avg_val_5d']}千円 増加率:{r['val_ratio_5d']}倍",
            f"ATR14:{r['atr14']}円(ATR比:{ctx.get('atr_pct')}%)",
            f"支持20日:{ctx.get('support_20d')} 抵抗20日:{ctx.get('resistance_20d')}",
            f"上髭ATR比:{ctx.get('upper_shadow_atr')} 下髭:{ctx.get('lower_shadow_atr')} レンジ位置:{ctx.get('range_position')}",
            f"週足↑:{ctx.get('weekly_trend_up')} 月足↑:{ctx.get('monthly_trend_up')}",
            f"5日:{ctx.get('ret_5d_pct')}% 20日:{ctx.get('ret_20d_pct')}%",
            f"SMA5/25/200:{ctx.get('sma5')}/{ctx.get('sma25')}/{ctx.get('sma200')}",
            f"25日乖離:{ctx.get('dist_sma25_pct')}% 5日乖離:{ctx.get('dist_sma5_pct')}%",
            f"RSI14:{ctx.get('rsi14')} 連騰:{ctx.get('run_up_days')}日 寄付ギャップ:{ctx.get('gap_pct')}%",
            f"過熱度:{plan['overheat_level']} ルール推奨入口:{plan['suggested_entry_type']} 押し目候補:{plan['entry_zone_low']}〜{plan['entry_zone_high']}円",
        ]
        if fund:
            parts.append(
                f"PER:{fund.get('per')}倍 PBR:{fund.get('pbr')}倍 ROE:{fund.get('roe')}% "
                f"営利:{fund.get('op_margin')}% 配当:{fund.get('div_yield')}%"
            )
        warn_ids = r.get("warnings") or []
        if warn_ids:
            labels = [str(warn_desc.get(w) or w) for w in warn_ids]
            parts.append("過熱警戒:" + " / ".join(labels))
        ed = r.get("earnings_date")
        if ed:
            d = _days_until(ed, base_date)
            if d is not None and 0 <= d <= EARNINGS_HORIZON_DAYS:
                parts.append(f"決算:{ed}(あと{d}日・接近)")
            else:
                parts.append(f"決算:{ed}")
        headlines = news_map.get(r["code"]) or []
        if headlines:
            parts.append("ニュース:" + " / ".join(headlines[:3]))
        lines.append(" | ".join(str(x) for x in parts))
    return "\n".join(lines)


def _ai_system_prompt(max_picks=5, max_output=15):
    return (
        "あなたは日本株スイングトレードのプロ。以下の候補銘柄を、上昇期待・リスク・流動性・テクニカル・"
        "ファンダメンタルの観点で1位から順位づけしてください。"
        f"verdict=recommend は本当に買い推奨できる銘柄だけに付け、件数を無理に埋めないでください（該当が無ければ0件で構いません。上限{max_picks}件）。"
        "【最重要】イナゴ買いによる高値掴みの防止を最優先してください。"
        "短期急騰・移動平均からの大きな乖離・RSI高値・窓開け・上ヒゲ拒否などの過熱サインがある銘柄では、"
        "現在値での成行追いかけ（breakout_chase）を推奨せず、押し目（pullback_wait）や打診（probe_only）に切り替えるか、"
        "見送り（wait）にしてください。ただしトレンドと上昇余地が強く、過熱が軽度なら追いかけ（breakout_chase）を許容します。"
        "entry_type は次から1つだけ選んでください: "
        "breakout_chase（上昇余地あり・追いかけ可）/ pullback_wait（移動平均・サポート近辺の押し目待ち）/ "
        "probe_only（過熱強・小口の打診のみ）/ wait（見送り・様子見）。"
        "breakout_chase / pullback_wait / probe_only は verdict=recommend を使ってよく、wait は watch / caution にしてください。"
        "entry_price は entry_type に応じた具体値（追いかけ=現値〜直近高値、押し目待ち=5日線や直近サポート近辺）を必ず数値で示し、"
        "pullback_wait では「何円まで待つか」を明記してください。"
        "回答は必ず以下のJSONのみを返してください（Markdownやコードフェンスなし）:"
        '{"overall":"市場・テーマの総評（2〜3文）",'
        '"stocks":[{"code":"候補一覧に記載の実際の銘柄コード文字列","rank":1,"verdict":"recommend",'
        '"entry_type":"breakout_chase",'
        '"reason":"おすすめ理由","news_note":"その銘柄の直近の材料・ニュース（与えたニュース見出しや一般知識から判断。不明なら要確認）",'
        '"entry_strategy":"押し目狙い","entry_price":数値,"support":数値,"resistance":数値,'
        '"tp_price":数値,"sl_price":数値,"trailing_plan":"トレーリング計画の説明"}]}'
        "verdict は recommend（推奨）/ watch（様子見）/ neutral（中立）/ caution（注意）/ avoid（回避）のいずれか1つだけを使ってください。"
        "code は必ず候補一覧に記載された実際のコードをそのままコピーし、「...」や省略形は使わないでください。"
        "news_note は直近の決算・ニュース・材料を具体的に記述し、見出しや確度が無い銘柄は『要確認』と付記してください。"
        "JSONが長すぎると途中で切れて無効になるため、reason / news_note / entry_strategy / trailing_plan は各60文字以内で簡潔にまとめてください。"
        f"stocks 配列には、あなたが選んだ上位{max_output}銘柄程度と、verdict=recommend を付けた全銘柄のみを含めてください（全候補を返す必要はありません。迷ったら上位を優先）。"
        "画像は使用しない。数値は与えられたデータに基づく。最終判断は人間が行う前提。"
    )


def _call_deepseek(user, system, params):
    key = os.environ.get("DEEPSEEK_API_KEY")
    ai = params.get("ai", {})
    if not key:
        print(">> DEEPSEEK_API_KEY 未設定のためAIをスキップします")
        return None
    url = "https://api.deepseek.com/chat/completions"
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    payload = {
        "model": ai.get("model", "deepseek-flash"),
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }
    last_err = None
    for attempt in range(3):
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=180)
            if resp.status_code in (429, 500, 502, 503, 504):
                last_err = f"{resp.status_code} {resp.reason}: {resp.text[:200]}"
                time.sleep(min(2 ** attempt, 10))
                continue
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            parsed = _extract_json(content)
            if parsed is None:
                last_err = "JSON解析失敗"
                print(f">> DeepSeek 200 OKだがJSON解析失敗（応答先頭）: {content[:300]}")
                time.sleep(min(2 ** attempt, 10))
                continue
            return parsed
        except Exception as e:
            last_err = str(e)
            time.sleep(min(2 ** attempt, 10))
    print(f">> DeepSeek呼び出し失敗（技術スコアでフォールバック）: {last_err}")
    return None


def _call_gemini(user, system, params):
    key = os.environ.get("GEMINI_API_KEY")
    ai = params.get("ai", {})
    if not key:
        return None
    models = [ai.get("model", "gemini-3.6-flash")]
    for m in ("gemini-3.1-pro-preview",):
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
        for attempt in range(5):
            try:
                resp = requests.post(url, params={"key": key}, json=payload, timeout=120)
                if resp.status_code in (500, 502, 503, 504):
                    last_err = f"{resp.status_code} {resp.reason} ({model}): {resp.text[:200]}"
                    time.sleep(min(2 ** attempt, 20))
                    continue
                if resp.status_code == 429:
                    last_err = f"{resp.status_code} {resp.reason} ({model}): {resp.text[:200]}"
                    if "quota" in resp.text.lower():
                        print(f">> Gemini {model} は課金クォータ超過のためスキップします")
                        break
                    time.sleep(min(2 ** attempt, 20))
                    continue
                resp.raise_for_status()
                data = resp.json()
                cand = (data.get("candidates") or [{}])[0]
                finish = cand.get("finishReason")
                parts = (cand.get("content") or {}).get("parts") or [{}]
                text = parts[0].get("text", "") or ""
                parsed = _extract_json(text)
                if parsed is None:
                    last_err = f"{model} JSON解析失敗 (finishReason={finish}, len={len(text)})"
                    print(f">> Gemini {model} は200 OKだがJSON解析失敗 "
                          f"(finishReason={finish}, 応答長={len(text)})（応答先頭）: {text[:300]}")
                    time.sleep(min(2 ** attempt, 10))
                    continue
                return parsed
            except requests.exceptions.HTTPError as e:
                last_err = f"{e} ({model}): {e.response.text[:200] if e.response is not None else ''}"
                break
            except Exception as e:
                last_err = f"{e} ({model})"
                break
        print(f">> Gemini {model} 失敗: {last_err} → 次のモデルへフォールバック")
    print(f">> Gemini呼び出し失敗（技術スコアでフォールバック）: {last_err}")
    return None


def call_ai(pool, fund_map, news_map, params, base_date=None):
    ai = params.get("ai", {})
    max_picks = ai.get("max_picks", ai.get("weekly_top_picks", 5))
    max_output = int(ai.get("max_output_stocks", 15))
    system = _ai_system_prompt(max_picks, max_output)
    provider = ai.get("provider", "deepseek")
    cap = ai.get("max_calls_per_run", 40)
    retry_cap = min(ai.get("retry_candidates", 15), cap)
    caps = [cap] if retry_cap >= cap else [cap, retry_cap]
    for c in caps:
        user = _build_ai_prompt(pool[:c], fund_map, news_map, params, base_date)
        res = _call_gemini(user, system, params) if provider == "gemini" else _call_deepseek(user, system, params)
        if res:
            return res
        print(f">> AI応答が得られませんでした（候補数 {c}）→ 候補を絞って再試行します")
    return None


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
    pool_codes = {str(r["code"]) for r in pool}
    stocks = ai_map.get("stocks") or []
    normalized = []
    for s in stocks:
        if not isinstance(s, dict):
            continue
        code = s.get("code")
        if code is None:
            continue
        code = str(code).strip()
        if code in pool_codes:
            s["code"] = code
            normalized.append(s)
    if not normalized:
        print(f">> AI応答のstocksが無効（返却={len(stocks)}件, 有効=0件）: {stocks[:2]}")
        return None
    overall = ai_map.get("overall")
    if not overall or str(overall).strip() in ("", "..."):
        overall = None
    return {"overall": overall, "stocks": normalized}


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


def _fmt_yen(v):
    try:
        return f"¥{int(round(float(v))):,}"
    except (TypeError, ValueError):
        return "-"


def _truncate_text(text, limit):
    if not text:
        return ""
    text = str(text).replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


# AI判定の優先度（小さいほど上位）。未知のverdictやAIなしは9（最下位）。
VERDICT_PRIORITY = {
    "recommend": 0,   # 推奨（買い）
    "watch": 1,       # 様子見
    "neutral": 2,     # 中立
    "hold": 3,        # 保有継続
    "caution": 4,     # 注意
    "avoid": 5,       # 回避
    "sell": 5,
}

# Discord通知で使う判定ラベル
VERDICT_LABELS = {
    "recommend": "🎯推奨",
    "watch": "👀様子見",
    "neutral": "⚪中立",
    "hold": "📌保有継続",
    "caution": "⚠️注意",
    "avoid": "⚠️回避",
    "sell": "⚠️売却",
    "technical_only": "📈スコア順",
}

# エントリー方式（高値掴み防止）のラベル
ENTRY_TYPE_LABELS = {
    "breakout_chase": "🚀追いかけ",
    "pullback_wait": "🎣押し目待ち",
    "probe_only": "🔍打診のみ",
    "wait": "⏸見送り",
}
# 過熱度のラベル
OVERHEAT_LABELS = {
    "low": "低",
    "moderate": "中",
    "strong": "強",
    "extreme": "極",
}


def _normalize_entry_type(v):
    """AIのentry_type（自由文含む）を正規の4区分へ寄せる。未知はNone。"""
    if not v:
        return None
    s = str(v).strip().lower()
    if s in F.ENTRY_TYPES:
        return s
    if any(k in s for k in ("押し目", "pullback", "待ち")):
        return "pullback_wait"
    if any(k in s for k in ("追い", "chase", "breakout", "成行", "上抜")):
        return "breakout_chase"
    if any(k in s for k in ("打診", "probe", "小口")):
        return "probe_only"
    if any(k in s for k in ("見送", "wait", "様子")):
        return "wait"
    return None


def build_recommendations(pool, fund_map, ai_map, news_map, params, date, regime=None, earnings_map=None):
    picks = []
    ai = params.get("ai", {})
    # 表示する推奨の最大件数（旧キー weekly_top_picks も後方互換で読む）
    limit = ai.get("max_picks", ai.get("weekly_top_picks", 5))

    overall = None
    ai_stocks = []
    if isinstance(ai_map, dict):
        overall = ai_map.get("overall")
        ai_stocks = ai_map.get("stocks") or []
    pool_codes = {r["code"] for r in pool}
    ai_by_code = {s.get("code"): s for s in ai_stocks if s.get("code") in pool_codes}

    def sort_key(r):
        item = ai_by_code.get(r["code"])
        verdict = item.get("verdict") if item else None
        v_pri = VERDICT_PRIORITY.get(verdict, 9)
        rank = _ai_rank(item) if item else None
        rank_val = rank if rank is not None else 10 ** 9
        # 検証済みスコアを主軸にし、AI順位は同スコア帯のタイブレーク（AIは補助）
        return (
            v_pri,
            -r["score"],
            rank_val,
            -r.get("val_ratio_5d", 0),
            -r.get("avg_val_5d", 0),
        )

    ordered = sorted(pool, key=sort_key)

    portfolio = params.get("portfolio", {})
    risk_pct = portfolio.get("risk_per_trade_pct", 1.0)
    ref_cap = portfolio.get("reference_capital_jpy", 1000000)
    max_per_sector = portfolio.get("max_per_sector")
    eg = params.get("entry_guard", {})
    earnings_map = earnings_map or {}

    # AI実行時は verdict=recommend のみを「推奨」として採用する（件数は無理に埋めない）。
    # AIなし（技術実行）はスコア上位を技術候補として扱う。
    if ai_by_code:
        candidates = [r for r in ordered if (ai_by_code.get(r["code"]) or {}).get("verdict") == "recommend"]
    else:
        candidates = ordered

    # 業種集中の上限（max_per_sector）を守りつつ、順位の高い銘柄から採用する
    selected = []
    sector_counts = {}
    for r in candidates:
        if len(selected) >= limit:
            break
        sec = r.get("sector") or "その他"
        if max_per_sector and sector_counts.get(sec, 0) >= max_per_sector:
            continue
        selected.append(r)
        if max_per_sector:
            sector_counts[sec] = sector_counts.get(sec, 0) + 1

    for r in selected:
        item = ai_by_code.get(r["code"]) or {}
        tier = F.tier_for_price(r["price"], params["price_tiers"]) or {}
        ctx = r.get("ctx") or {}
        atr_mult = tier.get("atr_sl_mult", 2.0)

        # 高値掴み防止: 過熱度からエントリー方式を決定（AI優先、欠落時はルール）
        plan = F.compute_entry_plan(ctx, params)
        entry_type = _normalize_entry_type(item.get("entry_type")) or plan["suggested_entry_type"]
        downgraded = False
        if entry_type == "breakout_chase" and plan["overheat_level"] in ("strong", "extreme"):
            # 強い過熱下での成行追いかけは打診へ格下げ（イナゴ買い防止の安全網）
            entry_type = "probe_only"
            downgraded = True
        entry_price = _to_num(item.get("entry_price"))
        if downgraded or entry_price is None:
            entry_price = plan["entry_price"]

        # エグジットは「ルール基本」: price_tiers（最適化済み係数）で算出。AI値は advice に参照保持。
        tp_price = int(round(r["price"] * (1 + tier.get("tp_pct", 0.12))))
        if r["atr14"]:
            sl_price = int(round(r["price"] - atr_mult * r["atr14"]))
        else:
            sl_price = _to_num(item.get("sl_price"))

        # リスクベースの推奨サイズ（参考資金 × リスク% ÷ 損切幅）。打診は小口に縮小。
        stop_dist = (r["price"] - sl_price) if (sl_price and r["price"] > sl_price) else None
        base_qty = int((ref_cap * (risk_pct / 100.0)) // stop_dist) if stop_dist else None
        qty_factor = eg.get("probe_qty_factor", 0.5) if entry_type == "probe_only" else 1.0
        suggested_qty = int(base_qty * qty_factor) if base_qty else None

        verdict = item.get("verdict") or "technical_only"
        ed = earnings_map.get(r["code"]) or {}

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
            "entry_type": entry_type,
            "entry_type_label": ENTRY_TYPE_LABELS.get(entry_type, entry_type),
            "entry_zone_low": plan["entry_zone_low"],
            "entry_zone_high": plan["entry_zone_high"],
            "entry_wait_days": plan.get("wait_days"),
            "overheat_level": plan["overheat_level"],
            "overheat_label": OVERHEAT_LABELS.get(plan["overheat_level"], plan["overheat_level"]),
            "overheat_flags": plan["overheat_flags"],
            "rsi14": ctx.get("rsi14"),
            "dist_sma5_pct": ctx.get("dist_sma5_pct"),
            "dist_sma25_pct": ctx.get("dist_sma25_pct"),
            "pos_52w": ctx.get("pos_52w"),
            "run_up_days": ctx.get("run_up_days"),
            "gap_pct": ctx.get("gap_pct"),
            "atr_pct": ctx.get("atr_pct"),
            "entry_strategy": item.get("entry_strategy"),
            "entry_price": entry_price,
            "support": _to_num(item.get("support")) if item.get("support") is not None else ctx.get("support_20d"),
            "resistance": _to_num(item.get("resistance")) if item.get("resistance") is not None else ctx.get("resistance_20d"),
            "tp_price": tp_price,
            "sl_price": sl_price,
            "ai_tp_price": _to_num(item.get("tp_price")),
            "ai_sl_price": _to_num(item.get("sl_price")),
            "atr_sl_mult": atr_mult,
            "stop_distance": stop_dist,
            "suggested_qty": suggested_qty,
            "trailing_plan": item.get("trailing_plan") or f"ATR{atr_mult}倍のトレーリング（{atr_mult}×ATRを下値に切上げ）",
            "earnings_date": ed.get("date") if isinstance(ed, dict) else ed,
            "earnings_soon": bool(ed.get("soon")) if isinstance(ed, dict) else False,
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
        "regime": regime,
        "portfolio_guide": {
            "max_positions": portfolio.get("max_positions", 5),
            "max_per_sector": max_per_sector,
            "risk_per_trade_pct": risk_pct,
            "reference_capital_jpy": ref_cap,
        },
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


def save_picks_snapshot(target_date, recommendations, pool, ai_map, is_ai):
    """日次の採用ピックと候補プールを保存する（週次答え合わせ用）。

    これまで recommendations.json は毎日上書きされ、過去に「何を推薦したか」が
    失われていた。週次の答え合わせ・校正に使うため、実行時点のスナップショットを
    docs/picks/{date}.json に残す（日付ごとに1ファイル・上書き可）。
    AI実行時は同日の技術スナップショットをAI版で上書きする。
    """
    os.makedirs(PICKS_DIR, exist_ok=True)
    path = os.path.join(PICKS_DIR, f"{target_date}.json")

    # 同日に AI 実行済みのスナップショットがある場合、後続の技術実行で
    # 上書きしない（AI判定・順位を週次レビューの校正に残すため）。
    if not is_ai and os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                existing = json.load(f)
            if existing.get("source") == "ai":
                print(f">> ピックスナップショットはAI版を保持: {path}")
                return
        except Exception:
            pass

    pool_records = []
    for r in (pool or []):
        ctx = r.get("ctx") or {}
        pool_records.append({
            "code": r.get("code"),
            "name": r.get("name"),
            "sector": r.get("sector"),
            "score": r.get("score"),
            "price": r.get("price"),
            "atr14": r.get("atr14"),
            "rsi14": ctx.get("rsi14"),
            "dist_sma25_pct": ctx.get("dist_sma25_pct"),
            "pos_52w": ctx.get("pos_52w"),
        })

    ai_records = []
    if isinstance(ai_map, dict):
        for s in (ai_map.get("stocks") or []):
            if not s.get("code"):
                continue
            ai_records.append({
                "code": s.get("code"),
                "rank": s.get("rank"),
                "verdict": s.get("verdict"),
                "entry_type": s.get("entry_type"),
            })

    snapshot = {
        "date": target_date,
        "generated_at": _jst_now().strftime("%Y-%m-%dT%H:%M:%S"),
        "source": "ai" if is_ai else "technical",
        "version": (recommendations or {}).get("version"),
        "regime": (recommendations or {}).get("regime"),
        "portfolio_guide": (recommendations or {}).get("portfolio_guide"),
        "overall": (recommendations or {}).get("overall"),
        "picks": (recommendations or {}).get("picks") or [],
        "pool": pool_records,
        "ai_stocks": ai_records,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False)
    print(f">> ピックスナップショット保存: {path}（picks {len(snapshot['picks'])} / pool {len(pool_records)} / ai {len(ai_records)}）")


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


def fetch_market_regime(sma_days=200):
    """地合い判定: TOPIX ETF(1306.T) が長期線より上か（risk-on/off）。"""
    try:
        df = yf.download("1306.T", period="2y", auto_adjust=True, progress=False)
        if df is None or len(df) < sma_days:
            return None
        close = df["Close"]
        if isinstance(close, pd.DataFrame):
            close = close.iloc[:, 0]
        close = close.dropna()
        if len(close) < sma_days:
            return None
        sma = close.rolling(sma_days).mean()
        return {
            "ticker": "1306.T",
            "close": round(float(close.iloc[-1]), 1),
            "sma": round(float(sma.iloc[-1]), 1),
            "sma_days": sma_days,
            "risk_on": bool(close.iloc[-1] > sma.iloc[-1]),
        }
    except Exception as e:
        print(f">> 地合い判定の取得失敗: {e}")
        return None


def _days_until(date_str, base_date_str):
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d")
        b = datetime.strptime(base_date_str, "%Y-%m-%d")
        return (d - b).days
    except Exception:
        return None


def save_earnings_json(earnings_map, target_date):
    """決算接近の警告用データを docs/earnings.json に保存する。"""
    items = {}
    for code, edate in earnings_map.items():
        du = _days_until(edate, target_date)
        items[code] = {"date": edate, "days_until": du, "soon": bool(du is not None and 0 <= du <= EARNINGS_HORIZON_DAYS)}
    data = {
        "generated_at": _jst_now().strftime("%Y-%m-%dT%H:%M:%S"),
        "date": target_date,
        "horizon_days": EARNINGS_HORIZON_DAYS,
        "items": items,
    }
    with open(EARNINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    soon = sum(1 for v in items.values() if v["soon"])
    print(f">> 決算データを保存: {EARNINGS_PATH}（取得 {len(items)} 銘柄 / 接近 {soon} 銘柄）")
    return data


def save_history_json(all_stocks, target_date, regime=None, portfolio=None):
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

    meta = {"date": target_date, "generated_at": _jst_now().strftime("%Y-%m-%dT%H:%M:%S"), "regime": regime, "portfolio": portfolio}
    with open(os.path.join(HISTORY_DIR, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)

    print(f">> 日別JSON (docs/history/{target_date}.json) と meta.json を保存しました。")


def send_recommendations_to_discord(recommendations, added_count, updated_count, target_date, webhook_url, is_ai=False):
    """スコア（技術）または AI 順位で並んだ推薦ランキングを Discord へ通知する。"""
    if not webhook_url:
        return
    now_str = _jst_now().strftime("%Y-%m-%d %H:%M")
    rec = recommendations or {}
    picks = rec.get("picks") or []
    regime = rec.get("regime") or {}
    state = "risk-on" if regime.get("risk_on") else ("risk-off" if regime else "-")
    overall = rec.get("overall")
    web = "👉 Webスクリーナー: https://mrkm3845-web.github.io/Stock_app/"

    def _post(content):
        try:
            requests.post(webhook_url, json={"content": content}, timeout=10)
        except Exception:
            pass

    # AI実行で推奨0件の日は「本日は推奨なし」を明示して通知する
    if not picks:
        if is_ai:
            msg = f"📊 **【AI推奨（本日）】** ({now_str})\n"
            msg += f"📅 対象営業日: **`{target_date}`** (地合い: {state})\n"
            msg += "🤖 本日はAI推奨（recommend）はありません（様子見）。\n"
            if overall:
                msg += f"総評: {_truncate_text(overall, 220)}\n"
            msg += f"\n{web}"
            _post(msg)
        return

    # 通常実行でデータ更新が無い場合は、順位も変わらないため簡潔な通知に留める（AI実行時は毎回通知）。
    if not is_ai and added_count == 0 and updated_count == 0:
        msg = (
            f"☕ **【株価データ変更なし】** ({now_str})\n"
            f"対象日: `{target_date}` ➔ 本日の更新はすでに完了済み、または市場データ更新待ちです。\n"
            f"{web}"
        )
        _post(msg)
        return
    title = "📊 **【AI推奨（本日）】**" if is_ai else "📊 **【技術スコア ランキング】**"
    msg = f"{title} ({now_str})\n"
    msg += f"📅 対象営業日: **`{target_date}`** (新規: +{added_count} / 更新: {updated_count} / 地合い: {state})\n"
    if overall:
        msg += f"🤖 総評: {_truncate_text(overall, 180)}\n"

    for i, p in enumerate(picks, start=1):
        advice = p.get("advice") or {}
        rank = p.get("rank")
        rank_label = f"{rank}位" if rank is not None else f"{i}位"
        verdict = VERDICT_LABELS.get(p.get("verdict"), p.get("verdict") or "-")
        et = p.get("entry_type_label") or ENTRY_TYPE_LABELS.get(p.get("entry_type"), "")
        header = f"{verdict} (score {p.get('score')})"
        if et:
            header += f" / {et}"
        if p.get("overheat_level") and p.get("overheat_level") != "low":
            header += f" / 過熱:{p.get('overheat_label')}"
        msg += (
            f"\n**{rank_label} `{p.get('code')}` {_truncate_text(p.get('name'), 12)}** "
            f"{header}\n"
        )
        line = f"　株価 {_fmt_yen(p.get('price'))}"
        if p.get("entry_price") is not None:
            if p.get("entry_type") == "pullback_wait":
                line += f" → 押し目 {_fmt_yen(p.get('entry_price'))} 待ち"
            elif p.get("entry_type") == "probe_only":
                line += f" → 打診 {_fmt_yen(p.get('entry_price'))}"
            elif p.get("entry_type") == "wait":
                line += f" → 様子見（押し目 {_fmt_yen(p.get('entry_price'))}）"
            else:
                line += f" → エントリー {_fmt_yen(p.get('entry_price'))}"
        line += f" / 利確 {_fmt_yen(p.get('tp_price'))} / 損切 {_fmt_yen(p.get('sl_price'))}"
        if p.get("suggested_qty"):
            line += f" / 推奨 {int(p['suggested_qty'])}株"
        msg += line + "\n"
        reason = advice.get("reason") or p.get("news_note")
        if reason:
            msg += f"　{_truncate_text(reason, 90)}\n"
        if p.get("earnings_soon"):
            msg += f"　⚠️ 決算接近 ({p.get('earnings_date')})\n"

    msg += f"\n{web}"
    if len(msg) > 1900:
        msg = msg[:1890] + "\n…（省略）"
    _post(msg)


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

    date = get_target_date_str()
    if not is_market_open_day(date):
        print(f">> 【{date}】は非営業日（土日祝・取引所休場）のため処理をスキップします。")
        return

    results = scan_market_merged(stock_list, params)
    if not results:
        print(">> ⚠️ 有効銘柄が0件のため、既存データを上書きしません。")
        return

    added, updated = _diff_counts(date, results)
    print(f">> 【{date}】全件スキャン: {len(results)} 件 (新規 +{added}, 更新 {updated})")

    save_to_sqlite(results, date)
    regime = fetch_market_regime()
    if regime:
        state = "risk-on" if regime["risk_on"] else "risk-off"
        print(f">> 地合い: {state} ({regime['ticker']} {regime['close']} vs SMA{regime['sma_days']} {regime['sma']})")
    save_history_json(results, date, regime, params.get("portfolio"))

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
            news_map = fetch_news_for_pool(pool, params)
            ai_map = call_ai(pool, fund_map, news_map, params, date)
            ai_map = sanitize_ai_map(ai_map, pool)
            if ai_map:
                os.makedirs(AI_ANALYSIS_DIR, exist_ok=True)
                with open(ai_file, "w", encoding="utf-8") as f:
                    json.dump(ai_map, f, ensure_ascii=False)
                print(f">> 当日のAI分析結果を保存: {ai_file}")
                update_ai_latest(ai_map, date)
            else:
                print(">> ⚠️ AI応答が無効（プレースホルダ等）のため技術スコアでフォールバックします")

    # 決算日はスキャン時に取得した info から抽出済み（追加通信なし）。プール限定でなく全銘柄を対象にする。
    earnings_map = {r["code"]: r["earnings_date"] for r in results if r.get("earnings_date")}
    earnings_items = save_earnings_json(earnings_map, date)["items"] if earnings_map else {}

    recommendations = build_recommendations(pool, fund_map, ai_map, news_map, params, date, regime, earnings_items)
    rec_path = RECOMMENDATIONS_PATH if args.ai else TECHNICAL_REC_PATH
    # 推奨0件でも書き出し、前回の推奨が残らないようにする
    with open(rec_path, "w", encoding="utf-8") as f:
        json.dump(recommendations, f, ensure_ascii=False)
    print(f">> おすすめ出力: {rec_path}（{len(recommendations.get('picks') or [])} 件）")

    is_ai = bool(args.ai and params.get("ai", {}).get("enabled") and ai_map)
    save_picks_snapshot(date, recommendations, pool, ai_map, is_ai)

    if not args.no_discord:
        send_recommendations_to_discord(recommendations, added, updated, date, DISCORD_WEBHOOK_URL, is_ai)

    print(">> スクリーニング（main8）完了")
    for p in recommendations["picks"]:
        print(f"  - {p['code']} {p['name']} score={p['score']} rank={p.get('rank')} verdict={p['verdict']} entry={p.get('entry_type')} tp={p['tp_price']} sl={p['sl_price']}")


if __name__ == "__main__":
    main()
