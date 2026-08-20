// perm-check-403.test.mjs — Issue #4 P1 の回帰テスト（静的整合 + 挙動）。
//
// ## 検証する不変条件
//
// 上流 PR（agent-cli-skills#63）の codex レビューで P1 指摘された論点:
// `addFirebase` が 403 を返した際、`testIamPermissions` が SKILL.md 記載の
// 必要 4 権限のうち 1 つ（`required_perm=`、単数）しか検査しておらず、他
// 3 権限の不足を見落とし得た。本リポジトリの bootstrap-firebase.sh は
// `required_perms=`（複数）へ改め、4 権限すべてを検査して不足を列挙する。
//
// 本テストが固定化する不変条件は次の 2 点:
//
//   1. （静的整合）script の required_perms と SKILL.md 記載の必要権限
//      リストが過不足なく一致する。旧実装の単数変数名（`required_perm=`）
//      が再導入されていないこと。
//   2. （挙動）403 時、testIamPermissions の実測結果に応じてメッセージが
//      正しく出し分けられる（不足権限をすべて列挙する／全権限あれば
//      規約未承諾の案内に倒す／確認コマンド自体の失敗は原因不明として扱う）。
//
// ## 検証方式（挙動テストが shim の未処理コマンドに誤って通過しないために）
//
// gcloud shim は key-deletion-authority.test.mjs と同じ設計方針（未知の
// サブコマンドは fail-closed）を踏襲する。ただし script は
// `gcloud projects test-iam-permissions ... || echo "__CHECK_FAILED__"`
// と `||` でチェック失敗を握り潰す作りのため、shim がこのサブコマンドを
// 処理し忘れても script 側は「確認コマンド自体が失敗した」パスへ静かに
// 落ちて停止し、404/1 終了という結果だけ見ると一見テストが通っているように
// 見えてしまう（誤検知）。これを防ぐため、各挙動テストは
//   (a) gcloud-calls.log に `test-iam-permissions ... --format=json` の
//       呼び出しが実際に記録されていること
//   (b) 3 分岐（不足あり / 全権限あり / チェック失敗）それぞれに固有の
//       文言が「期待した 1 つだけ」出力されていること
// の両方を assert する。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import {
  chmodSync,
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
const SKILL_MD_PATH = join(SKILL_DIR, 'SKILL.md')

const PROJECT_ID = 'test-proj'
const SITE_ID = 'test-site'
const GITHUB_REPO = 'owner/repo'

// script L267 と同じ 4 権限（意図的にここへも書き出し、script 側が変わったら
// このテストのシナリオ (a) が静的整合チェックとして検知する）
const REQUIRED_PERMS = [
  'firebase.projects.update',
  'resourcemanager.projects.get',
  'serviceusage.services.enable',
  'serviceusage.services.get',
]

// --- シナリオ (a): 静的整合 -------------------------------------------------
// スクリプト本文・SKILL.md それぞれから権限リストを機械抽出し、両者と
// REQUIRED_PERMS（このテストの正）が過不足なく一致することを検証する。

test('script の required_perms と SKILL.md 記載の必要権限が過不足なく一致する', () => {
  const scriptBody = readFileSync(BOOTSTRAP_SCRIPT_PATH, 'utf8')

  // 旧実装（Issue #4 が指摘した P1 の原因）は単数形 `required_perm=` を使い、
  // 1 権限しか保持できなかった。再導入されていないことを先に確認する。
  assert.ok(
    !/\brequired_perm=/.test(scriptBody),
    '退行: 単数形 required_perm=（1 権限しか検査しない旧実装）が再導入されている'
  )

  const scriptMatch = scriptBody.match(/required_perms="([^"]+)"/)
  assert.ok(scriptMatch, 'script から required_perms=\"...\" を抽出できなかった')
  const scriptPerms = scriptMatch[1].trim().split(/\s+/).sort()

  const skillMd = readFileSync(SKILL_MD_PATH, 'utf8')
  const skillMdMatch = skillMd.match(/"permissions":\[([^\]]+)\]/)
  assert.ok(skillMdMatch, 'SKILL.md から permissions リストを抽出できなかった')
  const skillMdPerms = skillMdMatch[1]
    .split(',')
    .map((s) => s.trim().replace(/^"|"$/g, ''))
    .sort()

  assert.deepEqual(scriptPerms, [...REQUIRED_PERMS].sort(), 'script の required_perms がこのテストの正と不一致')
  assert.deepEqual(skillMdPerms, [...REQUIRED_PERMS].sort(), 'SKILL.md の permissions リストがこのテストの正と不一致')
  assert.deepEqual(scriptPerms, skillMdPerms, 'script と SKILL.md で権限リストが食い違っている')
})

