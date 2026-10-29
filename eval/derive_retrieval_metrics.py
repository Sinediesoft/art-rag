"""檢索層衍生指標的離線 CLI（TASK-20261029-002）：只讀已保存的正式 run 與草稿 1 的 answers.jsonl，
重算 skip_meta、meta_answerable、precision_ceiling 三種衍生口徑，寫一份獨立的衍生 JSON。

離線：只用標準函式庫與 eval/retrieval_metrics.py、eval/retrieval_derived_metrics.py 兩個純函式庫；
不 import 產品 app、不讀 .env、不載入模型或索引、不連網路，也不用現行 KB 或新索引補資料。
衍生產物是 `derived_from_run`，不是新的真模型 run；原 run、answers 與原指標一律不改。

正式來源的 SHA256 釘選在 PINS（run 與 answers），不符就停；輸出預設放
eval/runs/retrieval-derived/<run_id>-derived.json（子目錄，不放 eval/runs 根目錄，避免產品 API 與
既有證據測試的非遞迴 glob 誤讀）。已存在的輸出、或與來源相同的路徑一律拒絕覆寫。

必要資料缺漏、同 chunk 有效原文衝突、順序或 ID 矛盾、保存的原指標與重算不符時，列出受影響題後
明確失敗（結束碼 1、不寫檔）；不當成未命中，也不悄悄縮小分母。

用法：
  python eval/derive_retrieval_metrics.py [--run PATH] [--answers PATH] [--out PATH]
結束碼：0＝完成；1＝資料不完整／矛盾（不寫檔）；2＝參數、來源 hash、輸出位置不對（不寫檔）。
"""

import argparse
import hashlib
import json
import shlex
import sys
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "eval"))
import retrieval_derived_metrics as rdm  # noqa: E402
import retrieval_metrics as rm  # noqa: E402

SCHEMA_VERSION = "retrieval-derived-metrics/1"
KIND = "retrieval-derived-metrics"
STATUS = "derived_from_run"
RUNS_DIR = ROOT / "eval" / "runs"
DEFAULT_RUN = RUNS_DIR / "20261027T184738-52cd-retrieval-layer-metrics.json"
DEFAULT_ANSWERS = ROOT / "eval" / "drawing_retrieval" / "answers.jsonl"
OUT_DIRNAME = "retrieval-derived"
PINS = {
    "20261027T184738-52cd": {
        "run": "834344262d75a9d64c566183b6ebf8c58bc5052e3954e0672e95575a7a620707",
        "answers": "70c249dac1f483f5df43d18aeab602bc38ec60ec22fc2dad126db864fe37bcfa",
    }
}
CLI_PATH = Path(__file__).resolve()
LIB_PATH = Path(rdm.__file__).resolve()
ANSWER_DOMAINS = ("mfg",)  # 草稿 1 的 answers 只涵蓋圖紙；畫作沒有答案，meta_answerable 不適用
REQUIRED_ROW = (
    "case_id",
    "owner_id",
    "status",
    "gold_chunk_ids",
    "owner_visible",
    "raw_sources",
    "returned",
    "score",
    "cohorts",
    "unreviewed_gold",
)
REQUIRED_GROUP = ("group_id", "domain", "mode", "call_kind", "rows")
ORIGINAL_NAMES = (*rdm.SKIP_META_METRICS, "precision")
DEFINITIONS = {
    "skip_meta": "先對保存的回傳 stable 去重，再移除 chunk_id 以精確 #meta 結尾的段落並重新排名；"
    "原可見 G 不變；有限既有回傳內的衍生重排，不代表真實重新檢索，也不補查新段落。",
    "meta_answerable": "草稿 1 keywords 的每個 AND 組至少一個 OR 候選完整逐字（區分大小寫、"
    "不正規化、不做語意推論）出現在同 owner #meta 原文才合格；"
    "合格才令 G'＝G∪{owner#meta}，排名不變；"
    "僅原 G 非空、可見且有草稿 1 答案的圖紙題適用，畫作不適用。",
    "precision_ceiling": "逐題 min(|G|,|R|)/|R|（原可見 G、原去重 R，不先 skip_meta、不用 G'）；"
    "G 非空 R 空記 0 並留分母，G 空記 null 並排除；再按題 macro 平均。",
    "aggregation": "各 group、cohort 獨立 macro 平均；每項附逐題值與 mean／numerator／denominator；"
    "集合 Recall＝|前 K ∩ G|／|G|，不是 hit rate。",
}


