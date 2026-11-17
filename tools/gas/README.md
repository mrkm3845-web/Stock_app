# GAS（Google Apps Script）による定刻起動セットアップ手順

GitHub Actions 標準の `schedule`（cron）は1〜2時間の遅延やスキップが発生するため、**無料の GAS を正確な目覚まし時計として使い**、クラウド上の GitHub Actions を定刻に起動します。PCの電源を落としていても動作します。

---

## 1. GitHub Personal Access Token（PAT）を発行する

1. GitHub の [Settings > Developer settings > Personal access tokens > Fine-grained tokens](https://github.com/settings/personal-access-tokens/new) を開く
2. 設定値：
   - **Token name**: `Stock_app_GAS_Trigger` など任意
   - **Expiration**: 90日〜1年（期限切れ前に再発行）
   - **Repository access**: `Only select repositories` → `Stock_app` のみ選択
   - **Permissions > Repository permissions > Actions**: **`Read and write`**（必須）
3. 生成されたトークン（`github_pat_...`）をコピーしておく（※画面を閉じると再表示できません）。

---

## 2. Google Apps Script プロジェクトを作る

1. [Google Apps Script](https://script.google.com/) を開き、「**新しいプロジェクト**」をクリック
2. 左上のプロジェクト名を `Stock_app 定時トリガー` などに変更
3. エディタ内の既存の `function myFunction() {}` をすべて消し、[`trigger_workflows.gs`](trigger_workflows.gs) の中身を丸ごと貼り付けて保存（`Ctrl+S`）
4. **タイムゾーン確認**：左メニュー「歯車（プロジェクトの設定）」を開き、タイムゾーンが **`(GMT+09:00) 日本標準時 - 東京`** になっていることを確認する

---

## 3. トークンをスクリプトプロパティに登録する

※コード内にトークンを直書きせず、安全な設定領域へ保存します。

1. 左メニュー「歯車（プロジェクトの設定）」を下へスクロールし、「**スクリプト プロパティ**」を開く
2. 「スクリプト プロパティを追加」をクリック
   - **プロパティ**: `GITHUB_TOKEN`
   - **値**: 手順1でコピーした GitHub PAT
3. 「スクリプト プロパティを保存」をクリック

---

## 4. 動作テストをする

1. エディタ上部の関数選択プルダウンから **`testMorningNow`** を選び、「**実行**」をクリック
2. 初回のみ「承認が必要です」ダイアログが出るので、自分の Google アカウントを選び、「詳細」→「（安全ではないページ）に移動」→「許可」をクリック
   - ※日本の祝日カレンダーの読み取りと GitHub API への通信許可のためです
3. 下部の実行ログに `起動成功: daily_briefing.yml {"slot":"morning","force":true}` と表示され、数分後に **Discord にテスト通知が届けば完了** です

---

## 5. 毎日の自動トリガーを1つだけ登録する

1. 左メニュー「時計（トリガー）」を開く
2. 右下の「**トリガーを追加**」をクリックし、以下のように設定：
   - **実行する関数を選択**: `setupTodayExactTriggers`
   - **イベントのソースを選択**: `時間主導型`
   - **時間ベースのトリガーのタイプを選択**: `日付ベースのタイマー`
   - **時刻を選択**: `午前 4時〜5時`
3. 「保存」をクリック

### これだけで毎日動くスケジュール（東証営業日のみ・JST）

- **06:15**：朝ブリーフィング（米国市場・為替・ニュース・今日の方針・CSV催促）
- **12:15**：昼チェック（前場引けの作戦台帳チェック＋日中ブリーフィング）
- **14:50**：引け前判断（大引け前の作戦台帳チェック＋買い/落ちるナイフ最終判断）
- **16:00**：引け後スクリーニング（`daily_main8.yml`＋引け後EODチェック）
- **20:15**：夜AI推奨＆まとめ（`daily_main8_ai.yml`＋EODチェック＋夜ブリーフィング・CSV催促）
- **土曜 09:00**：週次答え合わせ（`weekly_review.yml`）