// --- shim 本体 ---------------------------------------------------------------
// addFirebase を 403 で応答させ、(4)/(5) 以降（SA・鍵管理）へは到達しない
// 経路のみを対象にするため、gcloud shim は鍵管理サブコマンドを一切扱わない
// （扱う必要があるのに無ければ fail-closed で即座にテストが落ちる）。

function gcloudShim(permCheckBehavior) {
  // permCheckBehavior:
  //   'missing-2' -> 4 権限中 2 権限のみ JSON へ含める（不足あり）
  //   'all'       -> 4 権限すべて含める（規約未承諾の可能性）
  //   'fail'      -> test-iam-permissions 自体が非ゼロ終了（確認コマンド失敗）
  const grantedPerms =
    permCheckBehavior === 'missing-2' ? REQUIRED_PERMS.slice(0, 2) : REQUIRED_PERMS
  const grantedJson = JSON.stringify({ permissions: grantedPerms })
  const failBranch =
    permCheckBehavior === 'fail'
      ? 'echo "ERROR: (gcloud.projects.test-iam-permissions) PERMISSION_DENIED" >&2; exit 1'
      : `cat <<'JSON'\n${grantedJson}\nJSON`
  return `#!/usr/bin/env bash
# gcloud shim（P1 専用）— addFirebase 403 経路のみを対象にするため、鍵管理
# サブコマンドは意図的に持たない（未知のサブコマンドは fail-closed で失敗）。
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
  "projects test-iam-permissions "*)
    ${failBranch}
    ;;
  *)
    echo "gcloud shim: unhandled args: \${args}" >&2
    exit 1
    ;;
esac
`
}

const GH_SHIM = `#!/usr/bin/env bash
# gh shim — 403 経路は gh secret set まで到達しないため auth/repo view のみ。
set -euo pipefail
state="\${SHIM_STATE_DIR:?}"
printf 'gh %s\\n' "$*" >> "\${state}/gh-calls.log"
case "$*" in
  "auth status"*) : ;;
  "repo view "*) echo "ADMIN" ;;
  *) echo "gh shim: unhandled args: $*" >&2; exit 1 ;;
esac
`

const CURL_SHIM = `#!/usr/bin/env bash
# curl shim — addFirebase を常に 403 で応答させる（P1 の再現対象そのもの）。
set -euo pipefail
state="\${SHIM_STATE_DIR:?}"
printf 'curl %s\\n' "$*" >> "\${state}/curl-calls.log"
case "$*" in
  *":addFirebase"*) printf '{"error":{"message":"The caller does not have permission"}}\\nHTTP_STATUS:403' ;;
  *) echo "curl shim: unhandled args: $*" >&2; exit 1 ;;
esac
`

const NOOP_SHIM = '#!/bin/sh\nexit 0\n'

function setupScenario(permCheckBehavior) {
  const root = mkdtempSync(join(tmpdir(), 'perm-check-403-'))
  const bin = join(root, 'bin')
  const state = join(root, 'state')
  const scripts = join(root, 'scripts')
  const work = join(root, 'work')
  for (const dir of [bin, state, scripts, work]) mkdirSync(dir)

  writeFileSync(join(scripts, 'bootstrap-firebase.sh'), readFileSync(BOOTSTRAP_SCRIPT_PATH))
  chmodSync(join(scripts, 'bootstrap-firebase.sh'), 0o755)

  for (const [name, body] of [
    ['gcloud', gcloudShim(permCheckBehavior)],
    ['gh', GH_SHIM],
    ['curl', CURL_SHIM],
    ['node', NOOP_SHIM],
    ['npx', NOOP_SHIM],
  ]) {
    const path = join(bin, name)
    writeFileSync(path, body)
    chmodSync(path, 0o755)
  }

  const env = { ...process.env }
  for (const name of ['ROTATE_EXISTING_KEYS', 'ADOPT_EXISTING_SA', 'ALLOW_BLAZE']) delete env[name]
  Object.assign(env, {
    PATH: `${bin}:${process.env.PATH}`,
    PROJECT_ID,
    SITE_ID,
    GITHUB_REPO,
    SHIM_STATE_DIR: state,
    TMPDIR: work,
  })

  const result = spawnSync('bash', [join(scripts, 'bootstrap-firebase.sh')], {
    env,
    encoding: 'utf8',
    timeout: 60_000,
  })
  assert.equal(result.error, undefined, `スクリプトの起動に失敗: ${result.error}`)

  const gcloudCallsPath = join(state, 'gcloud-calls.log')
  const gcloudCalls = readFileSync(gcloudCallsPath, 'utf8')

  rmSync(root, { recursive: true, force: true })
  return { result, gcloudCalls }
}

