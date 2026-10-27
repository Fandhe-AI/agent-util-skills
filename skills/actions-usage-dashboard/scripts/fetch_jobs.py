#!/usr/bin/env python3
"""Actions jobs API からセルフホストランナーの日次行を作る。

役割と境界:
  actions-usage-dashboard の 2 つ目のデータ源。billing usage API（fetch_billing.py）には
  セルフホストランナーの実行時間が含まれないため、org の全リポジトリ（アーカイブ除く）の
  Actions runs / jobs API からセルフホストの job だけを拾い、
  「日（UTC）x リポジトリ x ランナーグループ」の行へ集計して JSON に書く。
  GitHub ホストの job は行にしない（分数は billing 側を正とし、二重計上を避ける）。
  GitHub への支払いが発生しないため charge_class は self_hosted で固定する。

呼び出し元: summarize.py / build_dashboard.py が fetch_billing.py の出力と合わせて読む。
  行の列は fetch_billing.py の row に揃え、セルフホスト固有の runner_group を足している。
  列名や区分を変えるときは fetch_billing.py と後続スクリプトを同時に更新する。

出力スキーマ（schema_version 1）:
  {schema_version, source: "jobs", org, since, until, generated_at, warnings, rows}
  row = day / repo / visibility / sku / runner_type / charge_class / runner_group /
        minutes / quota_minutes / billable_minutes / gross_usd / discount_usd / net_usd
  金額系と quota_minutes は 0。day は job の started_at の UTC 日。

増分キャッシュ: <--cache>/jobs-cache-<org>.json
  {schema_version, org, runs: {"<run_id>": {repo, attempt, created_at, agg: [{day, runner_group, minutes}]}}}
  jobs を全ページ取り終えた「完了済み run」だけを run 単位で登録する。未完了の run は
  後から job が完了するため登録せず、次回の一覧で再び候補にする。run_attempt が増えた
  （再実行）ときは全 attempt の jobs（filter=all）を job id で重複除去して取り直す。再実行中は
  完了済みの旧 attempt の分をキャッシュから残す。キャッシュは非信頼入力として検証し、壊れていれば止まる。

終了コード: 0 成功 / 1 失敗 / 3 レート制限の残量不足で中断（キャッシュ保存済み。
  同じコマンドで再実行すると続きから取る。中断時と失敗時は --out を書かない）/
  4 --max-seconds の時間上限で中断（キャッシュ保存済み。3 と違い待たずにすぐ再実行してよい。--out は書かない）。
  一時的な 5xx は --retries 回（既定 3）まで --retry-wait 秒（既定 5、指数バックオフ、1 回 60 秒上限）で
  再試行する。上限まで回復しなければ終了コード 1。4xx・レート制限・タイムアウトは再試行しない。
  再試行の待ちは --max-seconds の予算に含まれず、その上に乗る（run / リポジトリの境目でしか予算を見ないため）。

runs 一覧の縮小（cursor）: キャッシュの "cursor"（{repo: {since, from}}）に、リポジトリごとの
  「次回の一覧の下限候補」を持つ。2 回目以降は created>=from から一覧し、API 呼び出しを減らす。
  from は「実行日 - RELIST_WINDOW_DAYS」と「処理後も未登録の run の作成日」と「1,000 件上限で
  欠けた日」の最小値。窓内の再実行（run_attempt 増）と未完了 run を拾い続けるための下限である。
  --since を前回より過去にしたリポジトリ、cursor の無いリポジトリは全期間を一覧する。
  出力の行は一覧ではなく cache["runs"] から作るため、一覧から外れた古い run も集計に残る。
  cursor はキャッシュ（非信頼入力）なので load_cache で検証し、未来日の from は
  「今日 - RELIST_WINDOW_DAYS」で挟み込んで、一覧を飛ばしてデータを落とす改ざんを防ぐ。

進捗と checkpoint: 大きな org では初回が前景の timeout を超えるため、リポジトリの境目（一覧）と
  run の境目（jobs）で、--progress-interval 秒ごとに stderr へ数値だけの進捗を出し、同時に
  キャッシュを保存する。外部から kill されても、それまでの取得を失わず同じコマンドで続きから取れる。
  進捗にリポジトリ名を出さない（エージェントが stderr を読むためのトークン節約と、非公開名の露出回避）。
  --max-seconds は時間で区切る上限で、run / リポジトリの境目で中断する。各フェーズで少なくとも
  1 単位は処理してから中断するため、小さな値でも再実行を重ねれば必ず前進する。

判定: labels に self-hosted を持つ job だけをセルフホストとする（独自ランナーグループに置いた
  GitHub ホストの larger runner は self-hosted ラベルを持たないため含めず、billing との二重計上を避ける）。
  判定は is_self_hosted に集約している。runner_group は集計の見出しにだけ使う。

一覧の再開: cache の "listing"（{since, day, repos: {repo: [run...]}, truncated}）に、取り終えた
  リポジトリ単位の run 一覧を保存する。同じ --since・同じ UTC 日の再実行では保存済みの一覧を使い
  続きのリポジトリから取る。ただし未完了 run を含むリポジトリは再取得する（待機中に完了し得るため）。
  再利用した場合は listed_at（取得時点）を warnings に出す。待機中に作られた run は次回実行で反映される。
  全処理が成功したら listing を捨てる。
出力対象: 今回の一覧に無い run（GitHub 側で削除・1,000 件上限で欠落）でも、対象リポジトリの
  キャッシュ済み履歴は集計に残す。

helper の共有: gh_api / atomic_write_json / ORG_RE / FetchError は _common.py から import する
  （定義は 1 か所）。parse_concatenated_json・nonneg_num も _common.py から import する。
  load_cache の agg[].minutes 検証は nonneg_num を使い、FetchError を「キャッシュが壊れている」へ包み直す
  （受理・拒否する値と利用者向けエラー文は従来どおり。Issue #182）。DAY_RE / ISO_RE も _common.py から import する。REPO_RE は本スクリプト固有。
  検証用の正規表現の終端は `\\Z` に統一する（`$` は末尾の改行の手前にも一致するため）。

アクセス: gh api 経由のみ（読み取り専用）。トークンは扱わない。標準ライブラリだけで動く。
"""
import argparse
import json
import math
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone

# `-I`（隔離モード）では起動スクリプトのディレクトリが sys.path に入らないため、自分で足す。
# append にして、同じディレクトリに標準モジュール名のファイルがあっても標準ライブラリを先に解決させる。
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from _common import (  # noqa: E402
    DAY_RE, ISO_RE, ORG_RE, FetchError, atomic_write_json, gh_api, nonneg_num, parse_concatenated_json,
)

SCHEMA_VERSION = 1
REPO_RE = re.compile(r"^[A-Za-z0-9._-]+\Z")
SELF_HOSTED_LABEL = "self-hosted"
UNKNOWN_GROUP = "(unknown)"
RUNS_SEARCH_CAP = 1000
PER_PAGE = 100
MAX_JOB_PAGES = 100
DEFAULT_MIN_REMAINING = 200
DEFAULT_RATE_CHECK_EVERY = 25
EXIT_RATE_LIMITED = 3
EXIT_TIME_BUDGET = 4
DEFAULT_PROGRESS_INTERVAL = 30
# 5xx の再試行。長い取得が数十分かかる org で、1〜1.5 時間に 1 回返る一時的な 502 のたびに最初からやり直さない
# ための有界な再試行。上限は暴走防止（回数 10・1 回 60 秒）で、_common._MAX_RETRY_WAIT と揃える
DEFAULT_RETRIES = 3
DEFAULT_RETRY_WAIT = 5
MAX_RETRIES = 10
MAX_RETRY_WAIT = 60
# GitHub の run を再実行できる期間（公式: 初回実行から 30 日まで。
# https://docs.github.com/en/actions/how-tos/manage-workflow-runs/re-run-workflows-and-jobs ）に
# 余裕を持たせた値。公式の起点は作成日ではなく初回実行で、一覧の絞り込み（created>=）より遅れ得る
# （セルフホストのキュー待ち・fork PR の承認待ち）。settled 後の再実行を取りこぼさないよう 30 や 31 には
# 絞らない。一覧の下限を「前回実行日 - この日数」より新しくしないことで、窓内の再実行（run_attempt 増）を
# 取りこぼさない。CLI 引数にはしない（小さくすると取りこぼしで分数が減るため、利用者が触る値ではない）
RELIST_WINDOW_DAYS = 45
ZERO_COLS = ("quota_minutes", "billable_minutes", "gross_usd", "discount_usd", "net_usd")


class InvalidJob(FetchError):
    """job の要素が不正。main は該当 run だけ警告してスキップする（全体は止めない）。"""


class RateLimited(Exception):
    """レート制限の残量が閾値を下回った。main が保存して終了コード 3 で終える。"""

    def __init__(self, remaining, reset):
        super().__init__("rate limited")
        self.remaining, self.reset = remaining, reset


class TimeBudgetExceeded(Exception):
    """--max-seconds に達した。main が保存して終了コード 4 で終える（RateLimited と違い待たずに再実行できる）。"""


# ---- 純粋関数（gh に依存しない） ----

def relist_floor(entry, since, today):
    """リポジトリの runs 一覧の下限日を返す。cursor が無い・前回より過去を求めた場合は全期間（since）。"""
    if entry is None or since < date.fromisoformat(entry["since"]):
        return since
    # 正規のキャッシュでは from が常に「前回実行日 - 窓」以下なので min は何も変えない。
    # 改ざんされた未来日の from で一覧が飛ばされ、データが落ちるのを防ぐための挟み込み
    base = today - timedelta(days=RELIST_WINDOW_DAYS)
    return max(since, min(date.fromisoformat(entry["from"]), base))


