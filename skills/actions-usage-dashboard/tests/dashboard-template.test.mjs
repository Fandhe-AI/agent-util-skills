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
  for (const id of ids) assert.ok(['billing', 'jobs', 'billing_meta', 'jobs_meta', 'facts', 'totals', 'by_axis'].includes(id), id)
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
  assert.match(HTML, /coverage\(docs\.billing, period, today, docs\.jobs\)/)
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

test('「その他」の識別子は __other__ という実名リポジトリと衝突しない', () => {
  const mk = (repo, minutes) => ({ month: '2026-10', day: '2026-10-01', repo, runner_type: 'ubuntu', visibility: 'private', charge_class: 'included', minutes, quota_minutes: minutes })
  const idsOf = (rows) => plain(U.daily(rows, { period: '2026-10', group_by: 'repo', include_free: true }))
  // 7 件以下: 実名のまま系列になり、その他は現れない。
  const few = idsOf([mk('__other__', 10), mk('a', 5), mk('b', 3)])
  assert.deepEqual(few.groups.map((g) => g.id), ['__other__', 'a', 'b'])
  assert.ok(!few.groups.some((g) => g.id === U.OTHER))
  // 上位に入る場合: 圏外 2 件（3 + 2 分）は実名の __other__（10 分）と別系列になる。
  const rows = [mk('__other__', 10), ...[9, 8, 7, 6, 5, 4, 3, 2].map((m, i) => mk(`r${i}`, m))]
  const many = idsOf(rows)
  const total = (id) => many.groups.find((g) => g.id === id).total
  assert.equal(many.groups.length, 8)
  assert.equal(total('__other__'), 10)
  assert.equal(total(U.OTHER), 5)
  assert.notEqual(U.OTHER, '__other__')
  assert.doesNotMatch(U.OTHER, /^[A-Za-z0-9._-]+$/)
})

test('facts は年が 2000〜2099 の外の日付を (invalid) に寄せる', () => {
  const mk = (day) => ({ day, minutes: 1, charge_class: 'paid', repo: 'a', runner_type: 'ubuntu', visibility: 'private' })
  const out = plain(U.facts([mk('0001-01-01'), mk('9999-12-31'), mk('2026-10-01')], []))
  assert.equal(out[0].day, '(invalid)')
  assert.equal(out[1].day, '(invalid)')
  assert.ok(out[0].invalid >= 1 && out[1].invalid >= 1)
  assert.equal(out[2].day, '2026-10-01')
  assert.equal(out[2].invalid, 0)
})

test('daily はグラフの日数上限を超えるとループせず tooLong を返す', () => {
  const mk = (day) => ({ month: day.slice(0, 7), day, repo: 'a', runner_type: 'ubuntu', visibility: 'private', charge_class: 'paid', minutes: 1, quota_minutes: 0 })
  const sel = { period: 'all', group_by: 'charge_class', include_free: true }
  const over = plain(U.daily([mk('2000-01-01'), mk('2099-12-31')], sel))
  assert.equal(over.days.length, 0)
  assert.ok(over.tooLong > U.MAX_CHART_DAYS)
  const start = Date.parse('2026-01-01T00:00:00Z')
  const dayAt = (n) => new Date(start + n * 86400000).toISOString().slice(0, 10)
  const edge = plain(U.daily([mk(dayAt(0)), mk(dayAt(U.MAX_CHART_DAYS - 1))], sel))
  assert.equal(edge.days.length, U.MAX_CHART_DAYS)
  assert.equal(edge.tooLong, 0)
  const past = plain(U.daily([mk(dayAt(0)), mk(dayAt(U.MAX_CHART_DAYS))], sel))
  assert.equal(past.days.length, 0)
  assert.equal(past.tooLong, U.MAX_CHART_DAYS + 1)
})

test('drawChart は tooLong を参照して警告を出す', () => {
  const html = readFileSync(join(dirname(fileURLToPath(import.meta.url)), '..', 'templates', 'dashboard', 'index.html'), 'utf8')
  const body = html.slice(html.indexOf('function drawChart('))
  assert.match(body.slice(0, 600), /model\.tooLong/)
  assert.match(html, /グラフの上限/)
})

