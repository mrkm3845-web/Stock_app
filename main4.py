import io
import json
import math
import os
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from curl_cffi import requests as cffi_requests
import pandas as pd
import requests
import yfinance as yf

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
DOCS_DIR = "docs"
DATA_DIR = "data"
DB_PATH = os.path.join(DATA_DIR, "stocks.db")


# ==========================================
# 1. JPXから銘柄一覧を取得
# ==========================================
def fetch_jpx_stock_list(market_name):
    print(f">> 東証（JPX）から上場銘柄リスト（{market_name}）を取得中...")
    url = "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xls"
    headers = {"User-Agent": "Mozilla/5.0"}

    try:
        res = requests.get(url, headers=headers, timeout=30)
        df = pd.read_excel(io.BytesIO(res.content))
        df = df[["コード", "銘柄名", "市場・商品区分", "33業種区分"]]
        df["コード"] = df["コード"].astype(str)

        df_filtered = df[df["市場・商品区分"] == market_name]
        stocks = df_filtered.to_dict("records")
        print(f">> 【{market_name}】対象銘柄数: {len(stocks)} 件")
        return stocks
    except Exception as e:
        print(f">> JPXデータ取得エラー: {e}")
        return []


# ==========================================
# 2. 個別銘柄のデータ収集ロジック
# ==========================================
def analyze_single_stock(stock_info, market_name):
    code = stock_info["コード"]
    name = stock_info["銘柄名"]
    sector = stock_info.get("33業種区分", "その他")
    ticker_symbol = f"{code}.T"

    info = None
    try:
        session = cffi_requests.Session(impersonate="chrome")
        ticker = yf.Ticker(ticker_symbol, session=session)
        info = ticker.info
    except Exception:
        return None

    if not info:
        return None

    try:
        current_price = info.get("currentPrice") or info.get(
            "regularMarketPrice"
        )
        if not current_price or current_price <= 0:
            return None

        eps = info.get("trailingEps")
        bps = info.get("bookValue")
        pe = info.get("trailingPE") or info.get("forwardPE")
        pb = info.get("priceToBook")
        roe = info.get("returnOnEquity")
        op_margin = info.get("operatingMargins")
        div_yield = info.get("dividendYield")
        div_rate = info.get("dividendRate")

        # 基本的な財務数値のチェック
        if not (eps and bps and pe and pb and eps > 0 and bps > 0):
            return None

        # 異常値ガード
        if bps > current_price * 10 or eps > current_price * 2:
            return None
        if pe <= 0.5 or pe > 200.0 or pb <= 0.05 or pb > 30.0:
            return None

        mix_index = pe * pb
        graham_price = math.sqrt(22.5 * eps * bps)
        discount_rate = ((graham_price - current_price) / graham_price) * 100

        # 極端な外れ値の除外
        if discount_rate > 85.0 or discount_rate < -300.0:
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
            if div_yield < 0.20:
                div_yield_pct = div_yield * 100
            elif div_yield <= 15.0:
                div_yield_pct = div_yield
        elif div_rate and current_price > 0:
            calc_yield = (div_rate / current_price) * 100
            if calc_yield <= 15.0:
                div_yield_pct = calc_yield

        market_short = "プライム" if "プライム" in market_name else "スタンダード"

        return {
            "code": code,
            "name": name,
            "market": market_short,
            "sector": sector,
            "price": int(round(current_price)),
            "graham_price": int(round(graham_price)),
            "discount_rate": round(discount_rate, 1),
            "mix_index": round(mix_index, 2),
            "per": round(pe, 1),
            "pbr": round(pb, 2),
            "roe": round(roe_pct, 1),
            "op_margin": round(op_margin_pct, 1),
            "div_yield": round(div_yield_pct, 2),
        }
    except Exception:
        return None


# ==========================================
# 3. 並列スキャン処理
# ==========================================
def scan_market(stocks_list, market_name, max_workers=10):
    results = []
    print(
        f">> 【{market_name}】全 {len(stocks_list)} 銘柄のスキャン開始..."
    )

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(analyze_single_stock, s, market_name): s
            for s in stocks_list
        }
        for future in as_completed(futures):
            res = future.result()
            if res:
                results.append(res)

    print(f">> 【{market_name}】データ取得完了: {len(results)} 件")
    return results