class DerivationError(Exception):
    """資料不完整或矛盾；problems 列出每個問題（含受影響的 group／case）。"""

    def __init__(self, problems: Sequence[str]):
        self.problems = list(problems)
        super().__init__("\n".join(self.problems))


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def rel(path: Path) -> str:
    p = path.resolve()
    try:
        return p.relative_to(ROOT).as_posix()
    except ValueError:
        return str(p)


# ---------------------------------------------------------------- 驗證


def _label(group: Mapping, row: Mapping | None = None) -> str:
    gid = group.get("group_id", "?")
    return gid if row is None else f"{gid}/{row.get('case_id', '?')}"


def validate_answers(entries: Sequence[Mapping]) -> tuple[dict, list[str]]:
    """answers.jsonl：題號唯一；拒答題有 expect_refusal 且無 keywords，其餘必須有合法 keywords。"""
    by_id, problems = {}, []
    for i, e in enumerate(entries):
        cid = e.get("id") if isinstance(e, Mapping) else None
        if not isinstance(cid, str) or not cid:
            problems.append(f"answers 第 {i} 筆缺題號 id")
            continue
        if cid in by_id:
            problems.append(f"answers 題號重複：{cid}")
            continue
        by_id[cid] = e
        if e.get("expect_refusal"):
            if "keywords" in e:
                problems.append(f"answers {cid}：拒答題不應有 keywords")
            continue
        bad = rdm.validate_keywords(e.get("keywords"))
        if bad:
            problems.append(f"answers {cid}：keywords 不合法（{bad}）")
    return by_id, problems


def validate_rows(run: Mapping, answers_by_id: Mapping) -> list[str]:  # noqa: C901
    """保存的 run 結構、ID 與順序、原分數重算、cohort 成員與原彙總是否自洽。"""
    p: list[str] = []
    if run.get("kind") != "retrieval-layer-metrics" or run.get("status") != "ok":
        p.append(f"來源 run 不是完整的 retrieval-layer-metrics（kind={run.get('kind')!r}）")
    if run.get("problems"):
        p.append(f"來源 run 自己記錄了 problems：{run['problems']}")
    groups = run.get("groups")
    summary = run.get("summary")
    if not isinstance(groups, list) or not groups or not isinstance(summary, Mapping):
        return [*p, "來源 run 缺 groups 或 summary"]
    mfg_case_ids = set()
    for g in groups:
        missing = [k for k in REQUIRED_GROUP if k not in g]
        if missing:
            p.append(f"{_label(g)}：缺欄位 {missing}")
            continue
        cohorts = (summary.get(g["group_id"]) or {}).get("cohorts")
        if not isinstance(cohorts, Mapping) or "all" not in cohorts:
            p.append(f"{_label(g)}：summary 缺 cohorts.all")
            continue
        seen = set()
        for r in g["rows"]:
            lab = _label(g, r)
            missing = [k for k in REQUIRED_ROW if k not in r]
            if missing:
                p.append(f"{lab}：缺必要欄位 {missing}")
                continue
            if r["case_id"] in seen:
                p.append(f"{lab}：題號重複")
            seen.add(r["case_id"])
            if g["domain"] in ANSWER_DOMAINS:
                mfg_case_ids.add(r["case_id"])
            p += _validate_row(g, r, cohorts)
            if (
                g["domain"] in ANSWER_DOMAINS
                and r["gold_chunk_ids"]
                and r["owner_visible"]
                and r["case_id"] not in answers_by_id
            ):
                p.append(f"{lab}：answers 沒有這題（原 G 非空的圖紙題必須有答案關鍵字）")
        order = [r.get("case_id") for r in g["rows"]]
        if order != cohorts["all"].get("case_ids"):
            p.append(f"{_label(g)}：逐題順序與 summary.cohorts.all.case_ids 不同")
        p += _validate_cohort_summaries(g, cohorts)
    extra = sorted(set(answers_by_id) - mfg_case_ids)
    if extra:
        p.append(f"answers 有 run 裡沒有的圖紙題：{extra}")
    return p