test('isEstimate は estimate がオブジェクトの jobs 文書だけを真にする', () => {
  const est = { method: 'sample_mean_per_workflow', sample_runs: 3 }
  assert.equal(U.isEstimate({ ...jobsDoc, estimate: est }), true)
  assert.equal(U.isEstimate(jobsDoc), false)
  for (const bad of [[], null, 'x', 3, true]) {
    assert.equal(U.isEstimate({ ...jobsDoc, estimate: bad }), false, JSON.stringify(bad))
  }
  assert.equal(U.isEstimate(null), false)
  assert.equal(U.isEstimate(undefined), false)
  assert.equal(U.isEstimate(jobs), false)
})

test('ページ側の配線: 推定のときだけセルフホスト表示に「（推定）」を出す', () => {
  assert.match(HTML, /isEstimate\(docs\.jobs\)/)
  const marks = [...HTML.matchAll(/<span class="aud-est"([^>]*)>（推定）<\/span>/g)]
  assert.equal(marks.length, 2, 'リード文とタイルの 2 か所')
  for (const m of marks) assert.match(m[1], /\bhidden\b/)
  for (const id of ['aud-estimate', 'aud-sh-note', 'aud-total-note']) {
    assert.match(HTML, new RegExp(`id="${id}"`))
  }
  assert.match(HTML, /<span id="aud-estimate" hidden>/)
})

// ---- ランタイムが実際に渡す形（行配列だけ + {source, kind, value} の meta 行）の入力 ----
const ok = (data) => ({ status: 'ok', data })
const mrow = (source, kind, value) => ({ source, kind, value })
const billingMeta = [mrow('billing', 'since', '2026-09'), mrow('billing', 'until', '2026-10')]
const jobsMeta = [
  mrow('jobs', 'since', '2026-10-01'), mrow('jobs', 'until', '2026-10-03'),
  mrow('jobs', 'warning', '1,000 件上限で欠けた可能性: 2026-10-01..2026-10-03'),
  mrow('jobs', 'warning', '不正な job を含むため run をスキップ: 42'),
  mrow('jobs', 'estimate', 'true')
]
const runtimeInput = (over = {}) => ({
  billing: ok(billing), jobs: ok(jobs), billing_meta: ok(billingMeta), jobs_meta: ok(jobsMeta), ...over
})

test('再現: 行配列だけを渡すと従来の関数では取得期間が読めず推定も偽になる', () => {
  assert.equal(U.coverage(jobs, '2026-10', '2026-10-03'), 'unknown')
  assert.equal(U.isEstimate(jobs), false)
})

test('annotate: meta 行から取得期間・推定・警告が読める', () => {
  const a = plain(U.annotate(runtimeInput(), '2026-09', '2026-10-20'))
  assert.equal(a.cov.billing, 'full')
  assert.equal(a.cov.jobs, 'none')
  assert.equal(a.estimate, false, '選択期間にセルフホストが無いときは推定と書かない')
  const b = plain(U.annotate(runtimeInput(), '2026-10', '2026-10-03'))
  assert.equal(b.cov.jobs, 'full')
  assert.equal(b.estimate, true)
  assert.deepEqual(b.warnings, [
    'jobs: 1,000 件上限で欠けた可能性: 2026-10-01..2026-10-03',
    'jobs: 不正な job を含むため run をスキップ: 42'
  ])
  assert.ok(!b.notes.some((n) => n.includes('取得期間（since / until）が読めず')))
  assert.equal(plain(U.annotate(runtimeInput(), '2026-10', '2026-10-20')).cov.jobs, 'partial')
})

test('annotate: meta が missing / error なら unknown の注記を出す（黙って full にしない）', () => {
  for (const status of ['missing', 'error']) {
    const a = plain(U.annotate(runtimeInput({ jobs_meta: { status, data: undefined } }), '2026-10', '2026-10-03'))
    assert.equal(a.cov.jobs, 'unknown', status)
    assert.equal(a.estimate, false)
    assert.deepEqual(a.warnings, [])
    assert.ok(a.notes.some((n) => n.includes('取得期間（since / until）が読めず')), status)
  }
  const none = plain(U.annotate(runtimeInput({ billing_meta: ok([]) }), '2026-10', '2026-10-03'))
  assert.equal(none.cov.billing, 'unknown')
})

