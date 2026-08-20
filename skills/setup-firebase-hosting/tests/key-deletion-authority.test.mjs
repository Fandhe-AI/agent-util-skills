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
//   4. `iam service-accounts keys delete` のすべての出現箇所が、
//      (a) 案内文言（die/echo の文字列リテラル内、$( ) のネストを考慮して
//      構造的に判定）、(b) 発行記録ガード（`case ",${recorded_key_ids}," in`
//      の鍵 ID 一致アームに厳密に属する）、(c) 今回発行した鍵のロールバック
//      （cleanup() 内かつ削除対象が厳密に "${rollback_key_name}"）の
//      いずれか 1 つに分類できること（許可リスト方式。いずれにも分類でき
//      ない出現＝ガード外の削除経路として fail-closed で失敗させる）
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

// 行末バックスラッシュによる行継続を論理行へ結合する。`gcloud iam
// service-accounts keys \` + 次行 `delete "${key_name}" ...` のように
// `keys` と `delete` が物理行をまたいで分割されると、単純な行走査では
// `iam\s+service-accounts\s+keys\s+delete\b` に一致せず検査対象から漏れる
// （codex-review P1 指摘）。継続行は結合後の先頭インデックスへ内容を集約し、
// 後続の物理行は空文字へ置き換えることで、他の走査（case/esac/;; の判定・
// 関数境界の判定）に使う行インデックスの対応関係は変えずに済む。
function buildLogicalLines(content) {
  const raw = content.split('\n')
  const logical = raw.slice()
  for (let i = 0; i < logical.length; i++) {
    let j = i + 1
    while (/\\\s*$/.test(logical[i]) && j < raw.length) {
      logical[i] = logical[i].replace(/\\\s*$/, ' ') + raw[j].trim()
      logical[j] = ''
      j++
    }
  }
  return logical
}

// 「keys delete」の出現箇所を、実行不能と断定できる条件による**除外**では
// なく、実行され得ない案内文言だと構造的に断定できる場合のみ拾う**許可
// リスト**方式で判定する（fail-closed）。
//
// 以前は「行頭が echo で始まる」「行に <KEY_ID> を含む」という部分文字列・
// 行 prefix ベースの除外を使っていたが、これは容易にすり抜けられる
// （`echo ... && gcloud ... keys delete ...`・`echo "$(gcloud ... keys
// delete ...)"` は行頭が echo でも実際に削除を実行する。コメントに
// `<KEY_ID>` を足すだけでも実行可能な削除行を除外できてしまう。
// codex-review P1 指摘）。
//
// 本スクリプトの案内文言（die/echo のメッセージ文字列）は必ず
// `die "..."` / `echo "..."` という二重引用符で囲われた文字列リテラル
// なので、対象の出現位置が「その文字列リテラルが開いてから閉じるまでの
// 範囲内」にあるかどうかを、`$( ... )` によるコマンド置換のネストを
// 考慮しながら実際に走査して判定する（`$(shq "${x}")` のように置換内部に
// 二重引用符が現れても、それは外側の文字列を閉じない）。この判定を通った
// 場合のみ「案内文言（非実行）」として扱い、それ以外はすべて実行行の
// 候補として後続のガード検証に回す。
function findEnclosingGuidanceString(lines, occLineIdx) {
  for (let start = occLineIdx; start >= 0; start--) {
    const openMatch = /^\s*(?:echo|die)\s+"/.exec(lines[start])
    if (!openMatch) continue
    const openCol = lines[start].indexOf('"')
    let depth = 0
    for (let i = start; i < lines.length; i++) {
      const text = lines[i]
      const fromCol = i === start ? openCol + 1 : 0
      for (let ci = fromCol; ci < text.length; ci++) {
        if (text[ci] === '$' && text[ci + 1] === '(') {
          depth++
          ci++
          continue
        }
        if (text[ci] === ')' && depth > 0) {
          depth--
          continue
        }
        if (text[ci] === '"' && depth === 0) {
          return { start, closeLine: i, closeCol: ci }
        }
      }
    }
    // 閉じ引用符に到達できなかった（不整形）。案内文言とは断定できない
    // ため、ここでは扱わず候補として残す（fail-closed）。
    return null
  }
  return null
}

