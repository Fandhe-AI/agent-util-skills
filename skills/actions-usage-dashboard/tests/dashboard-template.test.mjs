// dashboard-template.test.mjs — templates/dashboard/index.html の回帰テスト。
//
// ページ本体は claude.ai の Dashboard 型 Artifact 上でしか動かないため、jsdom は使わない。
// 代わりに <script id="usage-agg"> の集計関数だけを取り出して node:vm で評価し、合成 fixture
// （tests/fixtures/dashboard/）に対する値と、ページ全体の静的な制約（数値の直書き・危険な API・
// 外部参照・サイズ）を検証する。期待値は fixture から手計算した定数で、集計関数で再計算しない。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { runInNewContext } from 'node:vm'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const TESTS_DIR = dirname(fileURLToPath(import.meta.url))
const HTML = readFileSync(join(TESTS_DIR, '..', 'templates', 'dashboard', 'index.html'), 'utf8')
const billing = JSON.parse(readFileSync(join(TESTS_DIR, 'fixtures', 'dashboard', 'billing.json'), 'utf8')).rows
const billingDoc = JSON.parse(readFileSync(join(TESTS_DIR, 'fixtures', 'dashboard', 'billing.json'), 'utf8'))
const jobsDoc = JSON.parse(readFileSync(join(TESTS_DIR, 'fixtures', 'dashboard', 'jobs.json'), 'utf8'))
const jobs = jobsDoc.rows

const blocks = [...HTML.matchAll(/<script id="usage-agg">([\s\S]*?)<\/script>/g)]
assert.equal(blocks.length, 1, 'usage-agg ブロックが 1 件だけあること')
const sandbox = { window: {} }
runInNewContext(blocks[0][1], sandbox)
const U = sandbox.window.UsageAgg

// vm の別 realm の配列・オブジェクトを deepEqual で比較できるよう、JSON 往復で素の値にする。
const plain = (v) => JSON.parse(JSON.stringify(v))
const near = (a, b, msg) => assert.ok(Math.abs(a - b) < 1e-9, `${msg ?? ''} ${a} != ${b}`)

const facts = plain(U.facts(billing, jobs))
const totalsRows = plain(U.totals(facts))
const axisRows = plain(U.byAxis(facts))
const totalOf = (period, runner = '*', repo = '*') => totalsRows.find((r) => r.key === U.keyOf([period, runner, repo]))
const groupsOf = (axis, period = '*', runner = '*', repo = '*') =>
  axisRows.filter((r) => r.period === period && r.runner_type === runner && r.repo === repo && r.axis === axis)

const CLASS_FIELDS = ['paid', 'included', 'public_free', 'unknown', 'self_hosted'].map((c) => `${c}_minutes`)
const sumFields = (row) => CLASS_FIELDS.reduce((s, f) => s + row[f], 0)

test('T1 料金区分の和が総分数に一致する', () => {
  const all = totalOf('*')
  near(all.total_minutes, 665.5, '総分数')
  near(sumFields(all), all.total_minutes, '区分の和')
  near(all.paid_minutes, 40)
  near(all.included_minutes, 460.5)
  near(all.public_free_minutes, 60)
  near(all.unknown_minutes, 55)
  near(all.self_hosted_minutes, 50)
  near(all.quota_minutes, 882.5)
  near(all.gross_usd, 1.04)
  near(all.net_usd, 0.32)
  assert.equal(all.latest_day, '2026-10-03')
  const sep = totalOf('2026-09')
  near(sep.total_minutes, 550)
  near(sumFields(sep), sep.total_minutes)
  const oct = totalOf('2026-10')
  near(oct.total_minutes, 115.5)
  near(sumFields(oct), oct.total_minutes)
})

test('T2 軸ごとの合計が総分数に一致する（絞り込みの組み合わせでも）', () => {
  for (const axis of U.AXES) {
    const sum = groupsOf(axis).reduce((s, r) => s + r.minutes, 0)
    near(sum, 665.5, axis)
  }
  const cases = [
    ['*', 'macos', '*', 88],
    ['*', '*', 'repo-a', 167.5],
    ['2026-10', 'ubuntu', 'repo-a', 37.5]
  ]
  for (const [period, runner, repo, expected] of cases) {
    for (const axis of U.AXES) {
      const sum = groupsOf(axis, period, runner, repo).reduce((s, r) => s + r.minutes, 0)
      near(sum, expected, `${axis} ${period} ${runner} ${repo}`)
    }
    near(totalOf(period, runner, repo).total_minutes, expected)
  }
  const runner = Object.fromEntries(groupsOf('runner_type').map((r) => [r.group, r.minutes]))
  near(runner.ubuntu, 367.5)
  near(runner['self-hosted'], 50)
  near(runner.other, 40)
  const vis = Object.fromEntries(groupsOf('visibility').map((r) => [r.group, r.minutes]))
  near(vis.private, 465.5)
  near(vis.public, 75)
  near(vis.internal, 70)
  near(vis.unknown, 55)
})

