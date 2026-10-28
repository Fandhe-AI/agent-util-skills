#!/usr/bin/env python3
"""billing usage API から GitHub ホストランナーの日次行を作る。

役割と境界:
  actions-usage-dashboard の最初のデータ源。org の Actions 利用分数を月ごとに
  billing usage API（organizations/<org>/settings/billing/usage）から取り、
  「日（UTC）x リポジトリ x SKU x 料金区分」の行へ正規化して JSON に書く。
  扱うのは GitHub ホストランナーだけである。セルフホストは jobs 側（fetch_jobs.py）が
  担当するため、ここでは取り込まず二重計上を避ける。

呼び出し元: 後続の fetch_jobs.py / summarize.py / build_dashboard.py が、この出力
  スキーマ（schema_version 1）に依存する。列名や区分を変えるときは全員を更新する。

出力スキーマ:
  {schema_version, source: "billing", org, since, until, generated_at,
   assume_current_visibility: bool（--assume-current-visibility を付けたか。summarize.py が
   含有枠の消費を「下限」と言える条件の判定に使う）,
   linux_price_per_minute: {YYYY-MM: 単価}, rows: [...]}
  row = day / repo / visibility / sku / runner_type / charge_class / minutes /
        quota_minutes / billable_minutes / gross_usd / discount_usd / net_usd
  charge_class: paid（金額が発生した分）/ included（含有枠）/ public_free（公開リポの無料分）/
  unknown（利用日時点の公開範囲を確認できない非有料分）/
  dependabot_free（Dependabot が起動したジョブの非有料分。含有枠を消費しないため quota_minutes は 0。
  --usage-csv 経路だけで生じる）。
  有料分は公開範囲ではなく netAmount から判定する。非有料分の included / public_free は
  利用日時点の公開範囲が前提だが、API が返すのは現在の公開範囲だけで履歴を取れない。
  公開範囲が途中で変わったリポジトリの過去分を誤分類（quota の消失・過大計上）しないよう、
  既定では非有料分を unknown とし quota_minutes を 0 にする。公開範囲が期間中に
  変わっていないと利用者が保証できる場合だけ --assume-current-visibility で現在値を採る。

入力の 2 経路:
  既定は billing usage API。API の明細には起動した利用者もワークフローも無く、Dependabot の分を
  見分けられないため、含有枠の消費は請求画面より上振れする（Issue #221）。
  --usage-csv に請求画面からダウンロードした「詳細」の使用量 CSV を渡すと、username /
  workflow_path から Dependabot を判定して dependabot_free に分ける（Issue #228）。公式文書は
  Dependabot ジョブを含有分に数えないと書く
  （https://docs.github.com/en/billing/reference/billing-reports ・
  https://docs.github.com/en/billing/concepts/product-billing/github-actions）。
  CSV 経路は API 経路の代わりであり、同じ期間の API の rows と併用すると summarize が重複で止める。
  CSV 経路は出力に "input": "usage_csv"、"dependabot": {判定規則と件数}、"warnings" を足す
  （API 経路は "input": "api"）。username / workflow_path の値は判定にだけ使い、出力・標準出力・
  標準エラー・例外文のどこにも入れない（rows は閲覧者全員が読める）。

アクセス: gh api 経由のみ（CSV 経路でもリポジトリ一覧のために gh を呼ぶ）。トークンは扱わない。標準ライブラリだけで動く。
helper の共有: FetchError / nonneg_num / gh_api / atomic_write_json / ORG_RE / MONTH_RE / parse_concatenated_json は _common.py から import する。
"""
import argparse
import csv
import json
import math
import os
import sys
from datetime import date, datetime, timezone

# `-I`（隔離モード）では起動スクリプトのディレクトリが sys.path に入らないため、自分で足す。
# append にして、同じディレクトリに標準モジュール名のファイルがあっても標準ライブラリを先に解決させる。
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from _common import (  # noqa: E402
    MONTH_RE, ORG_RE, FetchError, atomic_write_json, gh_api, nonneg_num,
    parse_concatenated_json, safe,
)