// 文字列リテラルの範囲内であっても、`$( ... )` コマンド置換の内側は
// 実際にシェルへ渡されて実行される（`echo "$(gcloud ... keys delete
// ...)"` は置換結果を echo するだけでなく、置換自体として `gcloud ...
// keys delete` を実際に実行する。codex-review P1 指摘）。そのため出現
// 位置がガード文字列の範囲内でも、置換ネストの深さが 0（＝置換の外＝
// 純粋な表示テキスト）である場合に限り「案内文言」として扱う。
function guidanceStringDepthAt(lines, range, targetIdx, targetCol) {
  let depth = 0
  for (let i = range.start; i <= targetIdx; i++) {
    const text = lines[i]
    const fromCol = i === range.start ? lines[range.start].indexOf('"') + 1 : 0
    const toCol = i === targetIdx ? targetCol : text.length
    for (let ci = fromCol; ci < toCol; ci++) {
      if (text[ci] === '$' && text[ci + 1] === '(') {
        depth++
        ci++
        continue
      }
      if (text[ci] === ')' && depth > 0) {
        depth--
        continue
      }
    }
  }
  return depth
}

function isGuidanceOccurrence(lines, idx, matchCol) {
  const range = findEnclosingGuidanceString(lines, idx)
  if (!range) return false
  if (idx < range.start || idx > range.closeLine) return false
  if (idx === range.closeLine && matchCol >= range.closeCol) return false
  if (guidanceStringDepthAt(lines, range, idx, matchCol) > 0) return false
  return true
}

// 同一（論理）行に `keys delete` が複数回現れる場合（例: 案内文言の echo
// と、その後ろに `&&` で連結された実際の実行が同一行に並ぶ攻撃パターン）
// に、最初の 1 件しか見つけない非 global 正規表現の `exec` だと 2 件目以降
// が検査から漏れる（codex-review P1 指摘）。`g` フラグ + `matchAll` で
// 各行内の全出現位置を収集する。
function findKeyDeleteOccurrences(lines) {
  const DELETE_RE = /iam\s+service-accounts\s+keys\s+delete\b/g
  const occurrences = []
  lines.forEach((line, i) => {
    for (const m of line.matchAll(DELETE_RE)) {
      occurrences.push({ idx: i, col: m.index })
    }
  })
  return occurrences
}

function extractKeyDeleteExecLines(content) {
  const lines = buildLogicalLines(content)
  return findKeyDeleteOccurrences(lines)
    .filter(({ idx, col }) => !isGuidanceOccurrence(lines, idx, col))
    .map(({ idx }) => lines[idx])
}

test('gcloud iam service-accounts keys delete の実行行が案内文言以外に最低限存在する（抽出ロジックの破損検知）', () => {
  const execLines = extractKeyDeleteExecLines(script)
  assert.ok(
    execLines.length >= 3,
    `鍵削除の実行行が想定より少ない（世代交代ループ 2 箇所・ロールバック cleanup 1 箇所の計 3 箇所を期待）: ${execLines.length} 件`
  )
})

// 削除行が `case ",${recorded_key_ids}," in` ブロックの「内側にあるか」
// だけでなく、実際に鍵 ID 一致アーム（`*",${key_id},"*)`）に属している
// かを構造的に検証する。ブロック内側判定だけだと、同じ case 文に
// ガードなしの default アーム（`*)`）を新設して削除を追加しても通過して
// しまう（codex-review P1 指摘）。
//
// アームラベルは「鍵 ID 一致パターンそのもの」であることを要求し、
// `${key_id}` を含んでさえいれば良いという緩い判定は使わない。緩い判定
// だと `*",${key_id},"*|*)` のように `|`（代替パターン）で default アーム
// を併記したラベルも「鍵 ID を含む」という理由だけで安全と誤判定して
// しまう（このラベルは記録に一致しない任意の鍵でも default 側で実行
// される＝ガードになっていない。codex-review P1 指摘）。
const RECORDED_KEY_ARM_LABEL_RE = /^\*",\$\{key_id\},"\*\)$/

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
      // アームラベルが鍵 ID 一致パターンそのものと厳密一致することを
      // 確認する（`|` を含む代替パターン・default アーム単独を排除）。
      return Boolean(armLabel) && RECORDED_KEY_ARM_LABEL_RE.test(armLabel)
    }
    if (/^\s*case\b/.test(line)) {
      // 別の case 文の開始行に達した＝目的の case の内側ではない。
      return false
    }
  }
  return false
}