def next_cursor_entry(items, settled, truncated_days, since, today):
    """次回の一覧の下限候補を cursor の 1 エントリにする。

    items: 今回一覧した run（{id, run_attempt, created_at}）。settled(id, attempt) は処理後も
    cache["runs"] に attempt 以上で登録済みかを返す。未登録の run（未完了・再実行中・不正 job）と
    1,000 件上限で欠けた日は、次回も一覧に入れるため from に含める。作成日が読めない run があれば
    安全側で since（全期間）にする。
    """
    cands = [today - timedelta(days=RELIST_WINDOW_DAYS)]
    for it in items:
        if settled(it["id"], it["run_attempt"]):
            continue
        d = it.get("created_at", "")[:10]
        try:
            if not DAY_RE.match(d):
                raise ValueError
            cands.append(date.fromisoformat(d))
        except ValueError:
            return {"since": since.isoformat(), "from": since.isoformat()}
    cands.extend(truncated_days)
    return {"since": since.isoformat(), "from": min(cands).isoformat()}


def is_self_hosted(job):
    # self-hosted ラベルだけで判定する。GitHub ホストの larger runner は独自ランナーグループに
    # 置けるが self-hosted ラベルは持たないため、グループ名からの推定は二重計上の原因になる
    labels = job.get("labels")
    return any(isinstance(x, str) and x.lower() == SELF_HOSTED_LABEL for x in labels)


def _parse_ts(v):
    if not isinstance(v, str):
        return None
    try:
        dt = datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return dt.astimezone(timezone.utc)


def job_window(job):
    """完了済み job の (開始 UTC, 分数) を返す。未完了・時刻不正・負の所要時間は None。"""
    if job.get("status") != "completed":
        return None
    s, c = _parse_ts(job.get("started_at")), _parse_ts(job.get("completed_at"))
    if s is None or c is None or c < s:
        return None
    return s, (c - s).total_seconds() / 60.0


def validate_job(job):
    if not isinstance(job, dict):
        raise InvalidJob("job の要素が不正")
    jid = job.get("id")
    if isinstance(jid, bool) or not isinstance(jid, int):
        raise InvalidJob("job の id が整数ではない")
    labels = job.get("labels")
    if not isinstance(labels, list) or not all(isinstance(x, str) for x in labels):
        raise InvalidJob("job の labels が文字列の配列ではない")
    return job


def aggregate_jobs(jobs):
    """jobs を (agg, hosted, unfinished) にする。agg は [{day, runner_group, minutes}]。job id で重複除去。"""
    seen, acc, hosted, unfinished = set(), {}, 0, 0
    for job in jobs:
        validate_job(job)
        if job["id"] in seen:
            continue
        seen.add(job["id"])
        if not is_self_hosted(job):
            hosted += 1
            continue
        w = job_window(job)
        if w is None:
            unfinished += 1
            continue
        start, minutes = w
        g = job.get("runner_group_name")
        group = g if isinstance(g, str) and g.strip() else UNKNOWN_GROUP
        key = (start.date().isoformat(), group)
        acc[key] = acc.get(key, 0.0) + minutes
    agg = [{"day": d, "runner_group": g, "minutes": round(m, 6)} for (d, g), m in sorted(acc.items())]
    return agg, hosted, unfinished


def build_rows(entries, visibility, since):
    """run エントリ（{repo, agg}）群から (day, repo, runner_group) 単位の行を作る。"""
    acc = {}
    for e in entries:
        for a in e["agg"]:
            if a["day"] < since:
                continue
            k = (a["day"], e["repo"], a["runner_group"])
            acc[k] = acc.get(k, 0.0) + a["minutes"]
    rows = []
    for (day, repo, group), m in sorted(acc.items()):
        m = round(m, 6)
        if m <= 0:
            continue
        row = {"day": day, "repo": repo, "visibility": visibility.get(repo, "unknown"),
               "sku": "self-hosted", "runner_type": "self-hosted", "charge_class": "self_hosted",
               "runner_group": group, "minutes": m}
        row.update({c: 0.0 for c in ZERO_COLS})
        rows.append(row)
    return rows


# ---- 進捗・時間上限 ----

class Pacer:
    """進捗の間引き出力・checkpoint 保存・時間上限の判定を担う。

    main から run / リポジトリの境目でだけ呼ばれる。進捗は数値のみ（外部由来の文字列を出さない）。
    """

    def __init__(self, max_seconds, interval, clock=time.monotonic):
        self.max_seconds, self.interval, self.clock = max_seconds, interval, clock
        self.start = self.last = clock()
        self.units = {"list": 0, "jobs": 0}

    def check_budget(self, phase):
        # 各フェーズで 1 単位は処理してから中断する（小さすぎる値でも再実行で必ず前進させるため）
        if self.max_seconds > 0 and self.units[phase] >= 1 and self.clock() - self.start >= self.max_seconds:
            raise TimeBudgetExceeded()

    def done(self, phase):
        self.units[phase] += 1

    def report(self, label, k, n, calls, save):
        now = self.clock()
        if k < n and self.interval > 0 and now - self.last < self.interval:
            return
        self.last = now
        print("進捗: %s %d/%d・API 呼び出し %d・経過 %d 秒" % (label, k, n, calls, now - self.start), file=sys.stderr)
        save()