test('annotate: meta が読み込み中の間は unknown の注記を出さない', () => {
  const a = plain(U.annotate(runtimeInput({ jobs_meta: { status: 'loading' }, billing_meta: { status: 'refreshing' } }), '2026-10', '2026-10-03'))
  assert.equal(a.cov.jobs, 'unknown')
  assert.deepEqual(a.notes, [])
})

test('annotate: jobs 未接続・従来の文書形の入力', () => {
  const a = plain(U.annotate({ billing: ok(billing), jobs: { status: 'missing' }, billing_meta: ok(billingMeta), jobs_meta: { status: 'missing' } }, '2026-10', '2026-10-03'))
  assert.equal(a.cov.jobsConnected, false)
  assert.equal(a.cov.jobs, 'none')
  assert.deepEqual(a.notes, [])
  const legacy = plain(U.annotate({ billing: ok(billingDoc), jobs: ok({ ...jobsDoc, warnings: ['w1'], estimate: { n: 1 } }) }, '2026-10', '2026-10-03'))
  assert.equal(legacy.cov.jobs, 'full')
  assert.equal(legacy.estimate, true)
  assert.deepEqual(legacy.warnings, ['jobs: w1'])
})

test('withMeta: 不正な meta 行を無視し、警告は 20 件で切る', () => {
  const rows = [{ x: 1 }]
  const w = plain(U.withMeta(rows, [
    null, 'x', mrow('billing', 'since', '2026-01'), mrow('jobs', 'since', 5), { source: 'jobs', kind: 7, value: 'v' },
    mrow('jobs', 'since', '2026-10-01'), mrow('jobs', 'since', '2026-10-02'), mrow('jobs', 'until', '2026-10-03'),
    mrow('jobs', 'bogus', 'v'),
    ...Array.from({ length: 25 }, (_, i) => mrow('jobs', 'warning', 'w' + i))
  ], 'jobs'))
  assert.equal(w.since, '2026-10-01')
  assert.equal(w.until, '2026-10-03')
  assert.equal(w.warnings.length, 20)
  assert.equal(w.estimate, undefined)
  assert.deepEqual(w.rows, rows)
  const noMeta = plain(U.withMeta(rows, undefined, 'jobs'))
  assert.equal(noMeta.since, undefined)
  assert.equal(U.coverage(U.withMeta(rows, { a: 1 }, 'jobs'), '2026-10', '2026-10-03'), 'unknown')
  const docIn = { since: '2026-10', until: '2026-10', rows: [] }
  assert.equal(U.withMeta(docIn, billingMeta, 'billing'), docIn)
  assert.deepEqual(plain(U.withMeta('bad', [], 'jobs').rows), [])
})

// paintWarnings だけを取り出し、DOM を最小のスタブに差し替えて表示を確かめる。
const paintWarn = (warnings) => {
  const m = HTML.match(/function paintWarnings\([\s\S]*?\n\}\n/)
  assert.ok(m, 'paintWarnings が見つかること')
  const el = { hidden: true, kids: [], replaceChildren(...k) { this.kids = k } }
  const box = {
    $: () => el,
    document: { createElement: () => ({ textContent: '' }) }
  }
  runInNewContext(`${m[0]}\npaintWarnings(w)`, { ...box, w: warnings })
  return { hidden: el.hidden, texts: el.kids.map((k) => k.textContent) }
}

test('paintWarnings は annotate の warnings を「注意: 」付きで出し、0 件なら隠す', () => {
  const w = plain(U.annotate(runtimeInput(), '2026-10', '2026-10-03')).warnings
  const out = paintWarn(w)
  assert.equal(out.hidden, false)
  assert.deepEqual(out.texts, w.map((t) => '注意: ' + t))
  assert.equal(paintWarn([]).hidden, true)
  assert.equal(paintWarn(undefined).hidden, true)
  assert.equal(paintWarn(['a'.repeat(400)]).texts[0].length, '注意: '.length + 300)
})

