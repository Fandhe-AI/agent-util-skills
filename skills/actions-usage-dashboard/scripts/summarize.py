#!/usr/bin/env python3
"""fetch_billing.py / fetch_jobs.py の出力から、ターミナル用の利用状況要約を出す（check モード）。

役割と境界:
  actions-usage-dashboard の確認用入口。ダッシュボード（Artifact）を開かずに、当月の
  無料・有料の内訳と、GitHub ホストランナーの有料分・含有枠を使っているリポジトリ
  （セルフホスト移行の候補）を 20 行以内で示す。モデルはデータ本体ではこの要約だけを読む。
  読み取り専用で、ファイルは書かない。集計の取得は行わない（取得は fetch_*.py の責務）。

呼び出し元: SKILL.md の check モード。入力は fetch_billing.py（source: billing）と
  fetch_jobs.py（source: jobs）が書く rows JSON（schema_version 1）。since / until は必須で、
  対象月が取得期間外の取得元は 0 ではなく「不明」として扱う。列名や区分を変える
  ときは両 fetch スクリプトと揃える。

プランの含有分: PLAN_INCLUDED_MINUTES が唯一の定義。--plan 未指定時だけ
  gh api orgs/<org> でプラン名を取り、取れなければ「不明」と警告して続行する。

helper の共有: FetchError / gh_api / ORG_RE / MONTH_RE は _common.py から import する（定義は 1 か所）。
  safe / CTRL_RE は本スクリプトが正典で、build_dashboard.py が複製している（一致は tests/test_build_dashboard.py が検査）。
  _num は判定を同じにし、例外メッセージだけ入力の違いに合わせて変えている（API ではなく rows）。DAY_RE は本スクリプト固有。
  検証用の正規表現の終端は `\\Z` に統一する（`$` は末尾の改行の手前にも一致するため）。

アクセス: gh api（読み取り 1 回、--plan 指定時は 0 回）のみ。トークンは扱わない。標準ライブラリだけで動く。
"""
import argparse
import calendar
import json
import math
import os
import re
import sys
from datetime import date, datetime, timezone

# `-I`（隔離モード）では起動スクリプトのディレクトリが sys.path に入らないため、自分で足す。
# append にして、同じディレクトリに標準モジュール名のファイルがあっても標準ライブラリを先に解決させる。
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from _common import MONTH_RE, ORG_RE, FetchError, gh_api  # noqa: E402

SCHEMA_VERSION = 1
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\Z")
# プランごとの月間含有分（Linux 換算の分）。別名は同じ定数ブロックで持つ。
PLAN_INCLUDED_MINUTES = {"free": 2000, "team": 3000, "enterprise": 50000}
PLAN_ALIASES = {"business": "enterprise"}
CHARGE_CLASSES = {"paid", "included", "public_free", "unknown", "self_hosted"}
GROUP_KEYS = ("charge_class", "repo", "runner_type", "visibility")
DEFAULT_TOP = 5
MAX_NAME_LEN = 60
MAX_WARNING_LEN = 500
MAX_SRC_WARNINGS = 5
# rows 入力の上限。build_dashboard.py の MAX_ROWS_BYTES と同値（変えるときは両方を揃える）。
MAX_ROWS_BYTES = 8 * 1024 * 1024
CTRL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _num(item, key, required=True):
    v = item.get(key)
    if v is None and not required:
        return 0.0
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
        raise FetchError("rows の数値フィールド %s が不正（有限の非負数ではない）" % key)
    return float(v)


def safe(s, limit=MAX_NAME_LEN):
    """端末への制御文字注入を防ぐため、制御文字を除去し長い文字列を切り詰める。

    名前用の既定 60 文字は取得元の警告には短く、末尾の識別情報（org・repo・日付）が欠けるため、
    警告は limit に MAX_WARNING_LEN を渡して呼ぶ。
    """
    s = CTRL_RE.sub("?", str(s))
    return s if len(s) <= limit else s[:limit - 1] + "…"


def valid_day(v):
    """YYYY-MM-DD 形式かつ暦上存在する日付か（2026-02-31 や 2026-99-01 は偽）。"""
    if not isinstance(v, str) or not DAY_RE.match(v):
        return False
    try:
        date.fromisoformat(v)
    except ValueError:
        return False
    return True


