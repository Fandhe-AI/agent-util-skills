#!/usr/bin/env python3
"""fetch_billing.py の回帰テスト。

役割: 区分展開（paid / included / public_free）・quota 換算・runner_type の対応と、
gh をスタブにした CLI 全体の挙動（月の取得失敗時に出力が作られないこと、標準出力が
10 行以内であること）を固定する。CI（ci.yml の skill-tests ジョブ）で `python3 -I -B -m unittest discover` により直接実行される。

fixture はすべて合成データ（org は acme-synth）。本リポは PUBLIC のため、実在の
非公開リポ名や金額を入れない。
"""
import json
from datetime import datetime, timezone
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "scripts", "fetch_billing.py")
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))

import fetch_billing as fb  # noqa: E402

ORG = "acme-synth"
VISIBILITY = {"pub-site": "public", "priv-api": "private", "int-tools": "internal"}
REPOS_PAGES = [
    [{"name": "pub-site", "visibility": "public"}, {"name": "priv-api", "visibility": "private"}],
    [{"name": "int-tools", "visibility": "internal"}],
]


def item(date, repo, sku, qty, price, gross, net, discount=None, product="Actions", unit="Minutes"):
    return {
        "date": date + "T00:00:00Z", "product": product, "sku": sku, "quantity": qty,
        "unitType": unit, "pricePerUnit": price, "grossAmount": gross,
        "discountAmount": gross - net if discount is None else discount, "netAmount": net,
        "organizationName": ORG, "repositoryName": repo,
    }


PAYLOADS = {
    "2026-07": {"usageItems": [
        item("2026-07-01", "priv-api", "Actions Linux", 100, 0.008, 0.8, 0.0),
        item("2026-07-01", "priv-api", "Actions Linux", 50, 0.008, 0.4, 0.0),  # 重複明細
        item("2026-07-02", "int-tools", "Actions Linux", 10, 0.008, 0.08, 0.0),
        item("2026-07-02", "pub-site", "Actions Linux", 20, 0.008, 0.16, 0.0),
        item("2026-07-03", "gone-repo", "Actions Linux", 5, 0.008, 0.04, 0.0),
        item("2026-07-03", "priv-api", "Actions storage", 3, 0.0, 0.0, 0.0, unit="GigabyteHours"),
        item("2026-07-03", "priv-api", "Copilot premium", 7, 0.04, 0.28, 0.0, product="Copilot"),
    ]},
    "2026-08": {"usageItems": [  # Linux 行なし: 前月の単価を引き継ぐ
        item("2026-08-01", "acme-synth/priv-api", "Actions macOS 3-core", 10, 0.08, 0.8, 0.0),
        item("2026-08-01", "priv-api", "Actions Windows", 10, 0.016, 0.16, 0.0),
    ]},
    "2026-09": {"usageItems": [
        item("2026-09-01", "priv-api", "Actions Linux", 100, 0.008, 0.8, 0.3, discount=0.5),
        item("2026-09-02", "pub-site", "Actions Linux 4-core", 40, 0.016, 0.64, 0.32, discount=0.32),
        item("2026-09-03", "priv-api", "Actions Linux Arm 2-core", 10, 0.005, 0.05, 0.0),
        item("2026-09-03", "priv-api", "Actions Zero", 0, 0.0, 0.0, 0.0),
    ]},
}


def index(rows):
    return {(r["day"], r["repo"], r["sku"], r["charge_class"]): r for r in rows}