# ---- gh 層 ----

def _json(text, what):
    try:
        return json.loads(text)
    except ValueError:
        raise FetchError("%s の応答を JSON として解析できない" % what)


class GhClient:
    """API 呼び出し数の計数とレート制限の確認を担う。runs / jobs は page を手で回す。"""

    def __init__(self, min_remaining, check_every, retries=0, retry_wait=0.0):
        self.counts = {"repos": 0, "runs": 0, "jobs": 0}
        self.rate_checks = 0
        self.retries, self.retry_wait = 0, retry_wait
        self._max_retries = retries
        self.min_remaining, self.check_every = min_remaining, check_every
        self._since_check = None  # None は未確認

    def total(self):
        return sum(self.counts.values())

    def _on_retry(self):
        # 5xx の試行もレート制限を消費し得るため、安全側に倒して呼び出し数へ加える
        self.retries += 1
        if self._since_check is not None:
            self._since_check += 1

    def _gh(self, args):
        """gh_api の唯一の入口。runs / jobs / rate_limit / リポジトリ一覧のすべてで 5xx を再試行する（GET で冪等）。"""
        return gh_api(args, retries=self._max_retries, retry_wait=self.retry_wait, on_retry=self._on_retry)

    def _call(self, kind, args, n=1):
        out = self._gh(args)
        self.counts[kind] += n
        if self._since_check is not None:
            self._since_check += n
        return out

    def check_rate(self, force=False):
        """run / リポジトリの境目でだけ呼ぶ。途中ページで止めず、半端な状態を残さない。"""
        if not force and self._since_check is not None and self._since_check < self.check_every:
            return
        d = _json(self._gh(["rate_limit"]), "rate_limit")
        self.rate_checks += 1
        self._since_check = 0
        core = d.get("resources", {}).get("core", {}) if isinstance(d, dict) else {}
        rem, reset = core.get("remaining"), core.get("reset")
        if isinstance(rem, bool) or not isinstance(rem, int):
            raise FetchError("rate_limit の remaining を解釈できない")
        if rem < self.min_remaining:
            raise RateLimited(rem, reset)

    def list_repos(self, org):
        text = self._gh(["--paginate", "orgs/%s/repos?per_page=100&type=all" % org])
        repos, pages = parse_concatenated_json(text)
        self.counts["repos"] += max(pages, 1)
        if self._since_check is not None:
            self._since_check += max(pages, 1)
        return repos

    def _runs_pages(self, repo, org, created):
        runs, total, page = [], None, 1
        while True:
            self.check_rate()  # 二分探索で呼び出しが増えても残量を確認する（runs 一覧は途中中断してもキャッシュを壊さない）
            text = self._call("runs", ["-X", "GET", "repos/%s/%s/actions/runs" % (org, repo),
                                       "-f", "created=%s" % created, "-f", "per_page=%d" % PER_PAGE,
                                       "-f", "page=%d" % page])
            d = _json(text, "runs")
            wr = d.get("workflow_runs") if isinstance(d, dict) else None
            tc = d.get("total_count") if isinstance(d, dict) else None
            if not isinstance(wr, list) or isinstance(tc, bool) or not isinstance(tc, int):
                raise FetchError("runs の応答形式が不正")
            if total is None:
                total = tc
            runs.extend(wr)
            if len(wr) < PER_PAGE or page * PER_PAGE >= RUNS_SEARCH_CAP:
                return runs, total
            page += 1

    def list_runs(self, repo, org, since, today, truncated):
        """created>=since の run を全件取る。1,000 件上限を超えるときは期間を二分して取り直す。"""
        out = {}

        def collect(lo, hi, spec):
            runs, total = self._runs_pages(repo, org, spec)
            if total > RUNS_SEARCH_CAP:
                if lo >= hi:
                    truncated.append("%s/%s %s" % (org, repo, lo.isoformat()))
                else:
                    mid = lo + (hi - lo) // 2
                    collect(lo, mid, "%s..%s" % (lo.isoformat(), mid.isoformat()))
                    collect(mid + timedelta(days=1), hi,"%s..%s" % ((mid + timedelta(days=1)).isoformat(), hi.isoformat()))
                    return
            for r in runs:
                out[_run_key(r)[0]] = r

        collect(since, today, ">=%s" % since.isoformat())
        return list(out.values())

    def list_jobs(self, repo, org, run_id):
        jobs = []
        for page in range(1, MAX_JOB_PAGES + 1):
            text = self._call("jobs", ["-X", "GET", "repos/%s/%s/actions/runs/%d/jobs" % (org, repo, run_id),
                                       "-f", "filter=all", "-f", "per_page=%d" % PER_PAGE,
                                       "-f", "page=%d" % page])
            d = _json(text, "jobs")
            lst = d.get("jobs") if isinstance(d, dict) else None
            if not isinstance(lst, list):
                raise FetchError("jobs の応答形式が不正")
            jobs.extend(lst)
            if len(lst) < PER_PAGE:
                return jobs
        raise FetchError("jobs のページ数が上限を超えた")