test('ページ側の配線: render は annotate の結果で注意欄・推定・注記を描く', () => {
  assert.match(HTML, /U\.annotate\(input, period, today\)/)
  assert.match(HTML, /paintWarnings\(ann\.warnings\)/)
  assert.match(HTML, /const est = ann\.estimate/)
  assert.doesNotMatch(HTML, /dash\.data\('warnings'\)/)
  assert.doesNotMatch(HTML, /data-source="warnings"/)
  for (const id of ['aud-coverage', 'aud-warnings']) {
    assert.match(HTML, new RegExp(`id="${id}"[^>]*data-source="billing_meta jobs_meta"|data-source="billing_meta jobs_meta"[^>]*id="${id}"`))
  }
})

// ---- 含有枠の消費の確度（#209）: 下限（「以上」）か概算（「（概算）」）か ----
// 既定モード（--assume-current-visibility なし）の billing: 非有料分はすべて unknown で quota は構造上 0。
const defaultModeBilling = [
  { day: '2026-10-01', repo: 'priv-api', visibility: 'unknown', runner_type: 'linux', charge_class: 'paid', minutes: 100, quota_minutes: 0, billable_minutes: 100, gross_usd: 0.8, net_usd: 0.8 },
  { day: '2026-10-02', repo: 'priv-api', visibility: 'unknown', runner_type: 'linux', charge_class: 'unknown', minutes: 500, quota_minutes: 0, billable_minutes: 0, gross_usd: 4, net_usd: 0 }
]
const defaultFacts = plain(U.facts(defaultModeBilling, []))

test('quotaApprox は summarize.py の is_classified と同じ規則で文書全体から判定する', () => {
  assert.equal(U.quotaApprox(null, defaultFacts), false, '既定モードは下限')
  assert.equal(U.quotaApprox({ rows: [], assume_current_visibility: true }, defaultFacts), true, 'フラグが真なら概算')
  assert.equal(U.quotaApprox({ rows: [], assume_current_visibility: 'true' }, defaultFacts), false, '真偽値の真だけを読む')
  assert.equal(U.quotaApprox(null, facts), true, 'included 行がある旧形式の文書は概算')
  const zeroIncluded = plain(U.facts([{ day: '2026-10-01', charge_class: 'included', minutes: 0, quota_minutes: 0 }], []))
  assert.equal(U.quotaApprox(null, zeroIncluded), false, '分数 0 の included 行だけでは分類済みにしない')
  const pub = plain(U.facts([{ day: '2026-10-01', charge_class: 'public_free', minutes: 3, quota_minutes: 0 }], []))
  assert.equal(U.quotaApprox(null, pub), true)
  assert.equal(U.quotaApprox(null, undefined), false)
})

test('withMeta / annotate: billing_meta の assume_current_visibility 行（"true"）だけがフラグになる', () => {
  const flagRow = mrow('billing', 'assume_current_visibility', 'true')
  assert.equal(U.withMeta([], [flagRow], 'billing').assume_current_visibility, true)
  for (const bad of [mrow('billing', 'assume_current_visibility', 'false'), mrow('billing', 'assume_current_visibility', '1'),
    { source: 'billing', kind: 'assume_current_visibility', value: true }]) {
    assert.equal(U.withMeta([], [bad], 'billing').assume_current_visibility, undefined, JSON.stringify(bad))
  }
  assert.equal(U.withMeta([], [mrow('jobs', 'assume_current_visibility', 'true')], 'jobs').assume_current_visibility, undefined,
    'jobs には付けない')
  const ann = U.annotate(runtimeInput({ billing: ok(defaultModeBilling), billing_meta: ok([...billingMeta, flagRow]) }), '2026-10', '2026-10-03')
  assert.equal(U.quotaApprox(ann.docs.billing, defaultFacts), true, 'meta 経由のフラグで既定モードの行でも概算になる')
  const plainAnn = U.annotate(runtimeInput({ billing: ok(defaultModeBilling) }), '2026-10', '2026-10-03')
  assert.equal(U.quotaApprox(plainAnn.docs.billing, defaultFacts), false)
})