class BuildRowsTest(unittest.TestCase):
    def setUp(self):
        self.rows, self.prices = fb.build_rows(PAYLOADS, VISIBILITY, ORG, True)
        self.idx = index(self.rows)

    def test_minutes_sum_matches_source_per_unit(self):
        expect = {
            ("2026-07-01", "priv-api", "Actions Linux"): 150,
            ("2026-09-01", "priv-api", "Actions Linux"): 100,
            ("2026-09-02", "pub-site", "Actions Linux 4-core"): 40,
        }
        for key, total in expect.items():
            got = sum(r["minutes"] for r in self.rows if (r["day"], r["repo"], r["sku"]) == key)
            self.assertAlmostEqual(got, total, places=6, msg=str(key))

    def test_charge_classes(self):
        i = self.idx
        self.assertAlmostEqual(i[("2026-07-01", "priv-api", "Actions Linux", "included")]["minutes"], 150)
        self.assertAlmostEqual(i[("2026-07-02", "pub-site", "Actions Linux", "public_free")]["minutes"], 20)
        self.assertAlmostEqual(i[("2026-07-02", "int-tools", "Actions Linux", "included")]["minutes"], 10)
        # 金額が発生した分: 0.3 / 0.008 = 37.5 分が paid、残りが included
        paid = i[("2026-09-01", "priv-api", "Actions Linux", "paid")]
        self.assertAlmostEqual(paid["minutes"], 37.5)
        self.assertAlmostEqual(paid["billable_minutes"], 37.5)
        self.assertAlmostEqual(i[("2026-09-01", "priv-api", "Actions Linux", "included")]["minutes"], 62.5)
        self.assertEqual(i[("2026-09-01", "priv-api", "Actions Linux", "included")]["billable_minutes"], 0)
        # 公開リポでも有料分は paid（公開範囲だけでは決めない）
        pub_paid = i[("2026-09-02", "pub-site", "Actions Linux 4-core", "paid")]
        self.assertAlmostEqual(pub_paid["minutes"], 20)
        self.assertAlmostEqual(i[("2026-09-02", "pub-site", "Actions Linux 4-core", "public_free")]["minutes"], 20)

    def test_quota_minutes(self):
        i = self.idx
        self.assertAlmostEqual(i[("2026-07-01", "priv-api", "Actions Linux", "included")]["quota_minutes"], 150)
        self.assertEqual(i[("2026-07-02", "pub-site", "Actions Linux", "public_free")]["quota_minutes"], 0)
        # 8 月は Linux 行が無く、7 月の単価 0.008 を引き継ぐ。macOS は 0.8 / 0.008 = 100、Windows は 20
        self.assertEqual(self.prices["2026-08"], 0.008)
        self.assertAlmostEqual(i[("2026-08-01", "priv-api", "Actions macOS 3-core", "included")]["quota_minutes"], 100)
        self.assertAlmostEqual(i[("2026-08-01", "priv-api", "Actions Windows", "included")]["quota_minutes"], 20)
        # 含有枠を消費した分だけを換算する。paid 行は 0、included 行は割引分 0.5 / 0.008 = 62.5
        rows9 = [r for r in self.rows if r["day"] == "2026-09-01" and r["repo"] == "priv-api"
                 and r["sku"] == "Actions Linux"]
        self.assertAlmostEqual(sum(r["quota_minutes"] for r in rows9), 62.5)
        self.assertEqual([r["quota_minutes"] for r in rows9 if r["charge_class"] == "paid"], [0.0])

    def test_visibility_change_not_decided_by_current_value(self):
        # 現在は public でも過去は private だった可能性があるため、既定では確定しない
        rows, _ = fb.build_rows(PAYLOADS, VISIBILITY, ORG)
        for r in rows:
            self.assertNotIn(r["charge_class"], ("included", "public_free"))
            if r["charge_class"] == "unknown":
                self.assertEqual(r["quota_minutes"], 0.0)
        self.assertEqual(sum(r["minutes"] for r in rows if r["day"] == "2026-07-02" and r["repo"] == "pub-site"), 20)
        # 有料分は公開範囲に依らず paid のまま
        self.assertIn(("2026-09-01", "priv-api", "Actions Linux", "paid"), index(rows))

    def test_cli_flag_assumes_current_visibility(self):
        with self.subTest("flag"):
            rows, _ = fb.build_rows(PAYLOADS, VISIBILITY, ORG, True)
            self.assertIn(("2026-07-02", "pub-site", "Actions Linux", "public_free"), index(rows))

    def test_assume_current_keeps_unknown_visibility_unknown(self):
        # 公開範囲を引けないリポジトリはフラグ指定時も unknown のまま quota へ計上しない
        payloads = {"2026-07": {"usageItems": [
            item("2026-07-01", "gone-repo", "Actions Linux", 10, 0.008, 0.08, 0.0),
            item("2026-07-01", "priv-api", "Actions Linux", 10, 0.008, 0.08, 0.0),
        ]}}
        rows, _ = fb.build_rows(payloads, VISIBILITY, ORG, True)
        idx = index(rows)
        self.assertEqual(idx[("2026-07-01", "gone-repo", "Actions Linux", "unknown")]["quota_minutes"], 0.0)
        self.assertIn(("2026-07-01", "priv-api", "Actions Linux", "included"), idx)
        self.assertFalse([r for r in rows if r["repo"] == "gone-repo" and r["charge_class"] != "unknown"])

    def test_runner_type(self):
        expect = {
            "Actions Linux": "ubuntu", "Actions Linux 4-core": "ubuntu",
            "Actions Linux Arm 2-core": "ubuntu-arm", "Actions macOS 3-core": "macos",
            "Actions Windows": "windows", "Actions Zero": "other",
        }
        for r in self.rows:
            self.assertEqual(r["runner_type"], expect[r["sku"]], r["sku"])

    def test_amounts_sum_back(self):
        def total(key, day, repo, sku):
            return sum(r[key] for r in self.rows if (r["day"], r["repo"], r["sku"]) == (day, repo, sku))
        self.assertAlmostEqual(total("gross_usd", "2026-09-01", "priv-api", "Actions Linux"), 0.8)
        self.assertAlmostEqual(total("net_usd", "2026-09-01", "priv-api", "Actions Linux"), 0.3)
        self.assertAlmostEqual(total("discount_usd", "2026-09-01", "priv-api", "Actions Linux"), 0.5)
        self.assertAlmostEqual(total("gross_usd", "2026-09-02", "pub-site", "Actions Linux 4-core"), 0.64)
        self.assertAlmostEqual(total("net_usd", "2026-09-02", "pub-site", "Actions Linux 4-core"), 0.32)

    def test_visibility_and_prefix(self):
        self.assertEqual(self.idx[("2026-07-03", "gone-repo", "Actions Linux", "unknown")]["visibility"], "unknown")
        self.assertEqual(self.idx[("2026-07-02", "int-tools", "Actions Linux", "included")]["visibility"], "internal")
        self.assertIn(("2026-08-01", "priv-api", "Actions macOS 3-core", "included"), self.idx)  # org/ を除去

    def test_zero_minutes_unit_kept_once(self):
        zero = [r for r in self.rows if r["sku"] == "Actions Zero"]
        self.assertEqual(len(zero), 1)
        self.assertEqual(zero[0]["charge_class"], "included")
        self.assertEqual(zero[0]["minutes"], 0)

    def test_mixed_unit_prices_keep_paid_minutes(self):
        # 単価の異なる明細は元明細ごとに有料分を求める（平均単価で割ると 6.67 分に狂う）
        pay = {"2026-07": {"usageItems": [
            item("2026-07-01", "priv-api", "Actions Linux", 10, 0.008, 0.08, 0.08),
            item("2026-07-01", "priv-api", "Actions Linux", 10, 0.016, 0.16, 0.0),
        ]}}
        rows, _ = fb.build_rows(pay, VISIBILITY, ORG, True)
        by = {r["charge_class"]: r for r in rows}
        self.assertEqual(by["paid"]["minutes"], 10)
        self.assertEqual(by["paid"]["billable_minutes"], 10)
        self.assertEqual(by["paid"]["net_usd"], 0.08)
        self.assertEqual(by["included"]["minutes"], 10)
        self.assertEqual(by["included"]["billable_minutes"], 0)
        self.assertEqual(round(by["paid"]["gross_usd"] + by["included"]["gross_usd"], 6), 0.24)

    def test_mixed_unit_prices_order_independent(self):
        # 返却順を入れ替えても換算単価と quota が変わらない
        a = item("2026-07-01", "priv-api", "Actions Linux", 10, 0.008, 0.08, 0.08)
        b = item("2026-07-01", "priv-api", "Actions Linux", 10, 0.016, 0.16, 0.0)
        r1, p1 = fb.build_rows({"2026-07": {"usageItems": [a, b]}}, VISIBILITY, ORG, True)
        r2, p2 = fb.build_rows({"2026-07": {"usageItems": [b, a]}}, VISIBILITY, ORG, True)
        self.assertEqual(r1, r2)
        self.assertEqual(p1, p2)
        self.assertEqual(p1["2026-07"], 0.008)
        inc = [r for r in r1 if r["charge_class"] == "included"][0]
        self.assertEqual(inc["quota_minutes"], 20)

    def test_excluded_and_deterministic_order(self):
        self.assertFalse([r for r in self.rows if r["sku"] in ("Actions storage", "Copilot premium")])
        keys = [(r["day"], r["repo"], r["sku"], fb.CHARGE_ORDER[r["charge_class"]]) for r in self.rows]
        self.assertEqual(keys, sorted(keys))
        again, _ = fb.build_rows(dict(reversed(list(PAYLOADS.items()))), VISIBILITY, ORG, True)
        self.assertEqual(again, self.rows)

    def test_missing_linux_price_errors_only_when_needed(self):
        only_aug = {"2026-08": PAYLOADS["2026-08"]}
        with self.assertRaises(fb.FetchError):
            fb.build_rows(only_aug, VISIBILITY, ORG, True)
        public_only = {"2026-08": {"usageItems": [item("2026-08-01", "pub-site", "Actions macOS", 5, 0.08, 0.4, 0.0)]}}
        rows, _ = fb.build_rows(public_only, VISIBILITY, ORG, True)
        self.assertEqual(rows[0]["charge_class"], "public_free")

    def test_invalid_numbers_rejected(self):
        for bad in (-1, float("nan"), True, "5", None):
            payload = {"2026-07": {"usageItems": [item("2026-07-01", "priv-api", "Actions Linux", bad, 0.008, 0.8, 0.0)]}}
            with self.assertRaises(fb.FetchError, msg=repr(bad)):
                fb.build_rows(payload, VISIBILITY, ORG, True)

    def test_helpers(self):
        self.assertEqual(fb.month_range("2025-11", "2026-02"), ["2025-11", "2025-12", "2026-01", "2026-02"])
        items, pages = fb.parse_concatenated_json('[{"a":1}]\n[{"a":2}][]')
        self.assertEqual(len(items), 2)
        self.assertEqual(pages, 3)


