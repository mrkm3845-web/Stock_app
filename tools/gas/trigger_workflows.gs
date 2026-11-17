/**
 * Stock_app 用 Google Apps Script (GAS) 定期起動スクリプト
 *
 * 役割:
 *   GitHub Actions の schedule (cron) の遅延・スキップを防ぐため、
 *   GAS を「時計」として GitHub REST API (workflow_dispatch) を定刻起動します。
 *
 * 事前準備:
 *   1. スクリプトプロパティに `GITHUB_TOKEN`（GitHub PAT: Actions write 権限）を登録する
 *   2. トリガー画面で `setupTodayExactTriggers` を「毎日 午前4時〜5時」に1つだけ登録する
 *      （その日が東証営業日なら、当日の 06:15 / 12:15 / 14:50 / 16:00 / 20:15 に
 *       分単位のピンポイントトリガーを自動予約します。土曜は 09:00 の週次レビューを予約）
 */

const GITHUB_OWNER = 'mrkm3845-web';
const GITHUB_REPO = 'Stock_app';
const GITHUB_BRANCH = 'main';
const JP_HOLIDAY_CALENDAR_ID = 'ja.japanese#holiday@group.v.calendar.google.com';

// setupTodayExactTriggers で自動予約する関数名一覧（掃除対象）
const MANAGED_HANDLERS = [
  'triggerMorning',
  'triggerMidday',
  'triggerPreclose',
  'triggerMain8AfterClose',
  'triggerMain8AI',
  'triggerWeeklyReview',
];

/**
 * 毎日 04:00〜05:00 に1回実行し、当日分のピンポイントトリガーを予約する。
 */
function setupTodayExactTriggers() {
  cleanupManagedTriggers_();

  const now = new Date();
  const y = now.getFullYear();
  const m = now.getMonth();
  const d = now.getDate();
  const dayOfWeek = now.getDay(); // 0:日, 6:土

  // 土曜日は週次レビュー（09:00 JST）だけ予約
  if (dayOfWeek === 6) {
    scheduleAt_(y, m, d, 9, 0, 'triggerWeeklyReview');
    console.log('土曜日のため 09:00 に triggerWeeklyReview を予約しました。');
    return;
  }

  // 日曜・祝日・年末年始は予約しない
  if (!isTseBusinessDay(now)) {
    console.log('東証休場日のため本日の予約をスキップします。');
    return;
  }

  // 東証営業日の定刻スケジュール（JST）
  scheduleAt_(y, m, d, 6, 15, 'triggerMorning');          // 朝ブリーフィング
  scheduleAt_(y, m, d, 12, 15, 'triggerMidday');          // 前場引けチェック
  scheduleAt_(y, m, d, 14, 50, 'triggerPreclose');        // 大引け前最終判断
  scheduleAt_(y, m, d, 16, 0, 'triggerMain8AfterClose');  // 引け後スクリーニング
  scheduleAt_(y, m, d, 20, 15, 'triggerMain8AI');         // 夜AI推奨＋夜ブリーフィング
  console.log('本日の東証営業日トリガー（06:15 / 12:15 / 14:50 / 16:00 / 20:15）を予約しました。');
}

/** 朝（06:15）: 米国市場・為替・ニュース確認＆今日の方針・CSV催促 */
function triggerMorning() {
  if (!isTseBusinessDay(new Date())) return;
  dispatchWorkflow_('daily_briefing.yml', { slot: 'morning' });
}

/** 昼（12:15）: 前場引けの作戦台帳チェック＋日中ブリーフィング */
function triggerMidday() {
  if (!isTseBusinessDay(new Date())) return;
  dispatchWorkflow_('plan_check_intraday.yml', {});
  dispatchWorkflow_('daily_briefing.yml', { slot: 'midday' });
}

/** 引け前（14:50）: 大引け前の作戦台帳チェック＋買い/落ちるナイフ最終判断 */
function triggerPreclose() {
  if (!isTseBusinessDay(new Date())) return;
  dispatchWorkflow_('plan_check_intraday.yml', {});
  dispatchWorkflow_('daily_briefing.yml', { slot: 'preclose' });
}

