#!/usr/bin/env python3
"""rows JSON から、Dashboard 型 Artifact の store へ書く文書と batch.json を一括生成する。

役割と境界:
  actions-usage-dashboard の「ページ組み立て」工程。fetch_*.py が書いた rows JSON と
  templates/dashboard/index.html を受け取り、ArtifactData の batch にそのまま渡せる
  writes（batch.json）と、それが file_path で参照する文書ファイルを --out-dir へ書く。
  モデルは雛形 HTML も rows 本体も読み書きせず、このスクリプトの数行の案内に従って
  「データファイルのアップロード」と「batch.json の中身を writes へ渡す」だけを行う。
  取得・集計・アップロード・Artifact への書き込みは行わない（ネットワークにも gh にも出ない）。

呼び出し元: SKILL.md の作成・更新フロー。入力は fetch_billing.py（source: billing）と
  fetch_jobs.py（source: jobs）の rows JSON（schema_version 1）。
  出力は Dashboard 型の store のパス（dash/meta・dash/params・datasets/<id>・files/index.html）。

付帯情報（since / until / warnings / estimate / assume_current_visibility）の渡し方: Dashboard 型の添付ファイルはページへ行オブジェクトの
  配列だけを渡し、fetch 出力のトップレベルの値は届かない。そこで rows の dataset 1 つにつき、付帯情報の
  dataset を 1 つ対応させる（billing と billing_meta、jobs と jobs_meta）。meta は
  {source, kind, value} の行（値はすべて文字列。kind は since / until / warning / estimate /
  estimate_budget_runs / estimate_zero_groups / estimate_zero_runs / assume_current_visibility）で、
  検証済みの rows 文書から本スクリプトが --out-dir/meta-<source>.json として作る。
  source ごとに分けるのは、jobs だけの更新で billing の取得期間が消えないようにするため。

作成は 2 段階: 1 段目（--asset-url なし）はアップロードすべきファイルの一覧を出す。
  一覧は「rows ファイル → その meta ファイル」の順に並ぶ。アップロードで得た /_blob/ の url を
  この一覧と同じ順・同じ数で --asset-url に渡して再実行すると batch.json が完成する。--update は既存 dataset の source と updated（billing は description も）だけを書き換える
  op: update を作る（if_version は --if-version <id>=<N> で受ける）。
  --update --add-dataset <jobs|billing_meta|jobs_meta> は、作成時に無かった dataset を op: set（全体）で足し、
  既存分の update と同じ batch に入れる。「--if-version が無ければ新規」と推測しないのは、
  書き忘れが版固定なしの上書きにならないよう fail-closed にするため。
  --update --refresh-template は、ページ本体（files/index.html）をスキル同梱の雛形で置き換える
  op: set を batch の末尾に足す。雛形を直しても datasets の更新だけでは既存ダッシュボードの
  ページは変わらないため、opt-in の経路として持つ。store には作成時の files/index.html が必ずあり
  版なしの set は拒否されるので、2 段目では --template-if-version <N> を必須にして、他所の変更を
  黙って上書きしない（--add-dataset と同じく「版が無ければ新規」とは推測せず fail-closed）。
  指定しない更新の batch は従来と変わらない。
  更新では billing の rows を省いて jobs だけを渡せる（作成は billing 必須のまま）。
  --add-dataset jobs は jobs_meta も新規（op: set）にする。billing_meta / jobs_meta の単独指定は、
  meta なしで作られた既存ダッシュボードの移行用。受け付ける id は固定の 4 つだけで、任意の dataset や
  任意の JSON を store へ送る経路は持たない（meta は検証済みの rows 文書から作るものに限る。詳細は SKILL.md）。

使用量 CSV 由来の billing rows（input: usage_csv）を --update で載せ、--refresh-template が無いときは、
  雛形の差し替えを促す案内を 1 行出す（失敗にはしない。Issue #228）。

雛形だけの差し替え（--update --refresh-template --template-only）は rows を伴わない 1 段の経路で、
  files/index.html の op: set 1 件（if_version 付き）だけの batch.json と index.json を書く。
  rows・meta・datasets・dash は書かず、--template-if-version <N> が必須。--rows・--asset-url・--if-version・
  --add-dataset・--title・--plan-included-minutes・--updated-at・--updated-by とは併用できず、指定されたら
  何も書かずに終了する。明示フラグにしたのは、--rows の付け忘れを「雛形だけ」と推測すると通常の更新が
  データを書かずにページだけを置き換える batch に化けるため（--add-dataset と同じ fail-closed）。
  承認（ページ差し替えの確認）は SKILL.md 側の責務で、このスクリプトはローカルのファイル生成だけを行う。

batch.json にはローカルの絶対パスが入る。このファイル自体は store へ送らない
  （ArtifactData が file_path を読み、参照先の中身だけを文書として送る）。

helper の複製: ORG_RE / ISO_RE / MAX_ROWS_BYTES / atomic_write_json / safe / CTRL_RE は複製である。「ネットワークに出ない」保証
  （subprocess を import しない）を守るため、_common.py も summarize.py も import しない。
  6 つすべての正典は _common.py。safe の既定長だけが意図した差（端末向けの名前は 60 字、ここはパスとエラー文を載せるため 300 字）。
  一致と差は tests/test_build_dashboard.py が検査する。
  標準ライブラリだけで動く。
"""
import argparse
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone

SCHEMA_VERSION = 1
ORG_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})\Z")
BLOB_RE = re.compile(r"^/_blob/[A-Za-z0-9_-]{1,200}\Z")
IFV_RE = re.compile(r"^(billing|jobs|billing_meta|jobs_meta)=([0-9]+)\Z")
ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z\Z")
CTRL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
MAX_ROWS_BYTES = 8 * 1024 * 1024
# 推定の件数の上限とキー。summarize.py の MAX_EST_COUNT / EST_COUNT_KEYS の複製（正典は summarize、テストで一致検査）
MAX_EST_COUNT = 10 ** 9
EST_COUNT_KEYS = ("budget_runs", "zero_groups", "zero_runs")
MAX_DOC_BYTES = 256 * 1024
MAX_BATCH_BYTES = 1024 * 1024
MAX_BATCH_WRITES = 50
MAX_TEXT_LEN = 100
DEFAULT_BY = "actions-usage-dashboard"
# rows の dataset id と、その付帯情報の dataset id。store への書き込み順は ALL_IDS の順。
DATASET_IDS = ("billing", "jobs")
META_IDS = {"billing": "billing_meta", "jobs": "jobs_meta"}
ALL_IDS = ("billing", "billing_meta", "jobs", "jobs_meta")
MAX_META_WARNINGS = 20
MAX_META_TEXT = 300
DATASET_META = {
    "billing_meta": ("GitHub ホスト分の取得情報",
                     "fetch_billing.py の出力の取得期間（since / until）と警告（warning）"),
    "jobs_meta": ("セルフホスト分の取得情報",
                  "fetch_jobs.py の出力の取得期間・警告・推定の有無と予算・打ち切り数（estimate）"),
    "billing": ("GitHub ホストの利用分数",
                "fetch_billing.py が billing usage API から作った日次の行（料金区分つき）"),
    "jobs": ("セルフホストの実行時間",
             "fetch_jobs.py が jobs API から作ったセルフホスト分の日次の行"),
}
# billing の説明は rows の input で切り替える。判定は検証済みの固定文字列（usage_csv）との一致だけで、
# rows 由来の文字列を説明文に入れない。
BILLING_DESC_CSV = ("fetch_billing.py が請求画面の使用量 CSV から作った日次の行"
                    "（料金区分つき。Dependabot の更新ジョブは dependabot_free）")
# 雛形の readSel() が読むキーと揃える。
DEFAULT_PARAMS = {"group_by": "charge_class", "include_free": True, "period": "latest",
                  "runner_type": None, "repo": None}
# 雛形はスキル同梱の 1 ファイルに固定する。任意パスを読む引数は持たない（Issue #145 L-4）。
TEMPLATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "..", "templates", "dashboard", "index.html")


def load_template():
    """スキル同梱の雛形 HTML を読む。作成と --refresh-template が共有する（パスは固定）。"""
    try:
        with open(TEMPLATE_PATH, "rb") as f:
            return f.read().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        raise BuildError("雛形を読めない")


class BuildError(Exception):
    """入力・サイズの不正。main が日本語 1 行にして終了コード 1 で終える。"""


def safe(s, limit=300):
    s = CTRL_RE.sub("?", str(s))
    return s if len(s) <= limit else s[:limit - 1] + "…"