SCHEMA_VERSION = 1
LINUX_PRICE_SKU = "actions linux"
UNATTRIBUTED = "(unattributed)"
# 区分の並び順（行の決定的ソートにも使う）
CHARGE_ORDER = {"paid": 0, "included": 1, "public_free": 2, "dependabot_free": 3, "unknown": 4}
# SKU（正規化後）に対する runner_type の対応表。上から順に照合し、最初に当たったものを採る。
# 実データで SKU 表記が異なる場合はここのキーワードだけを直す。
RUNNER_TYPE_RULES = (
    (("linux", "arm"), "ubuntu-arm"),
    (("linux",), "ubuntu"),
    (("macos",), "macos"),
    (("windows",), "windows"),
)
SUMMARY_MONTHS_MAX = 12
# 取得期間の上限（月数）。同系エンドポイント（usage/summary 等）の公式記載「過去 24 か月のみ」に合わせた値。
# usage 本体のページ（https://docs.github.com/en/rest/billing/usage ）に保持期間の記載は無く未確認（2026-10-24）。`--since 0001-01` のような入力で gh を大量に呼ばないための多層防御。
MAX_MONTHS = 24
# 使用量 CSV の上限。詳細レポートは 1 日あたり大量の行を持ち、31 日分でも数十 MB になり得る。
# 全行をメモリへ載せるため、巨大ファイルを渡された場合に止める多層防御（実データの大きさは未確認）。
MAX_CSV_BYTES = 64 * 1024 * 1024
MAX_CSV_ROWS = 1_000_000
# 詳細の使用量 CSV の列名（公式 billing-reports のフィールド表）。列名の別名表は持たず、
# 公式名と違えば推測で合わせず止める。discount_amount と organization は任意。
CSV_REQUIRED = ("date", "product", "sku", "quantity", "unit_type", "applied_cost_per_quantity",
                "gross_amount", "net_amount", "repository", "username", "workflow_path")
# Dependabot の判定規則。値は判定にだけ使い、出力へは固定文言として規則だけを残す。
DEPENDABOT_USER = "dependabot[bot]"
DEPENDABOT_PATH_PREFIX = "dynamic/dependabot/"
DEPENDABOT_RULE = "username=dependabot[bot] or workflow_path=dynamic/dependabot/*"


def normalize_sku(sku):
    s = str(sku).lower().replace("_", " ").replace("-", " ")
    return " ".join(s.split())


def runner_type_for(sku):
    s = normalize_sku(sku)
    for keywords, rtype in RUNNER_TYPE_RULES:
        if all(k in s for k in keywords):
            return rtype
    return "other"


