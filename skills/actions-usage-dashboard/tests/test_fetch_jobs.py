#!/usr/bin/env python3
"""fetch_jobs.py の回帰テスト。純粋関数の単体テストと、gh スタブを使った CLI の E2E を持つ。

fixture はすべて合成データ（org は acme-synth）。gh スタブは PATH の先頭に置いた
Python スクリプトで、呼び出しを calls.log に記録し、cfg.json の内容から応答を返す。
"""
import importlib.util
import json
import os
import random
import shutil
import subprocess
import sys
import signal
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "scripts", "fetch_jobs.py")
spec = importlib.util.spec_from_file_location("fetch_jobs", SCRIPT)
fj = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fj)

ORG = "acme-synth"

# 一覧の縮小は「今日」基準の窓で決まるため、固定日付だと時間経過でテストが壊れる。相対日付にする
TODAY = datetime.now(timezone.utc).date()
D0 = (TODAY - timedelta(days=10)).isoformat()
D1 = (TODAY - timedelta(days=9)).isoformat()
D2 = (TODAY - timedelta(days=8)).isoformat()
DM1 = (TODAY - timedelta(days=11)).isoformat()
SINCE = (TODAY - timedelta(days=20)).isoformat()

STUB = r'''#!%(py)s
import json, os, sys, time
d = os.environ["STUB_DIR"]
args = sys.argv[1:]
with open(os.path.join(d, "calls.log"), "a") as f:
    f.write(" ".join(args) + "\n")
cfg = json.load(open(os.path.join(d, "cfg.json")))
log = open(os.path.join(d, "calls.log")).read().splitlines()
n_api = len([l for l in log if not l.startswith("api rate_limit")])
fail_after = cfg.get("fail_after")
low_after = cfg.get("low_after")
q = {}
a = args[1:]
i = 0
path = None
while i < len(a):
    if a[i] == "-f":
        k, v = a[i + 1].split("=", 1); q[k] = v; i += 2
    elif a[i] == "-X":
        i += 2
    elif a[i] == "--paginate":
        i += 1
    else:
        path = a[i]; i += 1
if path == "rate_limit":
    n_rate = len([l for l in log if l.startswith("api rate_limit")])
    if n_rate <= cfg.get("rate5xx_times", 0):
        sys.stderr.write("gh: Bad Gateway (HTTP 502)\n"); sys.exit(1)
    rem = 5000
    if low_after is not None and n_api >= low_after:
        rem = cfg.get("low_value", 3)
    print(json.dumps({"resources": {"core": {"remaining": rem, "reset": 1}}}))
    sys.exit(0)
if n_api <= cfg.get("fail5xx_until", 0):
    sys.stderr.write("gh: Bad Gateway (HTTP 502)\n"); sys.exit(1)
if fail_after is not None and n_api > fail_after:
    if cfg.get("fail_status") == 404:
        sys.stderr.write("gh: Not Found (HTTP 404)\n"); sys.exit(1)
    if cfg.get("fail_status") == 502:
        sys.stderr.write("gh: Bad Gateway (HTTP 502)\n"); sys.exit(1)
    sys.stderr.write("HTTP 500 boom\n"); sys.exit(1)
if cfg.get("sleep_after") is not None and n_api > cfg["sleep_after"]:
    time.sleep(30)
if path.startswith("orgs/"):
    repos = cfg["repos"]
    print(json.dumps(repos[:2])); print(json.dumps(repos[2:]))
    sys.exit(0)
parts = path.split("?")[0].split("/")
page = int(q.get("page", "1")); per = int(q.get("per_page", "30"))
if parts[-1] == "runs":
    runs = cfg["runs"].get(parts[2], [])
    c = q["created"]
    if c.startswith(">="):
        lo, hi = c[2:], "9999-12-31"
    else:
        lo, hi = c.split("..")
    runs = [r for r in runs if lo <= r["created_at"][:10] <= hi]
    capped = runs[:1000]
    print(json.dumps({"total_count": len(runs), "workflow_runs": capped[(page-1)*per:page*per]}))
elif parts[-1] == "jobs":
    jobs = cfg["jobs"].get(parts[-2], [])
    print(json.dumps({"total_count": len(jobs), "jobs": jobs[(page-1)*per:page*per]}))
else:
    sys.stderr.write("unknown " + path + "\n"); sys.exit(1)
'''


def job(jid, labels, group, start, minutes, status="completed"):
    s = datetime.fromisoformat(start)
    c = (s + timedelta(minutes=minutes)).isoformat() if status == "completed" else None
    return {"id": jid, "labels": labels, "runner_group_name": group, "status": status,
            "started_at": start, "completed_at": c}


def run(rid, created=D0 + "T00:00:00Z", status="completed", attempt=1, workflow_id=1):
    return {"id": rid, "status": status, "run_attempt": attempt, "created_at": created,
            "workflow_id": workflow_id}


def base_cfg():
    return {
        "repos": [
            {"name": "pub-site", "visibility": "public", "archived": False},
            {"name": "priv-api", "visibility": "private", "archived": False},
            {"name": "old-archived", "visibility": "private", "archived": True},
        ],
        "runs": {"pub-site": [run(1)], "priv-api": [run(2), run(3, status="in_progress")],
                 "old-archived": [run(9)]},
        "jobs": {
            "1": [job(101, ["self-hosted", "linux"], "build-farm", D0 + "T10:00:00+00:00", 10),
                  job(102, ["ubuntu-latest"], "GitHub Actions", D0 + "T10:00:00+00:00", 99)],
            "2": [job(201, ["Self-Hosted"], "build-farm", D0 + "T23:30:00+00:00", 90),
                  job(202, ["x"], "build-farm", D0 + "T10:00:00+00:00", 5, status="in_progress")],
            "3": [job(301, ["self-hosted"], "build-farm", D0 + "T10:00:00+00:00", 7)],
        },
    }


class TrailingNewlineTest(unittest.TestCase):
    """Python の `$` は末尾の改行の手前にも一致する。検証用の正規表現は `\\Z` で終端する。"""

    def test_org_repo_date_reject_trailing_newline(self):
        for name, ok in (("ORG_RE", "acme-synth"), ("REPO_RE", "repo.name_1"), ("DAY_RE", "2026-09-01")):
            rx = getattr(fj, name)
            self.assertIsNotNone(rx.match(ok), name)
            self.assertIsNone(rx.match(ok + "\n"), name)


class Pure(unittest.TestCase):
    def test_relist_window_covers_official_rerun_period(self):
        # 公式の再実行期間は初回実行から 30 日（references/data-sources.md の出典節）。UTC の日単位の
        # 境界 1 日を足した 31 日を下回ると、settled 後の再実行（run_attempt 増）を取りこぼして分数が過少になる
        self.assertGreaterEqual(fj.RELIST_WINDOW_DAYS, 31)

    def test_is_self_hosted(self):
        f = fj.is_self_hosted
        self.assertTrue(f({"labels": ["self-hosted", "linux"], "runner_group_name": "GitHub Actions"}))
        self.assertTrue(f({"labels": ["Self-Hosted"], "runner_group_name": None}))
        # 独自グループの GitHub ホスト larger runner は self-hosted ラベルを持たず、二重計上しない
        self.assertFalse(f({"labels": ["a"], "runner_group_name": "build-farm"}))
        self.assertFalse(f({"labels": ["ubuntu-latest"], "runner_group_name": "GitHub Actions"}))
        self.assertFalse(f({"labels": ["ubuntu-latest"], "runner_group_name": None}))
        self.assertFalse(f({"labels": ["ubuntu-latest"], "runner_group_name": ""}))

    def test_unfinished_excluded(self):
        t = "2026-10-10T10:00:00+00:00"
        base = job(1, ["self-hosted"], "g", t, 5)
        self.assertIsNotNone(fj.job_window(base))
        for patch in ({"status": "in_progress"}, {"completed_at": None}, {"started_at": None},
                      {"completed_at": "2026-10-10T09:00:00+00:00"}, {"started_at": "garbage"}):
            self.assertIsNone(fj.job_window(dict(base, **patch)), patch)

    def test_aggregate_tz_dedupe_zero(self):
        jobs = [
            job(1, ["self-hosted"], "g", "2026-10-10T08:30:00+09:00", 1.5),  # UTC では 10-09 の 23:30（日付が UTC へ寄る）
            job(1, ["self-hosted"], "g", "2026-10-10T08:30:00+09:00", 1.5),  # 重複
            job(2, ["self-hosted"], "g", "2026-10-09T23:30:00+00:00", 90),   # 日またぎは開始日
            job(3, ["self-hosted"], None, "2026-10-09T23:00:00+00:00", 0),   # 0 分
            job(4, ["self-hosted"], None, "2026-10-09T23:00:00+00:00", 2),
        ]
        agg, hosted, unfinished, selfh = fj.aggregate_jobs(jobs)
        self.assertEqual(agg, [{"day": "2026-10-09", "runner_group": "(unknown)", "minutes": 2.0},
                               {"day": "2026-10-09", "runner_group": "g", "minutes": 91.5}])
        self.assertEqual((hosted, unfinished), (0, 0))
        self.assertEqual(selfh, 4)  # 重複 id は 1 件、0 分の job も数える

    def test_aggregate_counts_unfinished_selfhosted(self):
        # #227: agg に載らない未完了・時刻不正のセルフホスト job も self_hosted に数える
        jobs = [job(1, ["self-hosted"], "g", "2026-10-09T23:00:00+00:00", 0, status="in_progress"),
                job(2, ["ubuntu-latest"], "GitHub Actions", "2026-10-09T23:00:00+00:00", 3),
                job(2, ["ubuntu-latest"], "GitHub Actions", "2026-10-09T23:00:00+00:00", 3)]
        agg, hosted, unfinished, selfh = fj.aggregate_jobs(jobs)
        self.assertEqual((agg, hosted, unfinished, selfh), ([], 1, 1, 1))

    def test_build_rows_columns_and_order(self):
        ents = [{"repo": "b", "agg": [{"day": "2026-10-02", "runner_group": "g", "minutes": 1.0}]},
                {"repo": "a", "agg": [{"day": "2026-10-02", "runner_group": "g", "minutes": 2.0},
                                      {"day": "2026-09-01", "runner_group": "g", "minutes": 9.0},
                                      {"day": "2026-10-03", "runner_group": "g", "minutes": 0.0}]}]
        rows = fj.build_rows(ents, {"a": "public"}, "2026-10-01")
        self.assertEqual([(r["day"], r["repo"]) for r in rows], [("2026-10-02", "a"), ("2026-10-02", "b")])
        self.assertEqual(set(rows[0]), {"day", "repo", "visibility", "sku", "runner_type", "charge_class",
                                        "runner_group", "minutes", "quota_minutes", "billable_minutes",
                                        "gross_usd", "discount_usd", "net_usd"})
        self.assertEqual(rows[0]["charge_class"], "self_hosted")
        self.assertEqual(rows[0]["visibility"], "public")
        self.assertEqual(rows[0]["net_usd"], 0.0)

    def test_validation(self):
        with self.assertRaises(fj.InvalidJob):
            fj.aggregate_jobs([{"id": "x", "labels": []}])
        with self.assertRaises(fj.FetchError):
            fj.aggregate_jobs([{"id": 1, "labels": "self-hosted"}])
        for bad in (True, "1", 0, -1):
            with self.assertRaises(fj.FetchError):
                fj._run_key({"id": 1, "run_attempt": bad})
        with self.assertRaises(fj.FetchError):
            fj._run_key({"id": True})