def _coverage(src, data, p):
    """取得期間 (since, until) を日付文字列の組へ正規化する。billing は月粒度、jobs は日粒度。

    期間がない・不正な入力は、範囲外の月を 0 と誤読させないため fail-closed で拒否する。
    """
    since, until = data.get("since"), data.get("until")
    if src == "billing":
        ms, mu = (MONTH_RE.match(v) if isinstance(v, str) else None for v in (since, until))
        if not ms or not mu:
            raise FetchError("rows の since / until が YYYY-MM 形式ではない: %s" % safe(p))
        first = since + "-01"
        last = "%s-%02d" % (until, calendar.monthrange(int(mu.group(1)), int(mu.group(2)))[1])
    else:
        if not all(valid_day(v) for v in (since, until)):
            raise FetchError("rows の since / until が YYYY-MM-DD 形式ではない: %s" % safe(p))
        first, last = since, until
    if first > last:
        raise FetchError("rows の since が until より後: %s" % safe(p))
    return first, last


def month_overlap(cov, month, today=None):
    """取得期間が対象月と重なるか。(重なり, 月初から取れているか, 期待末日まで取れているか) を返す。

    期待末日は対象月の末日。ただし当月（today が対象月内）は today を超えて取れないため today とする。
    """
    if cov is None:
        return False, False, False
    m_first = month + "-01"
    m_last = "%s-%02d" % (month, calendar.monthrange(int(month[:4]), int(month[5:]))[1])
    expected_end = min(m_last, today) if today else m_last
    return (cov[0] <= m_last and cov[1] >= m_first, cov[0] <= m_first,
            cov[1] >= expected_end)


def load_rows(paths):
    """rows ファイルを検証して (org, rows, sources, warnings) を返す。非信頼入力として fail-closed。

    warnings は取得元（fetch_*.py）が記録した欠損・取得時点の警告で、出力へ引き継ぐ。
    """
    org, rows, sources, warnings = None, [], {}, []
    for p in paths:
        try:
            if os.path.getsize(p) > MAX_ROWS_BYTES:
                raise FetchError("rows が 8 MB を超える: %s" % safe(p))
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
        except OSError:
            raise FetchError("rows ファイルを読めない: %s" % safe(p))
        except ValueError:
            raise FetchError("rows ファイルを JSON として解析できない: %s" % safe(p))
        if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
            raise FetchError("rows の schema_version が 1 ではない: %s" % safe(p))
        src = data.get("source")
        if src not in ("billing", "jobs"):
            raise FetchError("rows の source が不正: %s" % safe(p))
        if src in sources:
            raise FetchError("同じ source（%s）の rows が重複している" % src)
        sources[src] = _coverage(src, data, p)
        o = data.get("org")
        if not isinstance(o, str) or not ORG_RE.match(o):
            raise FetchError("rows の org が不正: %s" % safe(p))
        if org is not None and o != org:
            raise FetchError("rows ファイル間で org が一致しない")
        org = o
        ws = data.get("warnings", [])
        if not isinstance(ws, list) or not all(isinstance(w, str) for w in ws):
            raise FetchError("rows の warnings が文字列の配列ではない: %s" % safe(p))
        warnings.extend("取得元(%s): %s" % (src, safe(w, MAX_WARNING_LEN)) for w in ws[:MAX_SRC_WARNINGS])
        if len(ws) > MAX_SRC_WARNINGS:
            warnings.append("取得元(%s): 警告が他 %d 件ある" % (src, len(ws) - MAX_SRC_WARNINGS))
        rs = data.get("rows")
        if not isinstance(rs, list):
            raise FetchError("rows が配列ではない: %s" % safe(p))
        for r in rs:
            if not isinstance(r, dict):
                raise FetchError("rows の要素が不正")
            day = r.get("day")
            if not valid_day(day):
                raise FetchError("rows の day が不正")
            if r.get("charge_class") not in CHARGE_CLASSES:
                raise FetchError("rows の charge_class が不正")
            for k in ("repo", "visibility", "runner_type"):
                if not isinstance(r.get(k), str):
                    raise FetchError("rows の %s が文字列ではない" % k)
            rows.append({
                "day": day, "repo": r["repo"], "visibility": r["visibility"],
                "runner_type": r["runner_type"], "charge_class": r["charge_class"],
                "minutes": _num(r, "minutes"), "quota_minutes": _num(r, "quota_minutes", False),
                "net_usd": _num(r, "net_usd", False),
            })
    return org, rows, sources, warnings