// ページ側の小関数を取り出し、U・labelOf・フォーマッタだけを与えて凡例・ツールチップ・注記の文言を確かめる。
const pageFn = (name) => {
  const m = HTML.match(new RegExp(`function ${name}\\([\\s\\S]*?\\n\\}\\n`))
  assert.ok(m, `${name} が見つかること`)
  return m[0]
}
const pageConst = (name) => {
  const m = HTML.match(new RegExp(`const ${name} = '[^'\\n]*';`))
  assert.ok(m, `${name} が見つかること`)
  return m[0]
}
const chartTexts = (factRows, sel, approx) => {
  const src = ['labelOf', 'quotaMark', 'legendLabel', 'tipRowLabel', 'tipTotalLabel', 'chartModel', 'lowerNote'].map(pageFn).join('\n')
    + pageConst('LOWER_NOTE') + pageConst('APPROX_NOTE')
  const code = `${src}
    const model = chartModel(factRows, sel, [], approx);
    const day = model.days[model.days.length - 1];
    const out = {
      legend: model.groups.map((g) => legendLabel(sel.group_by, g)),
      tipRows: model.groups.map((g) => tipRowLabel(sel.group_by, g, model.values[g.id][day] || 0)),
      tipTotal: tipTotalLabel(model.groups, 0),
      note: lowerNote(model, approx)
    };
    out`
  return plain(runInNewContext(code, { U, factRows, sel, approx, fmtMin: (v) => String(v) }))
}
const quotaSel = { period: '*', group_by: 'charge_class', include_free: false }

test('凡例・ツールチップ: 下限のときだけ「以上」、概算では「以上」を出さず「（概算）」を付ける', () => {
  const lower = chartTexts(defaultFacts, quotaSel, false)
  assert.ok(lower.legend.some((t) => t.endsWith(' 以上')), JSON.stringify(lower.legend))
  assert.ok(lower.tipRows.some((t) => t.endsWith(' 以上')), JSON.stringify(lower.tipRows))
  assert.match(lower.tipTotal, / 分以上$/)
  assert.deepEqual(lower.note, { hidden: false, text: '区分不明の利用があるため、グラフの枠消費は下限値（「以上」）になる。' })

  for (const [name, rows] of [['フラグ由来の概算', defaultFacts], ['分類済みの行がある概算', facts]]) {
    const ap = chartTexts(rows, quotaSel, true)
    const all = [...ap.legend, ...ap.tipRows, ap.tipTotal]
    assert.ok(all.every((t) => !t.includes('以上')), `${name}: ${JSON.stringify(all)}`)
    assert.ok(all.every((t) => t.endsWith('（概算）')), `${name}: ${JSON.stringify(all)}`)
    assert.match(ap.tipTotal, / 分（概算）$/)
    assert.equal(ap.note.hidden, false, name)
    assert.match(ap.note.text, /（概算）。$/)
    assert.doesNotMatch(ap.note.text, /下限|以上/)
  }
})

test('注記は枠消費のグラフに区分不明の行があるときだけ出す（軸・無料枠トグルに従う）', () => {
  for (const approx of [false, true]) {
    assert.equal(chartTexts(defaultFacts, { ...quotaSel, include_free: true }, approx).note.hidden, true, '無料枠込みは分数そのもの')
    assert.equal(chartTexts(defaultFacts, { ...quotaSel, group_by: 'repo' }, approx).note.hidden, false, 'リポジトリ軸でも区分不明を拾う')
  }
  const noUnknown = plain(U.facts(defaultModeBilling.filter((r) => r.charge_class !== 'unknown'), []))
  assert.equal(chartTexts(noUnknown, quotaSel, false).note.hidden, true)
})

test('凡例・ツールチップ: 区分不明がなくても概算なら「（概算）」、無料枠込みのグラフには印を付けない', () => {
  const noUnknown = plain(U.facts(defaultModeBilling.filter((r) => r.charge_class !== 'unknown'), []))
  const ap = chartTexts(noUnknown, quotaSel, true)
  assert.ok([...ap.legend, ...ap.tipRows].every((t) => t.endsWith('（概算）')), JSON.stringify(ap))
  assert.match(ap.tipTotal, / 分（概算）$/)
  assert.equal(ap.note.hidden, true, '区分不明がなければグラフ下の注記は出さない')
  const exact = chartTexts(noUnknown, quotaSel, false)
  assert.ok([...exact.legend, ...exact.tipRows].every((t) => !/以上|概算/.test(t)), JSON.stringify(exact))
  assert.match(exact.tipTotal, / 分$/)
  for (const approx of [false, true]) {
    const free = chartTexts(defaultFacts, { ...quotaSel, include_free: true }, approx)
    assert.ok([...free.legend, ...free.tipRows, free.tipTotal].every((t) => !/以上|概算/.test(t)), JSON.stringify(free))
  }
})