def atomic_write_json(path, obj):
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=".%s." % os.path.basename(path), suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            # Infinity / NaN は JSON の規格外で、読む側（ページ・jq）が壊れる。書かずに ValueError で止める
            json.dump(obj, f, ensure_ascii=False, indent=1, allow_nan=False)
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


def doc_bytes(obj):
    return len(json.dumps(obj, ensure_ascii=False).encode("utf-8"))


def check_doc_size(name, obj):
    """1 文書 256 KB 未満（Dashboard 型の store の上限）。超えたら BuildError。"""
    n = doc_bytes(obj)
    if n >= MAX_DOC_BYTES:
        raise BuildError("%s が 256 KB を超える（%d バイト）" % (name, n))
    return n


def clean_text(label, s):
    s = CTRL_RE.sub("", s).strip()
    if not s or len(s) > MAX_TEXT_LEN:
        raise BuildError("%s は 1〜%d 字で指定する" % (label, MAX_TEXT_LEN))
    return s


def load_rows_doc(path, org):
    """rows JSON の先頭の属性だけを検証する。行の中身は検証しない（fetch_*.py と summarize.py の責務）。"""
    ap = os.path.abspath(path)
    base = safe(os.path.basename(ap))
    try:
        size = os.path.getsize(ap)
    except OSError:
        raise BuildError("rows を読めない: %s" % base)
    if size > MAX_ROWS_BYTES:
        raise BuildError("rows が 8 MB を超える: %s" % base)
    try:
        with open(ap, "r", encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        raise BuildError("rows が JSON として読めない: %s" % base)
    if not isinstance(doc, dict) or doc.get("schema_version") != SCHEMA_VERSION:
        raise BuildError("rows の schema_version が 1 ではない: %s" % base)
    if doc.get("source") not in DATASET_IDS:
        raise BuildError("rows の source が billing / jobs ではない: %s" % base)
    if doc.get("org") != org:
        raise BuildError("rows の org が --org と一致しない: %s" % base)
    if not isinstance(doc.get("since"), str) or not isinstance(doc.get("until"), str):
        raise BuildError("rows に since / until が無い: %s" % base)
    if not isinstance(doc.get("rows"), list):
        raise BuildError("rows の rows が配列ではない: %s" % base)
    if "assume_current_visibility" in doc and not isinstance(doc["assume_current_visibility"], bool):
        # summarize.py と同じく真偽値以外は止める（ページの概算判定を黙って既定モードに倒さない）。
        raise BuildError("rows の assume_current_visibility が真偽値ではない: %s" % base)
    ga = doc.get("generated_at")
    return {"id": doc["source"], "path": ap,
            "generated_at": ga if isinstance(ga, str) and ISO_RE.match(ga) else None,
            "meta_rows": build_meta_rows(doc),
            # 使用量 CSV 由来（dependabot_free を含み得る）かの目印。行は走査せず、先頭の属性だけで判る
            "usage_csv": doc["source"] == "billing" and doc.get("input") == "usage_csv"}


def build_meta_rows(doc):
    """rows 文書の付帯情報を {source, kind, value} 行（値は文字列）にする。

    ページ（雛形の withMeta）が読む契約。warnings は非信頼の文字列なので、制御文字を除き
    300 字・20 件で切る（雛形側でも同じ上限を重ねる）。estimate は有無（"true"）に加え、jobs 文書では
    予算・打ち切り数の整数 3 値（estimate_<key> 行、10 進文字列）だけを渡す（method 等の文字列は渡さない）。
    値は bool 以外の 0..MAX_EST_COUNT の整数でなければ BuildError（キーが無ければその行を作らない）。
    assume_current_visibility は billing 文書で真のときだけ "true" の行を足す（雛形の quotaApprox が
    含有枠の消費を下限ではなく概算と表示する根拠。偽・欠落なら行を作らず、雛形は行の内容で判定する）。
    """
    src = doc["source"]

    def clean(v):
        return CTRL_RE.sub("", v)[:MAX_META_TEXT]

    rows = [{"source": src, "kind": "since", "value": clean(doc["since"])},
            {"source": src, "kind": "until", "value": clean(doc["until"])}]
    ws = doc.get("warnings")
    if isinstance(ws, list):
        texts = [clean(w) for w in ws if isinstance(w, str)]
        for t in [t for t in texts if t][:MAX_META_WARNINGS]:
            rows.append({"source": src, "kind": "warning", "value": t})
    if isinstance(doc.get("estimate"), dict):
        rows.append({"source": src, "kind": "estimate", "value": "true"})
        if src == "jobs":
            for k in EST_COUNT_KEYS:
                if k not in doc["estimate"]:
                    continue
                v = doc["estimate"][k]
                if isinstance(v, bool) or not isinstance(v, int) or v < 0 or v > MAX_EST_COUNT:
                    raise BuildError("rows の estimate.%s が不正" % k)
                rows.append({"source": src, "kind": "estimate_" + k, "value": str(v)})
    if src == "billing" and doc.get("assume_current_visibility") is True:
        rows.append({"source": src, "kind": "assume_current_visibility", "value": "true"})
    return rows


def resolve_updated_at(explicit, entries):
    if explicit:
        if not ISO_RE.match(explicit):
            raise BuildError("--updated-at は YYYY-MM-DDTHH:MM:SSZ 形式で指定する")
        return explicit
    gens = [e["generated_at"] for e in entries if e["generated_at"]]
    if gens:
        return max(gens)
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_meta(title):
    return {"title": title}


def build_params(plan):
    p = dict(DEFAULT_PARAMS)
    p["plan_included_minutes"] = plan
    return p


def build_source(url, path):
    name = os.path.basename(path)
    src = {"kind": "file", "url": url, "name": name}
    if not name.lower().endswith(".json"):
        src["format"] = "json"
    return src


def dataset_description(did, usage_csv=False):
    """dataset の説明文。billing だけ rows の取得元（usage_csv か否か）で切り替える。"""
    if did == "billing" and usage_csv:
        return BILLING_DESC_CSV
    return DATASET_META[did][1]


def build_dataset(did, url, path, updated, usage_csv=False):
    title = DATASET_META[did][0]
    desc = dataset_description(did, usage_csv)
    return {"title": title, "description": desc, "source": build_source(url, path), "updated": updated}


def build_update(url, path, updated, description=None):
    # source は merge で kind / name が消えないよう丸ごと送る。触るのは source と updated だけ。
    # billing だけは取得元を後から切り替えられるので、description も同時に送る（jobs と meta は送らない）。
    doc = {"source": build_source(url, path), "updated": updated}
    if description is not None:
        doc["description"] = description
    return doc


def build_index(html):
    return {"text": html}


def write_entry(coll, doc_id, fname, out_dir, op="set", if_version=None):
    w = {"op": op, "collection": coll, "doc_id": doc_id, "file_path": os.path.join(out_dir, fname)}
    if if_version is not None:
        w["if_version"] = if_version
    return w


def parse_if_versions(items, ids):
    got = {}
    for it in items:
        m = IFV_RE.match(it)
        if not m or int(m.group(2)) < 1:
            raise BuildError("--if-version は <billing|jobs|billing_meta|jobs_meta>=<正の整数> で指定する")
        if m.group(1) in got:
            raise BuildError("--if-version %s が重複している" % m.group(1))
        got[m.group(1)] = int(m.group(2))
    if set(got) != set(ids):
        raise BuildError("--if-version は --add-dataset 以外の dataset（%s）ごとに 1 つ必要" % ", ".join(ids))
    return got


def main(argv=None):
    ap = argparse.ArgumentParser(description="Dashboard 型 Artifact へ書く文書と batch.json を生成する")
    ap.add_argument("--org", required=True)
    ap.add_argument("--rows", action="append", default=[], help="rows JSON（作成は billing 必須・jobs 任意。--update では jobs だけも可。--template-only では指定しない）")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--asset-url", action="append", default=[], help="アップロード後の /_blob/ url（1 段目の一覧と同順・同数。rows → meta の交互）")
    ap.add_argument("--update", action="store_true")
    ap.add_argument("--if-version", action="append", default=[], help="<id>=<N>（--update 用）")
    ap.add_argument("--add-dataset", action="append", default=[], choices=["jobs", "billing_meta", "jobs_meta"],
                    help="--update で store にまだ無い dataset を op: set で作る（jobs は jobs_meta も。理由は SKILL.md）")
    ap.add_argument("--refresh-template", action="store_true",
                    help="--update 用。files/index.html をスキル同梱の雛形で置き換える（op: set）")
    ap.add_argument("--template-only", action="store_true",
                    help="--update --refresh-template 用。rows・meta を書かず files/index.html だけを置き換える（1 段。--template-if-version 必須）")
    ap.add_argument("--template-if-version", type=int,
                    help="--refresh-template の 2 段目と --template-only で必須。store 上の files/index.html の版")
    ap.add_argument("--title")
    ap.add_argument("--plan-included-minutes", type=int)
    ap.add_argument("--updated-at")
    ap.add_argument("--updated-by", default=None)
    a = ap.parse_args(argv)
    try:
        return run(a)
    except BuildError as e:
        sys.stderr.write("エラー: %s\n" % safe(e))
        return 1
    except OSError as e:
        sys.stderr.write("エラー: ファイルの読み書きに失敗（%s）\n" % safe(e.__class__.__name__))
        return 1


def run_template_only(a):
    """雛形だけを置き換える 1 段の経路（rows・meta・datasets・dash は書かない）。

    run() から --template-only のときだけ呼ばれる。検証はすべてファイル操作の前に行い、
    違反時は何も消さず何も書かない。承認の取得は SKILL.md の手順側の責務。
    """
    if not (a.update and a.refresh_template):
        raise BuildError("--template-only は --update --refresh-template と併用する")
    if a.rows:
        raise BuildError("--template-only は --rows と併用できない")
    if a.asset_url:
        raise BuildError("--template-only は --asset-url と併用できない")
    if a.if_version:
        raise BuildError("--template-only は --if-version と併用できない")
    if a.add_dataset:
        raise BuildError("--template-only は --add-dataset と併用できない")
    for flag, v in (("--title", a.title), ("--plan-included-minutes", a.plan_included_minutes),
                    ("--updated-at", a.updated_at), ("--updated-by", a.updated_by)):
        if v is not None:
            raise BuildError("--template-only は %s と併用できない" % flag)
    if a.template_if_version is None:
        raise BuildError("--template-only には --template-if-version <N> が必要（files/index.html の版を get で取る）")
    if a.template_if_version < 1:
        raise BuildError("--template-if-version は正の整数で指定する")
    out_dir = os.path.realpath(a.out_dir)
    index = build_index(load_template())
    total = check_doc_size("files/index.html", index)
    if total >= MAX_BATCH_BYTES:
        raise BuildError("batch の本文の合計が 1 MiB を超える（%d バイト）" % total)
    batch = [write_entry("files", "index.html", "index.json", out_dir, "set", a.template_if_version)]

    os.makedirs(out_dir, exist_ok=True)
    # 前回の通常更新の成果物が残ると別 batch.json の誤用を招くため、固定名だけを消す。
    names = ["batch.json", "index.json", "dash-meta.json", "dash-params.json"]
    names += ["dataset-%s.json" % d for d in ALL_IDS] + ["dataset-%s.update.json" % d for d in ALL_IDS]
    names += ["meta-%s.json" % d for d in DATASET_IDS]
    for fn in names:
        try:
            os.unlink(os.path.join(out_dir, fn))
        except FileNotFoundError:
            pass
    atomic_write_json(os.path.join(out_dir, "index.json"), index)
    bp = os.path.join(out_dir, "batch.json")
    atomic_write_json(bp, batch)
    print("ArtifactData の batch の writes に %s の中身（1 件）を渡す。" % safe(bp))
    print("ページ（files/index.html）だけをスキル同梱の雛形で置き換える（op: set、if_version %d）。"
          "rows・meta・datasets は書かない。" % a.template_if_version)
    print("書き込み後、check 文書を確認する（summarize.py は rows が無いので使わない）。")
    return 0


def run(a):
    if not ORG_RE.match(a.org):
        raise BuildError("--org が不正")
    if a.template_only:
        return run_template_only(a)
    if not 1 <= len(a.rows) <= 2:
        raise BuildError("--rows は 1〜2 回指定する")
    if a.plan_included_minutes is not None and a.plan_included_minutes < 0:
        raise BuildError("--plan-included-minutes は非負の整数で指定する")
    entries = [load_rows_doc(p, a.org) for p in a.rows]
    ids = [e["id"] for e in entries]
    if len(set(ids)) != len(ids):
        raise BuildError("同じ source の rows が重複している")
    # billing 必須は作成だけ。更新は dataset 単位の op なので jobs だけも通す
    # （billing は月粒度・jobs は日粒度で、片方だけ取り直したい場面がある）。
    if not a.update and "billing" not in ids:
        raise BuildError("作成には billing の rows が必要")
    title = clean_text("--title", a.title) if a.title else "%s の Actions 利用状況" % a.org
    by = clean_text("--updated-by", a.updated_by if a.updated_by is not None else DEFAULT_BY)
    updated = {"at": resolve_updated_at(a.updated_at, entries), "by": by}
    out_dir = os.path.realpath(a.out_dir)
    # アップロードの一覧は「rows → その meta」の交互。--asset-url はこの順・この数で受ける。
    uploads = []
    for e in entries:
        e["meta_path"] = os.path.join(out_dir, "meta-%s.json" % e["id"])
        uploads += [("rows", e, e["path"]), ("meta", e, e["meta_path"])]
    if a.asset_url and len(a.asset_url) != len(uploads):
        raise BuildError("--asset-url は 1 段目の一覧と同じ回数（rows と meta で %d 件）で指定する" % len(uploads))
    for u in a.asset_url:
        if not BLOB_RE.match(u):
            raise BuildError("--asset-url は /_blob/<id> 形式のみ受け付ける")
    if a.if_version and not a.update:
        raise BuildError("--if-version は --update と併用する")
    adds = list(a.add_dataset)
    if adds and not a.update:
        raise BuildError("--add-dataset は --update と併用する")
    if len(set(adds)) != len(adds):
        raise BuildError("--add-dataset が重複している")
    # --add-dataset jobs は jobs_meta も新規にする（meta は rows と同じ扱い）。
    new_ids = set(adds) | ({"jobs_meta"} if "jobs" in adds else set())
    for d in adds:
        if d.split("_")[0] not in ids:
            raise BuildError("--add-dataset %s には対応する --rows が必要" % d)
    for d in sorted(new_ids):
        if any(i.split("=")[0] == d for i in a.if_version):
            raise BuildError("--add-dataset %s と --if-version %s は同時に指定できない" % (d, d))
    stage1 = not a.asset_url
    if a.refresh_template and not a.update:
        raise BuildError("--refresh-template は --update と併用する（作成は常に雛形を書く）")
    if a.template_if_version is not None:
        if not a.refresh_template:
            raise BuildError("--template-if-version は --refresh-template と併用する")
        if a.template_if_version < 1:
            raise BuildError("--template-if-version は正の整数で指定する")
    if a.refresh_template and not stage1 and a.template_if_version is None:
        raise BuildError("--refresh-template の 2 段目には --template-if-version <N> が必要"
                         "（files/index.html の版を get で取る）")
    written = [i for i in ALL_IDS if i.split("_")[0] in ids]
    if_ver = (parse_if_versions(a.if_version, [i for i in written if i not in new_ids])
              if (a.update and not stage1) else None)
    # 出力先の固定名が入力 rows と同じ実体を指すと、削除・上書きで rows を壊す。変更前に拒否する。
    reserved = ["batch.json", "dash-meta.json", "dash-params.json", "index.json"]
    for did in ALL_IDS:
        reserved += ["dataset-%s.json" % did, "dataset-%s.update.json" % did]
    reserved += ["meta-%s.json" % did for did in DATASET_IDS]
    outs = set(os.path.realpath(os.path.join(out_dir, n)) for n in reserved)
    for e in entries:
        if os.path.realpath(e["path"]) in outs:
            raise BuildError("--rows が --out-dir の生成物と衝突する: %s（別の --out-dir を指定する）"
                             % safe(os.path.basename(e["path"])))

    # 書く内容をすべて組み立て、検査を通ってからファイルを書く。
    files = {}
    assets = {}
    batch = []
    for e in entries:
        assets["meta-%s.json" % e["id"]] = e["meta_rows"]
    by_id = {}
    csv_billing = any(e.get("usage_csv") for e in entries)
    if not stage1:
        # did（rows と meta の dataset id）→ (アップロード元のパス, url)。uploads と asset_url は同順。
        for (kind, e, path), u in zip(uploads, a.asset_url):
            by_id[e["id"] if kind == "rows" else META_IDS[e["id"]]] = (path, u)
    if a.update:
        if not stage1:
            for did in ALL_IDS:
                if did not in by_id:
                    continue
                path, u = by_id[did]
                if did in new_ids:
                    fn = "dataset-%s.json" % did
                    files[fn] = build_dataset(did, u, path, updated, csv_billing)
                    batch.append(write_entry("datasets", did, fn, out_dir))
                else:
                    fn = "dataset-%s.update.json" % did
                    files[fn] = build_update(
                        u, path, updated,
                        dataset_description(did, csv_billing) if did == "billing" else None)
                    batch.append(write_entry("datasets", did, fn, out_dir, "update", if_ver[did]))
        if a.refresh_template:
            # 1 段目でもサイズ超過を検出できるよう、組み立ては段に依らず行う。
            files["index.json"] = build_index(load_template())
            if not stage1:
                batch.append(write_entry("files", "index.html", "index.json", out_dir,
                                         "set", a.template_if_version))
    else:
        html = load_template()
        files["dash-meta.json"] = build_meta(title)
        files["dash-params.json"] = build_params(a.plan_included_minutes)
        files["index.json"] = build_index(html)
        if not stage1:
            for did in ALL_IDS:
                if did in by_id:
                    path, u = by_id[did]
                    files["dataset-%s.json" % did] = build_dataset(did, u, path, updated, csv_billing)
            batch.append(write_entry("dash", "meta", "dash-meta.json", out_dir))
            batch.append(write_entry("dash", "params", "dash-params.json", out_dir))
            for did in ALL_IDS:
                if did in by_id:
                    batch.append(write_entry("datasets", did, "dataset-%s.json" % did, out_dir))
            batch.append(write_entry("files", "index.html", "index.json", out_dir))
    total = 0
    for fn, obj in files.items():
        total += check_doc_size("files/index.html" if fn == "index.json" else fn, obj)
    for fn, obj in assets.items():
        check_doc_size(fn, obj)
    if len(batch) > MAX_BATCH_WRITES:
        raise BuildError("batch の要素数が 50 件を超える")
    if total >= MAX_BATCH_BYTES:
        raise BuildError("batch の本文の合計が 1 MiB を超える（%d バイト）" % total)

    if a.update and not a.refresh_template and any(e.get("usage_csv") for e in entries):
        # 古い雛形は dependabot_free を知らず区分不明と表示する。store 上の雛形の版は分からず、
        # 差し替え済みのページのデータ更新を止めたくないため、失敗にはせず案内だけを出す（Issue #228）。
        print("案内: 使用量 CSV 由来の rows は Dependabot の分（dependabot_free）を含み得る。"
              "既存ページの雛形が古いと区分不明と表示するため、--refresh-template で雛形を差し替える。")
    os.makedirs(out_dir, exist_ok=True)
    if stage1:
        # 古い url のまま書く事故を防ぐため、固定名の前回成果物だけを消す。
        # --update でも前回の batch.json（op: set など）を残すと誤使用で params が初期値に戻る。
        names = (["batch.json", "index.json"] + ["dataset-%s.json" % d for d in sorted(new_ids)] if a.update
                 else ["batch.json"] + ["dataset-%s.json" % d for d in ALL_IDS])
        for fn in names:
            try:
                os.unlink(os.path.join(out_dir, fn))
            except FileNotFoundError:
                pass
    for fn, obj in list(files.items()) + list(assets.items()):
        atomic_write_json(os.path.join(out_dir, fn), obj)
    if batch:
        bp = os.path.join(out_dir, "batch.json")
        atomic_write_json(bp, batch)
        print("ArtifactData の batch の writes に %s の中身（%d 件）を渡す。" % (safe(bp), len(batch)))
        if a.update:
            for d in sorted(new_ids):
                print("%s は新しい dataset として作る（op: set）。既にあれば store が拒否する。"
                      "その場合は --add-dataset を外し --if-version %s=<N> にする。" % (d, d))
            print("既存 dataset は source と updated（billing は description も）だけを書き換える（if_version で版を固定）。")
            if a.refresh_template:
                print("ページ（files/index.html）をスキル同梱の雛形で置き換える（op: set、if_version で版を固定）。")
        else:
            print("書き込み後、summarize.py の check モードで内容を確認する。")
    else:
        print("アップロード（upload_asset）するファイル（この順）:")
        for i, (_, _, path) in enumerate(uploads, 1):
            print("  %d. %s" % (i, safe(path)))
        print("返った url を同じ順で --asset-url に渡し、同じ引数で再実行する%s。"
              % ("（--update は既存 dataset ごとに --if-version <id>=<N>、新しく足す dataset に --add-dataset <id> が必要%s）"
                 % ("、ページ差し替えに --template-if-version <N>（files/index.html の版）が必要" if a.refresh_template else "")
                 if a.update else ""))
        print("注意: 添付したファイル（meta も。警告文にリポジトリ名を含み得る）はダッシュボードを開ける全員が読める。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