STUB = """#!{py}
import os, re, sys
d = os.environ["STUB_DIR"]
args = sys.argv[1:]
with open(os.path.join(d, "calls.log"), "a") as f:
    f.write(" ".join(args) + "\\n")
path = args[-1]
if "/repos" in path:
    sys.stdout.write(open(os.path.join(d, "repos.txt")).read())
    sys.exit(0)
m = re.search(r"year=(\\d+)&month=(\\d+)", path)
ym = "%04d-%02d" % (int(m.group(1)), int(m.group(2)))
if os.environ.get("STUB_FAIL_MONTH") == ym:
    sys.stderr.write("HTTP 500: synthetic failure\\n")
    sys.exit(1)
p = os.path.join(d, "usage-" + ym + ".json")
sys.stdout.write(open(p).read() if os.path.exists(p) else '{"usageItems": []}')
"""


class FloatEdgeTest(unittest.TestCase):
    def build(self, qty, price, gross, net, repo="priv-api"):
        payload = {"2026-10": {"usageItems": [item("2026-10-01", repo, "Actions Linux", qty, price, gross, net)]}}
        rows, _ = fb.build_rows(payload, VISIBILITY, ORG, True)
        return rows

    def test_fully_paid_has_no_ghost_row_and_zero_quota(self):
        # 0.8 / 0.008 と 0.344 / 0.008 は浮動小数点誤差で残余が正になる
        for qty, gross in ((100, 0.8), (43, 0.344), (29, 0.232)):
            rows = self.build(qty, 0.008, gross, gross)
            self.assertEqual([r["charge_class"] for r in rows], ["paid"], qty)
            self.assertEqual(rows[0]["quota_minutes"], 0.0, qty)
            self.assertAlmostEqual(rows[0]["minutes"], qty)


class TrailingNewlineTest(unittest.TestCase):
    """Python の `$` は末尾の改行の手前にも一致する。検証用の正規表現は `\\Z` で終端する。"""

    def test_org_and_month_reject_trailing_newline(self):
        self.assertIsNotNone(fb.ORG_RE.match("acme-synth"))
        self.assertIsNone(fb.ORG_RE.match("acme-synth\n"))
        self.assertIsNotNone(fb.MONTH_RE.match("2026-09"))
        self.assertIsNone(fb.MONTH_RE.match("2026-09\n"))


class CliStubTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.stub_dir = os.path.join(self._tmp.name, "stub")
        self.bin = os.path.join(self._tmp.name, "bin")
        self.work = os.path.join(self._tmp.name, "work")
        for d in (self.stub_dir, self.bin, self.work):
            os.mkdir(d)
        gh = os.path.join(self.bin, "gh")
        with open(gh, "w") as f:
            f.write(STUB.replace("{py}", sys.executable))
        os.chmod(gh, 0o755)
        with open(os.path.join(self.stub_dir, "repos.txt"), "w") as f:
            f.write("".join(json.dumps(p) + "\n" for p in REPOS_PAGES))
        for ym, payload in PAYLOADS.items():
            with open(os.path.join(self.stub_dir, "usage-%s.json" % ym), "w") as f:
                json.dump(payload, f)
        self.out = os.path.join(self.work, "billing.json")

    def run_cli(self, *args, fail_month=None):
        env = dict(os.environ, PATH=self.bin + os.pathsep + os.environ["PATH"], STUB_DIR=self.stub_dir)
        env.pop("STUB_FAIL_MONTH", None)
        if fail_month:
            env["STUB_FAIL_MONTH"] = fail_month
        return subprocess.run([sys.executable, "-I", "-B", SCRIPT] + list(args),
                              capture_output=True, text=True, env=env, timeout=60)

    def calls(self):
        p = os.path.join(self.stub_dir, "calls.log")
        if not os.path.exists(p):
            return []
        with open(p) as f:
            return f.read().splitlines()

    def test_success(self):
        r = self.run_cli("--org", ORG, "--since", "2026-07", "--until", "2026-09", "--out", self.out)
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(self.out) as f:
            data = json.load(f)
        self.assertEqual(data["schema_version"], 1)
        self.assertEqual(data["source"], "billing")
        self.assertIs(data["assume_current_visibility"], False)
        rows, _ = fb.build_rows(PAYLOADS, VISIBILITY, ORG)
        self.assertEqual(data["rows"], rows)
        self.assertEqual(data["linux_price_per_minute"]["2026-08"], 0.008)
        self.assertLessEqual(len(r.stdout.splitlines()), 10)
        self.assertNotIn("priv-api", r.stdout)
        self.assertEqual([f for f in os.listdir(self.work)], ["billing.json"])

    def test_success_records_assume_current_flag(self):
        r = self.run_cli("--org", ORG, "--since", "2026-07", "--until", "2026-09", "--out", self.out,
                         "--assume-current-visibility")
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(self.out) as f:
            self.assertIs(json.load(f)["assume_current_visibility"], True)

    def test_month_failure_leaves_no_output(self):
        r = self.run_cli("--org", ORG, "--since", "2026-07", "--until", "2026-09", "--out", self.out,
                         fail_month="2026-08")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("2026-08", r.stderr)
        self.assertEqual(os.listdir(self.work), [])

    def test_month_failure_keeps_existing_output(self):
        with open(self.out, "wb") as f:
            f.write(b"previous\n")
        r = self.run_cli("--org", ORG, "--since", "2026-07", "--until", "2026-09", "--out", self.out,
                         fail_month="2026-09")
        self.assertNotEqual(r.returncode, 0)
        with open(self.out, "rb") as f:
            self.assertEqual(f.read(), b"previous\n")
        self.assertEqual(os.listdir(self.work), ["billing.json"])

    def test_long_range_stdout_within_10_lines(self):
        r = self.run_cli("--org", ORG, "--since", "2025-08", "--until", "2026-09", "--out", self.out)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertLessEqual(len(r.stdout.splitlines()), 10)
        self.assertIn("他 2 か月", r.stdout)

    def test_argument_validation_never_calls_gh(self):
        cases = [
            ["--org", "../x", "--since", "2026-07", "--out", self.out],
            ["--org", "a b", "--since", "2026-07", "--out", self.out],
            ["--org", ORG, "--since", "2026-7", "--out", self.out],
            ["--org", ORG, "--since", "2026-09", "--until", "2026-07", "--out", self.out],
            ["--org", ORG, "--since", "2026-07", "--until", "2999-01", "--out", self.out],
            ["--org", ORG, "--since", "2026-07", "--out", self.work],
            ["--org", ORG, "--since", "2026-07", "--out", os.path.join(self.work, "nodir", "x.json")],
        ]
        for args in cases:
            r = self.run_cli(*args)
            self.assertNotEqual(r.returncode, 0, args)
        self.assertEqual(self.calls(), [])
        self.assertEqual(os.listdir(self.work), [])