/** 引け後（16:00）: 全銘柄テクニカルスクリーニング＋引け後EODチェック */
function triggerMain8AfterClose() {
  if (!isTseBusinessDay(new Date())) return;
  dispatchWorkflow_('daily_main8.yml', {});
}

/** 夜（20:15）: AI推奨生成＋EODチェック＋夜ブリーフィング（ワークフロー内で連続実行） */
function triggerMain8AI() {
  if (!isTseBusinessDay(new Date())) return;
  dispatchWorkflow_('daily_main8_ai.yml', {});
}

/** 土曜（09:00）: 週次答え合わせ＆来週の作戦生成 */
function triggerWeeklyReview() {
  dispatchWorkflow_('weekly_review.yml', {});
}

/** 手動動作確認用: 朝ブリーフィングを即時1回テスト実行する */
function testMorningNow() {
  dispatchWorkflow_('daily_briefing.yml', { slot: 'morning', force: true });
}

/** 東証の営業日判定（土日・日本の祝日・年末年始 12/31〜1/3 を除外） */
function isTseBusinessDay(targetDate) {
  const day = targetDate.getDay();
  if (day === 0 || day === 6) return false;

  const month = targetDate.getMonth() + 1;
  const dateNum = targetDate.getDate();
  if ((month === 12 && dateNum === 31) || (month === 1 && dateNum <= 3)) {
    return false;
  }

  try {
    const cal = CalendarApp.getCalendarById(JP_HOLIDAY_CALENDAR_ID);
    if (cal) {
      const events = cal.getEventsForDay(targetDate);
      if (events && events.length > 0) {
        console.log('祝日判定により休場扱い: ' + events[0].getTitle());
        return false;
      }
    }
  } catch (e) {
    console.warn('祝日カレンダー参照に失敗したため平日のみで継続します: ' + e);
  }
  return true;
}

/** GitHub Actions workflow_dispatch を実行する */
function dispatchWorkflow_(workflowFile, inputs) {
  const token = PropertiesService.getScriptProperties().getProperty('GITHUB_TOKEN');
  if (!token) {
    throw new Error('スクリプトプロパティ GITHUB_TOKEN が未設定です。');
  }

  const url =
    'https://api.github.com/repos/' +
    GITHUB_OWNER +
    '/' +
    GITHUB_REPO +
    '/actions/workflows/' +
    encodeURIComponent(workflowFile) +
    '/dispatches';

  const payload = {
    ref: GITHUB_BRANCH,
    inputs: inputs || {},
  };

  const res = UrlFetchApp.fetch(url, {
    method: 'post',
    contentType: 'application/json',
    headers: {
      Authorization: 'Bearer ' + token,
      Accept: 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28',
    },
    payload: JSON.stringify(payload),
    muteHttpExceptions: true,
  });

  const code = res.getResponseCode();
  if (code === 204) {
    console.log('起動成功: ' + workflowFile + ' ' + JSON.stringify(inputs || {}));
  } else {
    console.error('起動失敗 (' + code + '): ' + workflowFile + ' -> ' + res.getContentText());
  }
}

/** 指定時刻（すでに過去ならスキップ）にワンショットトリガーを予約する */
function scheduleAt_(year, month, day, hour, minute, handlerName) {
  const target = new Date(year, month, day, hour, minute, 0);
  if (target.getTime() <= Date.now() + 60 * 1000) {
    console.log('時刻経過のためスキップ: ' + handlerName + ' (' + target.toLocaleString('ja-JP') + ')');
    return;
  }
  ScriptApp.newTrigger(handlerName).timeBased().at(target).create();
}

/** 前日以前・残存している管理対象トリガーを削除する */
function cleanupManagedTriggers_() {
  const triggers = ScriptApp.getProjectTriggers();
  for (let i = 0; i < triggers.length; i++) {
    const fn = triggers[i].getHandlerFunction();
    if (MANAGED_HANDLERS.indexOf(fn) !== -1) {
      ScriptApp.deleteTrigger(triggers[i]);
    }
  }
}
