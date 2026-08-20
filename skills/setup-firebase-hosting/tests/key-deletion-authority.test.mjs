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
  // 単一の複雑な正規表現で代入文全体を貫通させると、printf の引数に
  // `)` を含む変更（例: 関数呼び出しの追加）だけで正規表現が途中で
  // 止まり誤検出（偽陽性の PASS）する。行を特定してから要素ごとに
  // 存在確認する方式にして、この種の脆さを避ける（Bugbot 指摘）。
  const recordedKeyIdsLine = script
    .split('\n')
    .find((line) => line.includes('recorded_key_ids="$('))
  assert.ok(
    recordedKeyIdsLine,
    'recorded_key_ids="$(...)" の代入行が見つからない'
  )
  assert.match(
    recordedKeyIdsLine,
    /\bprintf\b/,
    'recorded_key_ids の代入に printf が使われていない'
  )
  assert.match(
    recordedKeyIdsLine,
    /\$\{sa_description\}/,
    'recorded_key_ids が sa_description（SA description の取得結果）から導出されていない'
  )
  assert.match(
    recordedKeyIdsLine,
    /sed\s+-n\s+"s\/\^\$\{KEY_RECORD_MARKER\}/,
    'recorded_key_ids が KEY_RECORD_MARKER 行の sed 抽出を経由していない'
  )
  assert.match(
    recordedKeyIdsLine,
    /head\s+-1\b/,
    'recorded_key_ids の抽出に head -1（マーカー行の一意化）が使われていない'
  )
})

// 実際に実行される gcloud keys delete 呼び出しだけを対象にする。行頭
// （インデント許容）に `if` / `if !` / `elif` / `while` / `until` /
// `command` のいずれかの制御構文プレフィックス（0 個以上）を許し、その
// 直後に `gcloud` が来る行に限定する。`^\s*gcloud\b` のみに限定すると
// `if ! gcloud ... keys delete` のような正当な行（現行のロールバック
// cleanup）自体が対象外になり、将来 `if gcloud ...` 形で追加される
// ガード外の削除も検知できない（codex-review P1 指摘）。
// `echo`/`printf` によるユーザー向け案内文言（変数展開込みでも実行され
// ない文字列）は、この位置に `gcloud` が来ないため自然に除外される。
// `<KEY_ID>` プレースホルダを含む行は die メッセージ内の案内コマンド例
// として別途除外する。
const DELETE_INVOCATION_PREFIX_RE =
  /^\s*(?:command\s+)?(?:if\s+|elif\s+|while\s+|until\s+)?!?\s*gcloud\b/

function extractKeyDeleteExecLines(content) {
  return content
    .split('\n')
    .filter((line) => /iam\s+service-accounts\s+keys\s+delete\b/.test(line))
    .filter((line) => !line.includes('<KEY_ID>'))
    .filter((line) => DELETE_INVOCATION_PREFIX_RE.test(line))
}

test('gcloud iam service-accounts keys delete の実行行が案内文言以外に最低限存在する（抽出ロジックの破損検知）', () => {
  const execLines = extractKeyDeleteExecLines(script)
  assert.ok(
    execLines.length >= 3,
    `鍵削除の実行行が想定より少ない（世代交代ループ 2 箇所・ロールバック cleanup 1 箇所の計 3 箇所を期待）: ${execLines.length} 件`
  )
})

// 削除行が `case ",${recorded_key_ids}," in` ブロックの「内側にあるか」
// だけでなく、実際に鍵 ID 一致アーム（`*",${key_id},"*)` 等）に属して
// いるかを構造的に検証する。ブロック内側判定だけだと、同じ case 文に
// ガードなしの default アーム（`*)`）を新設して削除を追加しても通過して
// しまう（codex-review P1 指摘）。
function isGuardedByRecordedKeys(lines, idx) {
  let armLabel = null
  for (let i = idx - 1; i >= 0; i--) {
    const line = lines[i]
    if (/^\s*;;\s*$/.test(line)) {
      // 直前のアームの終端に達した＝自分のアーム開始ラベルより先に
      // case 開始行へは到達できない（アーム境界をまたいでいる）。
      return false
    }
    if (/^\s*esac\s*$/.test(line)) {
      return false
    }
    if (armLabel === null && !/^\s*case\b/.test(line) && /\)\s*$/.test(line.trim())) {
      // このアームの開始ラベル行（例: `*",${key_id},"*)`）
      armLabel = line.trim()
      continue
    }
    if (/^\s*case\s+",\$\{recorded_key_ids\},"\s+in\s*$/.test(line)) {
      // 発行記録との一致を判定する case 文の開始行へ到達した。
      // アームラベルが鍵 ID を参照しており、かつ default アーム
      // （`*)` 単独）でないことを確認する。
      return Boolean(armLabel) && armLabel !== '*)' && /\$\{key_id\}/.test(armLabel)
    }
    if (/^\s*case\b/.test(line)) {
      // 別の case 文の開始行に達した＝目的の case の内側ではない。
      return false
    }
  }
  return false
}

test('鍵削除の実行行はすべて発行記録の一致アーム内、またはロールバック cleanup 内に属する', () => {
  const lines = script.split('\n')
  const execLineIndexes = []
  lines.forEach((line, i) => {
    if (
      /iam\s+service-accounts\s+keys\s+delete\b/.test(line) &&
      !line.includes('<KEY_ID>') &&
      DELETE_INVOCATION_PREFIX_RE.test(line)
    ) {
      execLineIndexes.push(i)
    }
  })

  assert.ok(execLineIndexes.length > 0, '鍵削除の実行行が抽出できていない（抽出ロジックの破損の可能性）')

  for (const idx of execLineIndexes) {
    // ロールバック判定: 削除対象が rollback_key_name（今回発行し、まだ
    // Secret へ登録できていない鍵）を指している行。所有が実行そのものに
    // 自明なため発行記録ガードを要しない。
    const isRollbackDelete = lines[idx].includes('rollback_key_name')

    const inRecordedGuardArm = isGuardedByRecordedKeys(lines, idx)

    assert.ok(
      isRollbackDelete || inRecordedGuardArm,
      `ガード外（発行記録の一致アーム外）の鍵削除行を検出（行 ${idx + 1}）: ${lines[idx].trim()}`
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