def included_for(plan):
    """プラン名から含有分を引く。表にないときは None（不明）。"""
    if not isinstance(plan, str):
        return None
    key = plan.strip().lower()
    return PLAN_INCLUDED_MINUTES.get(PLAN_ALIASES.get(key, key))


def fetch_plan(org):
    return gh_api(["orgs/%s" % org, "--jq", ".plan.name"]).strip()


def select_month(rows, month):
    return [r for r in rows if r["day"][:7] == month]


def _r(x):
    return round(x, 6)


def breakdown(rows, included_minutes, has_jobs, has_billing=True):
    """内訳を作る。unknown 区分の利用がある月は含有枠の消費が過小になるため、
    remaining / ratio を確定値にせず None（不明）にし、quota_minutes は下限として扱う。
    """
    s = {c: {"minutes": 0.0, "net_usd": 0.0, "quota": 0.0} for c in CHARGE_CLASSES}
    for r in rows:
        a = s[r["charge_class"]]
        a["minutes"] += r["minutes"]
        a["net_usd"] += r["net_usd"]
        a["quota"] += r["quota_minutes"]
    quota = s["included"]["quota"]
    inc = {"minutes": _r(s["included"]["minutes"]), "quota_minutes": _r(quota),
           "included_minutes": included_minutes, "remaining": None, "over": None, "ratio": None}
    inc["lower_bound"] = s["unknown"]["minutes"] > 0
    if included_minutes:
        # 消費は下限でも超過分は確定するため over は残す。残量と割合は下限からは決まらない。
        inc["over"] = _r(max(quota - included_minutes, 0.0))
        if not inc["lower_bound"]:
            inc["remaining"] = _r(max(included_minutes - quota, 0.0))
            inc["ratio"] = _r(quota / included_minutes)
    if not has_billing:
        # billing 未入力では GitHub ホスト分の内訳と枠の残りを計算できない。0 と誤読させず不明にする。
        return {
            "paid": None, "included": None, "public_free": None, "unknown": None,
            "self_hosted": {"minutes": _r(s["self_hosted"]["minutes"]), "available": has_jobs},
        }
    return {
        "paid": {"minutes": _r(s["paid"]["minutes"]), "net_usd": _r(s["paid"]["net_usd"])},
        "included": inc,
        "public_free": {"minutes": _r(s["public_free"]["minutes"])},
        "self_hosted": {"minutes": _r(s["self_hosted"]["minutes"]), "available": has_jobs},
        "unknown": {"minutes": _r(s["unknown"]["minutes"])},
    }


def group_top(rows, key, top):
    acc = {}
    for r in rows:
        a = acc.setdefault(r[key], [0.0, 0.0])
        a[0] += r["minutes"]
        a[1] += r["net_usd"]
    ordered = sorted(acc.items(), key=lambda kv: (-kv[1][0], kv[0]))
    return {"key": key,
            "top": [{"key": k, "minutes": _r(v[0]), "net_usd": _r(v[1])} for k, v in ordered[:top]],
            "others": max(len(ordered) - top, 0)}


def candidates(month_rows, all_rows, top):
    """GitHub ホストの有料分または含有枠を使うリポジトリを、有料優先で並べる。"""
    acc = {}
    for r in month_rows:
        if r["charge_class"] not in ("paid", "included"):
            continue
        a = acc.setdefault(r["repo"], {"paid": 0.0, "usd": 0.0, "inc": 0.0, "quota": 0.0})
        if r["charge_class"] == "paid":
            a["paid"] += r["minutes"]
            a["usd"] += r["net_usd"]
        else:
            a["inc"] += r["minutes"]
        a["quota"] += r["quota_minutes"]
    last = {}
    for r in all_rows:
        if r["charge_class"] != "self_hosted" and r["minutes"] > 0:
            if r["day"] > last.get(r["repo"], ""):
                last[r["repo"]] = r["day"]
    live = [(repo, a) for repo, a in acc.items() if a["paid"] > 0 or a["inc"] > 0 or a["quota"] > 0]
    paid = sorted([x for x in live if x[1]["paid"] > 0],
                  key=lambda x: (-x[1]["usd"], -x[1]["paid"], x[0]))
    rest = sorted([x for x in live if x[1]["paid"] <= 0],
                  key=lambda x: (-x[1]["quota"], -x[1]["inc"], x[0]))
    ordered = paid + rest
    return {"top": [{"repo": repo, "paid_minutes": _r(a["paid"]), "net_usd": _r(a["usd"]),
                     "included_minutes": _r(a["inc"]), "quota_minutes": _r(a["quota"]),
                     "last_used": last.get(repo)} for repo, a in ordered[:top]],
            "others": max(len(ordered) - top, 0)}