// paintTotals を取り出し、枠消費のタイル・注記・残量の文言を捕まえる。
const paintQuota = (row, approx) => {
  const tile = { textContent: '', setAttribute() {}, getAttribute: () => 'quota_minutes' }
  const qnote = { textContent: '' }
  const remaining = { textContent: '' }
  const el = { textContent: '', classList: { toggle() {} } }
  const box = {
    U,
    page: { querySelectorAll: (q) => (q === '[data-source="totals"]' ? [tile] : []) },
    $: (id) => (id === '#aud-remaining' ? remaining : el),
    document: { getElementById: (id) => (id === 'aud-quota-note' ? qnote : null) },
    fmtMin: (v) => String(v),
    fmtUsd: (v) => '$' + v
  }
  const rows = [{ key: U.keyOf(['2026-10', '*', '*']), quota_minutes: 800, unknown_minutes: row.unknown_minutes, paid_minutes: 0 }]
  runInNewContext(`${pageFn('quotaMark')}\n${pageFn('paintTotals')}\npaintTotals(rows, sel, '2026-10', cov, true, approx)`,
    { ...box, rows, sel: selOf(), cov: covFull, approx })
  return { tile: tile.textContent, qnote: qnote.textContent, remaining: remaining.textContent }
}

test('タイル: 下限なら「以上」、概算なら区分不明の有無にかかわらず「（概算）」。残量は区分不明があれば不明、概算なら「約」', () => {
  const lower = paintQuota({ unknown_minutes: 50 }, false)
  assert.equal(lower.tile, '800 以上')
  assert.match(lower.qnote, /下限値/)
  assert.equal(lower.remaining, '不明（区分不明の利用があり、消費は下限値）')
  const ap = paintQuota({ unknown_minutes: 50 }, true)
  assert.equal(ap.tile, '800（概算）')
  assert.match(ap.qnote, /概算/)
  assert.doesNotMatch(ap.qnote, /下限/)
  assert.equal(ap.remaining, '不明（区分不明の利用があり、消費は概算）')
  const none = paintQuota({ unknown_minutes: 0 }, true)
  assert.equal(none.tile, '800（概算）')
  assert.equal(none.remaining, '約 2200 分')
  const exact = paintQuota({ unknown_minutes: 0 }, false)
  assert.equal(exact.tile, '800')
  assert.equal(exact.remaining, '2200 分')
})

test('表とタイルは同じ quotaMark を使う', () => {
  const mark = runInNewContext(`${pageFn('quotaMark')}\nquotaMark`, {})
  assert.equal(mark(false, false), '')
  assert.equal(mark(false, true), '（概算）')
  assert.equal(mark(true, false), ' 以上')
  assert.equal(mark(true, true), '（概算）')
  assert.match(pageFn('paintTable'), /const lb = quotaMark\(r\.unknown_minutes > 0, approx\)/)
  assert.match(pageFn('paintTotals'), /quotaMark\(r\.unknown_minutes > 0, approx\)/)
})

test('ページ側の配線: 確度は描画より前に確定し、凡例・ツールチップはその値で文言を作る', () => {
  const render = pageFn('render')
  assert.match(render, /const approx = U\.quotaApprox\(ann\.docs\.billing, facts\)/)
  const iModel = render.indexOf('chartModel(facts, sel, srcs, approx)')
  assert.ok(iModel >= 0, 'render は chartModel でモデルを作る')
  assert.ok(iModel < render.indexOf('drawChart(chart'), 'chartModel は drawChart より前')
  assert.ok(iModel < render.indexOf('drawLegend(legend'), 'chartModel は drawLegend より前')
  assert.doesNotMatch(render, /g\.lower = false/, 'render は描画後に g.lower を書き換えない')
  assert.match(render, /paintTotals\([^)]*, approx\)/)
  assert.match(render, /paintTable\([^)]*, approx\)/)
  const legendFn = pageFn('drawLegend')
  const chartFn = pageFn('drawChart')
  assert.match(legendFn, /legendLabel\(axis, g\)/)
  assert.match(chartFn, /tipRowLabel\(axis, g, v\)/)
  assert.match(chartFn, /tipTotalLabel\(visible, total\)/)
  for (const body of [legendFn, chartFn]) assert.doesNotMatch(body, /以上/)
})

