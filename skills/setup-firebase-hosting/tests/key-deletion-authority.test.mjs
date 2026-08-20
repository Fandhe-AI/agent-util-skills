// key-deletion-authority.test.mjs — Issue #3 の回帰テスト（挙動テスト）。
//
// ## 検証する不変条件
//
// 上流 PR（agent-cli-skills#63）の codex レビューで P0 指摘された論点:
// SA 鍵の世代交代（旧鍵の自動削除）の根拠として GitHub 側の可変データ
// （Actions 変数等）を使うと、GitHub 側の誤設定・改ざん・トークン侵害が
// 実行者の GCP 権限を通じて任意の既存鍵の失効へ波及する（可用性攻撃・DoS）。
//
// 本リポジトリの bootstrap-firebase.sh は削除根拠を GCP 側の記録（専用 SA の
// description に持つ発行記録。書き換えに iam.serviceAccounts.update 権限を
// 要し、鍵管理と同一の信頼境界にある）のみに限定する。このテストが固定化
// する不変条件は次の 1 点に集約される:
//
//   **削除される鍵は、GCP 側の発行記録にある鍵（+ 今回の実行が発行した鍵の
//   ロールバック）に限られる。記録に無い鍵はどのシナリオでも削除されない。**
//
// ## 検証方式（なぜ静的解析ではなく挙動テストか）
//
// 旧版はスクリプト本文を正規表現・行走査で静的解析していたが、シェルの
// 表現力（行分割・ラッパー関数・変数組み立て・バッククォート置換・引用符
// 分割・同一行複数呼び出し等）に対して迂回経路を塞ぎきれないという P1 指摘
// が反復した（PR #5 レビュー）。本版はアプローチを変え、**スクリプトを実際に
// 実行して観測された削除呼び出しだけを検証する**。
//
//   1. PATH 先頭にフェイク `gcloud` / `gh` / `curl` の shim を置く。shim は
//      SHIM_STATE_DIR 配下のファイルを疑似 GCP/GitHub 状態として読み書きし、
//      鍵削除の呼び出しを deletions.log / delete-attempts.log へ記録する。
//      未知のサブコマンドは fail-closed（exit 1 → set -e で本体停止）。
//   2. bootstrap-firebase.sh 全体を一時ディレクトリへコピーして実行し
//      （.firebaserc 生成でリポジトリを汚さないため）、終了コード・削除
//      ログ・残存鍵・発行記録（description）の終状態を assert する。
//
// この方式では「スクリプトがどう書かれているか」ではなく「実際に何を削除
// したか」を検証するため、シェル構文による迂回はそのまま削除ログに現れて
// テストが失敗する（削除を隠すには shim を迂回して本物の gcloud を呼ぶしか
// なく、それは PATH 制御下では起きない）。
//
// ## シナリオ
//
//   (a) 正常系: 発行記録にある旧鍵のみ削除され、記録は現行鍵のみに更新される
//   (b) 記録外の鍵が混在 + GitHub 側データ（FIREBASE_SA_KEY_IDS 環境変数）が
//       記録外の鍵を指しても、その鍵は削除されない（fail-safe + Issue #3 本体）
//   (c) Secret 登録失敗: 今回発行した鍵だけがロールバック削除され、旧鍵は残る
//   (d) 削除 API が 403: スクリプトは停止し、発行記録が保持される（次回再試行可能）
//   (e) 鍵数上限（10 個）: 記録にある鍵のみで空きを作り、最後に記録した鍵
//       （現行 Secret が指す可能性が高い鍵）は事前削除しない
//   (f) ROTATE_EXISTING_KEYS=false: 削除が一切行われない（opt-out）
//   (g) 上限だが記録にある鍵が無い: 何も削除せず fail-closed で停止する
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import {
  chmodSync,
  copyFileSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const SKILL_DIR = join(dirname(fileURLToPath(import.meta.url)), '..')
const BOOTSTRAP_SCRIPT_PATH = join(SKILL_DIR, 'scripts', 'bootstrap-firebase.sh')

// スクリプトへ渡す設定値。shim もこの値でフル鍵名を組み立てる。
const PROJECT_ID = 'test-proj'
const SITE_ID = 'test-site'
const GITHUB_REPO = 'owner/repo'
const SA_EMAIL = `github-actions-hosting@${PROJECT_ID}.iam.gserviceaccount.com`
// shim の keys create が発行する鍵 ID（決定的にして assert 可能にする）
const NEW_KEY_ID = 'newly-issued-key'
const MARKER = 'firebase-bootstrap-issued-keys='

const keyName = (id) =>
  `projects/${PROJECT_ID}/serviceAccounts/${SA_EMAIL}/keys/${id}`

// --- shim 本体 -------------------------------------------------------------
// 疑似 GCP 状態（SHIM_STATE_DIR）:
//   description         SA の description（発行記録の置き場）
//   keys                現存する USER_MANAGED 鍵のフル名（1 行 1 鍵）
//   next-key-id         keys create が発行する鍵 ID
//   deletions.log       実際に削除された鍵（成功した delete のみ）
//   delete-attempts.log 削除が試行された鍵（失敗含む）
//   fail-delete         存在すると keys delete が 403 相当で失敗する
//   fail-secret         存在すると gh secret set が失敗する
//   secret-payload.json gh secret set が受け取った鍵ファイルの内容

const GCLOUD_SHIM = `#!/usr/bin/env bash
# gcloud shim — 本物の gcloud を呼ばず SHIM_STATE_DIR を疑似 GCP として読み書き
# する。未知のサブコマンドは fail-closed で失敗させ、想定外の API 呼び出しが
# 追加された場合にテストが必ず気付けるようにする。
set -euo pipefail
state="\${SHIM_STATE_DIR:?}"
printf 'gcloud %s\\n' "$*" >> "\${state}/gcloud-calls.log"
args="$*"
case "\${args}" in
  "auth list "*) echo "tester@example.com" ;;
  "auth print-access-token"*) echo "fake-token" ;;
  "billing projects describe "*) echo "False" ;;
  "projects describe "*) : ;;
  "services enable "*) : ;;
  "projects add-iam-policy-binding "*) : ;;
  "iam service-accounts describe "*)
    if [[ "\${args}" == *"--format=value(description)"* ]]; then
      cat "\${state}/description"
    fi
    ;;
  "iam service-accounts create "*|"iam service-accounts update "*)
    for a in "$@"; do
      case "\${a}" in
        --description=*) printf '%s\\n' "\${a#--description=}" > "\${state}/description" ;;
      esac
    done
    ;;
  "iam service-accounts keys list "*) cat "\${state}/keys" ;;
  "iam service-accounts keys create "*)
    key_file="$5"
    key_id="$(cat "\${state}/next-key-id")"
    printf '{"type":"service_account","private_key_id":"%s"}\\n' "\${key_id}" > "\${key_file}"
    printf 'projects/%s/serviceAccounts/%s/keys/%s\\n' "\${FAKE_PROJECT_ID:?}" "\${FAKE_SA_EMAIL:?}" "\${key_id}" >> "\${state}/keys"
    ;;
  "iam service-accounts keys delete "*)
    key_name="$5"
    printf '%s\\n' "\${key_name}" >> "\${state}/delete-attempts.log"
    if [[ -f "\${state}/fail-delete" ]]; then
      echo "ERROR: (gcloud.iam.service-accounts.keys.delete) PERMISSION_DENIED: 403" >&2
      exit 1
    fi
    printf '%s\\n' "\${key_name}" >> "\${state}/deletions.log"
    grep -vFx "\${key_name}" "\${state}/keys" > "\${state}/keys.tmp" || true
    mv "\${state}/keys.tmp" "\${state}/keys"
    ;;
  *)
    echo "gcloud shim: unhandled args: \${args}" >&2
    exit 1
    ;;
esac
`

const GH_SHIM = `#!/usr/bin/env bash
# gh shim — Secret / 変数の登録を記録するだけで GitHub へは一切通信しない。
set -euo pipefail
state="\${SHIM_STATE_DIR:?}"
printf 'gh %s\\n' "$*" >> "\${state}/gh-calls.log"
case "$*" in
  "auth status"*) : ;;
  "repo view "*) echo "ADMIN" ;;
  "secret set "*)
    cat > "\${state}/secret-payload.json"
    if [[ -f "\${state}/fail-secret" ]]; then
      echo "gh shim: secret set failed (simulated)" >&2
      exit 1
    fi
    ;;
  "variable set "*) : ;;
  *) echo "gh shim: unhandled args: $*" >&2; exit 1 ;;
esac
`

const CURL_SHIM = `#!/usr/bin/env bash
# curl shim — Firebase API 経路を最短で通過させる（addFirebase は 409 =
# 追加済み、サイト作成は 200）。鍵管理は curl を経由しないため、それ以外の
# URL が現れたら設計変更なので fail-closed で失敗させる。
set -euo pipefail
state="\${SHIM_STATE_DIR:?}"
printf 'curl %s\\n' "$*" >> "\${state}/curl-calls.log"
case "$*" in
  *":addFirebase"*) printf '{}\\nHTTP_STATUS:409' ;;
  *"/sites?siteId="*) printf '{"name":"ok"}\\nHTTP_STATUS:200' ;;
  *) echo "curl shim: unhandled args: $*" >&2; exit 1 ;;
esac
`

// node / npx は前提ツール確認（command -v）に応答するだけで実行されない
const NOOP_SHIM = '#!/bin/sh\nexit 0\n'

// --- シナリオ構築・実行 -----------------------------------------------------

// 一時ディレクトリへスクリプトと shim を配置し、疑似 GCP 状態を初期化する。
// scripts/ サブディレクトリへコピーするのは、スクリプトが project_root
// （= scripts の親）へ .firebaserc を書くため。
function setupScenario({ recordedKeyIds, existingKeyIds, failSecret = false, failDelete = false }) {
  const root = mkdtempSync(join(tmpdir(), 'key-deletion-authority-'))
  const bin = join(root, 'bin')
  const state = join(root, 'state')
  const scripts = join(root, 'scripts')
  const work = join(root, 'work')
  for (const dir of [bin, state, scripts, work]) mkdirSync(dir)

  copyFileSync(BOOTSTRAP_SCRIPT_PATH, join(scripts, 'bootstrap-firebase.sh'))
  for (const [name, body] of [
    ['gcloud', GCLOUD_SHIM],
    ['gh', GH_SHIM],
    ['curl', CURL_SHIM],
    ['node', NOOP_SHIM],
    ['npx', NOOP_SHIM],
  ]) {
    const path = join(bin, name)
    writeFileSync(path, body)
    chmodSync(path, 0o755)
  }

  writeFileSync(join(state, 'description'), `${MARKER}${recordedKeyIds}\n`)
  writeFileSync(
    join(state, 'keys'),
    existingKeyIds.map((id) => `${keyName(id)}\n`).join('')
  )
  writeFileSync(join(state, 'next-key-id'), `${NEW_KEY_ID}\n`)
  if (failSecret) writeFileSync(join(state, 'fail-secret'), '')
  if (failDelete) writeFileSync(join(state, 'fail-delete'), '')

  const run = (envOverrides = {}) => {
    const env = { ...process.env }
    // 外側の環境から漏れてシナリオを変え得る変数は明示的に排除する
    for (const name of [
      'ROTATE_EXISTING_KEYS',
      'ADOPT_EXISTING_SA',
      'ALLOW_BLAZE',
      'FIREBASE_SA_KEY_IDS',
    ]) {
      delete env[name]
    }
    Object.assign(env, {
      PATH: `${bin}:${process.env.PATH}`,
      PROJECT_ID,
      SITE_ID,
      GITHUB_REPO,
      SHIM_STATE_DIR: state,
      FAKE_PROJECT_ID: PROJECT_ID,
      FAKE_SA_EMAIL: SA_EMAIL,
      TMPDIR: work,
      ...envOverrides,
    })
    const result = spawnSync('bash', [join(scripts, 'bootstrap-firebase.sh')], {
      env,
      encoding: 'utf8',
      timeout: 60_000,
    })
    assert.equal(result.error, undefined, `スクリプトの起動に失敗: ${result.error}`)
    return result
  }

  const readLines = (name) => {
    const path = join(state, name)
    if (!existsSync(path)) return []
    return readFileSync(path, 'utf8').split('\n').filter((line) => line !== '')
  }

  return {
    root,
    run,
    deletions: () => readLines('deletions.log'),
    deleteAttempts: () => readLines('delete-attempts.log'),
    remainingKeys: () => readLines('keys'),
    description: () => readFileSync(join(state, 'description'), 'utf8').trim(),
    secretPayload: () => readFileSync(join(state, 'secret-payload.json'), 'utf8'),
    cleanup: () => rmSync(root, { recursive: true, force: true }),
  }
}

// --- シナリオ (a): 正常系 ---------------------------------------------------

test('発行記録にある旧鍵のみ削除され、記録が現行鍵のみへ更新される（正常系）', () => {
  const s = setupScenario({
    recordedKeyIds: 'old-key-1',
    existingKeyIds: ['old-key-1'],
  })
  try {
    const r = s.run()
    assert.equal(r.status, 0, `正常終了するはず: ${r.stdout}\n${r.stderr}`)
    // 削除されたのは記録にある旧鍵ちょうど 1 件
    assert.deepEqual(s.deletions(), [keyName('old-key-1')])
    // 残存するのは今回発行した鍵のみ
    assert.deepEqual(s.remainingKeys(), [keyName(NEW_KEY_ID)])
    // 発行記録は現行鍵 ID のみへ更新される
    assert.equal(s.description(), `${MARKER}${NEW_KEY_ID}`)
    // GitHub Secret へ登録されたのは今回発行した鍵ファイル
    assert.match(s.secretPayload(), new RegExp(`"private_key_id":"${NEW_KEY_ID}"`))
    // 記録外鍵の警告は出ない
    assert.ok(!r.stdout.includes('記録に無い鍵が残っています'), r.stdout)
  } finally {
    s.cleanup()
  }
})

// --- シナリオ (b): 記録外の鍵は削除されない（Issue #3 の本体） ---------------

test('発行記録に無い鍵は、GitHub 側データがその鍵を指しても削除されない', () => {
  const s = setupScenario({
    recordedKeyIds: 'old-key-1',
    existingKeyIds: ['old-key-1', 'foreign-key-1'],
  })
  try {
    // FIREBASE_SA_KEY_IDS は上流 PR で P0 指摘された GitHub Actions 変数の
    // 名前。GitHub 側で改ざんされ得るデータが記録外の鍵を指す状況を環境変数
    // で再現し、削除根拠として一切参照されないことを実測する。
    const r = s.run({ FIREBASE_SA_KEY_IDS: 'old-key-1,foreign-key-1' })
    assert.equal(r.status, 0, `正常終了するはず: ${r.stdout}\n${r.stderr}`)
    // 削除は記録にある鍵だけ。記録外の foreign-key-1 は試行すらされない
    assert.deepEqual(s.deletions(), [keyName('old-key-1')])
    assert.deepEqual(s.deleteAttempts(), [keyName('old-key-1')])
    // 記録外の鍵は残存し、手動整理の警告が出る
    assert.deepEqual(s.remainingKeys().sort(), [keyName('foreign-key-1'), keyName(NEW_KEY_ID)].sort())
    assert.ok(r.stdout.includes('自動削除しません'), r.stdout)
    assert.ok(r.stdout.includes(keyName('foreign-key-1')), r.stdout)
  } finally {
    s.cleanup()
  }
})

// --- シナリオ (c): Secret 登録失敗時のロールバック ---------------------------

test('Secret 登録に失敗すると今回発行した鍵のみロールバック削除され、旧鍵は残る', () => {
  const s = setupScenario({
    recordedKeyIds: 'old-key-1',
    existingKeyIds: ['old-key-1'],
    failSecret: true,
  })
  try {
    const r = s.run()
    assert.notEqual(r.status, 0, '登録失敗時は異常終了するはず')
    // ロールバックで削除されるのは今回発行した鍵ちょうど 1 件
    assert.deepEqual(s.deletions(), [keyName(NEW_KEY_ID)])
    // 現行 Secret が指し得る旧鍵は失効しない（可用性の保全）
    assert.deepEqual(s.remainingKeys(), [keyName('old-key-1')])
    assert.ok(r.stderr.includes('ロールバック'), r.stderr)
    // 発行記録に旧鍵が残っており、再実行で世代交代を再試行できる
    assert.ok(s.description().includes('old-key-1'), s.description())
  } finally {
    s.cleanup()
  }
})

// --- シナリオ (d): 削除 API の失敗（403 等） --------------------------------

test('旧鍵の削除が失敗するとスクリプトは停止し、発行記録が保持される（次回再試行可能）', () => {
  const s = setupScenario({
    recordedKeyIds: 'old-key-1',
    existingKeyIds: ['old-key-1'],
    failDelete: true,
  })
  try {
    const r = s.run()
    assert.notEqual(r.status, 0, '削除失敗はエラー抑制せず停止するはず（set -e）')
    // 試行はされたが削除は成立していない
    assert.deepEqual(s.deleteAttempts(), [keyName('old-key-1')])
    assert.deepEqual(s.deletions(), [])
    // 発行記録は現行鍵のみへ更新されず、旧鍵 ID が残る（次回実行で再削除）
    assert.ok(s.description().includes('old-key-1'), s.description())
    assert.ok(s.remainingKeys().includes(keyName('old-key-1')))
  } finally {
    s.cleanup()
  }
})

// --- シナリオ (e): 鍵数上限時の事前削除 --------------------------------------

test('鍵数上限時も削除は記録にある鍵のみで、最後に記録した鍵は事前削除しない', () => {
  const unrecorded = Array.from({ length: 7 }, (_, i) => `unrec-${i + 1}`)
  const s = setupScenario({
    recordedKeyIds: 'rec-1,rec-2,rec-3',
    existingKeyIds: ['rec-1', 'rec-2', 'rec-3', ...unrecorded],
  })
  try {
    const r = s.run()
    assert.equal(r.status, 0, `正常終了するはず: ${r.stdout}\n${r.stderr}`)
    assert.ok(r.stdout.includes('上限'), r.stdout)
    // 事前削除は rec-1, rec-2（rec-3 = 最後に記録した鍵 = 現行 Secret が指す
    // 可能性が高い鍵は温存）。rec-3 は Secret 登録成功後の世代交代で削除される
    assert.deepEqual(s.deletions(), [keyName('rec-1'), keyName('rec-2'), keyName('rec-3')])
    // 記録外の 7 鍵はすべて残存する
    assert.deepEqual(
      s.remainingKeys().sort(),
      [...unrecorded.map(keyName), keyName(NEW_KEY_ID)].sort()
    )
    assert.equal(s.description(), `${MARKER}${NEW_KEY_ID}`)
  } finally {
    s.cleanup()
  }
})

// --- シナリオ (f): ROTATE_EXISTING_KEYS=false（opt-out） --------------------

test('ROTATE_EXISTING_KEYS=false では鍵の削除が一切行われない', () => {
  const s = setupScenario({
    recordedKeyIds: 'old-key-1',
    existingKeyIds: ['old-key-1'],
  })
  try {
    const r = s.run({ ROTATE_EXISTING_KEYS: 'false' })
    assert.equal(r.status, 0, `正常終了するはず: ${r.stdout}\n${r.stderr}`)
    assert.deepEqual(s.deleteAttempts(), [])
    assert.deepEqual(s.deletions(), [])
    assert.ok(r.stdout.includes('旧鍵の削除をスキップします'), r.stdout)
    assert.ok(s.remainingKeys().includes(keyName('old-key-1')))
  } finally {
    s.cleanup()
  }
})

// --- シナリオ (g): 上限だが記録にある鍵が無い場合は fail-closed --------------

test('鍵数上限で記録にある鍵が無い場合、何も削除せず停止する（fail-closed）', () => {
  const s = setupScenario({
    recordedKeyIds: '',
    existingKeyIds: Array.from({ length: 10 }, (_, i) => `unrec-${i + 1}`),
  })
  try {
    const r = s.run()
    assert.notEqual(r.status, 0, '記録に無い鍵しか無いなら自動削除せず停止するはず')
    assert.deepEqual(s.deleteAttempts(), [])
    assert.deepEqual(s.deletions(), [])
    assert.ok(r.stderr.includes('安全に削除できる鍵がありません'), r.stderr)
    // 10 鍵すべて残存する
    assert.equal(s.remainingKeys().length, 10)
  } finally {
    s.cleanup()
  }
})