// 3 分岐を判別する固有文言（script L267-330）
const MISSING_MARKER = '必要 4 権限のうち以下が不足しています'
const ALL_PRESENT_MARKER = 'はすべて確認できました'
const CHECK_FAILED_MARKER = '確認コマンド自体が'
const ALL_MARKERS = [MISSING_MARKER, ALL_PRESENT_MARKER, CHECK_FAILED_MARKER]

function assertOnlyMarkerPresent(output, expectedMarker) {
  for (const marker of ALL_MARKERS) {
    if (marker === expectedMarker) {
      assert.ok(output.includes(marker), `期待した文言が出力に無い: ${marker}\n---\n${output}`)
    } else {
      assert.ok(!output.includes(marker), `別分岐の文言が混入している: ${marker}\n---\n${output}`)
    }
  }
}

// --- シナリオ (b): 4 権限中 2 権限のみ許可 → 不足 2 権限を列挙 -------------

test('403 時、testIamPermissions が4権限中2権限のみ返す場合は不足2権限を両方列挙する', () => {
  const { result, gcloudCalls } = setupScenario('missing-2')
  assert.notEqual(result.status, 0, '権限不足時は非ゼロ終了するはず')
  // 誤検知防止（advisor 指摘）: 実際に test-iam-permissions が --format=json
  // 付きで呼ばれたことを確認してから分岐の文言を判定する
  assert.match(gcloudCalls, /projects test-iam-permissions .*--format=json/)
  assertOnlyMarkerPresent(result.stderr, MISSING_MARKER)
  // 不足しているのは後半 2 権限（missing-2 は先頭 2 権限のみを許可として返す）
  for (const perm of REQUIRED_PERMS.slice(2)) {
    assert.ok(result.stderr.includes(`  - ${perm}`), `不足権限として列挙されていない: ${perm}\n${result.stderr}`)
  }
  // 許可済みの権限は不足リストに出ない
  for (const perm of REQUIRED_PERMS.slice(0, 2)) {
    assert.ok(!result.stderr.includes(`  - ${perm}`), `許可済みの権限が誤って不足扱いされている: ${perm}\n${result.stderr}`)
  }
})

// --- シナリオ (c): 4 権限すべて許可 → 規約未承諾の可能性を案内 -------------

test('403 時、testIamPermissions が4権限すべて返す場合は規約未承諾の可能性を案内して停止する（権限不足と断定しない）', () => {
  const { result, gcloudCalls } = setupScenario('all')
  assert.notEqual(result.status, 0, '4権限すべてあっても403自体はエラーとして停止するはず')
  assert.match(gcloudCalls, /projects test-iam-permissions .*--format=json/)
  assertOnlyMarkerPresent(result.stderr, ALL_PRESENT_MARKER)
  assert.ok(result.stderr.includes('console.firebase.google.com'), result.stderr)
})

// --- 確認コマンド自体が失敗した場合の分岐（挙動の網羅性を補強） -------------

test('403 時、testIamPermissions の確認コマンド自体が失敗した場合は原因不明として手動確認を案内する', () => {
  const { result, gcloudCalls } = setupScenario('fail')
  assert.notEqual(result.status, 0)
  assert.match(gcloudCalls, /projects test-iam-permissions .*--format=json/)
  assertOnlyMarkerPresent(result.stderr, CHECK_FAILED_MARKER)
})
