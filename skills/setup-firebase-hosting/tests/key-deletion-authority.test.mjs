// key-deletion-authority.test.mjs — Issue #3 の回帰テスト。
//
// 上流 PR（agent-cli-skills#63）の codex レビューで P0 指摘された論点:
// SA 鍵の世代交代（旧鍵の自動削除）の根拠として GitHub Actions 変数
// （FIREBASE_SA_KEY_IDS）を使うと、GitHub 側の誤設定・改ざん・トークン
// 侵害が実行者の GCP 権限を通じて任意の既存鍵の失効へ波及する
// （可用性攻撃・DoS の経路になる）。
//
// 本リポジトリの現行実装（bootstrap-firebase.sh）は削除根拠を GCP 側の
// 記録（専用 SA の description に持つ発行記録。書き換えに GCP IAM の
// iam.serviceAccounts.update 権限を要し、鍵管理と同一の信頼境界にある）
// のみに限定する設計を既に採用している。このテストはその設計が退行
// しないことを静的検査で固定化する。
//
// このテストは:
//   1. GitHub 側可変データ（FIREBASE_SA_KEY_IDS）が bootstrap-firebase.sh /
//      SKILL.md のどちらにも出現しないこと
//   2. bootstrap-firebase.sh に GitHub 側から値を読み出す経路
//      （gh variable get / gh api）が存在しないこと（gh variable set /
//      gh secret set の書き込みは許可）
//   3. GCP 側記録アンカー（KEY_RECORD_MARKER）が定義され、SA description
//      から発行記録（recorded_key_ids）を導出していること
//   4. `gcloud iam service-accounts keys delete` として実行されるコマンド
//      行が、(a) 発行記録ガード（`case ",${recorded_key_ids}," in` の内側）
//      または (b) 今回発行した鍵のロールバック用 cleanup のいずれかに
//      属すること（ガード外の削除経路が新設されていないか）
//   5. 記録に無い鍵を削除しない fail-safe 分岐と、ROTATE_EXISTING_KEYS
//      による opt-out 分岐が存在すること
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const SKILL_DIR = join(dirname(fileURLToPath(import.meta.url)), '..')
const SKILL_MD_PATH = join(SKILL_DIR, 'SKILL.md')
const BOOTSTRAP_SCRIPT_PATH = join(SKILL_DIR, 'scripts', 'bootstrap-firebase.sh')

const skillMd = readFileSync(SKILL_MD_PATH, 'utf8')
const script = readFileSync(BOOTSTRAP_SCRIPT_PATH, 'utf8')

test('GitHub Actions 変数 FIREBASE_SA_KEY_IDS が SKILL.md / bootstrap-firebase.sh のどちらにも出現しない', () => {
  assert.ok(
    !skillMd.includes('FIREBASE_SA_KEY_IDS'),
    'SKILL.md に FIREBASE_SA_KEY_IDS が出現している（GitHub 側可変データを削除根拠に戻す退行の可能性）'
  )
  assert.ok(
    !script.includes('FIREBASE_SA_KEY_IDS'),
    'bootstrap-firebase.sh に FIREBASE_SA_KEY_IDS が出現している（GitHub 側可変データを削除根拠に戻す退行の可能性）'
  )
})

test('bootstrap-firebase.sh に GitHub 側から値を読み出す経路（gh variable get / gh api）が無い', () => {
  assert.ok(
    !/\bgh\s+variable\s+get\b/.test(script),
    'bootstrap-firebase.sh に `gh variable get` が存在する（GitHub 側可変データの読み出し経路）'
  )
  assert.ok(
    !/\bgh\s+api\b/.test(script),
    'bootstrap-firebase.sh に `gh api` が存在する（GitHub 側可変データの読み出し経路の可能性）'
  )
  // 書き込み（gh variable set / gh secret set）は許可対象であり、退行検知の
  // 誤検出でないことを確認する（抽出ロジックの破損検知）
  assert.match(script, /\bgh\s+variable\s+set\b/, 'gh variable set が見つからない（抽出ロジックの破損の可能性）')
  assert.match(script, /\bgh\s+secret\s+set\b/, 'gh secret set が見つからない（抽出ロジックの破損の可能性）')
})