// ---- 推定の予算と打ち切り数（#219） ----
const countRows = [
  mrow('jobs', 'estimate_budget_runs', '15'), mrow('jobs', 'estimate_zero_groups', '2'), mrow('jobs', 'estimate_zero_runs', '330')
]
const withCounts = (extra = countRows) => runtimeInput({ jobs_meta: ok([...jobsMeta, ...extra]) })

test('推定の件数: meta 行から estimateCounts と文言が出る', () => {
  const a = plain(U.annotate(withCounts(), '2026-10', '2026-10-03'))
  assert.deepEqual(a.estimateCounts, { budget_runs: 15, zero_groups: 2, zero_runs: 330 })
  assert.equal(U.estimateText(a.estimateCounts),
    '抜き取りの予算は 15 run（jobs 呼び出しの上限）。試し取りが全部 0 分で打ち切った組は 2 組（330 run）で、その分は 0 分と推定している。')
})

test('推定の件数: 推定でないときは null（選択期間外・estimate 行なし・meta 欠落）', () => {
  assert.equal(U.annotate(withCounts(), '2026-09', '2026-10-20').estimateCounts, null)
  const noEst = jobsMeta.filter((m) => m.kind !== 'estimate')
  assert.equal(U.annotate(runtimeInput({ jobs_meta: ok([...noEst, ...countRows]) }), '2026-10', '2026-10-03').estimateCounts, null)
  for (const status of ['missing', 'error']) {
    assert.equal(U.annotate(runtimeInput({ jobs_meta: { status, data: undefined } }), '2026-10', '2026-10-03').estimateCounts, null)
  }
  assert.equal(U.annotate(runtimeInput(), '2026-10', '2026-10-03').estimateCounts, null, '件数行が無ければ null')
})

test('推定の件数: count 行が estimate 行より前でも反映し、不正値は無視する', () => {
  const before = [...countRows, ...jobsMeta]
  assert.deepEqual(plain(U.annotate(runtimeInput({ jobs_meta: ok(before) }), '2026-10', '2026-10-03').estimateCounts),
    { budget_runs: 15, zero_groups: 2, zero_runs: 330 })
  for (const bad of ['1.5', '-1', ' 3', 'abc', '99999999999', '1e3', '']) {
    const a = U.annotate(withCounts([mrow('jobs', 'estimate_budget_runs', bad)]), '2026-10', '2026-10-03')
    assert.equal(a.estimateCounts, null, JSON.stringify(bad))
  }
  for (const bad of [15, true, null]) {
    const a = U.annotate(withCounts([mrow('jobs', 'estimate_budget_runs', bad)]), '2026-10', '2026-10-03')
    assert.equal(a.estimateCounts, null)
  }
  assert.equal(U.annotate(withCounts([mrow('billing', 'estimate_budget_runs', '15')]), '2026-10', '2026-10-03').estimateCounts, null)
})

test('推定の件数: 従来の文書形でも読め、不正な型は無視する', () => {
  const d = (e) => plain(U.annotate({ billing: ok(billingDoc), jobs: ok({ ...jobsDoc, estimate: e }) }, '2026-10', '2026-10-03'))
  assert.deepEqual(d({ budget_runs: 15, zero_groups: 2 }).estimateCounts, { budget_runs: 15, zero_groups: 2 })
  assert.equal(d({ budget_runs: true, zero_groups: -1, zero_runs: '3' }).estimateCounts, null)
  assert.equal(U.estimateText(null), '')
  assert.equal(U.estimateText({}), '')
})

test('推定の件数: ページ配線', () => {
  assert.match(HTML, /<span id="aud-estimate-detail" data-source="jobs_meta" hidden><\/span>/)
  assert.match(HTML, /U\.estimateText\(ann\.estimateCounts\)/)
  assert.match(HTML, /\$\('#aud-estimate-detail'\)\.textContent = /)
})