# ==========================================
# 4. SQLiteデータベースへの保存処理
# ==========================================
def save_to_sqlite(all_stocks):
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS daily_stocks (
            date TEXT,
            code TEXT,
            name TEXT,
            market TEXT,
            sector TEXT,
            price REAL,
            graham_price REAL,
            discount_rate REAL,
            mix_index REAL,
            per REAL,
            pbr REAL,
            roe REAL,
            op_margin REAL,
            div_yield REAL,
            PRIMARY KEY (date, code)
        )
    """)

    today_str = datetime.now().strftime("%Y-%m-%d")
    inserted_count = 0

    for s in all_stocks:
        cursor.execute(
            """
            INSERT OR REPLACE INTO daily_stocks (
                date, code, name, market, sector, price, graham_price,
                discount_rate, mix_index, per, pbr, roe, op_margin, div_yield
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                today_str,
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
            ),
        )
        inserted_count += 1

    conn.commit()
    conn.close()
    print(
        f">> SQLite DB ({DB_PATH}) に {inserted_count} 件のデータを保存しました。"
    )


# ==========================================
# 5. インタラクティブHTMLの生成
# ==========================================
def generate_interactive_html(all_stocks):
    os.makedirs(DOCS_DIR, exist_ok=True)
    html_file = os.path.join(DOCS_DIR, "index.html")
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 全業種リストの抽出
    sectors = sorted(
        list(set(s["sector"] for s in all_stocks if s.get("sector")))
    )
    stocks_json = json.dumps(all_stocks, ensure_ascii=False)

    html_content = f"""<!DOCTYPE html>
<html lang="ja">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>割安優良株 インタラクティブ・スクリーナー</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.0/font/bootstrap-icons.css">
    <style>
        body {{ background-color: #f4f6f9; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; font-size: 0.88rem; padding-bottom: 60px; }}
        .card {{ border-radius: 12px; box-shadow: 0 2px 8px rgba(0,0,0,0.05); border: none; margin-bottom: 15px; }}
        .filter-label {{ font-size: 0.78rem; font-weight: 700; color: #495057; margin-bottom: 3px; }}
        .btn-preset {{ font-size: 0.78rem; padding: 4px 10px; border-radius: 20px; }}
        .table-container {{ overflow-x: auto; -webkit-overflow-scrolling: touch; max-height: 70vh; }}
        table th {{ background-color: #212529 !important; color: #fff !important; position: sticky; top: 0; z-index: 10; white-space: nowrap; text-align: center; font-size: 0.82rem; }}
        table td {{ vertical-align: middle; white-space: nowrap; text-align: center; }}
        .badge-mix {{ font-weight: 700; padding: 4px 7px; border-radius: 6px; }}
        .mix-ultra {{ background-color: #ffe3e3; color: #d63301; }}
        .mix-strict {{ background-color: #e6fcf5; color: #0ca678; }}
    </style>
</head>
<body>
    <div class="container-fluid py-3 px-md-4 max-w-7xl">
        <!-- ヘッダー -->
        <div class="card p-3 bg-white">
            <div class="d-flex flex-wrap justify-content-between align-items-center gap-2">
                <div>
                    <h1 class="h5 mb-0 text-primary fw-bold"><i class="bi bi-sliders"></i> 割安優良株 インタラクティブ・スクリーナー</h1>
                    <small class="text-muted">最終更新: {now_str} (JST) / 登録総数: <span id="total-count">{len(all_stocks)}</span> 件</small>
                </div>
                <!-- 銘柄コード・社名クイック検索 -->
                <div class="d-flex gap-2">
                    <input type="text" id="quick-search" class="form-control form-control-sm" placeholder="コード or 社名で検索..." style="width: 200px;">
                    <button class="btn btn-primary btn-sm px-3" onclick="searchStock()"><i class="bi bi-search"></i></button>
                </div>
            </div>
        </div>

        <!-- プリセットボタン群 -->
        <div class="card p-3 bg-white">
            <div class="filter-label mb-2"><i class="bi bi-lightning-charge-fill text-warning"></i> ワンタップ・プリセット条件:</div>
            <div class="d-flex flex-wrap gap-2">
                <button class="btn btn-outline-danger btn-preset fw-bold" onclick="applyPreset('ultra')">🔥 超割安 (係数 < 5.625)</button>
                <button class="btn btn-outline-success btn-preset fw-bold" onclick="applyPreset('strict')">🎯 厳選割安 (係数 < 11.25)</button>
                <button class="btn btn-outline-primary btn-preset fw-bold" onclick="applyPreset('graham')">📋 グレアム標準 (係数 < 22.5)</button>
                <button class="btn btn-outline-secondary btn-preset" onclick="applyPreset('per15')">PER 15以下</button>
                <button class="btn btn-outline-secondary btn-preset" onclick="applyPreset('pbr15')">PBR 1.5以下</button>
                <button class="btn btn-outline-info btn-preset" onclick="applyPreset('highRoe')">💎 高ROE割安 (ROE 10%↑)</button>
                <button class="btn btn-outline-warning btn-preset" onclick="applyPreset('highDiv')">💰 高配当重視 (利回り 3%↑)</button>
                <button class="btn btn-light btn-preset border text-muted ms-auto" onclick="resetFilters()"><i class="bi bi-arrow-counterclockwise"></i> リセット</button>
            </div>
        </div>

        <!-- 詳細フィルタ・並び替えコントロール -->
        <div class="card p-3 bg-white">
            <div class="row g-2 align-items-end">
                <div class="col-6 col-md-2">
                    <div class="filter-label">市場区分</div>
                    <select id="filter-market" class="form-select form-select-sm" onchange="applyFilters()">
                        <option value="ALL">全市場 (プライム+スタンダード)</option>
                        <option value="プライム">プライム市場のみ</option>
                        <option value="スタンダード">スタンダード市場のみ</option>
                    </select>
                </div>
                <div class="col-6 col-md-2">
                    <div class="filter-label">業種</div>
                    <select id="filter-sector" class="form-select form-select-sm" onchange="applyFilters()">
                        <option value="ALL">全業種</option>
                        {''.join(f'<option value="{s}">{s}</option>' for s in sectors)}
                    </select>
                </div>
                <div class="col-6 col-md-2">
                    <div class="filter-label">ミックス係数 (最大)</div>
                    <input type="number" id="filter-mix" class="form-control form-control-sm" step="0.5" placeholder="上限なし" oninput="applyFilters()">
                </div>
                <div class="col-6 col-md-2">
                    <div class="filter-label">PER (最大)</div>
                    <input type="number" id="filter-per" class="form-control form-control-sm" step="1" placeholder="上限なし" oninput="applyFilters()">
                </div>
                <div class="col-6 col-md-2">
                    <div class="filter-label">PBR (最大)</div>
                    <input type="number" id="filter-pbr" class="form-control form-control-sm" step="0.1" placeholder="上限なし" oninput="applyFilters()">
                </div>
                <div class="col-6 col-md-2">
                    <div class="filter-label">並び替え順 (ソート)</div>
                    <select id="sort-by" class="form-select form-select-sm fw-bold text-primary" onchange="applyFilters()">
                        <option value="mix_asc">ミックス係数 (昇順/割安順)</option>
                        <option value="discount_desc">割安度(%) (降順)</option>
                        <option value="roe_desc">ROE(%) (降順)</option>
                        <option value="margin_desc">営業利益率(%) (降順)</option>
                        <option value="div_desc">配当利回り(%) (降順)</option>
                    </select>
                </div>

                <!-- 2段目フィルタ -->
                <div class="col-6 col-md-3">
                    <div class="filter-label">割安度 (最小 %)</div>
                    <input type="number" id="filter-discount" class="form-control form-control-sm" step="5" placeholder="下限なし" oninput="applyFilters()">
                </div>
                <div class="col-6 col-md-3">
                    <div class="filter-label">ROE (最小 %)</div>
                    <input type="number" id="filter-roe" class="form-control form-control-sm" step="1" placeholder="下限なし" oninput="applyFilters()">
                </div>
                <div class="col-6 col-md-3">
                    <div class="filter-label">営業利益率 (最小 %)</div>
                    <input type="number" id="filter-margin" class="form-control form-control-sm" step="1" placeholder="下限なし" oninput="applyFilters()">
                </div>
                <div class="col-6 col-md-3">
                    <div class="filter-label">配当利回り (最小 %)</div>
                    <input type="number" id="filter-div" class="form-control form-control-sm" step="0.5" placeholder="下限なし" oninput="applyFilters()">
                </div>
            </div>
        </div>

        <!-- 結果一覧テーブル -->
        <div class="card p-3 bg-white">
            <div class="d-flex justify-content-between align-items-center mb-2">
                <span class="fw-bold text-dark">該当件数: <span id="filtered-count" class="text-primary fs-5">0</span> 件</span>
                <span class="text-muted small">👉 左右にスクロール可能</span>
            </div>
            <div class="table-container">
                <table class="table table-hover table-striped table-sm mb-0">
                    <thead>
                        <tr>
                            <th>順位</th>
                            <th>コード</th>
                            <th>社名</th>
                            <th>市場</th>
                            <th>業種</th>
                            <th>現在値</th>
                            <th>割安度</th>
                            <th>ミックス係数</th>
                            <th>利回り</th>
                            <th>PER</th>
                            <th>PBR</th>
                            <th>ROE</th>
                            <th>営業利益率</th>
                            <th>理論株価</th>
                            <th>詳細</th>
                        </tr>
                    </thead>
                    <tbody id="stock-table-body">
                        <!-- JSで動的描画 -->
                    </tbody>
                </table>
            </div>
        </div>
    </div>

    <!-- 銘柄詳細モーダル -->
    <div class="modal fade" id="stockModal" tabindex="-1">
        <div class="modal-dialog modal-dialog-centered">
            <div class="modal-content">
                <div class="modal-header bg-dark text-white">
                    <h5 class="modal-title" id="modalTitle">銘柄詳細</h5>
                    <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button>
                </div>
                <div class="modal-body" id="modalBody"></div>
            </div>
        </div>
    </div>

    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
    <script>
        const STOCKS = {stocks_json};
        let modalInstance = null;

        document.addEventListener("DOMContentLoaded", () => {{
            modalInstance = new bootstrap.Modal(document.getElementById('stockModal'));
            applyPreset('graham'); // 初期状態はグレアム標準
        }});

        function applyFilters() {{
            const market = document.getElementById("filter-market").value;
            const sector = document.getElementById("filter-sector").value;
            const maxMix = parseFloat(document.getElementById("filter-mix").value);
            const maxPer = parseFloat(document.getElementById("filter-per").value);
            const maxPbr = parseFloat(document.getElementById("filter-pbr").value);
            const minDiscount = parseFloat(document.getElementById("filter-discount").value);
            const minRoe = parseFloat(document.getElementById("filter-roe").value);
            const minMargin = parseFloat(document.getElementById("filter-margin").value);
            const minDiv = parseFloat(document.getElementById("filter-div").value);
            const sortBy = document.getElementById("sort-by").value;

            let filtered = STOCKS.filter(s => {{
                if (market !== "ALL" && s.market !== market) return false;
                if (sector !== "ALL" && s.sector !== sector) return false;
                if (!isNaN(maxMix) && s.mix_index > maxMix) return false;
                if (!isNaN(maxPer) && s.per > maxPer) return false;
                if (!isNaN(maxPbr) && s.pbr > maxPbr) return false;
                if (!isNaN(minDiscount) && s.discount_rate < minDiscount) return false;
                if (!isNaN(minRoe) && s.roe < minRoe) return false;
                if (!isNaN(minMargin) && s.op_margin < minMargin) return false;
                if (!isNaN(minDiv) && s.div_yield < minDiv) return false;
                return true;
            }});

            // ソート
            filtered.sort((a, b) => {{
                if (sortBy === "mix_asc") return a.mix_index - b.mix_index;
                if (sortBy === "discount_desc") return b.discount_rate - a.discount_rate;
                if (sortBy === "roe_desc") return b.roe - a.roe;
                if (sortBy === "margin_desc") return b.op_margin - a.op_margin;
                if (sortBy === "div_desc") return b.div_yield - a.div_yield;
                return 0;
            }});

            renderTable(filtered);
        }}

        function renderTable(data) {{
            document.getElementById("filtered-count").textContent = data.length;
            const tbody = document.getElementById("stock-table-body");
            tbody.innerHTML = "";

            if (data.length === 0) {{
                tbody.innerHTML = '<tr><td colspan="15" class="p-4 text-muted text-center">条件に一致する銘柄はありません。</td></tr>';
                return;
            }}

            let html = "";
            data.forEach((s, idx) => {{
                let mixBadgeClass = "";
                if (s.mix_index < 5.625) mixBadgeClass = "badge-mix mix-ultra";
                else if (s.mix_index < 11.25) mixBadgeClass = "badge-mix mix-strict";

                html += `<tr>
                    <td><strong>${{idx + 1}}</strong></td>
                    <td><strong>${{s.code}}</strong></td>
                    <td class="text-start">${{s.name}}</td>
                    <td><span class="badge ${{s.market === 'プライム' ? 'bg-primary' : 'bg-secondary'}}">${{s.market}}</span></td>
                    <td class="text-muted small">${{s.sector}}</td>
                    <td>¥${{s.price.toLocaleString()}}</td>
                    <td class="text-danger fw-bold">+${{s.discount_rate}}%</td>
                    <td><span class="${{mixBadgeClass}}">${{s.mix_index.toFixed(2)}}</span></td>
                    <td class="fw-bold">${{s.div_yield}}%</td>
                    <td>${{s.per.toFixed(1)}}</td>
                    <td>${{s.pbr.toFixed(2)}}</td>
                    <td>${{s.roe}}%</td>
                    <td>${{s.op_margin}}%</td>
                    <td>¥${{s.graham_price.toLocaleString()}}</td>
                    <td><button class="btn btn-outline-dark btn-sm py-0 px-2" onclick="showStockDetail('${{s.code}}')"><i class="bi bi-info-circle"></i></button></td>
                </tr>`;
            }});
            tbody.innerHTML = html;
        }}

        function applyPreset(type) {{
            resetInputs();
            if (type === 'ultra') {{
                document.getElementById("filter-mix").value = 5.625;
                document.getElementById("filter-roe").value = 7;
                document.getElementById("filter-margin").value = 6;
            }} else if (type === 'strict') {{
                document.getElementById("filter-mix").value = 11.25;
                document.getElementById("filter-roe").value = 7;
                document.getElementById("filter-margin").value = 6;
            }} else if (type === 'graham') {{
                document.getElementById("filter-mix").value = 22.5;
                document.getElementById("filter-roe").value = 7;
                document.getElementById("filter-margin").value = 6;
            }} else if (type === 'per15') {{
                document.getElementById("filter-per").value = 15;
            }} else if (type === 'pbr15') {{
                document.getElementById("filter-pbr").value = 1.5;
            }} else if (type === 'highRoe') {{
                document.getElementById("filter-mix").value = 22.5;
                document.getElementById("filter-roe").value = 10;
            }} else if (type === 'highDiv') {{
                document.getElementById("filter-mix").value = 22.5;
                document.getElementById("filter-div").value = 3.0;
            }}
            applyFilters();
        }}

        function resetInputs() {{
            document.getElementById("filter-market").value = "ALL";
            document.getElementById("filter-sector").value = "ALL";
            document.getElementById("filter-mix").value = "";
            document.getElementById("filter-per").value = "";
            document.getElementById("filter-pbr").value = "";
            document.getElementById("filter-discount").value = "";
            document.getElementById("filter-roe").value = "";
            document.getElementById("filter-margin").value = "";
            document.getElementById("filter-div").value = "";
        }}

        function resetFilters() {{
            resetInputs();
            applyFilters();
        }}

        function searchStock() {{
            const query = document.getElementById("quick-search").value.trim().toLowerCase();
            if (!query) return;
            const target = STOCKS.find(s => s.code.toLowerCase() === query || s.name.toLowerCase().includes(query));
            if (target) {{
                showStockDetail(target.code);
            }} else {{
                alert("該当する銘柄が見つかりませんでした: " + query);
            }}
        }}

        function showStockDetail(code) {{
            const s = STOCKS.find(item => item.code === code);
            if (!s) return;

            document.getElementById("modalTitle").innerHTML = `<span class="badge bg-primary me-2">${{s.code}}</span> ${{s.name}}`;
            document.getElementById("modalBody").innerHTML = `
                <div class="row g-2 mb-3">
                    <div class="col-6"><small class="text-muted">市場</small><div><strong>${{s.market}}</strong> (${{s.sector}})</div></div>
                    <div class="col-6"><small class="text-muted">現在株価</small><div class="fs-5 fw-bold text-primary">¥${{s.price.toLocaleString()}}</div></div>
                    <div class="col-6"><small class="text-muted">グレアム理論株価</small><div class="fs-5 fw-bold text-success">¥${{s.graham_price.toLocaleString()}}</div></div>
                    <div class="col-6"><small class="text-muted">理論株価との割安度</small><div class="fs-5 fw-bold text-danger">+${{s.discount_rate}}%</div></div>
                </div>
                <hr>
                <div class="row g-2">
                    <div class="col-4 text-center p-2 border rounded"><small class="text-muted">ミックス係数</small><div class="fw-bold fs-6">${{s.mix_index.toFixed(2)}}</div></div>
                    <div class="col-4 text-center p-2 border rounded"><small class="text-muted">PER</small><div class="fw-bold fs-6">${{s.per.toFixed(1)}} 倍</div></div>
                    <div class="col-4 text-center p-2 border rounded"><small class="text-muted">PBR</small><div class="fw-bold fs-6">${{s.pbr.toFixed(2)}} 倍</div></div>
                    <div class="col-4 text-center p-2 border rounded"><small class="text-muted">ROE</small><div class="fw-bold fs-6">${{s.roe}} %</div></div>
                    <div class="col-4 text-center p-2 border rounded"><small class="text-muted">営業利益率</small><div class="fw-bold fs-6">${{s.op_margin}} %</div></div>
                    <div class="col-4 text-center p-2 border rounded"><small class="text-muted">配当利回り</small><div class="fw-bold fs-6 text-danger">${{s.div_yield}} %</div></div>
                </div>
            `;
            modalInstance.show();
        }}
    </script>
</body>
</html>
"""
    with open(html_file, "w", encoding="utf-8") as f:
        f.write(html_content)
    print(f">> インタラクティブHTMLを {html_file} に出力しました。")