test('T3 上位 7 件 + その他', () => {
  const m = U.daily(facts, { period: 'all', group_by: 'repo', include_free: true })
  assert.deepEqual(plain(m.groups.map((g) => g.id)), ['repo-a', 'repo-b', 'repo-c', 'repo-e', 'repo-d', 'repo-f', 'repo-g', U.OTHER])
  near(m.groups[7].total, 80)
  near(m.groups.reduce((s, g) => s + g.total, 0), 665.5)
  const cls = U.daily(facts, { period: 'all', group_by: 'charge_class', include_free: true })
  assert.deepEqual(plain(cls.groups.map((g) => g.id)), ['paid', 'included', 'public_free', 'unknown', 'self_hosted'])
  const ties = plain(U.topGroups([{ group: 'b', value: 1 }, { group: 'a', value: 1 }, { group: 'c', value: 1 }], 2))
  assert.deepEqual(ties, [{ group: 'a', value: 1 }, { group: 'b', value: 1 }, { group: U.OTHER, value: 1 }])
  assert.equal(U.topGroups([{ group: 'a', value: 1 }], 7).length, 1)
})

test('T4 無料枠を除くと quota_minutes の値になる', () => {
  const m = U.daily(facts, { period: 'all', group_by: 'charge_class', include_free: false })
  const total = (id) => m.groups.find((g) => g.id === id).total
  near(total('included'), 882.5)
  near(total('paid'), 0)
  near(total('self_hosted'), 0)
  assert.equal(m.metric, 'quota_minutes')
  const oct = U.daily(facts, { period: 'latest', group_by: 'charge_class', include_free: true })
  assert.equal(oct.period, '2026-10')
  assert.equal(oct.days[0], '2026-10-01')
  assert.equal(oct.days.length, 3)
})

test('T5 dataset が 1 つでも 2 つでも動く', () => {
  const one = plain(U.facts(billing, []))
  assert.equal(one.filter((f) => f.charge_class === 'self_hosted').length, 0)
  assert.equal(plain(U.facts(billing, undefined)).length, one.length)
  const two = plain(U.facts(billing, jobs))
  near(two.filter((f) => f.charge_class === 'self_hosted').reduce((s, f) => s + f.minutes, 0), 50)
  assert.doesNotThrow(() => U.totals(U.facts([], [])))
})

test('T6 calc に渡す関数は自己完結している', () => {
  const rebuilt = (f) => runInNewContext('(' + f.toString() + ')', {})
  assert.deepEqual(plain(rebuilt(U.facts)(billing, jobs)), facts)
  assert.deepEqual(plain(rebuilt(U.totals)(facts)), totalsRows)
  assert.deepEqual(plain(rebuilt(U.byAxis)(facts)), axisRows)
})

test('T7 入力の頑健性', () => {
  assert.equal(facts.reduce((s, f) => s + f.invalid, 0), 2)
  const weird = facts.find((f) => f.repo === 'repo-b' && f.day === '2026-10-03')
  assert.equal(weird.charge_class, 'unknown')
  assert.equal(weird.minutes, 0)
  assert.equal(facts.find((f) => f.minutes === 12.5).quota_minutes, 12.5)
  assert.equal(U.resolvePeriod('latest', facts), '2026-10')
  assert.equal(U.resolvePeriod('all', facts), '*')
  assert.equal(U.resolvePeriod('2026-09', facts), '2026-09')
  assert.equal(U.keyOf(['a&b', 'c=d', '100%', 'x|y']), 'a%26b|c%3Dd|100%25|x%7Cy')
})

test('T8 数値のリテラル配列がない', () => {
  assert.doesNotMatch(HTML, /\[\s*-?\d+(?:\.\d+)?\s*(?:,\s*-?\d+(?:\.\d+)?\s*)+\]/)
})

