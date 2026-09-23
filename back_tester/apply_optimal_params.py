"""
apply_optimal_params.py
バックテスト結果（backtest_walkforward_result.json）をガード付きで strategy_params.json に反映するスクリプト。

反映対象:
- `signals`（スコア式の閾値: gc_window / val_ratio_min / avg_val_min_k）
- `price_tiers`（株価帯ごとの tp_pct / sl_pct / atr_sl_mult / max_hold_days）
- `entry_guard`（押し目エントリー: pullback_atr_shallow / pullback_wait_days。成行より改善した場合のみ）

ガード条件（各対象ごとにすべて満たす場合のみ反映）:
- テスト取引数が最低件数以上
- OOS PF が閾値以上
- OOS 期待値が正
- OOS 年率リターンがベンチマーク（TOPIX ETF）を上回る
- OOS 最大ドローダウンが上限以下
- （signals のみ）近傍安定性: 同じエグジットで PF>=1.0 かつ EV>0 の条件が 2 つ以上
- 前回反映済みの条件より有意に劣化していない（results/applied_state.json と比較）

使用例:
    python apply_optimal_params.py [results/backtest_walkforward_result.json]
"""

import json
import os
import sys
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(BASE_DIR)
PARAMS_PATH = os.path.join(REPO_ROOT, "docs", "strategy_params.json")
STATE_PATH = os.path.join(BASE_DIR, "results", "applied_state.json")
DEFAULT_RESULT_PATH = os.path.join(BASE_DIR, "results", "backtest_walkforward_result.json")

# ガード閾値
MIN_TRADES = 100
MIN_PF = 1.15
MIN_EV_PCT = 0.0
MAX_DD_PCT = 50.0
PLATEAU_MIN_COUNT = 2
PLATEAU_MIN_PF = 1.0
DEGRADATION_TOLERANCE_PF = 0.02