test('GCP 側記録アンカー（KEY_RECORD_MARKER）が定義され、SA description から発行記録を導出している', () => {
  assert.match(
    script,
    /KEY_RECORD_MARKER="[^"]+"/,
    'KEY_RECORD_MARKER の定義が見つからない'
  )
  assert.match(
    script,
    /recorded_key_ids="\$\(printf[^)]*\|\s*sed\s+-n\s+"s\/\^\$\{KEY_RECORD_MARKER\}/,
    'recorded_key_ids が SA description（KEY_RECORD_MARKER 行）から導出されていない'
  )
})

// 実際に実行されるコマンド行だけを対象にする。行頭（インデント許容）が
// `gcloud` で始まる行に限定し、`<KEY_ID>` プレースホルダを含む行は die
// メッセージ内のユーザー向け案内コマンド例（実行されない文字列）として
// 除外する。
function extractKeyDeleteExecLines(content) {
  return content
    .split('\n')
    .filter((line) => /^\s*gcloud\b/.test(line))
    .filter((line) => /iam\s+service-accounts\s+keys\s+delete\b/.test(line))
    .filter((line) => !line.includes('<KEY_ID>'))
}

test('gcloud iam service-accounts keys delete の実行行が案内文言以外に最低限存在する（抽出ロジックの破損検知）', () => {
  const execLines = extractKeyDeleteExecLines(script)
  assert.ok(
    execLines.length >= 2,
    `鍵削除の実行行が想定より少ない（世代交代ループ・上限解消の事前削除の 2 箇所を期待）: ${execLines.length} 件`
  )
})

test('鍵削除の実行行はすべて発行記録ガード内、またはロールバック cleanup 内に属する', () => {
  const lines = script.split('\n')
  const execLineIndexes = []
  lines.forEach((line, i) => {
    if (
      /^\s*gcloud\b/.test(line) &&
      /iam\s+service-accounts\s+keys\s+delete\b/.test(line) &&
      !line.includes('<KEY_ID>')
    ) {
      execLineIndexes.push(i)
    }
  })

  for (const idx of execLineIndexes) {
    // ロールバック判定: 削除対象が rollback_key_name（今回発行し、まだ
    // Secret へ登録できていない鍵）を指している行。所有が実行そのものに
    // 自明なため発行記録ガードを要しない。
    const isRollbackDelete = lines[idx].includes('rollback_key_name')

    // 発行記録ガード判定: 直前の行から遡り、間に case の終端（esac）を
    // またがずに `case ",${recorded_key_ids}," in` へ到達できること
    // （＝この削除行が当該 case ブロックの内側にある）。
    let inRecordedGuard = false
    for (let i = idx - 1; i >= 0; i--) {
      if (/case\s+",\$\{recorded_key_ids\},"\s+in/.test(lines[i])) {
        inRecordedGuard = true
        break
      }
      if (/^\s*esac\s*$/.test(lines[i])) {
        break
      }
    }

    assert.ok(
      isRollbackDelete || inRecordedGuard,
      `ガード外の鍵削除行を検出（行 ${idx + 1}）: ${lines[idx].trim()}`
    )
  }
})

test('記録に無い鍵を削除しない fail-safe 分岐と ROTATE_EXISTING_KEYS opt-out 分岐が存在する', () => {
  assert.match(
    script,
    /unrecorded_keys=/,
    '記録に無い鍵を退避する unrecorded_keys 分岐が見つからない（fail-safe の退行の可能性）'
  )
  assert.match(
    script,
    /ROTATE_EXISTING_KEYS:-true/,
    'ROTATE_EXISTING_KEYS の opt-out 分岐（既定 true）が見つからない'
  )
})