class PeriodLimitTests(CliStubTest):
    def test_over_max_months_never_calls_gh(self):
        r = self.run_cli("--org", ORG, "--since", "0001-01", "--out", self.out)
        self.assertEqual(r.returncode, 1)
        self.assertIn("24", r.stderr)
        self.assertEqual(self.calls(), [])
        self.assertEqual(os.listdir(self.work), [])

    def test_boundary_24_and_25_months(self):
        now = datetime.now(timezone.utc)
        idx = now.year * 12 + now.month - 1

        def ym(back):
            i = idx - back
            return "%04d-%02d" % (i // 12, i % 12 + 1)
        r = self.run_cli("--org", ORG, "--since", ym(25), "--out", self.out)
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.calls(), [])
        r = self.run_cli("--org", ORG, "--since", ym(23), "--out", self.out)
        self.assertNotIn("最大 24", r.stderr)


CSV_HEADER = ["date", "product", "sku", "quantity", "unit_type", "applied_cost_per_quantity",
              "gross_amount", "discount_amount", "net_amount", "organization", "repository",
              "cost_center_name", "username", "workflow_path"]
USER_ALICE = "alice-synth"
PATH_CI = ".github/workflows/ci-synth.yml"
PATH_DEP = "dynamic/dependabot/updates-synth"
TODAY = datetime(2026, 7, 31, tzinfo=timezone.utc).date()


# CSV の SKU 表記から billing usage API の表記への対応（テスト側で独立に持つ。fb の写像表をそのまま使わない）
API_SKU = {"actions_linux": "Actions Linux", "actions_linux_slim": "Actions Linux Slim",
           "actions_linux_arm": "Actions Linux ARM", "actions_windows": "Actions Windows",
           "actions_macos": "Actions macOS 3-core"}


def crow(date, repo, qty, price, gross, net, user=USER_ALICE, path=PATH_CI, sku="actions_linux",
         product="Actions", unit="Minutes", org=ORG, discount=None):
    d = gross - net if discount is None else discount
    return [date, product, sku, str(qty), unit, str(price), str(gross), str(d), str(net), org, repo, "", user, path]


def write_csv(path, rows, header=CSV_HEADER, bom=False):
    import csv as _csv
    with open(path, "w", encoding="utf-8-sig" if bom else "utf-8", newline="") as f:
        w = _csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def api_payload(rows):
    """CSV 行（crow）と同じ明細を API の usageItems にしたもの（等価性の検証用）。"""
    items = []
    for r in rows:
        items.append({"date": r[0] + "T00:00:00Z", "product": r[1], "sku": API_SKU.get(r[2], r[2]),
                      "quantity": float(r[3]),
                      "unitType": r[4], "pricePerUnit": float(r[5]), "grossAmount": float(r[6]),
                      "discountAmount": float(r[7]), "netAmount": float(r[8]), "repositoryName": r[10]})
    return {"usageItems": items}