def _validate_row(g: Mapping, r: Mapping, cohorts: Mapping) -> list[str]:  # noqa: C901
    lab = _label(g, r)
    p: list[str] = []
    if r.get("error") is not None:
        p.append(f"{lab}：這題在原 run 執行失敗（error={r['error']!r}），不能當成未命中")
        return p
    raw, returned, gold = r["raw_sources"], r["returned"], r["gold_chunk_ids"]
    if not all(isinstance(x, list) for x in (raw, returned, gold)):
        return [*p, f"{lab}：raw_sources／returned／gold_chunk_ids 必須是列表"]
    raw_ids = [s.get("chunk_id") if isinstance(s, Mapping) else None for s in raw]
    ret_ids = [x.get("chunk_id") if isinstance(x, Mapping) else None for x in returned]
    try:
        rm.dedupe_ids(raw_ids)
        rm.dedupe_ids(gold)
    except ValueError as e:
        return [*p, f"{lab}：ID 缺漏（{e}）"]
    if raw_ids != ret_ids:
        p.append(f"{lab}：raw_sources 與 returned 的 chunk_id 順序矛盾")
    if [x.get("rank") for x in returned] != list(range(1, len(returned) + 1)):
        p.append(f"{lab}：returned.rank 不是連續的 1..{len(returned)}")
    if (r["status"] == "answerable") != bool(gold):
        p.append(f"{lab}：status={r['status']!r} 與可見 gold（{len(gold)} 個）矛盾")
    score = r["score"]
    if score.get("returned_ids") != rm.dedupe_ids(raw_ids):
        p.append(f"{lab}：score.returned_ids 與去重後的回傳順序矛盾")
    if score.get("gold_ids") != rm.dedupe_ids(gold):
        p.append(f"{lab}：score.gold_ids 與 gold_chunk_ids 矛盾")
    try:
        expect = rm.score_text_case(
            raw_ids, gold, empty_kind=None if gold else r.get("empty_kind")
        )["metrics"]
        if expect != score.get("metrics"):
            p.append(f"{lab}：保存的原指標與由回傳重算的結果不符")
    except ValueError as e:
        p.append(f"{lab}：原指標無法重算（{e}）")
    for name, c in cohorts.items():
        if (r["case_id"] in c.get("case_ids", [])) != (name in r["cohorts"]):
            p.append(f"{lab}：cohort {name} 的成員與 row.cohorts 矛盾")
    return p


def _validate_cohort_summaries(g: Mapping, cohorts: Mapping) -> list[str]:
    """每個 cohort 的原 Recall／MRR／Precision macro（從逐題保存的分數重算）要等於保存的原彙總。"""
    p: list[str] = []
    by_case = {r["case_id"]: r for r in g["rows"] if "case_id" in r}
    for name, c in cohorts.items():
        members = [by_case.get(i) for i in c.get("case_ids", [])]
        if any(m is None or "score" not in m for m in members):
            p.append(f"{_label(g)}：cohort {name} 的成員不在逐題資料內")
            continue
        for metric in ORIGINAL_NAMES:
            got = rm.macro_average(m["score"]["metrics"].get(metric) for m in members)
            saved = c["answerable"]["metrics"][metric]
            same = (
                got["denominator"] == saved["denominator"]
                and abs(got["numerator"] - saved["numerator"]) < 1e-9
            )
            if not same:
                p.append(f"{_label(g)}：cohort {name} 的原 {metric} 彙總與逐題重算不符")
    return p


# ---------------------------------------------------------------- 原文彙集與衍生


