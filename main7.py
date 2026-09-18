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
HISTORY7_DIR = os.path.join(DOCS_DIR, "history7")
RECOMMENDATIONS_PATH = os.path.join(DOCS_DIR, "recommendations.json")
TECHNICAL_REC_PATH = os.path.join(DOCS_DIR, "recommendations_technical.json")
AI_LATEST_PATH = os.path.join(DOCS_DIR, "ai_strategy_latest.json")
AI_ANALYSIS_DIR = os.path.join(DOCS_DIR, "ai_analysis")


JST = timezone(timedelta(hours=9))


def _jst_now():
    return datetime.now(JST)


def get_target_date_str():
    """実行日付（トレーディングデー）を日本時間基準で返す。

    日本時間 15:30〜翌朝9:00 に取得したデータは、すべてその日のデータとして扱う。
    JST 9:00 より前は「前営業日」、9:00 以降は「当日」を返す。
    GitHub Actions の実行環境は UTC のため、timezone を明示する。
    """
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

    return []


# ---------------------------------------------------------------- 価格データ（増分キャッシュ）
def download_ohlcv_batch(codes, stock_dfs):
    print(f">> {len(codes)} 銘柄の日足を一括取得中...")
    start_date = (_jst_now() - timedelta(days=90)).strftime("%Y-%m-%d")
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

    # 「今日更新済み」と書いてあっても実データが無い/極端に少ない場合は再取得する。
    # これにより、前回のダウンロード失敗でスタンプだけが進んでしまう「毒キャッシュ」を回復できる。
    min_expected = max(10, int(len(codes) * 0.5))
    if last_update != today or len(stock_dfs) < min_expected:
        print(f">> 価格キャッシュを更新します（前回更新: {last_update or 'なし'} / 既存 {len(stock_dfs)} 銘柄）")
        downloaded = download_ohlcv_batch(codes, stock_dfs)
        if downloaded > 0:
            with open(stamp, "w", encoding="utf-8") as f:
                f.write(today)
            print(f">> 価格キャッシュ更新完了: {downloaded} 銘柄取得")
        else:
            print(">> ⚠️ 日足ダウンロードが0件のため、更新スタンプは書き込みません（次回再取得されます）")
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
    errors = []
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

            ctx = F.compute_stock_context(df, feat, weekly)

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
                "ctx": ctx,
                "excluded": excluded,
            })
        except Exception as e:
            errors.append((code, type(e).__name__, str(e)))
            continue

    if errors:
        print(f">> ⚠️ Stage1 で {len(errors)} 銘柄がエラー（先頭5件を表示）:")
        for code, err_type, err_msg in errors[:5]:
            print(f"   - {code}: {err_type}: {err_msg}")
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


# ---------------------------------------------------------------- ニュース（プール分のみ・レート制限対策）
def fetch_news_for_pool(pool):
    """プール銘柄の直近ニュース見出しを yfinance から取得する。"""
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
            time.sleep(0.25)  # レート制限対策
    return news_map


# ---------------------------------------------------------------- Stage2 (DeepSeek)
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
        '"reason":"おすすめ理由","news_note":"与えたニュース見出しに基づく材料",'
        '"entry_strategy":"押し目狙い","entry_price":数値,"support":数値,"resistance":数値,'
        '"tp_price":数値,"sl_price":数値,"trailing_plan":"トレーリング計画の説明"}]}'
        "code は必ず候補一覧に記載された実際のコードをそのままコピーし、「...」や省略形は使わないでください。"
        "news_note は与えたニュース見出しに基づいて記述し、見出しが無い銘柄は『要確認』と付記してください。"
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
    model = ai.get("model", "gemini-3.8-flash")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    payload = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"},
    }
    try:
        resp = requests.post(url, params={"key": key}, json=payload, timeout=120)
        resp.raise_for_status()
        data = resp.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        return _extract_json(text)
    except Exception as e:
        print(f">> Gemini呼び出し失敗（技術スコアでフォールバック）: {e}")
        return None


def call_ai(pool, fund_map, news_map, params):
    """プロバイダ設定に応じてAIを呼び、全体分析＋銘柄別戦略JSONを返す。"""
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
    """DeepSeek応答からプレースホルダ（"..."等）を排除し、候補プールに実在するコードだけ残す。

    無効な応答・0件の場合は None を返して技術スコアへフォールバックする。
    """
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
    """銘柄ごとの最新AI戦略インデックスを更新する（日をまたいで参照可能にする）。"""
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


def write_outputs(all_results, recommendations, date, rec_path):
    if not all_results:
        print(">> ⚠️ 有効銘柄が0件のため、既存データを上書きしません（保存をスキップ）")
        return
    os.makedirs(HISTORY7_DIR, exist_ok=True)
    serializable = []
    for r in all_results:
        item = {k: v for k, v in r.items()}
        serializable.append(item)

    for fname in (f"{date}.json", "latest.json"):
        with open(os.path.join(HISTORY7_DIR, fname), "w", encoding="utf-8") as f:
            json.dump(serializable, f, ensure_ascii=False)

    if recommendations.get("picks"):
        with open(rec_path, "w", encoding="utf-8") as f:
            json.dump(recommendations, f, ensure_ascii=False)
        print(f">> 出力完了: {HISTORY7_DIR}/{date}.json, {rec_path}")
    else:
        print(f">> 出力完了: {HISTORY7_DIR}/{date}.json（おすすめ0件のため {rec_path} は上書きしません）")


# ---------------------------------------------------------------- main
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--markets", nargs="+", default=["プライム", "スタンダード"])
    parser.add_argument("--max-stocks", type=int, default=None, help="テスト用に銘柄数を制限")
    parser.add_argument("--ai", action="store_true", help="DeepSeek分析を実行する（AI専用実行でのみ指定）")
    parser.add_argument("--force-ai", action="store_true", help="既存の当日AI分析を無視して再実行・上書きする")
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
    write_outputs(results, recommendations, date, rec_path)

    print(">> スクリーニング（main7）完了")
    for p in recommendations["picks"]:
        print(f"  - {p['code']} {p['name']} rank={p.get('rank')} verdict={p['verdict']} tp={p['tp_price']} sl={p['sl_price']}")


if __name__ == "__main__":
    main()