def month_range(since, until):
    m1, m2 = MONTH_RE.match(since), MONTH_RE.match(until)
    if not m1 or not m2:
        raise FetchError("月は YYYY-MM 形式で指定する")
    y, m = int(m1.group(1)), int(m1.group(2))
    ye, me = int(m2.group(1)), int(m2.group(2))
    out = []
    while (y, m) <= (ye, me):
        out.append("%04d-%02d" % (y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def normalize_repo(name, org):
    if not isinstance(name, str) or not name.strip():
        return UNATTRIBUTED
    name = name.strip()
    prefix = org.lower() + "/"
    if name.lower().startswith(prefix):
        name = name[len(prefix):]
    return name or UNATTRIBUTED


def _day_of(date_str):
    if not isinstance(date_str, str):
        raise FetchError("API の date が文字列ではない")
    try:
        dt = datetime.fromisoformat(date_str.strip().replace("Z", "+00:00"))
    except ValueError:
        raise FetchError("API の date を ISO 8601 として解析できない")
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    return dt.date()


def select_items(payload, month, org):
    """Actions の分数明細だけを絞り込み、(day, repo, sku) 単位に集約して返す。"""
    items = payload.get("usageItems") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise FetchError("%s: usageItems が配列ではない" % month)
    units = {}
    for it in items:
        if not isinstance(it, dict):
            raise FetchError("%s: usageItems の要素が不正" % month)
        if str(it.get("product", "")).lower() != "actions" or str(it.get("unitType", "")).lower() != "minutes":
            continue
        day = _day_of(it.get("date"))
        if "%04d-%02d" % (day.year, day.month) != month:
            continue
        sku = it.get("sku")
        if not isinstance(sku, str) or not sku:
            raise FetchError("%s: sku が不正" % month)
        q, g = nonneg_num(it, "quantity"), nonneg_num(it, "grossAmount")
        d, n = nonneg_num(it, "discountAmount", False), nonneg_num(it, "netAmount")
        p = nonneg_num(it, "pricePerUnit")
        key = (day.isoformat(), normalize_repo(it.get("repositoryName"), org), sku)
        u = units.setdefault(key, {"q": 0.0, "g": 0.0, "d": 0.0, "n": 0.0, "price": 0.0,
                                   "paid": 0.0, "gpaid": 0.0, "dq": 0.0, "dg": 0.0})
        # 単価が異なる明細を平均単価で割ると有料分数が狂うため、元明細ごとに有料分と
        # その按分 gross を求めてから集約する。
        paid_i = min(max(n / p, 0.0), q) if p > 0 else 0.0
        gpaid_i = g * paid_i / q if q > 0 else 0.0
        u["paid"] += paid_i
        u["gpaid"] += gpaid_i
        # Dependabot 明細の非有料分は含有枠を消費しない。有料かどうかは netAmount を正とするため、
        # 有料分（paid_i）は Dependabot でも paid に残し、残りだけを dependabot_free へ回す。
        if it.get("dependabot") is True:
            u["dq"] += q - paid_i
            u["dg"] += max(g - gpaid_i, 0.0)
        # 返却順に依存させないため、正の単価の最小値を保持する
        if p > 0 and (u["price"] <= 0 or p < u["price"]):
            u["price"] = p
        u["q"] += q
        u["g"] += g
        u["d"] += d
        u["n"] += n
    return units


def linux_price(units):
    """Linux 換算単価。明細の順序に依存しないよう、正の単価の最小値（標準 Linux の単価）を採る。"""
    prices = [u["price"] for (_, _, sku), u in units.items()
              if normalize_sku(sku) == LINUX_PRICE_SKU and u["price"] > 0]
    return min(prices) if prices else None


def _r(x):
    return round(x, 6)


def build_rows(month_payloads, visibility, org, assume_current=False):
    """月別 payload から区分展開済みの行と、使用した Linux 単価の表を返す。

    assume_current が False の間、非有料分は利用日時点の公開範囲を確認できないため
    unknown とし、quota へ計上しない。True のときだけ現在の公開範囲で分類する。
    """
    vis_map = {k.lower(): v for k, v in visibility.items()}
    rows, price_map, prev = [], {}, None
    for month in sorted(month_payloads):
        units = select_items(month_payloads[month], month, org)
        price = linux_price(units) or prev
        if price is not None:
            price_map[month] = price
            prev = price
        for (day, repo, sku), u in units.items():
            vis = vis_map.get(repo.lower(), "unknown") if repo != UNATTRIBUTED else "unknown"
            public = vis == "public"
            minutes, gross, net = u["q"], u["g"], u["n"]
            paid, gpaid = u["paid"], u["gpaid"]
            if not assume_current:
                rest_cls = "unknown"
            elif public:
                rest_cls = "public_free"
            elif vis in ("private", "internal"):
                rest_cls = "included"
            else:
                # 削除済み・閲覧権限不足・帰属不明は現在値も無いため確定しない
                rest_cls = "unknown"
            parts = []
            if paid > 0:
                parts.append(("paid", paid, _r(gpaid)))
            # 浮動小数点誤差（net / 単価）で生じる微小な残余は、出力精度で丸めてから判定する
            dep = _r(min(u["dq"], max(minutes - paid, 0.0)))
            dep_gross = _r(u["dg"])
            if dep > 0:
                parts.append(("dependabot_free", dep, dep_gross))
            rest = _r(minutes - paid - dep)
            if rest > 0 or minutes == 0:
                parts.append((rest_cls, max(rest, 0.0), _r(max(gross - gpaid - u["dg"], 0.0))))
            # 有料分が無いときの金額（minutes == 0 を含む）は、最初の非有料行へだけ持たせて二重計上を避ける
            net_given = paid > 0
            for cls, m, gross_c in parts:
                if cls == "paid":
                    net_c = net
                elif net_given:
                    net_c = 0.0
                else:
                    net_c, net_given = net, True
                # quota は含有枠を消費した分だけ。paid 行・Dependabot の無料分・公開リポは 0
                if cls in ("paid", "unknown", "dependabot_free") or public or gross_c <= 0:
                    quota = 0.0
                elif price is None:
                    raise FetchError("%s: Linux 単価を決められず quota を換算できない" % month)
                else:
                    quota = gross_c / price
                rows.append({
                    "day": day, "repo": repo, "visibility": vis, "sku": sku,
                    "runner_type": runner_type_for(sku), "charge_class": cls,
                    "minutes": _r(m), "quota_minutes": _r(quota),
                    "billable_minutes": _r(m) if cls == "paid" else 0.0,
                    "gross_usd": gross_c, "discount_usd": _r(max(gross_c - net_c, 0.0)),
                    "net_usd": _r(net_c),
                })
    rows.sort(key=lambda r: (r["day"], r["repo"], r["sku"], CHARGE_ORDER[r["charge_class"]]))
    return rows, price_map


def summarize(rows, months, out_path, since, until):
    by_cls = {c: [0, 0.0] for c in CHARGE_ORDER}
    by_month = {m: [0.0, 0.0] for m in months}
    quota = 0.0
    for r in rows:
        by_cls[r["charge_class"]][0] += 1
        by_cls[r["charge_class"]][1] += r["minutes"]
        quota += r["quota_minutes"]
        mm = by_month.get(r["day"][:7])
        if mm is not None:
            mm[0] += r["minutes"]
            mm[1] += r["net_usd"]
    shown = months[-SUMMARY_MONTHS_MAX:]
    mtxt = ", ".join("%s=%.1f/%.2f" % (m, by_month[m][0], by_month[m][1]) for m in shown)
    if len(months) > len(shown):
        mtxt += " …他 %d か月" % (len(months) - len(shown))
    return [
        "出力: %s" % out_path,
        "期間: %s..%s（UTC・%d か月）" % (since, until, len(months)),
        "行数: %d（paid %d / included %d / public_free %d / dependabot_free %d / unknown %d）" % (
            len(rows), by_cls["paid"][0], by_cls["included"][0], by_cls["public_free"][0],
            by_cls["dependabot_free"][0], by_cls["unknown"][0]),
        "分数: paid %.1f / included %.1f / public_free %.1f / dependabot_free %.1f / unknown %.1f（quota 換算 %.1f）" % (
            by_cls["paid"][1], by_cls["included"][1], by_cls["public_free"][1],
            by_cls["dependabot_free"][1], by_cls["unknown"][1], quota),
        "月別合計(分/net USD): %s" % mtxt,
    ]


def _csv_num(row, idx, col, lineno, required=True):
    """CSV のセルを有限の非負数にする。エラー文には行番号と列名だけを出し、セルの値は出さない。"""
    raw = row[idx].strip() if idx is not None and idx < len(row) else ""
    if raw == "":
        if required:
            raise FetchError("CSV 行 %d: 列 %s が空" % (lineno, col))
        return 0.0
    try:
        v = float(raw)
    except ValueError:
        raise FetchError("CSV 行 %d: 列 %s が数値ではない" % (lineno, col)) from None
    if not math.isfinite(v) or v < 0:
        raise FetchError("CSV 行 %d: 列 %s が有限の非負数ではない" % (lineno, col))
    return v


def load_usage_csv(path, org, months):
    """請求画面の詳細の使用量 CSV を読み、月別の payload 相当 dict と統計を返す。

    入力は非信頼。必須列の欠落（集計レポートを含む）・数値の不正・上限超過・非通常ファイルは
    FetchError で止める（fail-closed）。username / workflow_path は Dependabot の判定にだけ使い、
    返す明細にもエラー文にも載せない。戻り値の明細は API の usageItems と同じキーに
    `dependabot: bool` を足した形で、select_items にそのまま渡せる。
    """
    if not os.path.isfile(path):
        raise FetchError("--usage-csv が通常ファイルではない")
    try:
        if os.path.getsize(path) > MAX_CSV_BYTES:
            raise FetchError("--usage-csv が大きすぎる（上限 %d MB）" % (MAX_CSV_BYTES // (1024 * 1024)))
        fh = open(path, encoding="utf-8-sig", newline="")
    except OSError as e:
        raise FetchError("--usage-csv を開けない: %s" % safe(e.strerror))
    allowed = set(months)
    payloads = {m: {"usageItems": []} for m in months}
    stats = {"items": 0, "by_username": 0, "by_path_only": 0, "minutes": 0.0,
             "other_org": 0, "out_of_period": 0, "dates": []}
    with fh:
        try:
            reader = csv.reader(fh)
            header = next(reader, None)
            if header is None:
                raise FetchError("--usage-csv が空")
            names = [h.strip().lower() for h in header]
            if "username" not in names or "workflow_path" not in names:
                raise FetchError("--usage-csv に username / workflow_path 列が無く Dependabot を判定できない。"
                                 "請求画面の詳細の使用量レポートを使う")
            missing = [c for c in CSV_REQUIRED if c not in names]
            if missing:
                raise FetchError("--usage-csv に必須列が無い: %s" % ", ".join(missing))
            col = {}
            for c in CSV_REQUIRED + ("discount_amount", "organization"):
                if names.count(c) > 1:
                    raise FetchError("--usage-csv の列 %s が重複している" % c)
                col[c] = names.index(c) if c in names else None

            def cell(row, c):
                i = col[c]
                return row[i].strip() if i is not None and i < len(row) else ""
            n_rows = 0
            for lineno, row in enumerate(reader, start=2):
                if not row:
                    continue
                if len(row) < len(names):
                    # セルが欠けた行は username / workflow_path も欠けうる。空文字扱いで通すと
                    # Dependabot でない行として含有枠を過大計上するため止める（値は出さない）
                    raise FetchError("CSV 行 %d: 列数が %d で、ヘッダーの %d 列に満たない"
                                     % (lineno, len(row), len(names)))
                n_rows += 1
                if n_rows > MAX_CSV_ROWS:
                    raise FetchError("--usage-csv の行数が上限（%d）を超えた" % MAX_CSV_ROWS)
                day = _day_of(cell(row, "date"))
                stats["dates"].append(day)
                if cell(row, "product").lower() != "actions" or cell(row, "unit_type").lower() != "minutes":
                    continue
                org_cell = cell(row, "organization")
                if org_cell and org_cell.lower() != org.lower():
                    stats["other_org"] += 1
                    continue
                month = "%04d-%02d" % (day.year, day.month)
                if month not in allowed:
                    stats["out_of_period"] += 1
                    continue
                q = _csv_num(row, col["quantity"], "quantity", lineno)
                it = {
                    "date": day.isoformat(), "product": "Actions", "sku": cell(row, "sku"),
                    "quantity": q, "unitType": "Minutes",
                    "pricePerUnit": _csv_num(row, col["applied_cost_per_quantity"],
                                             "applied_cost_per_quantity", lineno),
                    "grossAmount": _csv_num(row, col["gross_amount"], "gross_amount", lineno),
                    "discountAmount": _csv_num(row, col["discount_amount"], "discount_amount", lineno, False),
                    "netAmount": _csv_num(row, col["net_amount"], "net_amount", lineno),
                    "repositoryName": cell(row, "repository"),
                }
                by_user = cell(row, "username") == DEPENDABOT_USER
                by_path = cell(row, "workflow_path").startswith(DEPENDABOT_PATH_PREFIX)
                it["dependabot"] = by_user or by_path
                if it["dependabot"]:
                    stats["items"] += 1
                    stats["minutes"] += q
                    if by_user:
                        stats["by_username"] += 1
                    else:
                        stats["by_path_only"] += 1
                payloads[month]["usageItems"].append(it)
        except csv.Error:
            raise FetchError("--usage-csv を CSV として解析できない") from None
        except UnicodeDecodeError:
            raise FetchError("--usage-csv が UTF-8 ではない") from None
    return payloads, stats


def csv_warnings(stats, months, today):
    """CSV 経路の warnings。件数・日付・固定文言だけにし、利用者名・org 名・リポ名・金額を含めない。"""
    w = ["使用量 CSV から Dependabot のジョブを %d 明細・%.1f 分と判定し、含有枠の消費から除いた"
         "（username 一致 %d / workflow_path のみ %d）" % (
             stats["items"], stats["minutes"], stats["by_username"], stats["by_path_only"])]
    if stats["other_org"]:
        w.append("他の org の行 %d 件を捨てた" % stats["other_org"])
    if stats["out_of_period"]:
        w.append("対象期間外の明細 %d 件を捨てた" % stats["out_of_period"])
    y, m = int(months[0][:4]), int(months[0][5:])
    start = date(y, m, 1)
    ye, me = int(months[-1][:4]), int(months[-1][5:])
    end = date(ye + (me == 12), 1 if me == 12 else me + 1, 1).toordinal() - 1
    end = min(date.fromordinal(end), today)
    if stats["dates"]:
        lo, hi = min(stats["dates"]), max(stats["dates"])
        if lo > start or hi < end:
            w.append("使用量 CSV の日付範囲 %s..%s が指定期間（%s..%s）の一部だけで、範囲外の日は 0 ではなく未取得"
                     % (lo, hi, start, end))
    else:
        w.append("使用量 CSV に明細が無く、指定期間はすべて未取得")
    return w


def fetch_visibility(org):
    text = gh_api(["--paginate", "orgs/%s/repos?per_page=100&type=all" % org])
    out = {}
    repos, _ = parse_concatenated_json(text)
    for repo in repos:
        if not isinstance(repo, dict) or not isinstance(repo.get("name"), str):
            continue
        vis = repo.get("visibility")
        if not isinstance(vis, str):
            vis = "private" if repo.get("private") else "public"
        out[repo["name"]] = vis.lower()
    return out


def fetch_month(org, month):
    y, m = int(month[:4]), int(month[5:])
    text = gh_api(["-X", "GET", "organizations/%s/settings/billing/usage?year=%d&month=%d" % (org, y, m)])
    try:
        return json.loads(text)
    except ValueError:
        raise FetchError("%s: 応答を JSON として解析できない" % month)


def main(argv=None):
    ap = argparse.ArgumentParser(description="billing usage API から GitHub ホストランナーの日次行を作る")
    ap.add_argument("--org", required=True)
    ap.add_argument("--since", required=True, help="YYYY-MM")
    ap.add_argument("--until", help="YYYY-MM（既定は UTC の当月）")
    ap.add_argument("--out", required=True)
    ap.add_argument("--assume-current-visibility", action="store_true",
                    help="期間中に公開範囲が変わっていない前提で、現在の公開範囲により included / public_free を確定する")
    ap.add_argument("--usage-csv", help="請求画面の詳細の使用量 CSV。billing usage API の代わりに読み、"
                    "Dependabot の分を dependabot_free に分ける")
    a = ap.parse_args(argv)
    now = datetime.now(timezone.utc)
    current = "%04d-%02d" % (now.year, now.month)
    until = a.until or current
    try:
        if not ORG_RE.match(a.org):
            raise FetchError("--org が不正")
        if not MONTH_RE.match(a.since) or not MONTH_RE.match(until):
            raise FetchError("--since / --until は YYYY-MM 形式で指定する")
        if not a.since <= until <= current:
            raise FetchError("期間は since <= until <= 当月（UTC）で指定する")
        sy, sm = int(a.since[:4]), int(a.since[5:7])
        uy, um = int(until[:4]), int(until[5:7])
        if (uy * 12 + um) - (sy * 12 + sm) + 1 > MAX_MONTHS:
            raise FetchError("期間は最大 %d か月（since から until まで）" % MAX_MONTHS)
        if os.path.isdir(a.out):
            raise FetchError("--out がディレクトリを指している")
        if not os.path.isdir(os.path.dirname(os.path.abspath(a.out))):
            raise FetchError("--out の親ディレクトリが存在しない")
        months = month_range(a.since, until)
        extra = {"input": "api"}
        warnings = []
        if a.usage_csv:
            # CSV の検証は gh を呼ぶ前に済ませ、不正な入力でネットワークへ出ない
            payloads, cstats = load_usage_csv(a.usage_csv, a.org, months)
            warnings = csv_warnings(cstats, months, now.date())
            extra = {"input": "usage_csv", "dependabot": {
                "rule": DEPENDABOT_RULE, "items": cstats["items"],
                "by_username": cstats["by_username"],
                "by_workflow_path_only": cstats["by_path_only"],
                "minutes": round(cstats["minutes"], 6)}}
            visibility = fetch_visibility(a.org)
        else:
            visibility = fetch_visibility(a.org)
            payloads = {}
            for m in months:
                try:
                    payloads[m] = fetch_month(a.org, m)
                except FetchError as e:
                    raise FetchError("%s の取得に失敗: %s" % (m, e))
        rows, price_map = build_rows(payloads, visibility, a.org, a.assume_current_visibility)
        doc = {
            "schema_version": SCHEMA_VERSION, "source": "billing", "org": a.org,
            "since": a.since, "until": until,
            "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "assume_current_visibility": bool(a.assume_current_visibility),
            "linux_price_per_minute": price_map, "rows": rows,
        }
        doc.update(extra)
        if warnings:
            doc["warnings"] = warnings
        atomic_write_json(a.out, doc)
    except FetchError as e:
        print("エラー: %s" % e, file=sys.stderr)
        return 1
    except OSError as e:
        print("エラー: 書き込みに失敗: %s" % e.strerror, file=sys.stderr)
        return 1
    print("\n".join(summarize(rows, months, a.out, a.since, until)
                    + ["警告: %s" % safe(w, 200) for w in warnings]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