def _pos_int(v):
    return isinstance(v, int) and not isinstance(v, bool) and v > 0


def _run_key(r):
    """run の (id, attempt) を検証して返す。"""
    if not isinstance(r, dict) or not _pos_int(r.get("id")):
        raise FetchError("run の id が正の整数ではない")
    att = r.get("run_attempt", 1)
    if not _pos_int(att):
        raise FetchError("run の run_attempt が正の整数ではない")
    return r["id"], att


# ---- キャッシュ層 ----

def load_cache(path, org):
    empty = {"schema_version": SCHEMA_VERSION, "org": org, "runs": {}}
    if not os.path.exists(path):
        return empty
    bad = FetchError("キャッシュが壊れている: %s（削除して再取得するか確認すること）" % path)
    try:
        with open(path, encoding="utf-8") as f:
            c = json.load(f)
    except (OSError, ValueError):
        raise bad
    if not isinstance(c, dict) or c.get("schema_version") != SCHEMA_VERSION or c.get("org") != org \
            or not isinstance(c.get("runs"), dict):
        raise bad
    ls = c.get("listing")
    if ls is not None:
        if not isinstance(ls, dict) or not isinstance(ls.get("since"), str) or not isinstance(ls.get("day"), str) \
                or not isinstance(ls.get("repos"), dict) or not isinstance(ls.get("truncated"), list) \
                or not all(isinstance(t, str) for t in ls["truncated"]):
            raise bad
        sk = ls.get("skipped", {})
        if not isinstance(sk, dict) or not all(
                k.isdigit() and isinstance(v, dict) and _pos_int(v.get("attempt")) and isinstance(v.get("msg"), str)
                for k, v in sk.items()):
            raise bad
        # 形式に合わない再開時刻は破棄して「不明」に倒す（端末へ制御文字を出さない）
        la = ls.get("listed_at")
        if not (isinstance(la, str) and ISO_RE.match(la)):
            ls.pop("listed_at", None)
        for repo, items in ls["repos"].items():
            if not REPO_RE.match(repo) or not isinstance(items, list):
                raise bad
            for it in items:
                if not isinstance(it, dict) or not isinstance(it.get("status"), str) \
                        or not isinstance(it.get("created_at"), str):
                    raise bad
                try:
                    _run_key(it)
                except FetchError:
                    raise bad
    cur = c.get("cursor")
    if cur is not None:
        if not isinstance(cur, dict):
            raise bad
        for repo, ent in cur.items():
            if not REPO_RE.match(repo) or not isinstance(ent, dict):
                raise bad
            for k in ("since", "from"):
                v = ent.get(k)
                if not isinstance(v, str) or not DAY_RE.match(v):
                    raise bad
                try:
                    date.fromisoformat(v)
                except ValueError:
                    raise bad
    for rid, e in c["runs"].items():
        if not rid.isdigit() or not isinstance(e, dict) or not isinstance(e.get("repo"), str) \
                or not REPO_RE.match(e["repo"]) or not _pos_int(e.get("attempt")) \
                or not isinstance(e.get("created_at"), str) or not isinstance(e.get("agg"), list):
            raise bad
        for a in e["agg"]:
            if not isinstance(a, dict) or not isinstance(a.get("runner_group"), str) \
                    or not isinstance(a.get("day"), str) or not DAY_RE.match(a["day"]):
                raise bad
            # 数値の判定は nonneg_num に一本化し、利用者に見えるエラー文は「キャッシュが壊れている」に保つ
            try:
                nonneg_num(a, "minutes")
            except FetchError:
                raise bad
            try:
                date.fromisoformat(a["day"])
            except ValueError:
                raise bad
    return c


# ---- main ----

def parse_since(s, today):
    if not DAY_RE.match(s):
        raise FetchError("--since は YYYY-MM-DD 形式で指定する")
    try:
        d = date.fromisoformat(s)
    except ValueError:
        raise FetchError("--since が実在しない日付")
    if d > today:
        raise FetchError("--since が未来日")
    return d


