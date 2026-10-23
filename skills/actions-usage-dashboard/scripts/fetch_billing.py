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
   linux_price_per_minute: {YYYY-MM: 単価}, rows: [...]}
  row = day / repo / visibility / sku / runner_type / charge_class / minutes /
        quota_minutes / billable_minutes / gross_usd / discount_usd / net_usd
  charge_class: paid（金額が発生した分）/ included（含有枠）/ public_free（公開リポの無料分）/
  unknown（利用日時点の公開範囲を確認できない非有料分）。
  有料分は公開範囲ではなく netAmount から判定する。非有料分の included / public_free は
  利用日時点の公開範囲が前提だが、API が返すのは現在の公開範囲だけで履歴を取れない。
  公開範囲が途中で変わったリポジトリの過去分を誤分類（quota の消失・過大計上）しないよう、
  既定では非有料分を unknown とし quota_minutes を 0 にする。公開範囲が期間中に
  変わっていないと利用者が保証できる場合だけ --assume-current-visibility で現在値を採る。

アクセス: gh api 経由のみ。トークンは扱わない。標準ライブラリだけで動く。
"""
import argparse
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

SCHEMA_VERSION = 1
GH_TIMEOUT = 120
ORG_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})\Z")
MONTH_RE = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])\Z")
LINUX_PRICE_SKU = "actions linux"
UNATTRIBUTED = "(unattributed)"
# 区分の並び順（行の決定的ソートにも使う）
CHARGE_ORDER = {"paid": 0, "included": 1, "public_free": 2, "unknown": 3}
# SKU（正規化後）に対する runner_type の対応表。上から順に照合し、最初に当たったものを採る。
# 実データで SKU 表記が異なる場合はここのキーワードだけを直す。
RUNNER_TYPE_RULES = (
    (("linux", "arm"), "ubuntu-arm"),
    (("linux",), "ubuntu"),
    (("macos",), "macos"),
    (("windows",), "windows"),
)
SUMMARY_MONTHS_MAX = 12


class FetchError(Exception):
    """取得・検証の失敗。main が日本語 1 行にして終了コード 1 で終える。"""


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


def parse_concatenated_json(text):
    """gh --paginate が連結した JSON 配列（[..][..]）を 1 本のリストにする。"""
    dec, i, n, out = json.JSONDecoder(), 0, len(text), []
    while i < n:
        while i < n and text[i].isspace():
            i += 1
        if i >= n:
            break
        try:
            obj, i = dec.raw_decode(text, i)
        except ValueError:
            raise FetchError("リポジトリ一覧の JSON を解析できない")
        if not isinstance(obj, list):
            raise FetchError("リポジトリ一覧が配列ではない")
        out.extend(obj)
    return out


def normalize_repo(name, org):
    if not isinstance(name, str) or not name.strip():
        return UNATTRIBUTED
    name = name.strip()
    prefix = org.lower() + "/"
    if name.lower().startswith(prefix):
        name = name[len(prefix):]
    return name or UNATTRIBUTED


def _num(item, key, required=True):
    v = item.get(key)
    if v is None and not required:
        return 0.0
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
        raise FetchError("API の数値フィールド %s が不正（有限の非負数ではない）" % key)
    return float(v)


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
        q, g = _num(it, "quantity"), _num(it, "grossAmount")
        d, n = _num(it, "discountAmount", False), _num(it, "netAmount")
        p = _num(it, "pricePerUnit")
        key = (day.isoformat(), normalize_repo(it.get("repositoryName"), org), sku)
        u = units.setdefault(key, {"q": 0.0, "g": 0.0, "d": 0.0, "n": 0.0, "price": 0.0,
                                   "paid": 0.0, "gpaid": 0.0})
        # 単価が異なる明細を平均単価で割ると有料分数が狂うため、元明細ごとに有料分と
        # その按分 gross を求めてから集約する。
        paid_i = min(max(n / p, 0.0), q) if p > 0 else 0.0
        u["paid"] += paid_i
        u["gpaid"] += g * paid_i / q if q > 0 else 0.0
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
                parts.append(("paid", paid))
            # 浮動小数点誤差（net / 単価）で生じる微小な残余は、出力精度で丸めてから判定する
            rest = _r(minutes - paid)
            if rest > 0 or minutes == 0:
                parts.append((rest_cls, max(rest, 0.0)))
            for cls, m in parts:
                if cls == "paid":
                    net_c, gross_c = net, _r(gpaid)
                else:
                    # 有料分が無い行（minutes == 0 を含む）は金額をそのまま持たせる
                    net_c = net if paid <= 0 else 0.0
                    gross_c = _r(gross if paid <= 0 else max(gross - gpaid, 0.0))
                # quota は含有枠を消費した分だけ。paid 行と公開リポは 0
                if cls in ("paid", "unknown") or public or gross_c <= 0:
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
        "行数: %d（paid %d / included %d / public_free %d / unknown %d）" % (
            len(rows), by_cls["paid"][0], by_cls["included"][0], by_cls["public_free"][0],
            by_cls["unknown"][0]),
        "分数: paid %.1f / included %.1f / public_free %.1f / unknown %.1f（quota 換算 %.1f）" % (
            by_cls["paid"][1], by_cls["included"][1], by_cls["public_free"][1],
            by_cls["unknown"][1], quota),
        "月別合計(分/net USD): %s" % mtxt,
    ]


def gh_api(args):
    try:
        r = subprocess.run(["gh", "api"] + args, capture_output=True, text=True,
                           timeout=GH_TIMEOUT, shell=False)
    except FileNotFoundError:
        raise FetchError("gh コマンドが見つからない")
    except subprocess.TimeoutExpired:
        raise FetchError("gh api が %d 秒でタイムアウト" % GH_TIMEOUT)
    if r.returncode != 0:
        lines = (r.stderr or "").strip().splitlines()
        raise FetchError("gh api 失敗: %s" % (lines[0][:200] if lines else "exit %d" % r.returncode))
    return r.stdout


def fetch_visibility(org):
    text = gh_api(["--paginate", "orgs/%s/repos?per_page=100&type=all" % org])
    out = {}
    for repo in parse_concatenated_json(text):
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


def atomic_write_json(path, obj):
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=".%s." % os.path.basename(path), suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def main(argv=None):
    ap = argparse.ArgumentParser(description="billing usage API から GitHub ホストランナーの日次行を作る")
    ap.add_argument("--org", required=True)
    ap.add_argument("--since", required=True, help="YYYY-MM")
    ap.add_argument("--until", help="YYYY-MM（既定は UTC の当月）")
    ap.add_argument("--out", required=True)
    ap.add_argument("--assume-current-visibility", action="store_true",
                    help="期間中に公開範囲が変わっていない前提で、現在の公開範囲により included / public_free を確定する")
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
        if os.path.isdir(a.out):
            raise FetchError("--out がディレクトリを指している")
        if not os.path.isdir(os.path.dirname(os.path.abspath(a.out))):
            raise FetchError("--out の親ディレクトリが存在しない")
        months = month_range(a.since, until)
        visibility = fetch_visibility(a.org)
        payloads = {}
        for m in months:
            try:
                payloads[m] = fetch_month(a.org, m)
            except FetchError as e:
                raise FetchError("%s の取得に失敗: %s" % (m, e))
        rows, price_map = build_rows(payloads, visibility, a.org, a.assume_current_visibility)
        atomic_write_json(a.out, {
            "schema_version": SCHEMA_VERSION, "source": "billing", "org": a.org,
            "since": a.since, "until": until,
            "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "linux_price_per_minute": price_map, "rows": rows,
        })
    except FetchError as e:
        print("エラー: %s" % e, file=sys.stderr)
        return 1
    except OSError as e:
        print("エラー: 書き込みに失敗: %s" % e.strerror, file=sys.stderr)
        return 1
    print("\n".join(summarize(rows, months, a.out, a.since, until)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