# ==========================================
# 6. Discord通知
# ==========================================
def send_to_discord(all_stocks, webhook_url):
    if not webhook_url or "discord.com" not in webhook_url or not all_stocks:
        return

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    df = pd.DataFrame(all_stocks)
    df = df.sort_values(by="mix_index", ascending=True)

    # 有効な割安株（ROE 7%以上、マージン6%以上）
    valid_df = df[(df["roe"] >= 7.0) & (df["op_margin"] >= 6.0)]

    def make_section(market_name):
        m_df = valid_df[valid_df["market"] == market_name]
        ultra = m_df[m_df["mix_index"] < 5.625]
        strict = m_df[(m_df["mix_index"] >= 5.625) & (m_df["mix_index"] < 11.25)]

        text = f"\n**【{market_name}市場】** (計 {len(m_df)} 件合致)\n```\n"
        text += f"{'コード':<5} {'社名':<8} {'割安度':<6} {'係数':<5} {'利回り'}\n"
        text += "-" * 38 + "\n"

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

        text += "```"
        return text

    msg = f"📊 **【割安優良株スクリーニング速報】** ({now_str})\n"
    msg += make_section("プライム")
    msg += make_section("スタンダード")
    msg += "\n👉 Web簡易スクリーナー: https://mrkm3845-web.github.io/Stock_app/"

    try:
        requests.post(webhook_url, json={"content": msg}, timeout=10)
    except Exception:
        pass


# ==========================================
# 実行部
# ==========================================
if __name__ == "__main__":
    # 1. プライム ＆ スタンダードのスキャン
    prime_stocks = fetch_jpx_stock_list("プライム（内国株式）")
    prime_results = scan_market(prime_stocks, "プライム")

    time.sleep(3)

    standard_stocks = fetch_jpx_stock_list("スタンダード（内国株式）")
    standard_results = scan_market(standard_stocks, "スタンダード")

    all_results = prime_results + standard_results

    if all_results:
        # 2. SQLiteデータベースに日次スナップショット保存
        save_to_sqlite(all_results)

        # 3. インタラクティブHTMLの生成 (docs/index.html)
        generate_interactive_html(all_results)

        # 4. Discordへの速報通知
        send_to_discord(all_results, DISCORD_WEBHOOK_URL)

        print(">> 全ての処理が完了しました！")
    else:
        print(">> 有効な銘柄データが取得できませんでした。")