def _text_entries(run: Mapping):
    for gi, g in enumerate(run["groups"]):
        for ri, r in enumerate(g["rows"]):
            for si, s in enumerate(r["raw_sources"]):
                loc = {
                    "group_id": g["group_id"],
                    "group_index": gi,
                    "case_id": r["case_id"],
                    "row_index": ri,
                    "raw_sources_index": si,
                }
                yield {"chunk_id": s["chunk_id"], "text": s.get("text"), "location": loc}


def _needs_meta(g: Mapping, r: Mapping, answers_by_id: Mapping) -> bool:
    """這題是否要用到同 owner 的 meta 原文：圖紙、原 G 非空、可見、有合法 keywords。"""
    if g["domain"] not in ANSWER_DOMAINS or not r["gold_chunk_ids"] or not r["owner_visible"]:
        return False
    entry = answers_by_id.get(r["case_id"])
    return entry is not None and "keywords" in entry and not entry.get("expect_refusal")


def _text_problems(run, collected, answers_by_id) -> list[str]:
    p: list[str] = []
    needs = defaultdict(list)
    for g in run["groups"]:
        for r in g["rows"]:
            if _needs_meta(g, r, answers_by_id):
                needs[rdm.meta_chunk_id(r["owner_id"])].append(f"{_label(g, r)}")
    for c in collected["conflicts"]:
        cid = c["chunk_id"]
        located = sorted(
            {f"{loc['group_id']}/{loc['case_id']}" for v in c["variants"] for loc in v["locations"]}
        )
        affected = sorted(set(needs.get(cid, [])) | set(located))
        p.append(f"chunk {cid} 的有效原文衝突（{len(c['variants'])} 種版本）；受影響題：{affected}")
    for cid, cases in needs.items():
        if cid not in collected["texts"] and not any(
            c["chunk_id"] == cid for c in collected["conflicts"]
        ):
            p.append(
                f"缺 {cid} 的有效原文（同 run 的 raw_sources 沒有非空字串 text）；受影響題：{cases}"
            )
    return p


def build_derived(
    run: Mapping,
    answer_entries: Sequence[Mapping],
    *,
    run_path: str,
    run_sha256: str,
    answers_path: str,
    answers_sha256: str,
    program: Mapping,
    command: str,
    argv: Sequence[str],
    created_at: str,
) -> dict:
    """由保存的 run 與 answers 產生衍生 JSON（dict）。資料不完整或矛盾時丟 DerivationError。"""
    answers_by_id, problems = validate_answers(answer_entries)
    problems += validate_rows(run, answers_by_id)
    if problems:
        raise DerivationError(problems)

    collected = rdm.collect_chunk_texts(_text_entries(run))
    problems = _text_problems(run, collected, answers_by_id)
    if problems:
        raise DerivationError(problems)
    texts = collected["texts"]

    ks = rdm.TEXT_KS
    groups_out, summary_out = [], {}
    for gi, g in enumerate(run["groups"]):
        rows_out, derived_by_case = [], {}
        for ri, r in enumerate(g["rows"]):
            row_out, case = _derive_row(g, gi, ri, r, answers_by_id, texts, ks)
            rows_out.append(row_out)
            derived_by_case[r["case_id"]] = (r, case)
        groups_out.append(
            {
                "group_id": g["group_id"],
                "domain": g["domain"],
                "mode": g["mode"],
                "call_kind": g["call_kind"],
                "account": g.get("account"),
                "production": g.get("production"),
                "source_position": f"groups[{gi}]",
                "rows": rows_out,
            }
        )
        cohorts = {}
        for name, c in run["summary"][g["group_id"]]["cohorts"].items():
            members = [derived_by_case[i] for i in c["case_ids"]]
            agg = rdm.aggregate_cases([case for _, case in members], ks)
            original = {
                m: rm.macro_average(r["score"]["metrics"].get(m) for r, _ in members)
                for m in ORIGINAL_NAMES
            }
            cohorts[name] = {"case_ids": list(c["case_ids"]), **agg, "original_metrics": original}
        summary_out[g["group_id"]] = {"cohorts": cohorts}

    meta_texts = {}
    for cid, t in texts.items():
        if rdm.is_meta_id(cid):
            meta_texts[cid] = {
                "text": t["text"],
                "sha256": t["sha256"],
                "valid_source_count": len(t["locations"]),
                "sources": t["locations"],
            }
    prov = run.get("provenance") or {}
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "rule_version": rdm.RULE_VERSION,
        "status": STATUS,
        "real": False,
        "is_new_model_run": False,
        "created_at": created_at,
        "note": "由已保存的正式 run 離線重算的衍生指標；不是新的 real_run，沒有重跑模型，"
        "原 run 的 provenance 原樣揭露在 derived_from_run.provenance。",
        "derived_from_run": {
            "run_id": run.get("run_id"),
            "path": run_path,
            "sha256": run_sha256,
            "schema_version": run.get("schema_version"),
            "kind": run.get("kind"),
            "status": run.get("status"),
            "embed_mode": run.get("embed_mode"),
            "real": run.get("real"),
            "official": run.get("official"),
            "created_at": run.get("created_at"),
            "finished_at": run.get("finished_at"),
            "exec_sha": (prov.get("code") or {}).get("head"),
            "provenance": prov,
        },
        "answers": {
            "path": answers_path,
            "sha256": answers_sha256,
            "n_entries": len(answer_entries),
            "n_refusal": sum(bool(e.get("expect_refusal")) for e in answer_entries),
            "case_ids": [e["id"] for e in answer_entries],
        },
        "derivation": {
            "program": dict(program),
            "rule_version": rdm.RULE_VERSION,
            "command": command,
            "argv": list(argv),
            "created_at": created_at,
            "offline": True,
        },
        "definitions": DEFINITIONS,
        "chunk_texts": {
            "total_sources": collected["valid_sources"] + collected["omitted"],
            "valid_text_sources": collected["valid_sources"],
            "omitted_text_sources": collected["omitted"],
            "chunks_with_text": len(texts),
            "conflicts": [],
        },
        "meta_texts": meta_texts,
        "groups": groups_out,
        "summary": summary_out,
    }