def build_summary(org, rows, sources, month, plan, plan_warning, group_by, top, src_warnings=(), today=None):
    mrows = select_month(rows, month)
    inc_min = included_for(plan)
    warnings = list(src_warnings)
    if today is None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    has_billing, b_full, b_tail = month_overlap(sources.get("billing"), month, today)
    has_jobs, j_full, j_tail = month_overlap(sources.get("jobs"), month, today)
    coverage = {}
    if "billing" not in sources:
        warnings.append("billing 未入力のため有料・含有枠・公開無料の内訳と残量は不明（fetch_billing.py の出力を --rows に渡す）")
    elif not has_billing:
        warnings.append("対象月 %s は billing の取得期間（%s..%s）外のため有料・含有枠・公開無料の内訳と残量は不明"
                        % (month, *sources["billing"]))
    if "jobs" in sources and not has_jobs:
        warnings.append("対象月 %s は jobs の取得期間（%s..%s）外のためセルフホストは不明" % (month, *sources["jobs"]))
    for name, ok, full, tail in (("billing", has_billing, b_full, b_tail), ("jobs", has_jobs, j_full, j_tail)):
        if not ok:
            continue
        coverage[name] = {"since": sources[name][0], "until": sources[name][1],
                          "head_missing": not full, "tail_missing": not tail}
        if not full:
            warnings.append("%s の取得期間（%s..%s）が対象月の途中から始まるため月初分が欠ける"
                            % (name, *sources[name]))
        if not tail:
            warnings.append("%s の取得期間（%s..%s）が対象月の末尾まで届かないため %s 以降の分が欠ける"
                            % (name, *sources[name], sources[name][1]))
    if plan_warning:
        warnings.append(plan_warning)
    elif inc_min is None:
        warnings.append("プラン名「%s」の含有分が不明" % safe(plan))
    bd = breakdown(mrows, inc_min, has_jobs, has_billing)
    if has_billing and bd["unknown"]["minutes"] > 0:
        warnings.append("区分不明あり。含有枠の消費は下限で、残量・割合は不明。期間中に公開範囲が変わっていないと保証できる場合だけ、"
                        "fetch_billing.py に --assume-current-visibility を付けると現在の公開範囲で分類できる"
                        "（削除済み・権限不足のリポジトリは unknown のまま残る）")
    if not mrows:
        warnings.append("対象月の行なし")
    return {
        "schema_version": SCHEMA_VERSION, "org": org, "month": month,
        "plan": {"name": plan, "included_minutes": inc_min},
        "breakdown": bd, "group_by": group_top(mrows, group_by, top),
        "candidates": candidates(mrows, rows, top), "coverage": coverage, "warnings": warnings,
    }


