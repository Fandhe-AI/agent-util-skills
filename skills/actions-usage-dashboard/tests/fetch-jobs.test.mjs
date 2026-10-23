// fetch-jobs.test.mjs — fetch_jobs.py の回帰テスト（test_fetch_jobs.py）を
// `node --test` の入口から実行するブリッジ。fetch-billing.test.mjs と同じ型で、
// CI・ローカルの双方で Python 側のテストが実行されない状態を避ける。
// 失敗時は unittest の出力をそのまま assert メッセージに載せて原因を追えるようにする。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const TESTS_DIR = dirname(fileURLToPath(import.meta.url))

test('python3 が利用できる', () => {
  const r = spawnSync('python3', ['--version'], { encoding: 'utf8' })
  assert.equal(r.status, 0, 'python3 が必要（actions-usage-dashboard の前提条件）')
})

test('test_fetch_jobs.py（判定・集約・キャッシュ・レート制限再開・gh スタブ E2E の unittest）が全件成功する', () => {
  const r = spawnSync('python3', ['-I', '-B', join(TESTS_DIR, 'test_fetch_jobs.py')], { encoding: 'utf8' })
  assert.equal(r.status, 0, `unittest 失敗\n${r.stdout}\n${r.stderr}`)
  assert.match(r.stderr, /\nOK\n?$/, 'unittest が OK で終了していない')
})