class UsageCsvLoadTest(unittest.TestCase):
    """Issue #228: 使用量 CSV の読み込み・Dependabot 判定・fail-closed を固定する（合成データのみ）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "usage.csv")

    def load(self, rows, months=("2026-07",), **kw):
        write_csv(self.path, rows, **kw)
        return fb.load_usage_csv(self.path, ORG, list(months))

    def test_short_row_missing_trailing_cells_rejected(self):
        # 末尾の username / workflow_path が欠けた行は Dependabot 判定不能として止める（セル値は出さない）
        write_csv(self.path, [crow("2026-07-01", "priv-api", 10, 0.008, 0.08, 0.0)])
        with open(self.path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        header = lines[0].split(",")
        cut = min(header.index("username"), header.index("workflow_path"))
        body = ",".join(lines[1].split(",")[:cut])
        with open(self.path, "w", encoding="utf-8", newline="") as fh:
            fh.write(lines[0] + "\n" + body + "\n")
        with self.assertRaises(fb.FetchError) as cm:
            fb.load_usage_csv(self.path, ORG, ["2026-07"])
        self.assertIn("CSV 行 2", str(cm.exception))
        self.assertNotIn("priv-api", str(cm.exception))

    def test_dependabot_username_only_stays_billable(self):
        # Dependabot の PR が起動した通常の CI（username だけ一致）は課金対象。無料扱いせず件数だけ記録する
        rows = [crow("2026-07-01", "priv-api", 10, 0.008, 0.08, 0.0, user="dependabot[bot]"),
                crow("2026-07-01", "priv-api", 3, 0.008, 0.024, 0.0, user="dependabot[bot]", path="")]
        payloads, st = self.load(rows)
        self.assertEqual([i["dependabot"] for i in payloads["2026-07"]["usageItems"]], [False, False])
        self.assertEqual((st["items"], st["minutes"]), (0, 0.0))
        self.assertEqual((st["pr_items"], st["pr_minutes"]), (2, 13.0))
        r, _ = fb.build_rows(payloads, VISIBILITY, ORG, True)
        self.assertEqual([(x["charge_class"], x["minutes"], x["quota_minutes"]) for x in r],
                         [("included", 13.0, 13.0)])

    def test_dependabot_username_and_path_excluded_from_quota(self):
        payloads, st = self.load([crow("2026-07-01", "priv-api", 20, 0.008, 0.16, 0.0,
                                       user="dependabot[bot]", path=PATH_DEP)])
        self.assertEqual([i["dependabot"] for i in payloads["2026-07"]["usageItems"]], [True])
        self.assertEqual((st["items"], st["minutes"], st["pr_items"]), (1, 20.0, 0))
        r, _ = fb.build_rows(payloads, VISIBILITY, ORG, True)
        self.assertEqual([(x["charge_class"], x["quota_minutes"]) for x in r], [("dependabot_free", 0.0)])

    def test_dependabot_path_only_excluded_from_quota(self):
        payloads, st = self.load([crow("2026-07-01", "priv-api", 30, 0.008, 0.24, 0.0,
                                       user=USER_ALICE, path=PATH_DEP)])
        self.assertEqual([i["dependabot"] for i in payloads["2026-07"]["usageItems"]], [True])
        self.assertEqual((st["items"], st["minutes"], st["pr_items"]), (1, 30.0, 0))
        r, _ = fb.build_rows(payloads, VISIBILITY, ORG, True)
        self.assertEqual([x["charge_class"] for x in r], ["dependabot_free"])

    def test_dependabot_free_rows_and_default_mode(self):
        rows = [crow("2026-07-01", "priv-api", 10, 0.008, 0.08, 0.0, user="dependabot[bot]", path=PATH_DEP),
                crow("2026-07-01", "priv-api", 30, 0.008, 0.24, 0.0)]
        payloads, _ = self.load(rows)
        # 既定モード（公開範囲を確定しない）でも Dependabot 分は dependabot_free、残りは unknown
        r, _ = fb.build_rows(payloads, VISIBILITY, ORG)
        idx = index(r)
        dep = idx[("2026-07-01", "priv-api", "Actions Linux", "dependabot_free")]
        self.assertAlmostEqual(dep["minutes"], 10)
        self.assertEqual(dep["quota_minutes"], 0.0)
        self.assertEqual(dep["billable_minutes"], 0.0)
        self.assertAlmostEqual(dep["gross_usd"], 0.08)
        self.assertAlmostEqual(idx[("2026-07-01", "priv-api", "Actions Linux", "unknown")]["minutes"], 30)
        # 現在値を採るモードでは残りだけが included になり、quota に Dependabot 分は入らない
        r, _ = fb.build_rows(payloads, VISIBILITY, ORG, True)
        idx = index(r)
        inc = idx[("2026-07-01", "priv-api", "Actions Linux", "included")]
        self.assertAlmostEqual(inc["minutes"], 30)
        self.assertAlmostEqual(inc["quota_minutes"], 30)
        self.assertEqual(len(idx), len(r))  # (day, repo, sku, charge_class) が一意

    def test_dependabot_public_repo_still_dependabot_free(self):
        payloads, _ = self.load([crow("2026-07-01", "pub-site", 10, 0.008, 0.08, 0.0, user="dependabot[bot]", path=PATH_DEP)])
        r, _ = fb.build_rows(payloads, VISIBILITY, ORG, True)
        self.assertEqual([x["charge_class"] for x in r], ["dependabot_free"])

    def test_dependabot_paid_part_stays_paid(self):
        # net 0.3 / 0.008 = 37.5 分は有料のまま。残り 62.5 分だけが dependabot_free
        payloads, _ = self.load([crow("2026-07-01", "priv-api", 100, 0.008, 0.8, 0.3, user="dependabot[bot]", path=PATH_DEP)])
        r, _ = fb.build_rows(payloads, VISIBILITY, ORG, True)
        idx = index(r)
        self.assertAlmostEqual(idx[("2026-07-01", "priv-api", "Actions Linux", "paid")]["minutes"], 37.5)
        self.assertAlmostEqual(idx[("2026-07-01", "priv-api", "Actions Linux", "dependabot_free")]["minutes"], 62.5)
        self.assertEqual(len(r), 2)
        self.assertAlmostEqual(sum(x["gross_usd"] for x in r), 0.8)
        self.assertAlmostEqual(sum(x["net_usd"] for x in r), 0.3)
        self.assertAlmostEqual(sum(x["minutes"] for x in r), 100)

    def test_mixed_unit_sums_back(self):
        rows = [crow("2026-07-01", "priv-api", 10, 0.008, 0.08, 0.0, user="dependabot[bot]", path=PATH_DEP),
                crow("2026-07-01", "priv-api", 40, 0.008, 0.32, 0.0),
                crow("2026-07-01", "priv-api", 50, 0.008, 0.4, 0.16)]
        payloads, _ = self.load(rows)
        r, _ = fb.build_rows(payloads, VISIBILITY, ORG, True)
        self.assertEqual(len(index(r)), len(r))
        self.assertAlmostEqual(sum(x["minutes"] for x in r), 100)
        self.assertAlmostEqual(sum(x["gross_usd"] for x in r), 0.8)
        self.assertAlmostEqual(sum(x["net_usd"] for x in r), 0.16)
        self.assertAlmostEqual(sum(x["quota_minutes"] for x in r), 70)

    def test_equivalent_to_api_without_dependabot(self):
        rows = [crow("2026-07-01", "priv-api", 100, 0.008, 0.8, 0.3),
                crow("2026-07-02", "pub-site", 20, 0.008, 0.16, 0.0),
                crow("2026-07-03", "gone-repo", 5, 0.008, 0.04, 0.0)]
        payloads, _ = self.load(rows)
        for flag in (False, True):
            self.assertEqual(fb.build_rows(payloads, VISIBILITY, ORG, flag),
                             fb.build_rows({"2026-07": api_payload(rows)}, VISIBILITY, ORG, flag))

    def test_warnings_count_and_zero_case(self):
        rows = [crow("2026-07-01", "priv-api", 10, 0.008, 0.08, 0.0, user="dependabot[bot]", path=PATH_DEP),
                crow("2026-07-02", "priv-api", 7, 0.008, 0.056, 0.0, user="dependabot[bot]"),
                crow("2026-07-31", "priv-api", 5, 0.008, 0.04, 0.0)]
        _, st = self.load(rows)
        w = fb.csv_warnings(st, ["2026-07"], TODAY)
        self.assertEqual(len(w), 1)
        self.assertIn("1 明細・10.0 分", w[0])
        self.assertIn("Dependabot の PR が起動した通常のワークフロー 1 明細・7.0 分（無料扱いしない）", w[0])
        _, st = self.load([crow("2026-07-01", "priv-api", 10, 0.008, 0.08, 0.0),
                           crow("2026-07-31", "priv-api", 5, 0.008, 0.04, 0.0)])
        w = fb.csv_warnings(st, ["2026-07"], TODAY)
        self.assertIn("0 明細・0.0 分", w[0])

    def test_sensitive_values_not_in_warnings_or_rows(self):
        rows = [crow("2026-07-01", "priv-api", 10, 0.008, 0.08, 0.0, user="dependabot[bot]", path=PATH_DEP),
                crow("2026-07-02", "priv-api", 10, 0.008, 0.08, 0.0, user="dependabot[bot]"),
                crow("2026-07-03", "priv-api", 10, 0.0, 0.0, 0.0, user="dependabot[bot]",
                     path=PATH_DEP, sku="actions_self_hosted_linux")]
        payloads, st = self.load(rows)
        blob = json.dumps([fb.build_rows(payloads, VISIBILITY, ORG, True),
                           fb.csv_warnings(st, ["2026-07"], TODAY)], ensure_ascii=False)
        for secret in (USER_ALICE, PATH_CI, PATH_DEP, "dependabot[bot]"):
            self.assertNotIn(secret, blob)

    def test_rejected_inputs(self):
        full = crow("2026-07-01", "priv-api", 1, 0.008, 0.008, 0.0)
        with self.subTest("aggregate report (no username / workflow_path)"):
            with self.assertRaises(fb.FetchError) as cm:
                self.load([full[:-2]], header=CSV_HEADER[:-2])
            self.assertIn("Dependabot", str(cm.exception))
        with self.subTest("missing required column"):
            keep = [i for i, h in enumerate(CSV_HEADER) if h != "sku"]
            with self.assertRaises(fb.FetchError) as cm:
                self.load([[full[i] for i in keep]], header=[CSV_HEADER[i] for i in keep])
            self.assertIn("sku", str(cm.exception))
        with self.subTest("empty file"):
            open(self.path, "w").close()
            with self.assertRaises(fb.FetchError):
                fb.load_usage_csv(self.path, ORG, ["2026-07"])

    def test_bad_numbers_report_row_and_column_only(self):
        for bad in ("", "nan", "-1", "inf", "abc"):
            with self.subTest(bad=bad):
                r = crow("2026-07-01", "priv-api", 1, 0.008, 0.008, 0.0)
                r[3] = bad
                with self.assertRaises(fb.FetchError) as cm:
                    self.load([crow("2026-07-01", "priv-api", 1, 0.008, 0.008, 0.0), r])
                msg = str(cm.exception)
                self.assertIn("行 3", msg)
                self.assertIn("quantity", msg)
                if bad:
                    self.assertNotIn(bad, msg)

    def test_bom_header(self):
        payloads, _ = self.load([crow("2026-07-01", "priv-api", 1, 0.008, 0.008, 0.0)], bom=True)
        self.assertEqual(len(payloads["2026-07"]["usageItems"]), 1)

    def test_drops_period_and_non_actions(self):
        rows = [crow("2026-07-01", "priv-api", 10, 0.008, 0.08, 0.0),
                crow("2026-06-30", "priv-api", 10, 0.008, 0.08, 0.0),
                crow("2026-07-01", "priv-api", 3, 0.0, 0.0, 0.0, unit="GigabyteHours"),
                crow("2026-07-01", "priv-api", 7, 0.04, 0.28, 0.0, product="Copilot", org="")]
        payloads, st = self.load(rows)
        self.assertEqual(len(payloads["2026-07"]["usageItems"]), 1)
        self.assertEqual(st["out_of_period"], 1)
        w = fb.csv_warnings(st, ["2026-07"], TODAY)
        self.assertTrue(any("対象期間外の明細 1 件" in x for x in w))

    def test_other_or_empty_org_rejected(self):
        # 出どころを確かめられない行は捨てずに止める（org 名・セル値はエラー文に出さない）
        for bad, word in (("other-synth", "一致しない"), ("", "空")):
            with self.subTest(org=bad):
                with self.assertRaises(fb.FetchError) as cm:
                    self.load([crow("2026-07-01", "priv-api", 10, 0.008, 0.08, 0.0),
                               crow("2026-07-02", "priv-api", 10, 0.008, 0.08, 0.0, org=bad)])
                msg = str(cm.exception)
                self.assertIn("CSV 行 3", msg)
                self.assertIn(word, msg)
                self.assertNotIn("other-synth", msg)
                self.assertNotIn("priv-api", msg)
        # 大小文字の違いは同じ org として扱う
        payloads, _ = self.load([crow("2026-07-01", "priv-api", 10, 0.008, 0.08, 0.0, org=ORG.upper())])
        self.assertEqual(len(payloads["2026-07"]["usageItems"]), 1)

    def test_missing_organization_column_rejected(self):
        keep = [i for i, h in enumerate(CSV_HEADER) if h != "organization"]
        full = crow("2026-07-01", "priv-api", 1, 0.008, 0.008, 0.0)
        with self.assertRaises(fb.FetchError) as cm:
            self.load([[full[i] for i in keep]], header=[CSV_HEADER[i] for i in keep])
        self.assertIn("organization", str(cm.exception))

    def test_date_range_warning(self):
        _, st = self.load([crow("2026-07-10", "priv-api", 1, 0.008, 0.008, 0.0)])
        w = fb.csv_warnings(st, ["2026-07"], TODAY)
        self.assertTrue(any("日付範囲 2026-07-10..2026-07-10" in x and "未取得" in x for x in w))

    def test_size_and_row_limits(self):
        write_csv(self.path, [crow("2026-07-01", "priv-api", 1, 0.008, 0.008, 0.0)] * 3)
        old = (fb.MAX_CSV_BYTES, fb.MAX_CSV_ROWS)
        self.addCleanup(lambda: setattr(fb, "MAX_CSV_BYTES", old[0]))
        self.addCleanup(lambda: setattr(fb, "MAX_CSV_ROWS", old[1]))
        fb.MAX_CSV_ROWS = 2
        with self.assertRaises(fb.FetchError):
            fb.load_usage_csv(self.path, ORG, ["2026-07"])
        fb.MAX_CSV_ROWS = old[1]
        fb.MAX_CSV_BYTES = 10
        with self.assertRaises(fb.FetchError):
            fb.load_usage_csv(self.path, ORG, ["2026-07"])

    def test_non_regular_file(self):
        with self.assertRaises(fb.FetchError):
            fb.load_usage_csv(self._tmp.name, ORG, ["2026-07"])


class UsageCsvSkuTest(unittest.TestCase):
    """使用量 CSV のセルフホスト SKU の除外と、SKU 表記の API への写像を固定する（合成データのみ）。

    billing usage API はセルフホストの明細を返さず（fetch_billing.py は SKU で除外していない）、
    セルフホスト分は fetch_jobs.py の rows が担当する。CSV にはセルフホストの SKU 行があるため、
    CSV 経路だけが読み込み時に除く。SKU 表記は API と同じにそろえ、経路で結果を変えない。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "usage.csv")

    def load(self, rows, months=("2026-07",)):
        write_csv(self.path, rows)
        return fb.load_usage_csv(self.path, ORG, list(months))

    def test_self_hosted_sku_rows_excluded(self):
        rows = [crow("2026-07-01", "priv-api", 10, 0.008, 0.08, 0.0),
                crow("2026-07-01", "priv-api", 300, 0.0, 0.0, 0.0, sku="actions_self_hosted_linux"),
                crow("2026-07-02", "priv-api", 50, 0.0, 0.0, 0.0, sku="actions_self_hosted_windows")]
        payloads, st = self.load(rows)
        self.assertEqual([i["sku"] for i in payloads["2026-07"]["usageItems"]], ["Actions Linux"])
        self.assertEqual(st["self_hosted"], {"items": 2, "minutes": 350.0, "dependabot_items": 0,
                                             "with_amount_items": 0})
        # セルフホストしか無い日も CSV が覆う日として数える（GitHub ホストの利用 0 は事実）
        self.assertIn(fb.date(2026, 7, 2), st["dates"])
        r, _ = fb.build_rows(payloads, VISIBILITY, ORG, True)
        self.assertEqual([(x["charge_class"], x["minutes"], x["runner_type"]) for x in r],
                         [("included", 10.0, "ubuntu")])
        w = fb.csv_warnings(st, ["2026-07"], TODAY)
        self.assertTrue(any("セルフホストの SKU の明細 2 件・350.0 分を除いた" in x for x in w), w)

    def test_self_hosted_dependabot_rows_excluded(self):
        rows = [crow("2026-07-01", "priv-api", 10, 0.008, 0.08, 0.0),
                crow("2026-07-01", "priv-api", 6, 0.0, 0.0, 0.0, user="dependabot[bot]", path=PATH_DEP,
                     sku="actions_self_hosted_linux")]
        payloads, st = self.load(rows)
        self.assertEqual(len(payloads["2026-07"]["usageItems"]), 1)
        # Dependabot の集計にはセルフホスト分を混ぜない。セルフホスト側の内数として数える
        self.assertEqual((st["items"], st["minutes"]), (0, 0.0))
        self.assertEqual(st["self_hosted"]["dependabot_items"], 1)
        r, _ = fb.build_rows(payloads, VISIBILITY, ORG, True)
        self.assertNotIn("dependabot_free", {x["charge_class"] for x in r})
        self.assertAlmostEqual(sum(x["minutes"] for x in r), 10.0)

    def test_self_hosted_with_amount_is_reported(self):
        _, st = self.load([crow("2026-07-01", "priv-api", 10, 0.002, 0.02, 0.02, sku="actions_self_hosted_linux")])
        self.assertEqual(st["self_hosted"]["with_amount_items"], 1)
        w = fb.csv_warnings(st, ["2026-07"], TODAY)
        self.assertTrue(any("金額が付いた明細 1 件" in x for x in w), w)

    def test_sku_mapped_to_api_notation(self):
        rows = [crow("2026-07-01", "priv-api", 10, 0.008, 0.08, 0.0, sku="actions_linux"),
                crow("2026-07-01", "priv-api", 10, 0.064, 0.64, 0.0, sku="actions_macos"),
                crow("2026-07-01", "priv-api", 10, 0.016, 0.16, 0.0, sku="actions_windows"),
                crow("2026-07-01", "priv-api", 10, 0.002, 0.02, 0.0, sku="actions_linux_slim"),
                crow("2026-07-01", "priv-api", 10, 0.005, 0.05, 0.0, sku=" ACTIONS_LINUX_ARM ")]
        payloads, st = self.load(rows)
        self.assertEqual([i["sku"] for i in payloads["2026-07"]["usageItems"]],
                         ["Actions Linux", "Actions macOS 3-core", "Actions Windows",
                          "Actions Linux Slim", "Actions Linux ARM"])
        self.assertEqual(st["unmapped"], {"items": 0, "skus": []})
        rows[-1][2] = "actions_linux_arm"
        for flag in (False, True):
            csv_rows, csv_price = fb.build_rows(payloads, VISIBILITY, ORG, flag)
            api_rows, api_price = fb.build_rows({"2026-07": api_payload(rows)}, VISIBILITY, ORG, flag)
            self.assertEqual(csv_rows, api_rows)
            self.assertEqual(csv_price, {"2026-07": 0.008})
        rt = {x["sku"]: x["runner_type"] for x in csv_rows}
        self.assertEqual(rt, {"Actions Linux": "ubuntu", "Actions macOS 3-core": "macos",
                              "Actions Windows": "windows", "Actions Linux Slim": "ubuntu",
                              "Actions Linux ARM": "ubuntu-arm"})
        self.assertEqual([x for x in fb.csv_warnings(st, ["2026-07"], TODAY) if "写像" in x or "セルフホスト" in x], [])

    def test_unmapped_sku_kept_and_reported(self):
        # 写像に無い SKU は API 経路の未知 SKU と同じく名前のまま残し（runner_type はキーワード照合）、
        # 黙って通さず件数と SKU 名を warning に出す。識別子の形でない名前は件数だけにする
        odd = "x\x1b[31m" + "y" * 80
        rows = [crow("2026-07-01", "priv-api", 10, 0.022, 0.22, 0.22, sku="actions_linux_4_core"),
                crow("2026-07-01", "priv-api", 5, 0.01, 0.05, 0.0, sku="actions_windows_arm"),
                crow("2026-07-02", "priv-api", 5, 0.01, 0.05, 0.0, sku="actions_windows_arm"),
                crow("2026-07-02", "priv-api", 1, 0.01, 0.01, 0.0, sku=odd),
                crow("2026-07-02", "priv-api", 1, 0.008, 0.008, 0.0)]
        payloads, st = self.load(rows)
        skus = [i["sku"] for i in payloads["2026-07"]["usageItems"]]
        self.assertEqual(skus[:3], ["actions_linux_4_core", "actions_windows_arm", "actions_windows_arm"])
        self.assertEqual(st["unmapped"]["items"], 4)
        r, _ = fb.build_rows(payloads, VISIBILITY, ORG, True)
        rt = {x["sku"]: x["runner_type"] for x in r}
        self.assertEqual(rt["actions_linux_4_core"], "ubuntu")
        self.assertEqual(rt["actions_windows_arm"], "windows")
        w = [x for x in fb.csv_warnings(st, ["2026-07"], TODAY) if "写像" in x]
        self.assertEqual(len(w), 1)
        self.assertIn("4 件", w[0])
        self.assertIn("actions_linux_4_core", w[0])
        self.assertIn("actions_windows_arm", w[0])
        self.assertIn("他 1 種", w[0])
        self.assertNotIn("\x1b", w[0])

    def test_non_finite_totals_rejected(self):
        big = "1e308"
        r1 = crow("2026-07-01", "priv-api", 1, 0.008, 0.008, 0.0)
        r1[3] = big
        r2 = list(r1)
        payloads, _ = self.load([r1, r2])
        with self.assertRaises(fb.FetchError) as cm:
            fb.build_rows(payloads, VISIBILITY, ORG, True)
        self.assertIn("有限", str(cm.exception))