def load_json(path):
    with open(path, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def _summary(msg):
    p = os.environ.get("GITHUB_STEP_SUMMARY")
    if p:
        try:
            with open(p, "a", encoding="utf-8") as f:
                f.write(msg + "\n")
        except Exception:
            pass


def _emit(checks):
    ok = True
    for name, passed in checks:
        mark = "[PASS]" if passed else "[FAIL]"
        print(f"{mark} {name}")
        _summary(f"- {mark} {name}")
        ok = ok and passed
    return ok


def evaluate_signals(result, params, prev_state):
    """signals 更新の可否を判定し、(反映するか, 反映後のsignals, メタ) を返す。"""
    top = result.get("top_combos", [])
    if not top:
        return False, None, None

    best = top[0]
    t = best["test"]
    sig = best["signal"]
    exit_mode = best["exit_mode"]
    bench_avg = result.get("benchmark", {}).get("test_avg_pct")

    checks = []
    checks.append(("取引数 >= {}".format(MIN_TRADES), t["trade_count"] >= MIN_TRADES))
    checks.append(("OOS PF >= {}".format(MIN_PF), t["pf"] >= MIN_PF))
    checks.append(("OOS 期待値 > 0", t["ev_pct"] > MIN_EV_PCT))
    if bench_avg is None:
        checks.append(("ベンチマーク超え（ベンチマークデータ欠損のため判定不能）", False))
    else:
        checks.append(("ベンチマーク超え ({}% > {}%)".format(t["annualized_return_pct"], bench_avg),
                       t["annualized_return_pct"] > bench_avg))
    checks.append(("最大DD <= {}%".format(MAX_DD_PCT), t["max_dd_pct"] <= MAX_DD_PCT))

    same_exit = [c for c in top if c["exit_mode"] == exit_mode]
    plateau_ok = sum(
        1 for c in same_exit
        if c["test"]["pf"] >= PLATEAU_MIN_PF and c["test"]["ev_pct"] > 0
    ) >= PLATEAU_MIN_COUNT
    checks.append(("近傍安定性（同エグジットでプラス条件 {} 件以上）".format(PLATEAU_MIN_COUNT), plateau_ok))

    last_note = ""
    if prev_state and prev_state.get("signals"):
        prev_pf = prev_state["signals"].get("test", {}).get("pf", 0.0)
        not_degraded = t["pf"] >= prev_pf - DEGRADATION_TOLERANCE_PF
        checks.append(("前回比劣化なし (今回 {:.2f} >= 前回 {:.2f})".format(t["pf"], prev_pf), not_degraded))
        last_note = f"（前回反映 PF: {prev_pf}）"

    _summary("## 反映判定: signals（スコア閾値）")
    ok = _emit(checks)
    if not ok:
        _summary("**signals: ガード不通過 → 更新しない。**")
        print("[FAIL] signals は更新しません。")
        return False, None, None

    signals = dict(params.get("signals", {}))
    signals["gc_window"] = int(sig["gc_window"])
    signals["val_ratio_min"] = float(sig["val_ratio_min"])
    signals["avg_val_min_k"] = int(sig["avg_val_min_k"])
    signals.setdefault("trend_filter_sma200", True)

    meta = {
        "signal": sig,
        "exit_mode": exit_mode,
        "test": t,
        "note": last_note,
    }
    _summary("**signals: ガード通過 → 更新。**")
    print("[PASS] signals を更新: gc_window={}, val_ratio_min={}, avg_val_min_k={} {}"
          .format(signals["gc_window"], signals["val_ratio_min"], signals["avg_val_min_k"], last_note))
    return True, signals, meta


def evaluate_tiers(result, params, prev_state):
    """price_tiers 更新の可否を判定し、(反映するか, 更新後price_tiers, メタ) を返す。"""
    tier_exit = result.get("tier_exit", [])
    if not tier_exit:
        return False, None, None

    bench_avg = result.get("benchmark", {}).get("test_avg_pct")
    tiers = params.get("price_tiers", [])
    tier_by_id = {t.get("id"): t for t in tiers}

    prev_tier_map = {}
    if prev_state and prev_state.get("tiers"):
        for item in prev_state["tiers"].get("items", []):
            prev_tier_map[item.get("tier")] = item.get("test", {}).get("pf", 0.0)

    updated = [dict(t) for t in tiers]
    applied_items = []

    _summary("## 反映判定: price_tiers（株価帯別エグジット）")
    for rec in tier_exit:
        tid = rec["tier"]
        t = rec["test"]
        checks = []
        checks.append(("{}: 取引数 >= {}".format(rec["name"], MIN_TRADES), t["trade_count"] >= MIN_TRADES))
        checks.append(("{}: OOS PF >= {}".format(rec["name"], MIN_PF), t["pf"] >= MIN_PF))
        checks.append(("{}: OOS 期待値 > 0".format(rec["name"]), t["ev_pct"] > MIN_EV_PCT))
        if bench_avg is None:
            checks.append(("{}: ベンチマーク超え（データ欠損）".format(rec["name"]), False))
        else:
            checks.append(("{}: ベンチマーク超え ({}% > {}%)".format(rec["name"], t["annualized_return_pct"], bench_avg),
                           t["annualized_return_pct"] > bench_avg))
        checks.append(("{}: 最大DD <= {}%".format(rec["name"], MAX_DD_PCT), t["max_dd_pct"] <= MAX_DD_PCT))
        if tid in prev_tier_map:
            prev_pf = prev_tier_map[tid]
            checks.append(("{}: 前回比劣化なし (今回 {:.2f} >= 前回 {:.2f})".format(rec["name"], t["pf"], prev_pf),
                           t["pf"] >= prev_pf - DEGRADATION_TOLERANCE_PF))

        if _emit(checks):
            if tid in tier_by_id:
                tier_obj = next(x for x in updated if x.get("id") == tid)
                tier_obj["tp_pct"] = float(rec["tp_pct"])
                tier_obj["sl_pct"] = float(rec["sl_pct"])
                tier_obj["atr_sl_mult"] = float(rec["atr_sl_mult"])
                tier_obj["max_hold_days"] = int(rec["max_hold_days"])
                applied_items.append({
                    "tier": tid,
                    "exit_mode": rec["exit_mode"],
                    "params": {k: rec[k] for k in ("tp_pct", "sl_pct", "atr_sl_mult", "max_hold_days")},
                    "test": t,
                })
                print(f"[PASS] {rec['name']}: tp={rec['tp_pct']} sl={rec['sl_pct']} "
                      f"atr={rec['atr_sl_mult']} hold={rec['max_hold_days']}")
            else:
                print(f"[SKIP] {rec['name']}: 対応する tier が strategy_params.json に無いため反映しない。")
        else:
            print(f"[FAIL] {rec['name']}: ガード不通過のため反映しない。")

    if applied_items:
        _summary("**price_tiers: 一部/全株価帯を更新。**")
        return True, updated, {"items": applied_items}
    _summary("**price_tiers: 更新対象なし。**")
    return False, None, None


def evaluate_entry_guard(result, params, prev_state):
    """成行 vs 押し目指値の比較結果から entry_guard（過熱度別）を更新するか判定する。

    - moderate（25日線乖離18〜30%等）: `pullback_atr_shallow` / `pullback_wait_days`
    - high（strong + extreme）: `pullback_atr_deep` / `probe_wait_days`
    各区分で、押し目が成行より OOS 改善し十分なサンプルがある場合のみ反映する。
    """
    esc = result.get("entry_style_comparison")
    if not esc:
        return False, None, None
    by = esc.get("by_overheat") or {}

    prev_items = {}
    if prev_state and prev_state.get("entry_guard"):
        for it in (prev_state["entry_guard"].get("items") or []):
            prev_items[it.get("group")] = it

    def group_checks(g, label):
        bl = g.get("best_limit") or {}
        bt = bl.get("test") or {}
        mk = g.get("market") or {}
        checks = [
            ("{}: 取引数 >= {}".format(label, MIN_TRADES), (bt.get("trade_count") or 0) >= MIN_TRADES),
            ("{}: 約定率 >= 0.2".format(label), (bt.get("fill_rate") or 0) >= 0.2),
            ("{}: OOS PF >= {}".format(label, MIN_PF), (bt.get("pf") or 0) >= MIN_PF),
            ("{}: 期待値 > 0".format(label), (bt.get("ev_pct") or -9.0) > MIN_EV_PCT),
            ("{}: 成行比 PF改善 ({} >= {})".format(label, bt.get("pf"), mk.get("pf")),
             (bt.get("pf") or 0) >= (mk.get("pf") or 0)),
            ("{}: 成行比 EV改善 ({} >= {})".format(label, bt.get("ev_pct"), mk.get("ev_pct")),
             (bt.get("ev_pct") or -9.0) >= (mk.get("ev_pct") or 0.0)),
        ]
        prev = prev_items.get(label)
        if prev:
            prev_pf = (prev.get("test") or {}).get("pf", 0.0)
            checks.append(("{}: 前回比劣化なし (今回 {:.2f} >= 前回 {:.2f})".format(label, bt.get("pf") or 0, prev_pf),
                           (bt.get("pf") or 0) >= prev_pf - DEGRADATION_TOLERANCE_PF))
        return bl, bt, mk, checks

    eg = dict(params.get("entry_guard", {}))
    applied = []
    _summary("## 反映判定: entry_guard（高値掴み防止・過熱度別）")
    for label, keys in (("moderate", ("pullback_atr_shallow", "pullback_wait_days")),
                        ("high", ("pullback_atr_deep", "probe_wait_days"))):
        g = by.get(label)
        if not g:
            print(f"[SKIP] {label}: サンプル無し")
            continue
        bl, bt, mk, checks = group_checks(g, label)
        if _emit(checks):
            eg[keys[0]] = float(bl["atr_mult"])
            eg[keys[1]] = int(bl["wait_days"])
            applied.append({"group": label, "key": bl["key"],
                            "params": {keys[0]: eg[keys[0]], keys[1]: eg[keys[1]]},
                            "test": bt, "market": mk})
            print(f"[PASS] {label}: {keys[0]}={eg[keys[0]]}, {keys[1]}={eg[keys[1]]}")
        else:
            print(f"[FAIL] {label}: ガード不通過のため既存値を維持。")

    if not applied:
        _summary("**entry_guard: ガード不通過 → 更新しない。**")
        print("[FAIL] entry_guard は更新しません。")
        return False, None, None

    if float(eg.get("pullback_atr_deep", 1.0)) < float(eg.get("pullback_atr_shallow", 0.5)):
        eg["pullback_atr_deep"] = eg["pullback_atr_shallow"]

    _summary("**entry_guard: ガード通過 → 更新（{}区分）。**".format(len(applied)))
    return True, eg, {"items": applied}


def main():
    result_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_RESULT_PATH
    params_path = sys.argv[2] if len(sys.argv) > 2 else PARAMS_PATH
    if not os.path.exists(result_path):
        print(f"[FAIL] 結果JSONが見つかりません: {result_path}")
        return

    result = load_json(result_path)
    params = load_json(params_path)

    prev_state = load_json(STATE_PATH) if os.path.exists(STATE_PATH) else None

    changed = False
    now = datetime.now()

    sig_ok, signals, sig_meta = evaluate_signals(result, params, prev_state)
    if sig_ok:
        params["signals"] = signals
        changed = True

    tier_ok, tiers, tier_meta = evaluate_tiers(result, params, prev_state)
    if tier_ok:
        params["price_tiers"] = tiers
        changed = True

    eg_ok, entry_guard, eg_meta = evaluate_entry_guard(result, params, prev_state)
    if eg_ok:
        params["entry_guard"] = entry_guard
        changed = True

    if not changed:
        _summary("")
        _summary("**結果: 更新対象なし（ガード不通過）。**")
        print("\n[FAIL] 更新対象なし。strategy_params.json は変更しません。")
        return

    params["version"] = now.strftime("%Y-%m-%d")
    params["generated_at"] = now.strftime("%Y-%m-%dT%H:%M:%S")
    params["source_backtest"] = {
        "run_id": result.get("run_id"),
        "generated_at": result.get("generated_at"),
    }

    with open(params_path, "w", encoding="utf-8") as f:
        json.dump(params, f, ensure_ascii=False, indent=2)

    state = {
        "applied_at": now.strftime("%Y-%m-%dT%H:%M:%S"),
        "run_id": result.get("run_id"),
    }
    if sig_ok:
        state["signals"] = {**sig_meta, "run_id": result.get("run_id")}
    if tier_ok:
        state["tiers"] = {"run_id": result.get("run_id"), "items": tier_meta["items"]}
    if eg_ok:
        state["entry_guard"] = {"run_id": result.get("run_id"), **eg_meta}
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

    _summary("")
    _summary("**結果: strategy_params.json を更新。**")
    print("\n[PASS] strategy_params.json を更新しました。")


if __name__ == "__main__":
    main()