def render_text(s):
    bd, inc = s["breakdown"], s["breakdown"]["included"] or {}
    pm = s["plan"]["included_minutes"]
    out = ["%s / %s（UTC）/ プラン %s（含有 %s）" % (
        safe(s["org"]), s["month"], safe(s["plan"]["name"] or "不明"),
        "%d 分" % pm if pm else "不明")]
    if bd["paid"] is None:
        out.append("有料: データなし（billing 未入力または対象月が取得期間外）")
        out.append("含有枠: データなし（残量は不明）")
        out.append("公開リポの無料: データなし")
        return _render_tail(s, out)
    out.append("有料: %.1f 分 / %.2f USD" % (bd["paid"]["minutes"], bd["paid"]["net_usd"]))
    if inc["included_minutes"] and inc["lower_bound"]:
        txt = "含有枠: 消費 %.1f 分以上（下限。区分不明あり）（実 %.1f 分）/ %d 分、残量・割合は不明" % (
            inc["quota_minutes"], inc["minutes"], inc["included_minutes"])
        if inc["over"] > 0:
            txt += "、超過 %.1f 分以上" % inc["over"]
    elif inc["included_minutes"]:
        txt = "含有枠: 消費 %.1f 分（実 %.1f 分）/ %d 分 = %.1f%%、残り %.1f 分" % (
            inc["quota_minutes"], inc["minutes"], inc["included_minutes"],
            inc["ratio"] * 100, inc["remaining"])
        if inc["over"] > 0:
            txt += "、超過 %.1f 分" % inc["over"]
    else:
        txt = "含有枠: 消費 %.1f 分（実 %.1f 分）/ 含有分不明" % (inc["quota_minutes"], inc["minutes"])
    out.append(txt)
    out.append("公開リポの無料: %.1f 分" % bd["public_free"]["minutes"])
    return _render_tail(s, out)


def _render_tail(s, out):
    bd = s["breakdown"]
    out.append("セルフホスト: %s" % ("%.1f 分" % bd["self_hosted"]["minutes"]
                                 if bd["self_hosted"]["available"] else "データなし"))
    if bd["unknown"] is not None and bd["unknown"]["minutes"] > 0:
        out.append("区分不明: %.1f 分" % bd["unknown"]["minutes"])
    g = s["group_by"]
    out.append("%s 別 上位%s:" % (g["key"], "（他 %d 件）" % g["others"] if g["others"] else ""))
    for e in g["top"]:
        out.append("  %s  %.1f 分 / %.2f USD" % (safe(e["key"]), e["minutes"], e["net_usd"]))
    c = s["candidates"]
    out.append("移行候補%s:" % ("（他 %d 件）" % c["others"] if c["others"] else ""))
    for e in c["top"]:
        out.append("  %s  有料 %.1f 分 / %.2f USD、含有枠 %.1f 分、最終利用 %s" % (
            safe(e["repo"]), e["paid_minutes"], e["net_usd"], e["quota_minutes"], e["last_used"] or "-"))
    if s["warnings"]:
        out.append("注意: " + " / ".join(s["warnings"]))
    return out


def _top(v):
    try:
        n = int(v)
    except ValueError:
        raise argparse.ArgumentTypeError("--top は 1 以上の整数")
    if n < 1:
        raise argparse.ArgumentTypeError("--top は 1 以上の整数")
    return n


def main(argv=None):
    ap = argparse.ArgumentParser(description="Actions 利用状況の要約（check モード・読み取り専用）")
    ap.add_argument("--rows", action="append", required=True, help="fetch_*.py の出力（最大 2 件）")
    ap.add_argument("--month", help="YYYY-MM（既定は rows の最新月）")
    ap.add_argument("--group-by", choices=GROUP_KEYS, default="charge_class")
    ap.add_argument("--top", type=_top, default=DEFAULT_TOP)
    ap.add_argument("--plan", help="プラン名（未指定時は gh api orgs/<org> で取得）")
    ap.add_argument("--json", action="store_true", dest="as_json")
    a = ap.parse_args(argv)
    try:
        if len(a.rows) > 2:
            raise FetchError("--rows は最大 2 件")
        if a.month is not None and not MONTH_RE.match(a.month):
            raise FetchError("--month は YYYY-MM 形式で指定する")
        org, rows, sources, src_warnings = load_rows(a.rows)
        month = a.month or (max(r["day"][:7] for r in rows) if rows
                            else datetime.now(timezone.utc).strftime("%Y-%m"))
        plan, plan_warning = a.plan, None
        if plan is None:
            try:
                plan = fetch_plan(org)
            except FetchError as e:
                plan, plan_warning = None, "プラン取得に失敗（含有分は不明）: %s" % safe(e, MAX_WARNING_LEN)
        summary = build_summary(org, rows, sources, month, plan, plan_warning, a.group_by, a.top, src_warnings)
    except FetchError as e:
        print("エラー: %s" % e, file=sys.stderr)
        return 1
    if a.as_json:
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    else:
        print("\n".join(render_text(summary)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
