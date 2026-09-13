"""共有設定ローダー。

単一情報源 `strategy_params.json` を読み込む。
"""
import json
import os

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DEFAULT_PARAMS_PATH = os.path.join(REPO_ROOT, "docs", "strategy_params.json")


def load_strategy_params(path=None):
    """strategy_params.json を読み込んで dict を返す。"""
    p = path or DEFAULT_PARAMS_PATH
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)