test('T9 禁止パターンがない', () => {
  const forbidden = [
    /innerHTML/, /\.html\(/, /insertAdjacentHTML/, /document\.write/,
    /<[^>]*\son[a-z]+\s*=/i, /<form[\s>]/i, /<iframe/i, /<meta[\s>]/i, /<base[\s>]/i,
    /\s(?:src|href)\s*=\s*["']?(?:https?:)?\/\//i,
    /localStorage/, /sessionStorage/, /document\.cookie/,
    /\bfetch\(/, /XMLHttpRequest/, /\bimport\b/, /\bexport\b/, /console\./
  ]
  for (const re of forbidden) assert.doesNotMatch(HTML, re, String(re))
  const markup = HTML.replace(/<script[\s\S]*?<\/script>/g, '').replace(/<style[\s\S]*?<\/style>/g, '')
  assert.doesNotMatch(markup, />\s*[\d.,$]+\s*</, 'マークアップに数値の直書きがある')
})

test('T10 サイズと契約', () => {
  assert.ok(Buffer.byteLength(HTML) < 256 * 1024, '256 KB 未満')
  const ids = new Set()
  for (const m of HTML.matchAll(/data-source="([^"]+)"/g)) m[1].split(/\s+/).forEach((i) => ids.add(i))
  for (const id of ids) assert.ok(['billing', 'jobs', 'warnings', 'facts', 'totals', 'by_axis'].includes(id), id)
  assert.equal((HTML.match(/dash\.calc\(/g) || []).length, 3)
})

test('T11 fetch 出力のオブジェクトをそのまま渡しても全行が集計される', () => {
  const fromDocs = plain(U.facts(billingDoc, jobsDoc))
  assert.equal(fromDocs.length, 19)
  assert.deepEqual(fromDocs, facts)
})

test('T12 取得期間外は 0 ではなく none、一部は partial になる', () => {
  // fixture: billing は 2026-09〜2026-10、jobs は 2026-10-01〜2026-10-03 のみ。
  assert.equal(U.coverage(billingDoc, '2026-09', '2026-10-20'), 'full')
  assert.equal(U.coverage(jobsDoc, '2026-09', '2026-10-20'), 'none')
  assert.equal(U.coverage(jobsDoc, '2026-10', '2026-10-03'), 'full')
  assert.equal(U.coverage(jobsDoc, '2026-10', '2026-10-20'), 'partial')
  assert.equal(U.coverage(billingDoc, '2026-08', '2026-10-20'), 'none')
  assert.equal(U.coverage(undefined, '2026-10', '2026-10-20'), 'none')
  assert.equal(U.coverage(jobs, '2026-10', '2026-10-20'), 'unknown')
  assert.equal(U.coverage(jobsDoc, '*', '2026-10-20'), 'full')
  // 全期間は両取得元の期間の和集合で照合する（jobs は 10 月のみなので 9 月分が欠ける）。
  assert.equal(U.coverage(jobsDoc, '*', '2026-10-20', billingDoc), 'partial')
  assert.equal(U.coverage(billingDoc, '*', '2026-10-20', jobsDoc), 'full')
})

test('T13 ページは範囲外のセルフホスト分を不明表示にする', () => {
  assert.match(HTML, /U\.coverage\(/)
  assert.match(HTML, /cov\[k\] === 'none'/)
  assert.match(HTML, /id="aud-coverage"/)
})

test('ページ側の配線: unknown で残量不明・ゼロ補完・jobs 未接続の合計', () => {
  assert.match(HTML, /cov\.billing === 'unknown'/)
  assert.match(HTML, /cov\.jobsConnected/)
  assert.match(HTML, /取得済み期間に該当行がないとき/)
})

test('P1 利用ゼロの月も取得期間から選択でき、latest は取得済みの最新月に解決される', () => {
  const src = { since: '2026-09', until: '2026-11', rows: [] }
  assert.deepEqual(plain(U.months(facts, [src])), ['2026-09', '2026-10', '2026-11'])
  assert.equal(U.resolvePeriod('latest', facts, [src]), '2026-11')
  assert.equal(U.resolvePeriod('latest', facts), '2026-10')
  assert.equal(U.coverage(src, '2026-11', '2026-11-05'), 'full')
})

test('P1 区分不明を含む群は枠消費が下限値として扱われる', () => {
  const unk = axisRows.filter((r) => r.axis === 'charge_class' && r.group === 'unknown' && r.period === '*' && r.repo === '*' && r.runner_type === '*')
  assert.equal(unk.length, 1)
  near(unk[0].unknown_minutes, 55)
  const m = plain(U.daily(facts, { period: '*', group_by: 'charge_class', include_free: false }))
  assert.equal(m.lowerBound, true)
  assert.equal(m.groups.find((g) => g.id === 'unknown').lower, true)
  assert.equal(plain(U.daily(facts, { period: '*', group_by: 'charge_class', include_free: true })).lowerBound, false)
})

test('全期間の照合は当日で上限を切る（月末まで広がった billing と今日までの jobs）', () => {
  const b = { since: '2026-10', until: '2026-10', rows: [] }
  const j = { since: '2026-10-01', until: '2026-10-20', rows: [] }
  assert.equal(U.coverage(j, '*', '2026-10-20', b), 'full')
  assert.equal(U.coverage(b, '*', '2026-10-20', j), 'full')
})

test('paintTotals のゼロ補完は準備完了時だけ、項目の取得元（セルフホストは jobs）の取得状況で行う', () => {
  assert.match(HTML, /const zeroOk = ready && src\.every\(\(k\) => cov\[k\] !== 'none' && cov\[k\] !== 'unknown'\)/)
  assert.match(HTML, /field === 'self_hosted_minutes' \? \['jobs'\]/)
})

test('paintWarnings は billing / jobs の JSON 内 warnings も引き継ぐ', () => {
  assert.match(HTML, /\['billing', 'jobs'\]\.forEach\(\(k\) => \{[\s\S]*?\.warnings/)
})

// paintTotals だけを取り出し、DOM とフォーマッタを最小のスタブに差し替えて残量の文言を確かめる。
const paintRemaining = (rows, sel, period, cov, ready) => {
  const m = HTML.match(/function paintTotals\([\s\S]*?\n\}\n/)
  assert.ok(m, 'paintTotals が見つかること')
  const out = {}
  const el = { textContent: '', setAttribute() {}, getAttribute: () => '', classList: { toggle() {} } }
  const box = {
    U,
    page: { querySelectorAll: () => [] },
    $: (id) => (id === '#aud-remaining' ? out : el),
    document: { getElementById: () => null },
    fmtMin: (v) => String(v),
    fmtUsd: (v) => '$' + v
  }
  runInNewContext(`${m[0]}\npaintTotals(rows, sel, period, cov, ready)`, { ...box, rows, sel, period, cov, ready })
  return out.textContent
}
const covFull = { billing: 'full', jobs: 'full', jobsConnected: true }
const selOf = (o = {}) => ({ runner_type: '', repo: '', plan: 3000, ...o })

test('利用ゼロの単月は取得済み・プラン指定・絞り込みなしならプラン全量を残量にする', () => {
  assert.equal(paintRemaining([], selOf(), '2026-11', covFull, true), '3000 分')
  // 行がある月は従来どおり枠消費を引く（区分不明がなければ 3000 - 500）。
  const row = { key: U.keyOf(['2026-10', '*', '*']), quota_minutes: 500, unknown_minutes: 0, paid_minutes: 0 }
  assert.equal(paintRemaining([row], selOf(), '2026-10', covFull, true), '2500 分')
})

test('利用ゼロの月でも絞り込み・全期間・billing 未取得・準備未完了・プラン未指定では全量を出さない', () => {
  assert.equal(paintRemaining([], selOf({ repo: 'a/b' }), '2026-11', covFull, true), '月全体を選んだときだけ表示')
  assert.equal(paintRemaining([], selOf({ runner_type: 'linux' }), '2026-11', covFull, true), '月全体を選んだときだけ表示')
  assert.equal(paintRemaining([], selOf(), '*', covFull, true), '月全体を選んだときだけ表示')
  assert.match(paintRemaining([], selOf(), '2026-11', { ...covFull, billing: 'none' }, true), /^不明/)
  assert.match(paintRemaining([], selOf(), '2026-11', { ...covFull, billing: 'unknown' }, true), /^不明/)
  assert.match(paintRemaining([], selOf(), '2026-11', { ...covFull, billing: 'partial' }, true), /^不明/)
  assert.equal(paintRemaining([], selOf(), '2026-11', covFull, false), '不明（集計を取得できていない）')
  assert.equal(paintRemaining([], selOf({ plan: null }), '2026-11', covFull, true), 'プラン未指定のため不明')
})