def _derive_row(g, gi, ri, r, answers_by_id, texts, ks) -> tuple[dict, dict]:
    raw_ids = [s["chunk_id"] for s in r["raw_sources"]]
    entry = answers_by_id.get(r["case_id"]) if g["domain"] in ANSWER_DOMAINS else None
    applicable_row = g["domain"] in ANSWER_DOMAINS
    gold = rm.dedupe_ids(r["gold_chunk_ids"])
    if applicable_row and gold and r["owner_visible"] and entry is None:
        raise DerivationError(
            [f"{_label(g, r)}：answers 沒有這題（原 G 非空的圖紙題必須有答案關鍵字）"]
        )
    meta = rdm.meta_chunk_id(r["owner_id"])
    needs_text = _needs_meta(g, r, answers_by_id)
    keywords = None if entry is None or entry.get("expect_refusal") else entry["keywords"]
    case = rdm.derive_case(
        r["case_id"],
        raw_ids,
        gold,
        r["owner_id"],
        owner_visible=r["owner_visible"],
        answers_applicable=applicable_row,
        keywords=keywords,
        meta_text=texts[meta]["text"] if needs_text else None,
        ks=ks,
    )
    first_score = {}
    for x in r["returned"]:
        first_score.setdefault(x["chunk_id"], x["score"])
    deduped = rm.dedupe_ids(raw_ids)
    meta_out = dict(case["meta_answerable"])
    meta_out["meta_text_ref"] = (
        {
            "chunk_id": meta,
            "sha256": texts[meta]["sha256"],
            "first_source": texts[meta]["locations"][0],
            "valid_source_count": len(texts[meta]["locations"]),
        }
        if meta_out.get("keyword_match") is not None
        else None
    )
    row_out = {
        "case_id": r["case_id"],
        "owner_id": r["owner_id"],
        "status": r["status"],
        "empty_kind": r.get("empty_kind"),
        "owner_visible": r["owner_visible"],
        "unreviewed_gold": r["unreviewed_gold"],
        "answers_review_status": (entry or {}).get("review_status"),
        "cohorts": list(r["cohorts"]),
        "source_position": {"group_index": gi, "row_index": ri},
        "original": {
            "gold_ids": gold,
            "returned": [
                {"rank": i, "chunk_id": cid, "score": first_score[cid]}
                for i, cid in enumerate(deduped, 1)
            ],
            "raw_returned_count": len(raw_ids),
            "metrics": r["score"]["metrics"],
        },
        "skip_meta": case["skip_meta"],
        "meta_answerable": meta_out,
        "precision_ceiling": case["precision_ceiling"],
    }
    return row_out, case


