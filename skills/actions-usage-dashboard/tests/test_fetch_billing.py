#!/usr/bin/env python3
"""fetch_billing.py の回帰テスト。

役割: 区分展開（paid / included / public_free）・quota 換算・runner_type の対応と、
gh をスタブにした CLI 全体の挙動（月の取得失敗時に出力が作られないこと、標準出力が
10 行以内であること）を固定する。fetch-billing.test.mjs から `node --test` 経由で実行される。

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
        rows, _ = fb.build_rows(PAYLOADS, VISIBILITY, ORG)
        self.assertEqual(data["rows"], rows)
        self.assertEqual(data["linux_price_per_minute"]["2026-08"], 0.008)
        self.assertLessEqual(len(r.stdout.splitlines()), 10)
        self.assertNotIn("priv-api", r.stdout)
        self.assertEqual([f for f in os.listdir(self.work)], ["billing.json"])

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


class SharedCommonTest(unittest.TestCase):
    """helper の定義が scripts/_common.py の 1 か所であることを固定する（Issue #141）。"""

    def test_helpers_are_shared_with_common(self):
        import _common
        expected = os.path.realpath(os.path.join(HERE, "..", "scripts", "_common.py"))
        self.assertEqual(os.path.realpath(_common.__file__), expected)
        for name in ('FetchError', 'ORG_RE', 'MONTH_RE', 'gh_api', 'atomic_write_json',
                     'parse_concatenated_json'):
            self.assertIs(getattr(fb, name), getattr(_common, name), name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