// 削除呼び出しがラッパー関数経由で間接化されると、静的な行走査ではその
// 関数の「呼び出し側」（`delete_key "${x}"` のようなテキスト）が
// `keys delete` という文字列を含まないため検査対象に現れず、ガード外の
// 呼び出しを見逃す（codex-review P1 指摘）。この経路は正規表現ベースの
// 静的検査では原理的に閉じきれない（呼び出しグラフの解析が要る）ため、
// 「`keys delete` は `cleanup()` 内、またはトップレベル（いずれの関数
// 定義にも属さない場所）にしか出現してはならない」という不変条件を課す
// ことで間接化そのものを禁止し、実質的に塞ぐ。新規のラッパー関数を
// 定義した時点で、その定義行が cleanup() 以外の関数内に属することを
// 検知して fail-closed で失敗させる。
function enclosingFunctionName(lines, idx) {
  for (let i = idx; i >= 0; i--) {
    const m = lines[i].match(/^([A-Za-z_][A-Za-z0-9_]*)\(\)\s*\{\s*$/)
    if (m) {
      let closeIdx = -1
      for (let j = i + 1; j < lines.length; j++) {
        if (/^\}\s*$/.test(lines[j])) {
          closeIdx = j
          break
        }
      }
      if (closeIdx !== -1 && idx > i && idx < closeIdx) return m[1]
      // idx はこの（直前に見つかった）関数定義の本体範囲外。本スクリプトの
      // 関数定義は入れ子にならないため、これより前を遡っても idx を囲む
      // 関数は存在しない。
      return null
    }
  }
  return null
}

// ロールバック判定: 削除対象引数が厳密に `"${rollback_key_name}"` である
// ことを要求する（削除行に文字列 `rollback_key_name` が含まれるだけの
// 単純な文字列一致だと、ガード外の削除行のコメント・案内文言などに同語を
// 混ぜるだけで誤って PASS してしまう＝偽陰性経路になる。codex-review P1
// 指摘）。加えて、その行が `cleanup()` 関数の本体内にあることも確認する。
// cleanup() は今回発行した鍵のロールバック専用の関数であり、所有が実行
// そのものに自明（同一実行内で発行した鍵）なため発行記録ガードを要しない
// という前提は「cleanup() 内に限る」ことで初めて成り立つ。
// 出現位置（col、`iam service-accounts keys delete` の先頭）から始まる
// 部分文字列に対して照合するため `^` で先頭固定する（同一行内の別の
// 出現の削除対象を誤って拾わないようにするため）。
const ROLLBACK_DELETE_ARG_RE = /^iam\s+service-accounts\s+keys\s+delete\s+"\$\{rollback_key_name\}"(?:\s|\\|$)/

function isWithinFunction(lines, idx, functionName) {
  const startRe = new RegExp(`^${functionName}\\(\\)\\s*\\{\\s*$`)
  let start = -1
  for (let i = idx; i >= 0; i--) {
    if (startRe.test(lines[i])) {
      start = i
      break
    }
  }
  if (start === -1) return false
  // 関数開始直後から idx までの間に、関数本体を閉じる行頭 `}` 単独行が
  // 現れていないことを確認する（現れていれば idx は既に関数の外）。
  for (let i = start + 1; i < idx; i++) {
    if (/^\}\s*$/.test(lines[i])) return false
  }
  return true
}

// 判定は行全体に対してではなく、対象の出現位置（col）から始まる部分
// 文字列に対して行う。行全体への判定だと、同一行に正当なロールバック
// 削除と別の（不正な）削除が並んでいる場合、両方の出現がロールバック
// として誤って PASS してしまう（codex-review「同一行の2件目以降」指摘と
// 同種の見逃し経路。fail-closed の観点から出現単位で厳密化する）。
function isRollbackDelete(lines, idx, col) {
  return ROLLBACK_DELETE_ARG_RE.test(lines[idx].slice(col)) && isWithinFunction(lines, idx, 'cleanup')
}

test('鍵削除の出現箇所はすべて「案内文言」「発行記録の一致アーム」「ロールバック cleanup」のいずれかに分類できる', () => {
  const lines = buildLogicalLines(script)
  const occurrences = findKeyDeleteOccurrences(lines)

  assert.ok(occurrences.length > 0, '鍵削除の出現箇所が抽出できていない（抽出ロジックの破損の可能性）')

  const execOccurrences = occurrences.filter(({ idx, col }) => !isGuidanceOccurrence(lines, idx, col))
  assert.ok(
    execOccurrences.length > 0,
    '案内文言以外の鍵削除実行行が 1 件も抽出できていない（抽出ロジックの破損の可能性）'
  )

  for (const { idx, col } of execOccurrences) {
    const enclosingFn = enclosingFunctionName(lines, idx)
    assert.ok(
      enclosingFn === null || enclosingFn === 'cleanup',
      `keys delete が cleanup() 以外の関数（${enclosingFn}）内に定義されている（ラッパー関数経由の間接化はガード検証をすり抜けるため禁止。行 ${idx + 1}）: ${lines[idx].trim()}`
    )

    const inRecordedGuardArm = isGuardedByRecordedKeys(lines, idx)

    assert.ok(
      isRollbackDelete(lines, idx, col) || inRecordedGuardArm,
      `ガード外（発行記録の一致アーム内でも厳密な rollback_key_name 削除でもない）の鍵削除出現を検出（行 ${idx + 1}, 列 ${col + 1}）: ${lines[idx].trim()}`
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