class EstimatePure(unittest.TestCase):
    """抜き取り推定モードの純粋関数（Issue #197）。"""

    def test_spread_sample(self):
        items = [("2026-01-%02dT00:00:00Z" % d, d) for d in range(1, 11)]
        self.assertEqual(fj.spread_sample(items, 20), sorted(items))
        self.assertEqual(fj.spread_sample(items, 1), [sorted(items)[5]])
        three = fj.spread_sample(items, 3)
        self.assertEqual([i for _c, i in three], [1, 5, 10])  # round(4.5) は偶数丸めで index 4
        for k in range(2, 10):
            ids = [i for _c, i in fj.spread_sample(items, k)]
            self.assertEqual(len(set(ids)), k)
            self.assertEqual((ids[0], ids[-1]), (1, 10))
        # 入力の順序に依存しない
        self.assertEqual(fj.spread_sample(list(reversed(items)), 3), three)

    def test_plan_samples_groups_and_skips_pending(self):
        listed = [(1, 1, "r", run(1, workflow_id=5)), (2, 1, "r", run(2, workflow_id=5)),
                  (3, 1, "r", run(3, status="in_progress", workflow_id=5)),
                  (4, 1, "r", run(4, workflow_id=None)), (5, 1, "r", run(5, workflow_id=-3)),
                  (6, 1, "q", run(6, workflow_id=5))]
        g = fj.plan_samples(listed, 1)
        self.assertEqual(set(g), {("r", 5), ("r", None), ("q", 5)})
        self.assertEqual([r[0] for r in g[("r", 5)]["pool"]], [1, 2])
        self.assertEqual({r[0] for r in g[("r", None)]["pool"]}, {4, 5})

    def test_estimate_entries(self):
        def agg(m):
            return [{"day": D0, "runner_group": "g", "minutes": m}]
        listed = [(i, 1, "r", run(i, created=D0 + "T00:00:0%dZ" % i, workflow_id=5)) for i in range(1, 6)]
        listed.append((6, 2, "r", run(6, created=D0 + "T00:00:06Z", attempt=2, workflow_id=5)))
        g = fj.plan_samples(listed, 2)
        self.assertEqual(g[("r", 5)]["sample"], {1, 6})
        cache = {"1": {"repo": "r", "attempt": 1, "created_at": "", "agg": agg(10.0)},
                 "6": {"repo": "r", "attempt": 2, "created_at": "", "agg": agg(30.0)},
                 "3": {"repo": "r", "attempt": 1, "created_at": "", "agg": []}}
        ent, c = fj.estimate_entries(g, cache, {"4": {"attempt": 1}})
        # 2 と 5 は推定、3 はキャッシュ済み、4 は skipped
        self.assertEqual(c, {"sampled": 2, "estimated": 2, "unestimated": 0})
        self.assertEqual(ent, [{"repo": "r", "agg": agg(20.0)}] * 2)
        self.assertEqual(set(cache), {"1", "6", "3"})
        # 旧 attempt のキャッシュしか無い抜き取りは有効な抜き取りに数えない
        cache["6"]["attempt"] = 1
        ent, c = fj.estimate_entries(g, cache, {})
        self.assertEqual(c["sampled"], 1)
        # 抜き取りが全て無効なら推定不能
        ent, c = fj.estimate_entries(g, {}, {})
        self.assertEqual(c, {"sampled": 0, "estimated": 0, "unestimated": 4})
        self.assertEqual(ent, [])

    def test_estimate_entries_old_attempt_left_and_bad_created(self):
        listed = [(1, 1, "r", run(1, created=D0 + "T00:00:00Z")),
                  (2, 2, "r", run(2, created=D0 + "T00:00:01Z", attempt=2)),
                  (3, 1, "r", run(3, created="garbage")),
                  (4, 1, "r", run(4, created=D0 + "T00:00:03Z"))]
        g = fj.plan_samples(listed, 1)
        # k=1 は並べた列の中央（index 2）。created_at 昇順で "garbage" が最後なので (D0..03, 4) が選ばれる
        self.assertEqual(g[("r", 1)]["sample"], {4})
        cache = {"4": {"repo": "r", "attempt": 1, "created_at": "", "agg": [{"day": D0, "runner_group": "g", "minutes": 8.0}]},
                 "2": {"repo": "r", "attempt": 1, "created_at": "", "agg": []}}
        ent, c = fj.estimate_entries(g, cache, {})
        # run 1 は推定、run 2 は旧 attempt 残しで推定しない、run 3 は作成日が不正で推定不能
        self.assertEqual(c, {"sampled": 1, "estimated": 1, "unestimated": 1})
        self.assertEqual(len(ent), 1)