# ---------------------------------------------------------------- CLI


def _parse(argv: Sequence[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="離線衍生檢索指標（只讀保存的 run 與 answers）")
    ap.add_argument("--run", type=Path, default=DEFAULT_RUN)
    ap.add_argument("--answers", type=Path, default=DEFAULT_ANSWERS)
    ap.add_argument("--out", type=Path, default=None)
    return ap.parse_args(list(argv))


def _fail(code: int, msg: str) -> int:
    print(msg, file=sys.stderr)
    return code


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    args = _parse(argv)
    run_path, answers_path = args.run.resolve(), args.answers.resolve()
    for label, path in (("run", run_path), ("answers", answers_path)):
        if not path.is_file():
            return _fail(2, f"找不到 {label} 檔：{path}")
    try:
        run = json.loads(run_path.read_text(encoding="utf-8"))
        run_id = run["run_id"]
    except (ValueError, KeyError, TypeError) as e:
        return _fail(2, f"來源 run 不是有效的 JSON 或沒有 run_id：{e}")
    out = (args.out or RUNS_DIR / OUT_DIRNAME / f"{run_id}-derived.json").resolve()
    if out in (run_path, answers_path):
        return _fail(2, f"輸出路徑不可與來源相同：{out}")
    if out.parent == RUNS_DIR.resolve():
        return _fail(2, f"輸出不可放在 eval/runs 根目錄（請用 eval/runs/{OUT_DIRNAME}/）：{out}")
    if out.exists() or out.is_symlink():
        return _fail(2, f"輸出已存在，拒絕覆寫：{out}")

    run_sha, answers_sha = sha256_file(run_path), sha256_file(answers_path)
    pin = PINS.get(run_id)
    if pin is not None:
        for label, got, want in (
            ("run", run_sha, pin["run"]),
            ("answers", answers_sha, pin["answers"]),
        ):
            if got != want:
                return _fail(2, f"{label} 的 SHA256 與釘選不符：{got} ≠ {want}")
    try:
        entries = [
            json.loads(x)
            for x in answers_path.read_text(encoding="utf-8").splitlines()
            if x.strip()
        ]
    except ValueError as e:
        return _fail(2, f"answers 不是有效的 JSONL：{e}")

    command = "python eval/derive_retrieval_metrics.py " + " ".join(shlex.quote(a) for a in argv)
    try:
        derived = build_derived(
            run,
            entries,
            run_path=rel(run_path),
            run_sha256=run_sha,
            answers_path=rel(answers_path),
            answers_sha256=answers_sha,
            program={
                "cli": {"path": rel(CLI_PATH), "sha256": sha256_file(CLI_PATH)},
                "lib": {"path": rel(LIB_PATH), "sha256": sha256_file(LIB_PATH)},
            },
            command=command.strip(),
            argv=argv,
            created_at=datetime.now(UTC).isoformat(),
        )
    except DerivationError as e:
        return _fail(1, "衍生失敗（不寫檔）：\n" + "\n".join(f"- {x}" for x in e.problems))

    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(out, "x", encoding="utf-8") as f:  # x：已存在就失敗，不覆寫
            f.write(json.dumps(derived, ensure_ascii=False, indent=1) + "\n")
    except FileExistsError:
        return _fail(2, f"輸出已存在，拒絕覆寫：{out}")
    print(f"已寫入 {rel(out)}（derived_from_run {run_id}，sha256 {run_sha[:12]}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