def main(argv=None):
    ap = argparse.ArgumentParser(description="Actions jobs API からセルフホストランナーの日次行を作る")
    ap.add_argument("--org", required=True)
    ap.add_argument("--since", required=True, help="YYYY-MM-DD（UTC）")
    ap.add_argument("--out", required=True)
    ap.add_argument("--cache", required=True, help="増分キャッシュのディレクトリ")
    ap.add_argument("--min-remaining", type=int, default=DEFAULT_MIN_REMAINING,
                    help="core レート制限の残量がこの値を下回ったら中断する")
    ap.add_argument("--rate-check-every", type=int, default=DEFAULT_RATE_CHECK_EVERY,
                    help="API 呼び出しがこの回数増えるごとに残量を確認する")
    ap.add_argument("--max-seconds", type=float, default=0,
                    help="この秒数に達したら run / リポジトリの境目でキャッシュを保存して終了コード 4 で中断する（0 は無制限）")
    ap.add_argument("--retries", type=int, default=DEFAULT_RETRIES,
                    help="一時的な 5xx の再試行回数（0〜%d。0 で再試行しない）" % MAX_RETRIES)
    ap.add_argument("--retry-wait", type=float, default=DEFAULT_RETRY_WAIT,
                    help="5xx の再試行の初回待ち秒（指数バックオフ。0〜%d）" % MAX_RETRY_WAIT)
    ap.add_argument("--progress-interval", type=float, default=DEFAULT_PROGRESS_INTERVAL,
                    help="進捗の stderr 出力とキャッシュ保存の間隔（秒。0 で境目ごと）")
    a = ap.parse_args(argv)
    now = datetime.now(timezone.utc)
    today = now.date()
    stats = {"hosted": 0, "unfinished": 0, "listed": 0, "fetched": 0, "cached": 0, "pending": 0,
             "archived": 0, "badname": 0}
    truncated, skipped, cache, cache_path, client = [], [], None, None, None
    resumed_at = None
    relist = {"full": 0, "partial": 0, "oldest": None}
    until_s = today.isoformat()

    def lines(rows=None, note=None):
        out = ["出力: %s" % a.out, "期間: %s..%s（UTC）" % (a.since, until_s),
               "リポジトリ: 対象 %d（アーカイブ除外 %d / 名前不正で除外 %d）" % (
                   stats.get("targets", 0), stats["archived"], stats["badname"]),
               "API 呼び出し: 計 %d（repos %d / runs %d / jobs %d）・rate_limit 確認 %d 回%s" % (
                   client.total(), client.counts["repos"], client.counts["runs"],
                   client.counts["jobs"], client.rate_checks,
                   "・5xx 再試行 %d 回" % client.retries if client.retries else ""),
               "run: 一覧 %d / 新規取得 %d / キャッシュ利用 %d / 未完了で保留 %d" % (
                   stats["listed"], stats["fetched"], stats["cached"], stats["pending"])]
        if rows is not None:
            out.append("行数: %d・セルフホスト合計 %.1f 分（除外: GitHub ホスト job %d / 未完了 job %d）" % (
                len(rows), sum(r["minutes"] for r in rows), stats["hosted"], stats["unfinished"]))
        if relist["full"] or relist["partial"]:
            out.append("runs 一覧: 全期間 %d リポ / 前回位置から %d リポ%s" % (
                relist["full"], relist["partial"],
                "（下限の最古 %s）" % relist["oldest"] if relist["oldest"] else ""))
        if truncated:
            out.append("警告: 1,000 件上限で欠けた可能性のある区間 %d" % len(truncated))
        if skipped:
            out.append("警告: 不正な job でスキップした run %d" % len(skipped))
        if resumed_at:
            out.append("警告: run 一覧は中断前（%s）の取得を再利用している。以後に作られた run は次回の実行で反映される" % resumed_at)
        if note:
            out.append(note)
        return out

    try:
        if not ORG_RE.match(a.org):
            raise FetchError("--org が不正")
        since = parse_since(a.since, today)
        if a.min_remaining < 0 or a.rate_check_every < 1:
            raise FetchError("--min-remaining は 0 以上、--rate-check-every は 1 以上の整数で指定する")
        if not (math.isfinite(a.max_seconds) and a.max_seconds >= 0
                and math.isfinite(a.progress_interval) and a.progress_interval >= 0):
            raise FetchError("--max-seconds と --progress-interval は 0 以上の数で指定する")
        if not (0 <= a.retries <= MAX_RETRIES and math.isfinite(a.retry_wait)
                and 0 <= a.retry_wait <= MAX_RETRY_WAIT):
            raise FetchError("--retries は 0〜%d の整数、--retry-wait は 0〜%d の数で指定する" % (MAX_RETRIES, MAX_RETRY_WAIT))
        if os.path.isdir(a.out):
            raise FetchError("--out がディレクトリを指している")
        if not os.path.isdir(os.path.dirname(os.path.abspath(a.out))):
            raise FetchError("--out の親ディレクトリが存在しない")
        if os.path.exists(a.cache) and not os.path.isdir(a.cache):
            raise FetchError("--cache がディレクトリではない")
        if not os.path.isdir(os.path.dirname(os.path.abspath(a.cache))):
            raise FetchError("--cache の親ディレクトリが存在しない")
        cache_path = os.path.join(a.cache, "jobs-cache-%s.json" % a.org)
        cache = load_cache(cache_path, a.org)
        os.makedirs(a.cache, exist_ok=True)
        client = GhClient(a.min_remaining, a.rate_check_every, a.retries, a.retry_wait)
        pacer = Pacer(a.max_seconds, a.progress_interval)
    except FetchError as e:
        print("エラー: %s" % e, file=sys.stderr)
        return 1

    visibility, listed = {}, []
    try:
        client.check_rate(force=True)
        targets = []
        for repo in client.list_repos(a.org):
            if not isinstance(repo, dict) or not isinstance(repo.get("name"), str):
                continue
            name = repo["name"]
            if repo.get("archived") is True:
                stats["archived"] += 1
                continue
            if not REPO_RE.match(name) or name in (".", ".."):
                stats["badname"] += 1
                continue
            vis = repo.get("visibility")
            if not isinstance(vis, str):
                vis = "private" if repo.get("private") else "public"
            visibility[name] = vis.lower()
            targets.append(name)
        stats["targets"] = len(targets)
        ls = cache.get("listing")
        if not ls or ls["since"] != a.since or ls["day"] != until_s:
            ls = {"since": a.since, "day": until_s, "repos": {}, "truncated": [],
                  "listed_at": now.strftime("%Y-%m-%dT%H:%M:%SZ")}
            cache["listing"] = ls
        else:
            # 再開: 待機中に状態が変わり得る未完了 run を含むリポジトリは一覧を取り直す。
            # ただし時間上限での中断（すぐ再実行する前提）の直後は取り直さない。取り直すと、
            # 未完了 run を持つリポジトリが毎回先頭で消費されて後続の一覧が進まなくなる
            if ls.pop("budget_stop", False):
                stale = []
            else:
                stale = [r for r, its in ls["repos"].items() if any(i["status"] != "completed" for i in its)]
            for repo in stale:
                del ls["repos"][repo]
                ls["truncated"] = [t for t in ls["truncated"] if not t.startswith("%s/%s " % (a.org, repo))]
            if ls["repos"]:
                la = ls.get("listed_at")
                resumed_at = la if isinstance(la, str) and ISO_RE.match(la) else "不明"
        # 不正な job を含む run は、同じ周回（listing が生きている間）の再開では再取得しない。
        # 記録しないと時間上限で中断→再実行のたびに同じ run を先頭で取り直して進めなくなる。
        # listing を捨てる次の周回では空に戻るため、スキップ分も再試行される
        ls_skipped = ls.setdefault("skipped", {})
        skipped.extend(v["msg"] for v in ls_skipped.values())
        cursors = cache.get("cursor") or {}

        def checkpoint():
            _save_quietly(cache_path, cache)

        for ti, repo in enumerate(targets, 1):
            if repo not in ls["repos"]:
                pacer.check_budget("list")
                client.check_rate()
                tr = []
                floor = relist_floor(cursors.get(repo), since, today)
                if floor == since:
                    relist["full"] += 1
                else:
                    relist["partial"] += 1
                    if relist["oldest"] is None or floor.isoformat() < relist["oldest"]:
                        relist["oldest"] = floor.isoformat()
                items = [{"id": r["id"], "run_attempt": _run_key(r)[1], "status": r.get("status") or "",
                          "created_at": r.get("created_at") if isinstance(r.get("created_at"), str) else ""}
                         for r in client.list_runs(repo, a.org, floor, today, tr)]
                ls["repos"][repo] = items
                ls["truncated"].extend(tr)
                pacer.done("list")
            pacer.report("runs 一覧", ti, len(targets), client.total(), checkpoint)
            for run in ls["repos"][repo]:
                rid, att = _run_key(run)
                listed.append((rid, att, repo, run))
        truncated.extend(ls["truncated"])
        stats["listed"] = len(listed)
        listed.sort(key=lambda t: t[0])

        def needs_fetch(rid, att, run):
            ent = cache["runs"].get(str(rid))
            sk = ls_skipped.get(str(rid))
            if sk is not None and att <= sk["attempt"]:
                return False
            return (ent is None or att > ent["attempt"]) and run.get("status") == "completed"

        todo = sum(1 for rid, att, _r, run in listed if needs_fetch(rid, att, run))
        fetched_k = 0
        for rid, att, repo, run in listed:
            ent = cache["runs"].get(str(rid))
            if ent is not None and att <= ent["attempt"]:
                # リポジトリ名変更後も出力判定（visibility）から外れないよう現在名へ更新する
                ent["repo"] = repo
                stats["cached"] += 1
                continue
            sk = ls_skipped.get(str(rid))
            if sk is not None and att <= sk["attempt"]:
                continue  # 同じ周回で不正と判明済み（警告は listing から復元済み）
            if run.get("status") != "completed":
                # 再実行中でも、キャッシュ済みの旧 attempt の分は実際に消費された時間なので集計に残す
                stats["pending"] += 1
                continue
            pacer.check_budget("jobs")
            client.check_rate()
            jobs = client.list_jobs(repo, a.org, rid)
            pacer.done("jobs")
            fetched_k += 1
            try:
                agg, hosted, unfinished = aggregate_jobs(jobs)
            except InvalidJob as e:
                msg = "%s/%s run %d: %s" % (a.org, repo, rid, e)
                skipped.append(msg)
                ls_skipped[str(rid)] = {"attempt": att, "msg": msg}
                pacer.report("jobs 取得", fetched_k, todo, client.total(), checkpoint)
                continue
            created = run.get("created_at") if isinstance(run.get("created_at"), str) else ""
            cache["runs"][str(rid)] = {"repo": repo, "attempt": att, "created_at": created, "agg": agg}
            stats["fetched"] += 1
            stats["hosted"] += hosted
            stats["unfinished"] += unfinished
            pacer.report("jobs 取得", fetched_k, todo, client.total(), checkpoint)
        # 一覧に無い run（削除・1,000 件上限の欠落）も、対象リポジトリのキャッシュ済み履歴として残す
        entries = [e for k, e in cache["runs"].items() if e["repo"] in visibility]
        # --since より前に作られた run はこの期間では再参照されないため、キャッシュの無制限な増加を防ぐ
        # ただし期間内の day を持つ agg が残る run（期間内に開始した job を含む）は履歴として保持する
        for k in [k for k, e in cache["runs"].items()
                  if e["created_at"][:10] and e["created_at"][:10] < a.since
                  and not any(x["day"] >= a.since for x in e["agg"])]:
            del cache["runs"][k]
        rows = build_rows(entries, visibility, a.since)
        # 次回の一覧の下限を作り直す。今回の対象外（アーカイブ・削除・改名前の名前）の cursor は捨てる
        by_repo = {}
        for rid, att, repo, run in listed:
            by_repo.setdefault(repo, []).append({"id": rid, "run_attempt": att,
                                                 "created_at": run.get("created_at", "")})

        def settled(rid, att):
            ent = cache["runs"].get(str(rid))
            return ent is not None and ent["attempt"] >= att

        cache["cursor"] = {}
        for repo in targets:
            tdays = []
            for t in ls["truncated"]:
                if t.startswith("%s/%s " % (a.org, repo)):
                    try:
                        tdays.append(date.fromisoformat(t.rsplit(" ", 1)[1]))
                    except ValueError:
                        pass
            cache["cursor"][repo] = next_cursor_entry(by_repo.get(repo, []), settled, tdays, since, today)
        cache.pop("listing", None)
        atomic_write_json(cache_path, cache)
        atomic_write_json(a.out, {
            "schema_version": SCHEMA_VERSION, "source": "jobs", "org": a.org,
            "since": a.since, "until": until_s,
            "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "warnings": (["1,000 件上限で欠けた可能性: %s" % t for t in truncated]
                         + ["不正な job を含むため run をスキップ: %s" % t for t in skipped]
                         + (["run 一覧は %s 時点の取得を再利用（以後に作られた run は含まない）" % resumed_at]
                            if resumed_at else [])),
            "rows": rows,
        })
    except RateLimited as e:
        _save_quietly(cache_path, cache)
        print("\n".join(lines(note="中断: レート制限の残量不足（キャッシュ保存済み）")))
        print("エラー: レート制限の残量 %s が閾値 %d を下回ったため中断した。キャッシュ保存済み。"
              "reset（epoch %s）後に同じコマンドで再実行すると続きから取る" % (e.remaining, a.min_remaining, e.reset),
              file=sys.stderr)
        return EXIT_RATE_LIMITED
    except TimeBudgetExceeded:
        if isinstance(cache.get("listing"), dict):
            cache["listing"]["budget_stop"] = True
        _save_quietly(cache_path, cache)
        print("\n".join(lines(note="中断: 時間上限（キャッシュ保存済み）")))
        print("エラー: --max-seconds %g 秒に達したため中断した。キャッシュ保存済み。"
              "同じコマンドをすぐ再実行すると続きから取る" % a.max_seconds, file=sys.stderr)
        return EXIT_TIME_BUDGET
    except FetchError as e:
        _save_quietly(cache_path, cache)
        print("エラー: %s" % e, file=sys.stderr)
        return 1
    except OSError as e:
        print("エラー: 書き込みに失敗: %s" % e.strerror, file=sys.stderr)
        return 1
    print("\n".join(lines(rows)))
    return 0


def _save_quietly(path, cache):
    """中断・失敗時に、完了済み run だけを含むキャッシュを保存する（保存失敗は無視）。"""
    if path is None or cache is None:
        return
    try:
        atomic_write_json(path, cache)
    except OSError:
        pass


if __name__ == "__main__":
    sys.exit(main())