def _mk_groups(sizes, k):
    """sizes: {(repo, wf): n}。run id は組ごとに 1000 刻みで、created_at は id 昇順。"""
    listed = []
    for gi, ((repo, wf), n) in enumerate(sorted(sizes.items(), key=str)):
        for i in range(n):
            rid = (gi + 1) * 1000 + i
            day = (TODAY - timedelta(days=19) + timedelta(days=i % 19)).isoformat()
            listed.append((rid, 1, repo, run(rid, created=day + "T%02d:00:00Z" % (i // 19), workflow_id=wf)))
    g = fj.plan_samples(listed, k)
    fj.pilot_samples(g, k)
    return listed, g


def _ent(minutes, jobs=None):
    e = {"repo": "r", "attempt": 1, "created_at": "",
         "agg": [{"day": D0, "runner_group": "g", "minutes": minutes}] if minutes else []}
    if jobs is not None:
        e["jobs"] = {"hosted": jobs[0], "self_hosted": jobs[1]}
    return e


class EstimateAllocation(unittest.TestCase):
    """2 段階の抜き取り配分（Issue #205）。本番の plan_samples / pilot_samples / allocate_samples を使う。"""

    def test_pilot_samples(self):
        for n, k, want in ((1, 3, 1), (5, 3, 3), (5, 2, 2), (5, 1, 1), (2, 5, 2), (9, 5, 3)):
            _l, g = _mk_groups({("r", 1): n}, k)
            self.assertEqual(len(g[("r", 1)]["pilot"]), want, (n, k))
        _l, g = _mk_groups({("r", 1): 9}, 3)
        self.assertEqual(g[("r", 1)]["pilot"], {1000, 1004, 1008})
        _l, g = _mk_groups({("r", 1): 9}, 1)
        self.assertEqual(g[("r", 1)]["pilot"], g[("r", 1)]["sample"])

    def test_record_selfhosted_outside_pilot_blocks_cut(self):
        # #227 受入基準: 試し取り 3 run とも 0 分でも、記録上セルフホストの job がある組は打ち切らない
        k = 4
        _l, g = _mk_groups({("a", 1): 20}, k)
        ids = sorted(g[("a", 1)]["pilot"])
        cache = {str(r): _ent(0, (1, 0)) for r in ids}
        other = next(r[0] for r in g[("a", 1)]["pool"] if r[0] not in ids)
        cache[str(other)] = _ent(0, (0, 1))  # 未完了セルフホスト: agg は空だが記録が示す
        before = json.dumps(cache, sort_keys=True)
        info = fj.allocate_samples(g, k, cache, {})
        self.assertEqual(json.dumps(cache, sort_keys=True), before)
        self.assertEqual(info["zero_groups"], 0)
        self.assertGreaterEqual(len(g[("a", 1)]["sample"]), k)
        self.assertTrue(g[("a", 1)]["pilot"] <= g[("a", 1)]["sample"])

    def test_record_hosted_only_cuts(self):
        k = 4
        _l, g = _mk_groups({("a", 1): 20}, k)
        cache = {str(r): _ent(0, (2, 0)) for r in g[("a", 1)]["pilot"]}
        self.assertEqual(fj.allocate_samples(g, k, cache, {})["zero_groups"], 1)

    def test_record_selfhosted_in_pilot_not_cut(self):
        k = 4
        _l, g = _mk_groups({("a", 1): 20}, k)
        ids = sorted(g[("a", 1)]["pilot"])
        cache = {str(r): _ent(0, (1, 0)) for r in ids}
        cache[str(ids[1])] = _ent(0, (0, 1))
        self.assertEqual(fj.allocate_samples(g, k, cache, {})["zero_groups"], 0)

    def test_record_zero_jobs_is_not_evidence(self):
        # 意図的な厳格化: job が 0 件の run は 0 分の証拠にしない
        k = 4
        _l, g = _mk_groups({("a", 1): 20}, k)
        cache = {str(r): _ent(0, (0, 0)) for r in g[("a", 1)]["pilot"]}
        self.assertEqual(fj.allocate_samples(g, k, cache, {})["zero_groups"], 0)

    def test_no_record_fallback_and_nonempty_agg_elsewhere(self):
        k = 4
        _l, g = _mk_groups({("a", 1): 20}, k)
        cache = {str(r): _ent(0) for r in g[("a", 1)]["pilot"]}
        self.assertEqual(fj.allocate_samples(g, k, cache, {})["zero_groups"], 1)
        _l, g = _mk_groups({("a", 1): 20}, k)
        cache = {str(r): _ent(0) for r in g[("a", 1)]["pilot"]}
        other = next(r[0] for r in g[("a", 1)]["pool"] if r[0] not in g[("a", 1)]["pilot"])
        cache[str(other)] = _ent(7)
        self.assertEqual(fj.allocate_samples(g, k, cache, {})["zero_groups"], 0)

    def test_zero_group_cut_and_floor(self):
        k = 4
        listed, g = _mk_groups({("a", 1): 20, ("b", 1): 20, ("c", 1): 20, ("d", 1): 30, ("e", 1): 4}, k)
        cache = {}
        for key in (("a", 1), ("c", 1), ("d", 1)):  # a: 3 run とも 0 分、c: 末尾だけ 0 分でない、d: 0 分なし
            ids = sorted(g[key]["pilot"])
            cache[str(ids[0])] = _ent(0 if key != ("d", 1) else 5)
            cache[str(ids[1])] = _ent(0 if key != ("d", 1) else 5)
            cache[str(ids[2])] = _ent(0 if key == ("a", 1) else 5)
        # b は試し取りが skipped、e は n=4 で試し取り 3 run が 0 分（残り 1 run があり floor 4 > pilot 3）
        skipped = {str(r): {"attempt": 1} for r in g[("b", 1)]["pilot"]}
        for r in g[("e", 1)]["pilot"]:
            cache[str(r)] = _ent(0)
        before = json.dumps(cache, sort_keys=True)
        info = fj.allocate_samples(g, k, cache, skipped)
        self.assertEqual(json.dumps(cache, sort_keys=True), before)
        self.assertEqual(g[("a", 1)]["sample"], g[("a", 1)]["pilot"])
        self.assertEqual(g[("e", 1)]["sample"], g[("e", 1)]["pilot"])
        self.assertEqual(info["zero_groups"], 2)
        for key in (("b", 1), ("c", 1), ("d", 1)):
            self.assertGreaterEqual(len(g[key]["sample"]), min(len(g[key]["pool"]), k))
            self.assertTrue(g[key]["pilot"] <= g[key]["sample"])
        # 浮いた予算 (4-3) + (4-3) = 2 は run 数 20:20:30 で b/c/d へ比例。打ち切った run は (20-3) + (4-3)
        self.assertEqual(info["extra"], 2)
        self.assertEqual(info["zero_runs"], 18)
        self.assertEqual(sum(len(x["sample"]) for x in g.values()), info["budget"])
        # 試し取りが未取得なら打ち切らない
        _l, g2 = _mk_groups({("a", 1): 20}, k)
        fj.allocate_samples(g2, k, {}, {})
        self.assertEqual(len(g2[("a", 1)]["sample"]), k)

    def test_budget_cap_and_proportional(self):
        k = 5
        sizes = {("z", 1): 10, ("s", 1): 6, ("m", 1): 30, ("l", 1): 90}
        listed, g = _mk_groups(sizes, k)
        cache = {str(r): _ent(0) for r in g[("z", 1)]["pilot"]}
        info = fj.allocate_samples(g, k, cache, {})
        budget = sum(min(n, k) for n in sizes.values())
        self.assertEqual(info["budget"], budget)
        self.assertLessEqual(sum(len(x["sample"]) for x in g.values()), budget)
        for key, n in sizes.items():
            self.assertLessEqual(len(g[key]["sample"]), n)
        self.assertEqual(len(g[("z", 1)]["sample"]), 3)
        # 浮いた 2 run は 6:30:90 に比例し、大きい組が最も多く受け取る
        gain = {key: len(g[key]["sample"]) - min(n, k) for key, n in sizes.items() if key != ("z", 1)}
        self.assertEqual(sum(gain.values()), 2)
        self.assertGreaterEqual(gain[("l", 1)], gain[("m", 1)])
        # 全組が上限に達する場合は余りを使わない（上限 n=5 の組だけ・K=5）
        _l, g3 = _mk_groups({("z", 1): 8, ("t", 1): 6}, 6)
        c3 = {str(r): _ent(0) for r in g3[("z", 1)]["pilot"]}
        fj.allocate_samples(g3, 6, c3, {})
        self.assertEqual(len(g3[("t", 1)]["sample"]), 6)
        self.assertEqual(len(g3[("z", 1)]["sample"]), 3)

    def test_deterministic_and_nested(self):
        k = 4
        sizes = {("a", 1): 12, ("b", 2): 40, ("c", 3): 7, ("d", 4): 25}
        outs = []
        for order in (False, True):
            listed, g = _mk_groups(sizes, k)
            cache = {str(r): _ent(0) for r in g[("a", 1)]["pilot"]}
            cache.update({str(r): _ent(0) for r in g[("c", 3)]["pilot"]})
            items = list(g.items())
            if order:
                items.reverse()
            gg = dict(items)
            fj.allocate_samples(gg, k, cache, {})
            fj.allocate_samples(gg, k, cache, {})
            outs.append({key: sorted(x["sample"]) for key, x in g.items()})
            for x in g.values():
                self.assertTrue(x["pilot"] <= x["sample"])
        self.assertEqual(outs[0], outs[1])

    def test_k_le_3_matches_uniform(self):
        for k in (1, 2, 3):
            sizes = {("a", 1): 12, ("b", 2): 40, ("c", 3): 1}
            listed, g = _mk_groups(sizes, k)
            uni = fj.plan_samples(listed, k)
            cache = {str(r): _ent(0) for x in g.values() for r in x["pilot"]}
            fj.allocate_samples(g, k, cache, {})
            for key in sizes:
                self.assertEqual(g[key]["sample"], uni[key]["sample"], (k, key))

    def _population(self, seed):
        rnd = random.Random(seed)
        listed, truth, kinds = [], {}, {}
        rid = 0
        groups_spec = []
        for i in range(3):
            groups_spec.append(("big", rnd.randint(100, 300)))
        groups_spec += [("mid", rnd.randint(30, 60)) for _ in range(6)]
        groups_spec += [("small", rnd.randint(2, 10)) for _ in range(25)]
        groups_spec += [("zero", rnd.randint(4, 12)) for _ in range(40)]
        groups_spec += [("moved", rnd.randint(40, 90)) for _ in range(3)]
        groups_spec += [("sparse", rnd.randint(40, 90)) for _ in range(3)]
        for gi, (kind, n) in enumerate(groups_spec):
            key = ("repo%d" % (gi % 7), gi + 1)
            kinds[key] = kind
            mean = rnd.uniform(2, 40)
            cv = rnd.uniform(0.1, 0.8)
            for i in range(n):
                rid += 1
                day = (TODAY - timedelta(days=19) + timedelta(days=i * 19 // n)).isoformat()
                if kind == "zero":
                    m = 0.0
                elif kind == "moved":
                    m = 0.0 if i < n // 2 else max(0.1, rnd.gauss(mean, mean * cv))
                elif kind == "sparse":
                    m = 0.0 if rnd.random() < 0.5 else max(0.1, rnd.gauss(mean, mean * cv))
                else:
                    m = max(0.1, rnd.gauss(mean, mean * cv))
                truth[rid] = m
                listed.append((rid, 1, key[0], run(rid, created=day + "T%02d:00:00Z" % (i % 24), workflow_id=key[1])))
        return listed, truth

    @staticmethod
    def _total(groups, cache, truth):
        ent, _c = fj.estimate_entries(groups, cache, {})
        est = sum(a["minutes"] for e in ent for a in e["agg"])
        got = sum(e["agg"][0]["minutes"] if e["agg"] else 0.0 for e in cache.values())
        return est + got

    def test_total_error_smaller_with_same_calls(self):
        k = 5
        errs_old, errs_new = [], []
        for seed in range(15):
            listed, truth = self._population(seed)
            actual = sum(truth.values())
            go = fj.plan_samples(listed, k)
            co = {str(r): _ent(truth[r]) for g in go.values() for r in g["sample"]}
            old_calls = len(co)
            gn = fj.plan_samples(listed, k)
            fj.pilot_samples(gn, k)
            cn = {str(r): _ent(truth[r]) for g in gn.values() for r in g["pilot"]}
            fj.allocate_samples(gn, k, cn, {})
            cn = {str(r): _ent(truth[r]) for g in gn.values() for r in g["sample"]}
            new_calls = len(cn)
            self.assertLessEqual(new_calls, old_calls, seed)
            for key in gn:
                if not all(truth[r] == 0 for r in gn[key]["pilot"]):  # 打ち切り対象外の組は均等配分より減らさない
                    self.assertGreaterEqual(len(gn[key]["sample"]), len(go[key]["sample"]), (seed, key))
            errs_old.append(abs(self._total(go, co, truth) - actual))
            errs_new.append(abs(self._total(gn, cn, truth) - actual))
        self.assertLess(sum(errs_new) / len(errs_new), sum(errs_old) / len(errs_old),
                        (errs_old, errs_new))


    def test_ends_zero_middle_positive_not_cut(self):
        """Issue #218: 両端 0 分・中間 > 0 分の組を 0 分として打ち切らない。3 run とも 0 分の組は打ち切る。"""
        for k in (4, 5):
            listed, g = _mk_groups({("mid", 1): 20, ("zero", 1): 20}, k)
            cache = {}
            ids = sorted(g[("mid", 1)]["pilot"])
            cache[str(ids[0])] = _ent(0)
            cache[str(ids[1])] = _ent(7)  # 中央の試し取りだけセルフホスト分あり
            cache[str(ids[2])] = _ent(0)
            cache.update({str(r): _ent(0) for r in g[("zero", 1)]["pilot"]})
            info = fj.allocate_samples(g, k, cache, {})
            self.assertEqual(info["zero_groups"], 1, k)
            self.assertEqual(info["zero_runs"], 17, k)
            self.assertGreaterEqual(len(g[("mid", 1)]["sample"]), k)
            self.assertEqual(g[("zero", 1)]["sample"], g[("zero", 1)]["pilot"])
            full = {str(r): _ent(0) for r in g[("mid", 1)]["sample"]}
            full[str(ids[1])] = _ent(7)
            ent, _c = fj.estimate_entries(g, dict(cache, **full), {})
            self.assertGreater(sum(a["minutes"] for e in ent for a in e["agg"]), 0, k)

    def test_nested_and_budget_sweep(self):
        """試し取りは最終の抜き取りに含まれ、大きさは floor + extra、合計は予算以下で、同じ入力は同じ結果。"""
        for n in range(4, 31):
            for k in range(3, min(n, 20) + 1):
                sizes = {("z", 1): n, ("p", 1): n + 7, ("q", 2): 50}
                listed, g = _mk_groups(sizes, k)
                cache = {str(r): _ent(0) for r in g[("z", 1)]["pilot"]}
                info = fj.allocate_samples(g, k, cache, {})
                first = {key: set(x["sample"]) for key, x in g.items()}
                info2 = fj.allocate_samples(g, k, cache, {})
                self.assertEqual(info, info2)
                self.assertEqual(first, {key: set(x["sample"]) for key, x in g.items()})
                for key, x in g.items():
                    self.assertTrue(x["pilot"] <= x["sample"], (n, k, key))
                    self.assertLessEqual(len(x["sample"]), len(x["pool"]), (n, k, key))
                    if key != ("z", 1) or k == 3:  # 打ち切った 0 分組は下限の対象外（K=3 は打ち切らない）
                        self.assertGreaterEqual(len(x["sample"]), min(len(x["pool"]), k), (n, k, key))
                self.assertLessEqual(sum(len(x["sample"]) for x in g.values()), info["budget"], (n, k))
                extra_total = sum(len(g[key]["sample"]) - min(sz, k) for key, sz in sizes.items() if key != ("z", 1))
                self.assertEqual(extra_total, info["extra"], (n, k))


class Cli(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="fj-test-")
        self.stub = os.path.join(self.root, "stub")
        self.work = os.path.join(self.root, "work")
        os.makedirs(self.stub)
        os.makedirs(self.work)
        gh = os.path.join(self.stub, "gh")
        with open(gh, "w") as f:
            f.write(STUB % {"py": sys.executable})
        os.chmod(gh, 0o755)
        self.cfg = base_cfg()
        self.write_cfg()
        self.out = os.path.join(self.work, "jobs.json")
        self.cache = os.path.join(self.work, "cache")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def write_cfg(self):
        with open(os.path.join(self.stub, "cfg.json"), "w") as f:
            json.dump(self.cfg, f)

    def calls(self):
        p = os.path.join(self.stub, "calls.log")
        if not os.path.exists(p):
            return []
        with open(p) as f:
            return f.read().splitlines()

    def reset_calls(self):
        p = os.path.join(self.stub, "calls.log")
        if os.path.exists(p):
            os.unlink(p)

    def run_cli(self, extra=None, out=None, cache=None, org=ORG, since=SINCE):
        env = dict(os.environ, PATH=self.stub + os.pathsep + os.environ.get("PATH", ""), STUB_DIR=self.stub)
        cmd = [sys.executable, "-I", "-B", SCRIPT, "--org", org, "--since", since,
               "--out", out or self.out, "--cache", cache or self.cache] + (extra or [])
        return subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=self.work)

    def rows(self, path=None):
        with open(path or self.out) as f:
            return json.load(f)["rows"]

    def test_first_run(self):
        r = self.run_cli()
        self.assertEqual(r.returncode, 0, r.stderr)
        rows = self.rows()
        self.assertEqual([(x["day"], x["repo"], x["runner_group"], x["minutes"]) for x in rows],
                         [(D0, "priv-api", "build-farm", 90.0),
                          (D0, "pub-site", "build-farm", 10.0)])
        # run 2 の job 201 は 23:30 開始 90 分で開始日に計上、202 は未完了で除外。run 3 は保留
        self.assertEqual({(x["repo"], x["minutes"]) for x in rows}, {("priv-api", 90.0), ("pub-site", 10.0)})
        self.assertFalse(any("old-archived" in c or "/9/" in c for c in self.calls()))
        lines = r.stdout.splitlines()
        self.assertLessEqual(len(lines), 10)
        for name in ("pub-site", "priv-api", "old-archived"):
            self.assertNotIn(name, r.stdout)
        self.assertEqual([n for n in os.listdir(self.work) if n.startswith(".")], [])

    def test_incremental_and_pending_and_retry(self):
        self.assertEqual(self.run_cli().returncode, 0)
        self.reset_calls()
        self.cfg["runs"]["pub-site"].append(run(4))
        self.cfg["jobs"]["4"] = [job(401, ["self-hosted"], "g2", D1 + "T01:00:00+00:00", 4)]
        self.cfg["jobs"]["3"] = [job(301, ["self-hosted"], "build-farm", D0 + "T10:00:00+00:00", 7)]
        self.cfg["runs"]["priv-api"][1]["status"] = "completed"  # 保留していた run 3 が完了
        self.write_cfg()
        r = self.run_cli()
        self.assertEqual(r.returncode, 0, r.stderr)
        jobs_calls = [c for c in self.calls() if c.endswith("/jobs") or "/jobs " in c]
        ids = sorted(c.split("/runs/")[1].split("/")[0] for c in jobs_calls)
        self.assertEqual(ids, ["3", "4"])  # 既知の run 1・2 は取らない
        self.assertTrue(any("actions/runs " in c or c.endswith("actions/runs") or "actions/runs -f" in c
                            for c in self.calls()))
        total = sum(x["minutes"] for x in self.rows())
        self.assertEqual(total, 10 + 90 + 7 + 4)
        # 再実行（attempt 増）で取り直す
        self.reset_calls()
        self.cfg["runs"]["pub-site"][0]["run_attempt"] = 2
        self.cfg["jobs"]["1"] = [job(103, ["self-hosted"], "build-farm", D0 + "T10:00:00+00:00", 20)]
        self.write_cfg()
        self.assertEqual(self.run_cli().returncode, 0)
        self.assertEqual(sum(x["minutes"] for x in self.rows()), 20 + 90 + 7 + 4)

    def test_rerun_pending_excluded_and_invalid_job_skipped(self):
        self.assertEqual(self.run_cli().returncode, 0)
        before = sum(x["minutes"] for x in self.rows())
        # run 1 が再実行中（attempt 増・未完了）: 完了済みの旧 attempt の分は集計に残す
        self.cfg["runs"]["pub-site"][0]["run_attempt"] = 2
        self.cfg["runs"]["pub-site"][0]["status"] = "in_progress"
        self.write_cfg()
        self.assertEqual(self.run_cli().returncode, 0)
        self.assertEqual(sum(x["minutes"] for x in self.rows()), before)
        # 不正な job を含む run はスキップし、全体は成功する
        self.cfg["runs"]["pub-site"][0]["status"] = "completed"
        self.cfg["jobs"]["1"] = [{"id": "bad", "labels": []}]
        self.write_cfg()
        r = self.run_cli()
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(self.out) as f:
            self.assertTrue(any("スキップ" in w for w in json.load(f)["warnings"]))

    def test_rate_limit_resume_matches_uninterrupted(self):
        # 比較用: 中断なし
        ref_out = os.path.join(self.root, "ref.json")
        self.assertEqual(self.run_cli(out=ref_out, cache=os.path.join(self.root, "refcache")).returncode, 0)
        self.reset_calls()
        # 本番: 途中で残量不足
        self.cfg["low_after"] = 4  # repos 1 + runs 2 + jobs 1 回の後で残量不足にする
        self.write_cfg()
        r = self.run_cli(["--rate-check-every", "1"])
        self.assertEqual(r.returncode, 3, r.stderr + r.stdout)
        self.assertFalse(os.path.exists(self.out))
        with open(os.path.join(self.cache, "jobs-cache-%s.json" % ORG)) as f:
            first = json.load(f)["runs"]
        self.assertTrue(len(first) >= 1)
        self.reset_calls()
        self.cfg["low_after"] = None
        self.write_cfg()
        r = self.run_cli(["--rate-check-every", "1"])
        self.assertEqual(r.returncode, 0, r.stderr)
        for rid in first:
            self.assertFalse(any("/runs/%s/jobs" % rid in c for c in self.calls()))
        self.assertEqual(self.rows(), self.rows(ref_out))

    def test_jobs_requested_with_all_filter(self):
        self.assertEqual(self.run_cli().returncode, 0)
        jobs_calls = [c for c in self.calls() if "/jobs" in c]
        self.assertTrue(jobs_calls)
        self.assertTrue(all("filter=all" in c for c in jobs_calls))

    def test_cache_keeps_run_with_in_period_job(self):
        # 期間前（10/09）に作られた run が、期間内（10/10）開始の job を持つ
        self.cfg["runs"]["pub-site"] = [run(1, created=DM1 + "T23:00:00Z")]
        self.cfg["runs"]["priv-api"] = []
        self.write_cfg()
        self.assertEqual(self.run_cli(since=SINCE).returncode, 0)
        self.assertEqual(sum(x["minutes"] for x in self.rows()), 10)
        for _ in range(2):  # 期間を狭めても、繰り返しても分数が減らない
            self.assertEqual(self.run_cli(since=D0).returncode, 0)
            self.assertEqual(sum(x["minutes"] for x in self.rows()), 10)

    def test_renamed_repo_keeps_cached_minutes(self):
        self.assertEqual(self.run_cli().returncode, 0)
        before = sum(x["minutes"] for x in self.rows())
        self.cfg["repos"][0]["name"] = "pub-site-renamed"
        self.cfg["runs"]["pub-site-renamed"] = self.cfg["runs"].pop("pub-site")
        self.write_cfg()
        self.assertEqual(self.run_cli().returncode, 0)
        rows = self.rows()
        self.assertEqual(sum(x["minutes"] for x in rows), before)
        self.assertIn("pub-site-renamed", {x["repo"] for x in rows})

    def test_deleted_run_stays_in_output(self):
        self.assertEqual(self.run_cli().returncode, 0)
        before = sum(x["minutes"] for x in self.rows())
        self.cfg["runs"]["pub-site"] = []  # GitHub 側で run が一覧から消えた
        self.write_cfg()
        self.assertEqual(self.run_cli().returncode, 0)
        self.assertEqual(sum(x["minutes"] for x in self.rows()), before)

    def test_listing_resumes_after_rate_limit(self):
        self.cfg["low_after"] = 3  # repos 1 + runs 1 回の後で残量不足にする
        self.write_cfg()
        r = self.run_cli(["--rate-check-every", "1"])
        self.assertEqual(r.returncode, 3, r.stderr + r.stdout)
        with open(os.path.join(self.cache, "jobs-cache-%s.json" % ORG)) as f:
            saved = json.load(f)
        self.assertTrue(saved["listing"]["repos"])
        done = list(saved["listing"]["repos"])
        self.reset_calls()
        self.cfg["low_after"] = None
        self.write_cfg()
        r = self.run_cli(["--rate-check-every", "1"])
        self.assertEqual(r.returncode, 0, r.stderr)
        for repo in done:
            had_pending = any(i["status"] != "completed" for i in saved["listing"]["repos"][repo])
            relisted = any("repos/%s/%s/actions/runs " % (ORG, repo) in c for c in self.calls())
            # 未完了 run を含むリポジトリだけ一覧を取り直し、完了済みのみなら再利用する
            self.assertEqual(relisted, had_pending)
        with open(os.path.join(self.cache, "jobs-cache-%s.json" % ORG)) as f:
            self.assertNotIn("listing", json.load(f))

    def _resume_with_listed_at(self, value):
        self.cfg["low_after"] = 3
        self.write_cfg()
        self.assertEqual(self.run_cli(["--rate-check-every", "1"]).returncode, 3)
        cp = os.path.join(self.cache, "jobs-cache-%s.json" % ORG)
        with open(cp) as f:
            saved = json.load(f)
        saved["listing"]["listed_at"] = value
        with open(cp, "w") as f:
            json.dump(saved, f)
        self.cfg["low_after"] = None
        self.write_cfg()
        r = self.run_cli(["--rate-check-every", "1"])
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(self.out) as f:
            warnings = json.load(f).get("warnings", [])
        return r, warnings

    def test_listed_at_with_control_chars_becomes_unknown(self):
        r, warnings = self._resume_with_listed_at("\x1b]0;x\x07\x1b[2J\x9b")
        text = r.stdout + r.stderr + "".join(warnings)
        for ch in ("\x1b", "\x07", "\x9b"):
            self.assertNotIn(ch, text)
        self.assertIn("中断前（不明）", r.stdout + r.stderr)
        self.assertTrue(any("不明 時点の取得を再利用" in w for w in warnings), warnings)

    def test_listed_at_non_string_becomes_unknown(self):
        r, warnings = self._resume_with_listed_at(12345)
        self.assertTrue(any("不明 時点の取得を再利用" in w for w in warnings), warnings)

    def test_listed_at_valid_is_kept(self):
        r, warnings = self._resume_with_listed_at("2026-10-23T00:00:00Z")
        self.assertTrue(any("2026-10-23T00:00:00Z 時点" in w for w in warnings), warnings)

    def test_paging(self):
        self.cfg["runs"] = {"pub-site": [run(i) for i in range(1, 121)]}
        self.cfg["repos"] = self.cfg["repos"][:1]
        self.cfg["jobs"] = {str(i): [] for i in range(1, 121)}
        self.cfg["jobs"]["5"] = [job(1000 + i, ["self-hosted"], "g", D0 + "T10:00:00+00:00", 1)
                                 for i in range(150)]
        self.write_cfg()
        r = self.run_cli()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.rows()[0]["minutes"], 150.0)
        self.assertTrue(any("page=2" in c and "/runs/5/jobs" in c for c in self.calls()))
        self.assertTrue(any("actions/runs " in c and "page=2" in c for c in self.calls()))

    def test_search_cap_split(self):
        # 1,000 件上限を超える期間は二分して取り直し、欠けも重複もないことを一覧件数で確かめる
        runs = []
        rid = 1
        for day in (D0, D1, D2):
            for _ in range(400):
                runs.append(run(rid, created=day + "T00:00:00Z", status="queued"))
                rid += 1
        self.cfg["repos"] = self.cfg["repos"][:1]
        self.cfg["runs"] = {"pub-site": runs}
        self.cfg["jobs"] = {}
        self.write_cfg()
        r = self.run_cli()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("一覧 1200", r.stdout)
        self.assertNotIn("警告", r.stdout)

    def api_calls(self):
        return [c for c in self.calls() if not c.startswith("api rate_limit")]

    def test_gh_failure_keeps_cache_no_out(self):
        self.cfg["fail_after"] = 4
        self.write_cfg()
        r = self.run_cli(["--retry-wait", "0"])
        self.assertEqual(r.returncode, 1)
        self.assertFalse(os.path.exists(self.out))
        self.assertIn("gh api 失敗", r.stderr)
        # 失敗した 1 回目 + 既定の再試行 3 回
        self.assertEqual(len(self.api_calls()), 4 + 1 + fj.DEFAULT_RETRIES)

    def test_transient_5xx_then_success(self):
        base = self.run_cli()
        self.assertEqual(base.returncode, 0, base.stderr)
        expected = self.rows()
        os.unlink(self.out)
        shutil.rmtree(self.cache)
        self.reset_calls()
        self.cfg["fail5xx_until"] = 2
        self.write_cfg()
        r = self.run_cli(["--retry-wait", "0"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.rows(), expected)
        self.assertIn("5xx 再試行 2 回", r.stdout)

    def test_transient_5xx_on_rate_limit(self):
        self.cfg["rate5xx_times"] = 2
        self.write_cfg()
        r = self.run_cli(["--retry-wait", "0"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("5xx 再試行 2 回", r.stdout)

    def test_5xx_exhausted_exits_1(self):
        self.cfg["fail_after"] = 4
        self.cfg["fail_status"] = 502
        self.write_cfg()
        r = self.run_cli(["--retry-wait", "0", "--retries", "2"])
        self.assertEqual(r.returncode, 1)
        self.assertFalse(os.path.exists(self.out))
        for word in ("gh api 失敗", "HTTP 502", "5xx"):
            self.assertIn(word, r.stderr)
        self.assertEqual(len(self.api_calls()), 4 + 1 + 2)

    def test_4xx_not_retried(self):
        self.cfg["fail_after"] = 4
        self.cfg["fail_status"] = 404
        self.write_cfg()
        r = self.run_cli(["--retry-wait", "0"])
        self.assertEqual(r.returncode, 1)
        self.assertEqual(len(self.api_calls()), 4 + 1)
        self.assertNotIn("5xx", r.stderr)

    def test_retries_zero_does_not_retry(self):
        self.cfg["fail_after"] = 4
        self.cfg["fail_status"] = 502
        self.write_cfg()
        r = self.run_cli(["--retries", "0"])
        self.assertEqual(r.returncode, 1)
        self.assertEqual(len(self.api_calls()), 4 + 1)

    def bad_minutes_bodies(self):
        """agg[].minutes の不正値（bool・文字列・欠損・None・NaN・inf・巨大な整数）。受理集合は nonneg_num と同一（Issue #182）。"""
        def body(agg_item):
            return json.dumps({"schema_version": 1, "org": ORG, "runs": {"1": {
                "repo": "pub-site", "attempt": 1, "created_at": "",
                "agg": [agg_item]}}})
        base = {"day": D0, "runner_group": "g"}
        bodies = [body(dict(base, minutes=v)) for v in (True, "1", None, float("nan"))]
        bodies.append(body(base))
        # 1e400 は json.dumps で作れないため生の JSON を使う（読み込むと inf になる）
        bodies.append(body(dict(base, minutes=0)).replace('"minutes": 0', '"minutes": 1e400'))
        # 10**400 は 4300 桁未満なので JSON 解析を通って int になり、nonneg_num の float 変換で失敗する
        bodies.append(body(dict(base, minutes=10**400)))
        return tuple(bodies)

    def test_bad_cache_stops(self):
        os.makedirs(self.cache)
        p = os.path.join(self.cache, "jobs-cache-%s.json" % ORG)
        for body in ("{not json", json.dumps({"schema_version": 1, "org": "other", "runs": {}}),
                     json.dumps({"schema_version": 1, "org": ORG, "runs": {"1": {
                         "repo": "pub-site", "attempt": 1, "created_at": "",
                         "agg": [{"day": D0, "runner_group": "g", "minutes": -1}]}}}),
                     ) + tuple(json.dumps({"schema_version": 1, "org": ORG, "runs": {"1": dict(
                         {"repo": "pub-site", "attempt": 1, "agg": []}, **extra)}})
                         for extra in ({}, {"created_at": None}, {"created_at": 20261010})
                     ) + tuple(json.dumps({"schema_version": 1, "org": ORG, "listing": {
                         "since": SINCE, "day": D0, "repos": {"pub-site": [dict(run(1), workflow_id=wf)]},
                         "truncated": []}, "runs": {}}) for wf in ("x", True, 0, -1)
                     ) + self.bad_minutes_bodies():
            with open(p, "w") as f:
                f.write(body)
            r = self.run_cli()
            self.assertEqual(r.returncode, 1, body)
            self.assertIn("キャッシュが壊れている", r.stderr)
            self.assertNotIn("Traceback", r.stderr, body)
            with open(p) as f:
                self.assertEqual(f.read(), body)
            self.assertFalse(os.path.exists(self.out))
        self.assertEqual(self.calls(), [])

    def test_bad_job_records_stop(self):
        os.makedirs(self.cache)
        p = os.path.join(self.cache, "jobs-cache-%s.json" % ORG)
        bodies = [json.dumps({"schema_version": 3, "org": ORG, "runs": {}})]
        for jr in ("x", [], {"hosted": 1}, {"hosted": True, "self_hosted": 0}, {"hosted": 1.5, "self_hosted": 0},
                   {"hosted": -1, "self_hosted": 0}, {"hosted": 10001, "self_hosted": 0},
                   {"hosted": "1", "self_hosted": 0}):
            bodies.append(json.dumps({"schema_version": 2, "org": ORG, "runs": {"1": {
                "repo": "pub-site", "attempt": 1, "created_at": "", "agg": [], "jobs": jr}}}))
        for body in bodies:
            with open(p, "w") as f:
                f.write(body)
            r = self.run_cli()
            self.assertEqual(r.returncode, 1, body)
            self.assertIn("キャッシュが壊れている", r.stderr)
            with open(p) as f:
                self.assertEqual(f.read(), body)
            self.assertFalse(os.path.exists(self.out))

    def test_v1_cache_migrates_to_v2(self):
        os.makedirs(self.cache)
        p = os.path.join(self.cache, "jobs-cache-%s.json" % ORG)
        with open(p, "w") as f:
            json.dump({"schema_version": 1, "org": ORG, "runs": {"7777": {
                "repo": "pub-site", "attempt": 1, "created_at": D0 + "T00:00:00Z", "agg": []}}}, f)
        r = self.run_cli()
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(p) as f:
            c = json.load(f)
        self.assertEqual(c["schema_version"], 2)
        self.assertIn("7777", c["runs"])
        self.assertNotIn("jobs", c["runs"]["7777"])
        with open(self.out) as f:
            self.assertEqual(json.load(f)["schema_version"], 1)  # 出力文書の版は据え置き

    def runs_list_calls(self):
        return [c for c in self.calls() if "actions/runs -f" in c]

    def test_second_run_lists_fewer_runs(self):
        # 受け入れ条件: 2 回目以降は runs 一覧の API 呼び出しが減る（前回位置から取る）
        old = (TODAY - timedelta(days=60)).isoformat() + "T00:00:00Z"
        recent = (TODAY - timedelta(days=3)).isoformat() + "T00:00:00Z"
        self.cfg["repos"] = self.cfg["repos"][:1]
        self.cfg["runs"] = {"pub-site": [run(i, created=old) for i in range(1, 151)] + [run(1000, created=recent)]}
        self.cfg["jobs"] = {}
        self.write_cfg()
        since = (TODAY - timedelta(days=90)).isoformat()
        self.assertEqual(self.run_cli(since=since).returncode, 0)
        first = self.runs_list_calls()
        first_rows = self.rows()
        self.reset_calls()
        r = self.run_cli(since=since)
        self.assertEqual(r.returncode, 0, r.stderr)
        second = self.runs_list_calls()
        self.assertEqual((len(first), len(second)), (2, 1))
        floor = (TODAY - timedelta(days=fj.RELIST_WINDOW_DAYS)).isoformat()
        self.assertIn("created=>=%s" % floor, second[0])
        self.assertEqual(self.rows(), first_rows)
        self.assertIn("前回位置から 1 リポ", r.stdout)
        self.assertLessEqual(len(r.stdout.splitlines()), 10)

    def test_pending_old_run_keeps_lower_bound(self):
        d = (TODAY - timedelta(days=60)).isoformat()
        self.cfg["repos"] = self.cfg["repos"][:1]
        self.cfg["runs"] = {"pub-site": [run(1, created=d + "T00:00:00Z"),
                                         run(2, created=d + "T00:00:00Z", status="in_progress")]}
        self.cfg["jobs"] = {"1": [job(101, ["self-hosted"], "g", d + "T10:00:00+00:00", 5)]}
        self.write_cfg()
        since = (TODAY - timedelta(days=90)).isoformat()
        self.assertEqual(self.run_cli(since=since).returncode, 0)
        self.reset_calls()
        self.cfg["runs"]["pub-site"][1]["status"] = "completed"
        self.cfg["jobs"]["2"] = [job(201, ["self-hosted"], "g", d + "T11:00:00+00:00", 7)]
        self.write_cfg()
        self.assertEqual(self.run_cli(since=since).returncode, 0)
        # 未完了だった run の作成日から一覧するため、窓より古くても完了後に拾える
        self.assertIn("created=>=%s" % d, self.runs_list_calls()[0])
        self.assertEqual(sum(x["minutes"] for x in self.rows()), 12)

    def test_earlier_since_relists_full_range(self):
        self.assertEqual(self.run_cli().returncode, 0)
        self.reset_calls()
        earlier = (TODAY - timedelta(days=30)).isoformat()
        self.assertEqual(self.run_cli(since=earlier).returncode, 0)
        lists = self.runs_list_calls()
        self.assertEqual(len(lists), 2)
        self.assertTrue(all("created=>=%s" % earlier in c for c in lists))

    def test_truncated_warning_persists_on_second_run(self):
        self.cfg["repos"] = self.cfg["repos"][:1]
        self.cfg["runs"] = {"pub-site": [run(i, created=D0 + "T00:00:00Z", status="queued") for i in range(1, 1101)]}
        self.cfg["jobs"] = {}
        self.write_cfg()
        for _ in range(2):
            r = self.run_cli()
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("1,000 件上限", r.stdout)

    def test_tampered_future_cursor_does_not_skip_listing(self):
        wide = (TODAY - timedelta(days=90)).isoformat()
        self.assertEqual(self.run_cli(since=wide).returncode, 0)
        p = os.path.join(self.cache, "jobs-cache-%s.json" % ORG)
        with open(p) as f:
            c = json.load(f)
        future = (TODAY + timedelta(days=100)).isoformat()
        c["cursor"] = {"pub-site": {"since": wide, "from": future}}
        with open(p, "w") as f:
            json.dump(c, f)
        self.reset_calls()
        self.assertEqual(self.run_cli(since=wide).returncode, 0)
        floor = (TODAY - timedelta(days=fj.RELIST_WINDOW_DAYS)).isoformat()
        self.assertTrue(any("repos/%s/pub-site/actions/runs" % ORG in c and "created=>=%s" % floor in c
                            for c in self.calls()))

    def test_bad_cursor_stops(self):
        os.makedirs(self.cache)
        p = os.path.join(self.cache, "jobs-cache-%s.json" % ORG)
        good = {"since": SINCE, "from": SINCE}
        for cur in ("x", {"bad name!": good}, {"pub-site": "x"}, {"pub-site": {"since": SINCE}},
                    {"pub-site": {"since": SINCE, "from": "2026-13-01"}},
                    {"pub-site": {"since": SINCE, "from": "2026-10-1"}}):
            body = json.dumps({"schema_version": 1, "org": ORG, "runs": {}, "cursor": cur})
            with open(p, "w") as f:
                f.write(body)
            r = self.run_cli()
            self.assertEqual(r.returncode, 1, cur)
            self.assertIn("キャッシュが壊れている", r.stderr)
        self.assertEqual(self.calls(), [])

    def test_progress_stderr(self):
        r = self.run_cli(["--progress-interval", "0"])
        self.assertEqual(r.returncode, 0, r.stderr)
        prog = [l for l in r.stderr.splitlines() if l.startswith("進捗:")]
        self.assertTrue(any("runs 一覧 2/2" in l for l in prog), prog)
        self.assertTrue(any("jobs 取得 2/2" in l for l in prog), prog)
        for name in ("pub-site", "priv-api", "old-archived"):
            self.assertNotIn(name, r.stderr)
        # 既定の間隔では高速なスタブの進捗は各フェーズの最後の 1 行ずつに収まる
        self.reset_calls()
        shutil.rmtree(self.cache)
        r = self.run_cli()
        self.assertLessEqual(len([l for l in r.stderr.splitlines() if l.startswith("進捗:")]), 2)

    def test_max_seconds_resume_matches_uninterrupted(self):
        ref_out = os.path.join(self.root, "ref.json")
        self.assertEqual(self.run_cli(out=ref_out, cache=os.path.join(self.root, "refcache")).returncode, 0)
        codes = []
        for _ in range(30):
            r = self.run_cli(["--max-seconds", "0.001"])
            codes.append(r.returncode)
            if r.returncode == 0:
                break
            self.assertEqual(r.returncode, 4, r.stderr)
            self.assertFalse(os.path.exists(self.out))
            self.assertIn("すぐ再実行", r.stderr)
        self.assertEqual(codes[-1], 0, codes)
        self.assertIn(4, codes)
        self.assertEqual(self.rows(), self.rows(ref_out))

    def test_max_seconds_invalid_job_run_does_not_block_progress(self):
        # 不正な job を含む run が時間上限をまたいでも、同じ周回では取り直さず後続の run へ進む
        self.cfg["jobs"]["1"] = [{"id": "bad", "labels": []}]
        self.write_cfg()
        codes = []
        for _ in range(30):
            r = self.run_cli(["--max-seconds", "0.001"])
            codes.append(r.returncode)
            if r.returncode == 0:
                break
            self.assertEqual(r.returncode, 4, r.stderr)
        self.assertEqual(codes[-1], 0, codes)
        bad_fetches = [c for c in self.calls() if "actions/runs/1/jobs" in c]
        self.assertEqual(len(bad_fetches), 1, bad_fetches)
        with open(self.out) as f:
            self.assertTrue(any("スキップ" in w and "run 1" in w for w in json.load(f)["warnings"]))
        self.assertTrue(len(self.rows()) >= 1)
        # 周回が終わった（listing を捨てた）後の実行では、スキップ分も再試行される
        self.reset_calls()
        self.assertEqual(self.run_cli().returncode, 0)
        self.assertTrue(any("actions/runs/1/jobs" in c for c in self.calls()))

    def test_killed_run_keeps_checkpoint(self):
        ref_out = os.path.join(self.root, "ref.json")
        self.assertEqual(self.run_cli(out=ref_out, cache=os.path.join(self.root, "refcache")).returncode, 0)
        self.reset_calls()
        self.cfg["sleep_after"] = 3  # repos 1 + runs 2 回の後の API 呼び出し（jobs）で眠る
        self.write_cfg()
        env = dict(os.environ, PATH=self.stub + os.pathsep + os.environ.get("PATH", ""), STUB_DIR=self.stub)
        cmd = [sys.executable, "-I", "-B", SCRIPT, "--org", ORG, "--since", SINCE, "--out", self.out,
               "--cache", self.cache, "--progress-interval", "0"]
        proc = subprocess.Popen(cmd, env=env, cwd=self.work, start_new_session=True,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.time() + 20
            while time.time() < deadline and len([c for c in self.calls() if not c.startswith("api rate_limit")]) < 4:
                time.sleep(0.05)
            time.sleep(0.2)
        finally:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
        self.assertFalse(os.path.exists(self.out))
        with open(os.path.join(self.cache, "jobs-cache-%s.json" % ORG)) as f:
            saved = json.load(f)
        self.assertTrue(saved.get("listing", {}).get("repos") or saved["runs"])
        self.cfg["sleep_after"] = None
        self.write_cfg()
        self.assertEqual(self.run_cli().returncode, 0)
        self.assertEqual(self.rows(), self.rows(ref_out))

    def test_relist_floor_and_next_cursor(self):
        t = fj.date(2026, 10, 23)
        base = t - timedelta(days=fj.RELIST_WINDOW_DAYS)
        since = fj.date(2026, 6, 1)
        self.assertEqual(fj.relist_floor(None, since, t), since)
        ent = {"since": since.isoformat(), "from": base.isoformat()}
        self.assertEqual(fj.relist_floor(ent, since, t), base)
        self.assertEqual(fj.relist_floor(ent, fj.date(2026, 5, 1), t), fj.date(2026, 5, 1))
        self.assertEqual(fj.relist_floor(dict(ent, **{"from": "2027-01-01"}), since, t), base)
        items = [{"id": 1, "run_attempt": 1, "created_at": "2026-08-01T00:00:00Z"},
                 {"id": 2, "run_attempt": 1, "created_at": "2026-09-01T00:00:00Z"}]
        cur = fj.next_cursor_entry(items, lambda i, a: i == 2, [fj.date(2026, 7, 1)], since, t)
        self.assertEqual(cur, {"since": "2026-06-01", "from": "2026-07-01"})
        cur = fj.next_cursor_entry(items, lambda i, a: True, [], since, t)
        self.assertEqual(cur["from"], base.isoformat())
        cur = fj.next_cursor_entry([{"id": 3, "run_attempt": 1, "created_at": ""}], lambda i, a: False, [], since, t)
        self.assertEqual(cur["from"], "2026-06-01")

    def test_bad_args_never_call_gh(self):
        future = (datetime.now(timezone.utc) + timedelta(days=3)).date().isoformat()
        a_file = os.path.join(self.work, "afile")
        open(a_file, "w").close()
        cases = [
            dict(extra=["--max-seconds", "-1"]), dict(extra=["--progress-interval", "-1"]),
            dict(extra=["--max-seconds", "nan"]),
            dict(extra=["--retries", "-1"]), dict(extra=["--retries", "11"]),
            dict(extra=["--retry-wait", "-1"]), dict(extra=["--retry-wait", "61"]),
            dict(extra=["--retry-wait", "nan"]), dict(extra=["--retry-wait", "inf"]),
            dict(org="bad_org!"), dict(since="2026-10-1"), dict(since="2026-13-01"), dict(since=future),
            dict(out=self.work), dict(out=os.path.join(self.work, "nodir", "x.json")), dict(cache=a_file),
            dict(extra=["--estimate", "--sample-runs", "0"]), dict(extra=["--estimate", "--sample-runs", "-1"]),
            dict(extra=["--estimate", "--sample-runs", "21"]), dict(extra=["--estimate", "--sample-runs", "x"]),
            dict(extra=["--sample-runs", "3"]),
        ]
        for kw in cases:
            r = self.run_cli(**kw)
            self.assertEqual(r.returncode, 1, kw)
            self.assertEqual(self.calls(), [], kw)
        self.assertEqual(os.listdir(self.work), ["afile"])
        self.assertFalse(os.path.exists(self.cache))
        self.assertFalse(os.path.exists(self.out))


class EstimateCli(unittest.TestCase):
    """推定モードの E2E（Issue #197）。Cli の setUp と helper だけを借り、Cli のテストは再実行しない。"""

    setUp = Cli.setUp
    tearDown = Cli.tearDown
    write_cfg = Cli.write_cfg
    calls = Cli.calls
    reset_calls = Cli.reset_calls
    run_cli = Cli.run_cli
    rows = Cli.rows

    def make_cfg(self):
        # 2 リポ x 2 workflow、各組 9 完了 run + 未完了 1 run。wf 1 は途中で GitHub ホストへ移行、wf 2 は一定 10 分
        cfg = {"repos": [{"name": "pub-site", "visibility": "public", "archived": False},
                         {"name": "priv-api", "visibility": "private", "archived": False}],
               "runs": {}, "jobs": {}}
        self.groups = {}
        for ri, repo in enumerate(("pub-site", "priv-api")):
            cfg["runs"][repo] = []
            for wf in (1, 2):
                ids = []
                for n in range(9):
                    rid = (ri * 2 + wf) * 1000 + n
                    day = (TODAY - timedelta(days=18 - n)).isoformat()
                    cfg["runs"][repo].append(run(rid, created=day + "T00:00:00Z", workflow_id=wf))
                    if wf == 1 and n >= 5:
                        cfg["jobs"][str(rid)] = [job(rid * 10, ["ubuntu-latest"], "GitHub Actions", day + "T01:00:00+00:00", 5)]
                    else:
                        cfg["jobs"][str(rid)] = [job(rid * 10, ["self-hosted"], "g", day + "T01:00:00+00:00",
                                                     20 if wf == 1 else 10)]
                    ids.append(rid)
                self.groups[(repo, wf)] = ids
                cfg["runs"][repo].append(run((ri * 2 + wf) * 1000 + 99, status="in_progress", workflow_id=wf,
                                             created=(TODAY - timedelta(days=5)).isoformat() + "T00:00:00Z"))
        return cfg

    def prepare(self):
        self.cfg = self.make_cfg()
        self.write_cfg()

    def load_out(self):
        with open(self.out) as f:
            return json.load(f)

    def load_cache_file(self):
        with open(os.path.join(self.cache, "jobs-cache-%s.json" % ORG)) as f:
            return json.load(f)

    def test_call_count_cache_and_rows(self):
        # 受入基準 3 / 4 / 6
        self.prepare()
        r = self.run_cli(["--estimate", "--sample-runs", "3"])
        self.assertEqual(r.returncode, 0, r.stderr)
        calls = [c for c in self.calls() if not c.startswith("api rate_limit")]
        jobs_calls = [c for c in calls if "/jobs" in c]
        repo_calls = [c for c in calls if "orgs/" in c]
        runs_calls = [c for c in calls if "/actions/runs " in c]
        self.assertEqual(len(calls), len(repo_calls) + len(runs_calls) + len(jobs_calls))
        self.assertEqual(len(repo_calls), 1)
        self.assertEqual(len(runs_calls), 2)
        self.assertLessEqual(len(jobs_calls), 3 * len(self.groups))
        # 抜き取りは各組の先頭・中央・末尾
        expected = set()
        for ids in self.groups.values():
            expected.update({ids[0], ids[4], ids[8]})
        cache = self.load_cache_file()
        self.assertEqual({int(k) for k in cache["runs"]}, expected)
        for rid in expected:
            self.assertEqual(cache["runs"][str(rid)]["agg"], fj.aggregate_jobs(self.cfg["jobs"][str(rid)])[0])
        # wf 1 の抜き取りは {20, 20, 0}（移行後を含む）。平均 40/3 を 9 run 分、wf 2 は 10 x 9
        total = {}
        for x in self.rows():
            total[x["repo"]] = total.get(x["repo"], 0.0) + x["minutes"]
        for repo in ("pub-site", "priv-api"):
            self.assertAlmostEqual(total[repo], 40 / 3 * 9 + 90, places=3)

    def test_estimate_block_warning_and_stdout(self):
        # 受入基準 5
        self.prepare()
        r = self.run_cli(["--estimate", "--sample-runs", "3"])
        self.assertEqual(r.returncode, 0, r.stderr)
        doc = self.load_out()
        self.assertEqual(doc["estimate"], {"method": "sample_mean_per_workflow", "sample_runs": 3,
                                           "sampled_runs": 12, "estimated_runs": 24, "groups": 4,
                                           "unestimated_runs": 0, "budget_runs": 12, "zero_groups": 0,
                                           "zero_runs": 0})
        self.assertTrue(any(w.startswith("推定値を含む") for w in doc["warnings"]))
        self.assertLessEqual(len(r.stdout.splitlines()), 11)
        self.assertIn("推定:", r.stdout)
        for name in ("pub-site", "priv-api"):
            self.assertNotIn(name, r.stdout)
            self.assertFalse(any(name in w for w in doc["warnings"]))

    def test_full_mode_has_no_estimate_and_default_k(self):
        self.prepare()
        self.assertEqual(self.run_cli().returncode, 0)
        self.assertNotIn("estimate", self.load_out())
        self.assertEqual(self.run_cli(["--estimate"], out=os.path.join(self.root, "e.json"),
                                      cache=os.path.join(self.root, "ec")).returncode, 0)
        with open(os.path.join(self.root, "e.json")) as f:
            self.assertEqual(json.load(f)["estimate"]["sample_runs"], fj.DEFAULT_SAMPLE_RUNS)

    def test_max_seconds_resume_matches_uninterrupted(self):
        self.prepare()
        ref_out = os.path.join(self.root, "ref.json")
        self.assertEqual(self.run_cli(["--estimate"], out=ref_out, cache=os.path.join(self.root, "refcache")).returncode, 0)
        codes = []
        for _ in range(80):
            r = self.run_cli(["--estimate", "--max-seconds", "0.001"])
            codes.append(r.returncode)
            if r.returncode == 0:
                break
            self.assertEqual(r.returncode, 4, r.stderr)
        self.assertEqual(codes[-1], 0, codes)
        self.assertIn(4, codes)
        with open(ref_out) as f:
            ref = json.load(f)
        got = self.load_out()
        self.assertEqual(got["rows"], ref["rows"])
        self.assertEqual(got["estimate"], ref["estimate"])

    def make_alloc_cfg(self):
        # GitHub ホストだけの組 2 つ（各 9 run）と、run 数の多いセルフホストの組（40 run、所要時間に変動あり）
        cfg = {"repos": [{"name": "alloc-repo", "visibility": "private", "archived": False}],
               "runs": {"alloc-repo": []}, "jobs": {}}
        self.sizes = {1: 9, 2: 9, 3: 40}
        self.ids = {}
        for wf, n in self.sizes.items():
            self.ids[wf] = []
            for i in range(n):
                rid = wf * 1000 + i
                day = (TODAY - timedelta(days=19) + timedelta(days=i * 19 // n)).isoformat()
                cfg["runs"]["alloc-repo"].append(run(rid, created=day + "T00:00:00Z", workflow_id=wf))
                if wf == 3:
                    cfg["jobs"][str(rid)] = [job(rid * 10, ["self-hosted"], "g", day + "T01:00:00+00:00", 5 + i % 7)]
                else:
                    cfg["jobs"][str(rid)] = [job(rid * 10, ["ubuntu-latest"], "GitHub Actions", day + "T01:00:00+00:00", 5)]
                self.ids[wf].append(rid)
        return cfg

    def test_allocation_jobs_calls_bounded_and_zero_cut(self):
        # Issue #205: 0 分の組は試し取りの 3 run（先頭・中央・末尾）で打ち切り、浮いた分を run 数の多い組へ回す
        self.cfg = self.make_alloc_cfg()
        self.write_cfg()
        r = self.run_cli(["--estimate", "--sample-runs", "4"])
        self.assertEqual(r.returncode, 0, r.stderr)
        jobs_calls = [c for c in self.calls() if "/jobs" in c]
        budget = sum(min(n, 4) for n in self.sizes.values())
        doc = self.load_out()
        self.assertEqual(doc["estimate"]["budget_runs"], budget)
        self.assertEqual(doc["estimate"]["zero_groups"], 2)
        self.assertEqual(doc["estimate"]["zero_runs"], 12)
        cache = self.load_cache_file()
        fetched = {int(k) for k in cache["runs"]}
        for wf in (1, 2):
            ids = self.ids[wf]
            self.assertEqual(fetched & set(ids), {ids[0], ids[4], ids[-1]})
        self.assertGreater(len(fetched & set(self.ids[3])), 4)
        self.assertEqual(len(jobs_calls), len(fetched))
        self.assertLessEqual(len(jobs_calls), budget)
        self.assertLessEqual(len(jobs_calls), 4 * len(self.sizes))
        self.assertLessEqual(len(r.stdout.splitlines()), 11)
        self.assertNotIn("alloc-repo", r.stdout)
        self.assertFalse(any("alloc-repo" in w for w in doc["warnings"]))

    def test_ends_hosted_middle_selfhosted_not_cut(self):
        """Issue #218: 両端と中央の試し取りに 0 分でない run があれば、打ち切らずセルフホスト分を出す。"""
        cfg = self.make_alloc_cfg()
        wf, n = 4, 20
        self.ids[wf] = []
        for i in range(n):
            rid = wf * 1000 + i
            day = (TODAY - timedelta(days=19) + timedelta(days=i * 19 // n)).isoformat()
            cfg["runs"]["alloc-repo"].append(run(rid, created=day + "T00:00:00Z", workflow_id=wf))
            labels, grp = (["self-hosted"], "g") if 0 < i < n - 1 else (["ubuntu-latest"], "GitHub Actions")
            cfg["jobs"][str(rid)] = [job(rid * 10, labels, grp, day + "T01:00:00+00:00", 5)]
            self.ids[wf].append(rid)
        self.sizes[wf] = n
        self.cfg = cfg
        self.write_cfg()
        r = self.run_cli(["--estimate", "--sample-runs", "4"])
        self.assertEqual(r.returncode, 0, r.stderr)
        doc = self.load_out()
        est = doc["estimate"]
        budget = sum(min(m, 4) for m in self.sizes.values())
        jobs_calls = [c for c in self.calls() if "/jobs" in c]
        self.assertEqual(est["budget_runs"], budget)
        self.assertLessEqual(len(jobs_calls), budget)
        self.assertEqual(est["zero_groups"], 2)  # wf 1, 2 だけ（wf 4 は中央がセルフホストで打ち切らない）
        self.assertEqual(est["zero_runs"], 12)
        self.assertGreater(sum(x["minutes"] for x in doc["rows"]), 40 * 5)
        self.assertLessEqual(len(r.stdout.splitlines()), 11)
        self.assertNotIn("alloc-repo", r.stdout)
        self.assertFalse(any("alloc-repo" in w for w in doc["warnings"]))

    def test_recorded_selfhosted_outside_pilot_not_cut(self):
        """Issue #227: 試し取りが 0 分でも、全件取得で残した job 件数の記録がセルフホストを示す組は打ち切らない。"""
        cfg = self.make_alloc_cfg()
        wf, n = 4, 20
        pilot = {0, 10, 19}
        odd = 5
        for i in range(n):
            rid = wf * 1000 + i
            day = (TODAY - timedelta(days=19) + timedelta(days=i * 19 // n)).isoformat()
            cfg["runs"]["alloc-repo"].append(run(rid, created=day + "T00:00:00Z", workflow_id=wf))
            sh = i == odd
            cfg["jobs"][str(rid)] = [job(rid * 10, ["self-hosted"] if sh else ["ubuntu-latest"],
                                         "g" if sh else "GitHub Actions", day + "T01:00:00+00:00", 30)]
        self.sizes[wf] = n
        self.cfg = cfg
        self.write_cfg()
        self.assertEqual(self.run_cli([]).returncode, 0)  # 全件取得で全 run の記録を作る
        c = self.load_cache_file()
        self.assertEqual(c["schema_version"], 2)
        self.assertEqual(c["runs"][str(wf * 1000 + odd)]["jobs"], {"hosted": 0, "self_hosted": 1})
        self.assertEqual(c["runs"][str(wf * 1000)]["jobs"], {"hosted": 1, "self_hosted": 0})
        self.assertTrue(pilot)
        self.reset_calls()
        r = self.run_cli(["--estimate", "--sample-runs", "4"])
        self.assertEqual(r.returncode, 0, r.stderr)
        doc = self.load_out()
        self.assertEqual(doc["estimate"]["zero_groups"], 2)  # wf 1, 2 だけ
        self.assertGreater(sum(x["minutes"] for x in doc["rows"]), 0)
        self.assertLessEqual(len(r.stdout.splitlines()), 11)
        self.assertNotIn("alloc-repo", r.stdout)

    def test_allocation_resume_matches_uninterrupted(self):
        self.cfg = self.make_alloc_cfg()
        self.write_cfg()
        ref_out = os.path.join(self.root, "ref.json")
        self.assertEqual(self.run_cli(["--estimate", "--sample-runs", "4"], out=ref_out,
                                      cache=os.path.join(self.root, "refcache")).returncode, 0)
        codes = []
        for _ in range(150):
            r = self.run_cli(["--estimate", "--sample-runs", "4", "--max-seconds", "0.001"])
            codes.append(r.returncode)
            if r.returncode == 0:
                break
            self.assertEqual(r.returncode, 4, r.stderr)
        self.assertEqual(codes[-1], 0, codes)
        self.assertIn(4, codes)
        with open(ref_out) as f:
            ref = json.load(f)
        got = self.load_out()
        self.assertEqual(got["rows"], ref["rows"])
        self.assertEqual(got["estimate"], ref["estimate"])

    def test_full_run_after_estimate_replaces_estimates(self):
        # 推定区分の run は cache に無く cursor が作成日まで戻るため、次の全件取得で実測に置き換わる
        self.prepare()
        self.assertEqual(self.run_cli(["--estimate"]).returncode, 0)
        self.assertEqual(self.run_cli().returncode, 0)
        ref_out = os.path.join(self.root, "ref.json")
        self.assertEqual(self.run_cli(out=ref_out, cache=os.path.join(self.root, "refcache")).returncode, 0)
        self.assertEqual(self.rows(), self.rows(ref_out))

    def test_all_sampled_jobs_invalid_degrades(self):
        self.prepare()
        for ids in self.groups.values():
            for rid in ids:
                self.cfg["jobs"][str(rid)] = [{"id": "bad", "labels": []}]
        self.write_cfg()
        r = self.run_cli(["--estimate"])
        self.assertEqual(r.returncode, 0, r.stderr)
        doc = self.load_out()
        est = doc["estimate"]
        self.assertEqual((est["sampled_runs"], est["estimated_runs"]), (0, 0))
        self.assertEqual(est["unestimated_runs"], 24 - 0)
        self.assertTrue(any(w.startswith("推定できず") for w in doc["warnings"]))
        self.assertEqual(doc["rows"], [])

    def test_old_listing_without_workflow_id_is_relisted(self):
        self.prepare()
        os.makedirs(self.cache)
        legacy = {"since": SINCE, "day": TODAY.isoformat(), "truncated": [], "repos": {"pub-site": [
            {"id": 1, "run_attempt": 1, "status": "completed", "created_at": D0 + "T00:00:00Z"}]}}
        with open(os.path.join(self.cache, "jobs-cache-%s.json" % ORG), "w") as f:
            json.dump({"schema_version": 1, "org": ORG, "runs": {}, "listing": legacy}, f)
        r = self.run_cli(["--estimate"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.load_out()["estimate"]["groups"], 4)

    def test_build_dashboard_accepts_estimate_output(self):
        # 受入基準 7
        self.prepare()
        self.assertEqual(self.run_cli(["--estimate"]).returncode, 0)
        billing = os.path.join(self.work, "billing.json")
        with open(billing, "w") as f:
            json.dump({"schema_version": 1, "source": "billing", "org": ORG, "since": SINCE[:7],
                       "until": TODAY.isoformat()[:7], "rows": []}, f)
        outdir = os.path.join(self.work, "dash")
        os.makedirs(outdir)
        bd = os.path.join(HERE, "..", "scripts", "build_dashboard.py")
        r = subprocess.run([sys.executable, "-I", "-B", bd, "--org", ORG, "--rows", billing,
                            "--rows", self.out, "--out-dir", outdir],
                           capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"})
        self.assertEqual(r.returncode, 0, r.stderr)


class GhApiRetryTest(unittest.TestCase):
    """_common.gh_api の再試行契約（既定は再試行なし・5xx だけ・有界）。"""

    def setUp(self):
        import _common
        self.c = _common
        self.orig = _common.subprocess.run
        self.addCleanup(setattr, _common.subprocess, "run", self.orig)
        self.sleeps = []

    def fake(self, responses):
        it = iter(responses)
        self.n = 0

        def run(*a, **k):
            self.n += 1
            r = next(it)
            if isinstance(r, BaseException):
                raise r
            code, out, err = r
            return subprocess.CompletedProcess(a, code, out, err)
        self.c.subprocess.run = run

    def call(self, **kw):
        return self.c.gh_api(["x"], _sleep=self.sleeps.append, **kw)

    def test_5xx_then_success(self):
        e = (1, "", "gh: Bad Gateway (HTTP 502)")
        self.fake([e, e, (0, "ok", "")])
        self.assertEqual(self.call(retries=3, retry_wait=2), "ok")
        self.assertEqual(self.sleeps, [2, 4])

    def test_5xx_exhausted(self):
        self.fake([(1, "", "gh: HTTP 503")] * 4)
        with self.assertRaises(self.c.FetchError) as cm:
            self.call(retries=3, retry_wait=1000)
        self.assertIn("HTTP 503", str(cm.exception))
        self.assertEqual(self.n, 4)
        self.assertEqual(len(self.sleeps), 3)
        self.assertTrue(all(w <= fj.MAX_RETRY_WAIT for w in self.sleeps), self.sleeps)

    def test_4xx_and_timeout_not_retried(self):
        self.fake([(1, "", "gh: Not Found (HTTP 404)")])
        with self.assertRaises(self.c.FetchError):
            self.call(retries=3)
        self.fake([subprocess.TimeoutExpired("gh", 1)])
        with self.assertRaises(self.c.FetchError):
            self.call(retries=3)
        self.fake([(1, "", "no status here")])
        with self.assertRaises(self.c.FetchError):
            self.call(retries=3)
        self.assertEqual(self.sleeps, [])

    def test_default_does_not_retry_and_keeps_message(self):
        self.fake([(1, "", "gh: Bad Gateway (HTTP 502)")])
        with self.assertRaises(self.c.FetchError) as cm:
            self.call()
        self.assertEqual(str(cm.exception), "gh api 失敗: gh: Bad Gateway (HTTP 502)")
        self.assertEqual((self.n, self.sleeps), (1, []))


class SharedCommonTest(unittest.TestCase):
    """helper の定義が scripts/_common.py の 1 か所であることを固定する（Issue #141）。"""

    def test_helpers_are_shared_with_common(self):
        import _common
        expected = os.path.realpath(os.path.join(HERE, "..", "scripts", "_common.py"))
        self.assertEqual(os.path.realpath(_common.__file__), expected)
        for name in ('FetchError', 'ORG_RE', 'DAY_RE', 'ISO_RE', 'gh_api',
                     'atomic_write_json', 'parse_concatenated_json'):
            self.assertIs(getattr(fj, name), getattr(_common, name), name)

    def test_parse_concatenated_json(self):
        items, pages = fj.parse_concatenated_json('[{"a":1}]\n [{"a":2}][]')
        self.assertEqual((len(items), pages), (2, 3))
        for bad in ('{"a":1}', '[1][', 'x'):
            with self.assertRaises(fj.FetchError, msg=bad):
                fj.parse_concatenated_json(bad)


if __name__ == "__main__":
    unittest.main()
