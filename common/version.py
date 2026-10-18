"""プロンプト／戦略の版管理（単一ソース）。

- PROMPT_VERSION: AIへのシステムプロンプト（日次分析・週次作戦）の版。
  文面や「過去検証からの学び」の注入仕様を変えたら更新する。
- 戦略版は docs/strategy_params.json の version を参照する（自動更新）。

答え合わせ（weekly_review）は、この版を各ピックに刻印して
「どの版が良かったか」を後から検証できるようにする。
"""

# AIプロンプトの版（日次 _ai_system_prompt / 週次 _plan_system_prompt 共通）
PROMPT_VERSION = "2026-10-01-1"

# スコアの版・実験タグ（picks / history / 週次答え合わせに刻印して事後検証する）
SCORE_VERSION = "v3-research_upside-2026-10-18"
SCORE_EXPERIMENT = "research_upside_v3_2026-10"