class UsageCsvCliTest(CliStubTest):
    def setUp(self):
        super().setUp()
        self.csv = os.path.join(self._tmp.name, "usage.csv")
        write_csv(self.csv, [
            crow("2026-07-01", "priv-api", 10, 0.008, 0.08, 0.0, user="dependabot[bot]", path=PATH_DEP),
            crow("2026-07-01", "priv-api", 90, 0.008, 0.72, 0.0),
            crow("2026-07-31", "pub-site", 20, 0.008, 0.16, 0.0)])

    def cli(self, *extra):
        return self.run_cli("--org", ORG, "--since", "2026-07", "--until", "2026-07", "--out", self.out,
                            "--usage-csv", self.csv, *extra)

    def test_success_skips_billing_api_and_hides_names(self):
        r = self.cli()
        self.assertEqual(r.returncode, 0, r.stderr)
        calls = self.calls()
        self.assertEqual(len(calls), 1)
        self.assertIn("/repos", calls[0])
        self.assertNotIn("settings/billing", " ".join(calls))
        with open(self.out, encoding="utf-8") as f:
            raw = f.read()
        data = json.loads(raw)
        self.assertEqual(data["input"], "usage_csv")
        self.assertEqual(data["source"], "billing")
        self.assertEqual(data["dependabot"], {"rule": fb.DEPENDABOT_RULE, "items": 1, "minutes": 10.0,
                                              "pr_workflow_items": 0, "pr_workflow_minutes": 0.0})
        self.assertTrue(any("Dependabot" in w for w in data["warnings"]))
        self.assertIn("dependabot_free", {x["charge_class"] for x in data["rows"]})
        self.assertLessEqual(len(r.stdout.splitlines()), 10)
        # 判定規則の固定文言（公開の bot 名）は出力に残る。利用者名とパスの実値だけが対象
        for secret in (USER_ALICE, PATH_CI, PATH_DEP):
            for text in (raw, r.stdout, r.stderr):
                self.assertNotIn(secret, text)

    def test_self_hosted_and_sku_end_to_end(self):
        write_csv(self.csv, [
            crow("2026-07-01", "priv-api", 90, 0.008, 0.72, 0.0),
            crow("2026-07-01", "priv-api", 3000, 0.0, 0.0, 0.0, sku="actions_self_hosted_linux"),
            crow("2026-07-01", "priv-api", 6, 0.0, 0.0, 0.0, user="dependabot[bot]", path=PATH_DEP,
                 sku="actions_self_hosted_linux"),
            crow("2026-07-31", "pub-site", 20, 0.008, 0.16, 0.0)])
        r = self.cli("--assume-current-visibility")
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(self.out, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual({x["sku"] for x in data["rows"]}, {"Actions Linux"})
        self.assertAlmostEqual(sum(x["minutes"] for x in data["rows"] if x["charge_class"] == "included"), 90)
        self.assertEqual(data["self_hosted_excluded"], {"items": 2, "minutes": 3006.0, "dependabot_items": 1,
                                                        "with_amount_items": 0})
        self.assertEqual(data["dependabot"]["items"], 0)
        self.assertTrue(any("セルフホストの SKU の明細 2 件・3006.0 分を除いた" in w for w in data["warnings"]))
        self.assertNotIn("self-hosted", {x["runner_type"] for x in data["rows"]})

    def test_api_path_records_input_api(self):
        r = self.run_cli("--org", ORG, "--since", "2026-07", "--until", "2026-07", "--out", self.out)
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(self.out, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["input"], "api")
        self.assertNotIn("dependabot", data)

    def test_bad_csv_never_calls_gh_and_keeps_output(self):
        with open(self.out, "wb") as f:
            f.write(b"previous\n")
        write_csv(self.csv, [crow("2026-07-01", "priv-api", 1, 0.008, 0.008, 0.0)[:-2]], header=CSV_HEADER[:-2])
        r = self.cli()
        self.assertEqual(r.returncode, 1)
        self.assertIn("Dependabot", r.stderr)
        self.assertEqual(self.calls(), [])
        with open(self.out, "rb") as f:
            self.assertEqual(f.read(), b"previous\n")

    def test_wider_period_than_csv_rejected(self):
        # 7 月だけの CSV に 6..7 月を指定すると 6 月が「取得済み・利用ゼロ」になるため拒否する
        with open(self.out, "wb") as f:
            f.write(b"previous\n")
        r = self.run_cli("--org", ORG, "--since", "2026-06", "--until", "2026-07", "--out", self.out,
                         "--usage-csv", self.csv)
        self.assertEqual(r.returncode, 1)
        self.assertIn("2026-06", r.stderr)
        self.assertEqual(self.calls(), [])
        with open(self.out, "rb") as f:
            self.assertEqual(f.read(), b"previous\n")

    def test_other_product_rows_do_not_hide_missing_month(self):
        write_csv(self.csv, [
            crow("2026-07-01", "priv-api", 1, 0.008, 0.008, 0.0),
            crow("2026-06-15", "priv-api", 1, 0.008, 0.008, 0.0, product="Packages"),
            crow("2026-06-16", "priv-api", 1, 0.008, 0.008, 0.0, unit="GigabyteHours")])
        r = self.run_cli("--org", ORG, "--since", "2026-06", "--until", "2026-07", "--out", self.out,
                         "--usage-csv", self.csv)
        self.assertEqual(r.returncode, 1)
        self.assertIn("2026-06", r.stderr)

    def test_directory_csv_never_calls_gh(self):
        r = self.run_cli("--org", ORG, "--since", "2026-07", "--until", "2026-07", "--out", self.out,
                         "--usage-csv", self.work)
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.calls(), [])
        self.assertEqual(os.listdir(self.work), [])


class SharedCommonTest(unittest.TestCase):
    """helper の定義が scripts/_common.py の 1 か所であることを固定する（Issue #141）。"""

    def test_helpers_are_shared_with_common(self):
        import _common
        expected = os.path.realpath(os.path.join(HERE, "..", "scripts", "_common.py"))
        self.assertEqual(os.path.realpath(_common.__file__), expected)
        for name in ('FetchError', 'ORG_RE', 'MONTH_RE', 'gh_api', 'atomic_write_json',
                     'parse_concatenated_json', 'nonneg_num'):
            self.assertIs(getattr(fb, name), getattr(_common, name), name)

    def test_nonneg_num_message_and_default(self):
        with self.assertRaises(fb.FetchError) as cm:
            fb.nonneg_num({"k": -1}, "k")
        self.assertIn("API の数値フィールド k が不正（有限の非負数ではない）", str(cm.exception))
        self.assertEqual(fb.nonneg_num({}, "k", False), 0.0)

    def test_atomic_write_json_rejects_non_finite(self):
        # JSON に Infinity / NaN を書かない。拒否時は既存ファイルを残し、一時ファイルも残さない
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "out.json")
            with open(out, "w", encoding="utf-8") as f:
                f.write("previous\n")
            for bad in (float("inf"), float("nan")):
                with self.assertRaises(ValueError):
                    fb.atomic_write_json(out, {"rows": [{"minutes": bad}]})
            with open(out, encoding="utf-8") as f:
                self.assertEqual(f.read(), "previous\n")
            self.assertEqual(os.listdir(d), ["out.json"])

    def test_nonneg_num_huge_int(self):
        # float の範囲を超える巨大な整数は OverflowError ではなく FetchError にする（Issue #189）
        for v in (10**400, -10**400):
            with self.assertRaises(fb.FetchError) as cm:
                fb.nonneg_num({"k": v}, "k", origin="API")
            self.assertIn("API の数値フィールド k が不正（有限の非負数ではない）", str(cm.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
