"""檢索層評估的證據測試（TASK-20261027-001 AC-6、AC-7）：只讀 eval/runs 裡保存的 JSON／CSV 與
README，不呼叫任何模型。

1. 驗證器（本檔內）：真 run 的 provenance 與可重算的摘要，README 總表每個數字追到來源檔欄位與分母。
   用「自建的假產物」確認驗證器會拒絕：mock 冒充正式 run、缺收據或收據對不上、改名次、改分母、
   README 與 JSON 不符、缺來源、沒有真 run 卻寫了數字。
2. 真產物：*-retrieval-layer-metrics.json、新版 *-drawing-retrieval.json、*-retrieval-index.json
   還沒由人類在真模型環境交回時，需要證據的兩個測試用 pytest.xfail 標示「等待人類證據」
   （CI 不紅燈，但結果是 xfailed、不是 passed，AC-6／AC-7 不算完成）；產物一交回就照常嚴格驗證，
   有任何一份不合格就失敗，不會因為產物存在而放行。
README 裡 legacy 檔案（舊 run）的數字沒有等待的問題，永遠要追得到。

不使用 conftest 的 fixture，可以單檔跑：pytest --noconftest tests/test_retrieval_evidence.py
"""

import copy
import csv
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "eval"))
import retrieval_metrics as rm  # noqa: E402
import retrieval_runtime as rr  # noqa: E402
import run_drawing_retrieval_eval as dre  # noqa: E402
import run_retrieval_eval as rre  # noqa: E402

README = ROOT / "README.md"
RUNS = ROOT / "eval" / "runs"
PENDING = "待人類代跑真模型"
LEGACY = "legacy_provenance_incomplete"
DERIVED = "derived_from_legacy_run"
REAL = "real_run"
STATUSES = (LEGACY, DERIVED, PENDING, REAL)
TABLE_START, TABLE_END = (
    "<!-- retrieval-layer-table:start -->",
    "<!-- retrieval-layer-table:end -->",
)
COLUMNS = ("id", "layer", "metric", "number", "denominator", "den_path", "source", "path", "status")
HEX40 = re.compile(r"[0-9a-f]{40}")
SEP = "／"
LEGACY_SOURCES = (
    "eval/runs/20261004T165452-a7ba-met-photos.json",
    "eval/runs/20260929T075014-aeab.json",
    "eval/runs/20260929T075014-aeab-images.csv",
    "eval/runs/20261010T140539-f694-drawing-retrieval.json",
)
REQUIRED_ACCOUNTS = ("manager", "sales")
KIND_OF = {"index": "*-retrieval-index.json", "text": "*-retrieval-layer-metrics.json"}


# ---------------------------------------------------------------- 路徑與數字


def expand(path: str) -> list[str]:
    """`a.{x／y}.b` → [a.x.b, a.y.b]；沒有大括號就原樣一個。"""
    m = re.search(r"\{([^}]*)\}", path)
    if not m:
        return [path]
    return [path[: m.start()] + alt + path[m.end() :] for alt in m.group(1).split(SEP)]


def resolve(obj, dotted: str):
    """依 . 取 JSON 欄位；缺少就 KeyError。dict 的鍵一律當字串。"""
    cur = obj
    for key in dotted.split("."):
        if not isinstance(cur, dict) or key not in cur:
            raise KeyError(dotted)
        cur = cur[key]
    return cur


def same_number(text: str, value) -> bool:
    """README 的文字和 JSON 的值是否同一個數字：null／a/b 字串／整數／小數（README 寫 4 位）。"""
    text = text.strip()
    if value is None:
        return text == "null"
    if isinstance(value, bool):
        return False
    if isinstance(value, str):
        return text == value
    try:
        num = float(text)
    except ValueError:
        return False
    if isinstance(value, int) or float(value).is_integer() and "." not in text:
        return num == value
    return abs(num - value) <= 5e-5 + 1e-12


def csv_counts(path: Path) -> dict:
    """圖片辨識 CSV 的重算：known＝truth 不是「(未收錄)」的照片。"""
    with path.open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    known = [r for r in rows if r["truth"] != "(未收錄)"]
    return {
        "n_known": len(known),
        "top1": sum(r["top1"] == "True" for r in known),
        "top5": sum(r["top5"] == "True" for r in known),
    }


# ---------------------------------------------------------------- 真 run 驗證器


def _sha_ok(value) -> bool:
    return isinstance(value, str) and len(value) >= 8


def receipt_problems(receipt: dict) -> list[str]:
    """建索引收據能不能當正式證據：真模型、不讀 .env、manifest／KB／題庫 hash、模型 revision。"""
    p = [f"收據：{x}" for x in rr.verify_index_receipt(receipt, require_real=True)]
    p += ["收據：沒有 real 建索引紀錄（index_build.invoked）"] * (
        not (receipt.get("index_build") or {}).get("invoked")
    )
    if receipt.get("kind") != "retrieval-index-receipt":
        p.append(f"收據：kind 不對：{receipt.get('kind')!r}")
    if receipt.get("manifest_embed_mode") != "real" or receipt.get("embed_mode") != "real":
        p.append("收據：embed_mode／manifest.embed_mode 不是 real")
    if not HEX40.fullmatch((receipt.get("code") or {}).get("head") or ""):
        p.append("收據：沒有 40 碼程式 HEAD")
    if receipt.get("manifest_sha256") != rr.sha256_json(receipt.get("manifest")):
        p.append("收據：manifest_sha256 和 manifest 內容不符")
    cfg = receipt.get("config") or {}
    if cfg.get("retrieval_params_sha256") != rr.sha256_json(cfg.get("retrieval_params")):
        p.append("收據：retrieval_params_sha256 和參數內容不符")
    if not _sha_ok(cfg.get("models_yaml_sha256")):
        p.append("收據：缺 models.yaml hash")
    models = receipt.get("models") or {}
    if not models or any(
        not (m.get("name") and m.get("revision") and m.get("dim")) for m in models.values()
    ):
        p.append("收據：模型 name／revision／dim 不完整")
    for d, c in (receipt.get("collections") or {}).items():
        if not (
            _sha_ok(c.get("chunks_sha256")) and _sha_ok((c.get("chunk_vecs") or {}).get("sha256"))
        ):
            p.append(f"收據：{d} 缺段落／向量 digest")
    kb = receipt.get("kb") or {}
    if not (_sha_ok(kb.get("hash_now")) and _sha_ok(kb.get("hash_manifest"))):
        p.append("收據：缺 KB hash")
    if not (receipt.get("qa") and all(_sha_ok(h) for h in receipt["qa"].values())):
        p.append("收據：題庫 hash 不完整")
    return p


def _load_receipt(info: dict | None, root: Path) -> tuple[dict | None, list[str]]:
    """run 裡的收據指標 → 讀檔、核對 sha256、驗證收據；回傳 (收據, 問題)。"""
    info = info or {}
    rel = info.get("path")
    if not rel:
        return None, ["沒有索引收據路徑"]
    path = root / rel
    if not path.is_file():
        return None, [f"索引收據不存在：{rel}"]
    if rr.sha256_file(path) != info.get("sha256"):
        return None, [f"索引收據 sha256 和 run 記錄的不同：{rel}"]
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as e:
        return None, [f"索引收據不是 JSON：{rel}：{e}"]
    return receipt, receipt_problems(receipt)


def _common_provenance(code, isolation, settings, now, receipt, root, info) -> list[str]:
    p = []
    if not HEX40.fullmatch((code or {}).get("head") or ""):
        p.append("沒有 40 碼程式 HEAD")
    if (code or {}).get("dirty") is None:
        p.append("沒有記錄程式 dirty 狀態")
    if not (isolation or {}).get("env_file_disabled"):
        p.append("沒有關閉 env_file（可能讀了 .env）")
    if (settings or {}).get("embed_mode") != "real" or "index_backend" not in (settings or {}):
        p.append("settings 不是 EMBED_MODE=real 或缺 index_backend")
    receipt_obj, problems = _load_receipt(info, root)
    p += problems
    if now is None:
        p.append("沒有執行當下的索引收集（index_now／current）")
    else:
        p += [f"目前索引：{x}" for x in rr.verify_index_receipt(now, require_real=True)]
        if receipt_obj is not None:
            p += [
                f"索引收據對不上目前索引：{x}"
                for x in rr.verify_index_receipt(
                    receipt_obj, require_real=True, require_build=True, current=now
                )
            ]
    return p


def text_run_problems(report: dict, root: Path, *, name: str | None = None) -> list[str]:
    """文字檢索 run 能不能當正式成績：provenance、索引收據鏈，以及逐題資料可重算全部摘要。"""
    p = []
    if (
        report.get("schema_version") != rre.SCHEMA_VERSION
        or report.get("kind") != "retrieval-layer-metrics"
    ):
        p.append(f"schema_version／kind 不對：{report.get('schema_version')!r}")
    if name and not name.startswith(f"{report.get('run_id')}-"):
        p.append(f"run_id {report.get('run_id')!r} 和檔名 {name} 不符")
    if report.get("embed_mode") != "real" or report.get("real") is not True:
        p.append(
            f"不是真模型 run：embed_mode={report.get('embed_mode')!r}，real={report.get('real')!r}"
        )
    if report.get("allow_mock"):
        p.append("allow_mock＝true：mock 測試產物不能當正式成績")
    if (
        report.get("status") != "ok"
        or report.get("exit_code") != 0
        or report.get("official") is not True
    ):
        p.append(
            f"run 沒有成功完成：status={report.get('status')!r}，"
            f"exit_code={report.get('exit_code')!r}，"
            f"official={report.get('official')!r}"
        )
    if report.get("problems"):
        p.append(f"run 帶有問題清單：{report['problems']}")
    if not (report.get("created_at") and report.get("finished_at")):
        p.append("缺 created_at／finished_at")
    prov = report.get("provenance") or {}
    now = prov.get("index_now")
    p += _common_provenance(
        prov.get("code"),
        prov.get("isolation"),
        prov.get("settings"),
        now,
        None,
        root,
        prov.get("index_receipt"),
    )
    if (prov.get("isolation") or {}).get("strict") is not True:
        p.append("bootstrap 不是 strict 模式")
    inputs = prov.get("inputs") or {}
    if len(inputs) < 3 or not all(_sha_ok(h) for h in inputs.values()):
        p.append("題庫／gold 的 hash 不完整")
    params = prov.get("retrieval_params") or {}
    for key in ("top_k_chunks", "min_chunk_score", "relative_chunk_ratio"):
        if key not in params:
            p.append(f"沒有記錄檢索參數 {key}")
    if report.get("k") != params.get("top_k_chunks"):
        p.append(f"k={report.get('k')!r} 和參數 top_k_chunks 不同")
    p += _text_corpus_problems(report, now)
    p += _text_group_problems(report)
    return p


def _text_corpus_problems(report: dict, now: dict | None) -> list[str]:
    p = []
    corpus = report.get("corpus") or {}
    for d in rr.DOMAINS:
        c = corpus.get(d) or {}
        if not (c.get("item_ids") and c.get("chunk_count")):
            p.append(f"corpus 缺 {d} 的文件 ID／段落數")
        elif now is not None:
            n = (now.get("collections") or {}).get(d) or {}
            if c["chunk_count"] != n.get("chunk_count") or c["item_ids"] != n.get("item_ids"):
                p.append(f"corpus.{d} 和 index_now 不同")
    return p


def _text_group_problems(report: dict) -> list[str]:
    p = []
    accounts = report.get("accounts") or {}
    missing = [a for a in REQUIRED_ACCOUNTS if a not in accounts]
    if missing:
        p.append(f"缺帳號 {missing}")
    want = ["art/production_retrieve/specified", "art/production_retrieve/global"]
    for a in accounts:
        want += [
            f"mfg/production_retrieve/specified/{a}",
            f"mfg/{rre.DIAGNOSTIC}/global/{a}",
        ]
    groups = report.get("groups") or []
    if [g.get("group_id") for g in groups] != want:
        p.append(f"groups 和預期不同：{[g.get('group_id') for g in groups]} ≠ {want}")
    gold = report.get("gold") or {}
    for d in rr.DOMAINS:
        if not (gold.get(d) or {}).get("case_ids"):
            p.append(f"gold 缺 {d} 的題號")
    if report.get("summary") != {g.get("group_id"): g.get("summary") for g in groups}:
        p.append("summary 和各 group 的 summary 不同")
    for g in groups:
        p += [f"{g.get('group_id')}：{x}" for x in _recompute_group(g, gold)]
    return p


def _recompute_group(g: dict, gold: dict) -> list[str]:
    p = []
    rows, s = g.get("rows") or [], g.get("summary") or {}
    if not rows:
        return ["沒有逐題資料"]
    if len(rows) != len((gold.get(g.get("domain")) or {}).get("case_ids", [])):
        p.append("題數和 gold.case_ids 不同")
    if g.get("mode") == rre.DIAGNOSTIC and any(r.get("diagnostic") is None for r in rows):
        p.append("全庫診斷的逐題資料缺 diagnostic 說明")
    if g.get("mode") == rre.DIAGNOSTIC and g.get("production"):
        p.append("診斷被標成 production")
    ok = []
    for r in rows:
        if r.get("error") is not None:
            p.append(f"{r.get('case_id')} 執行錯誤：{r['error']}")
            continue
        ids = [e["chunk_id"] for e in r.get("returned", [])]
        kind = r["empty_kind"] if r.get("status") == "expected_empty" else None
        if bool(r.get("gold_chunk_ids")) == (r.get("status") == "expected_empty"):
            p.append(f"{r.get('case_id')}：status 和 gold_chunk_ids 互相矛盾")
            continue
        if r.get("score") != rm.score_text_case(ids, r["gold_chunk_ids"], empty_kind=kind):
            p.append(f"{r['case_id']}：逐題指標和回傳 ID／gold 重算結果不同")
        ok.append(r)
    if (s.get("n_cases"), s.get("n_scored"), s.get("n_errors")) != (
        len(rows),
        len(ok),
        len(rows) - len(ok),
    ):
        p.append("n_cases／n_scored／n_errors 和逐題資料不符")
    for name, cohort in (s.get("cohorts") or {}).items():
        picked = [r for r in ok if name in r.get("cohorts", [])]
        want = rm.aggregate_text_cases([r["score"] for r in picked])
        if [r["case_id"] for r in picked] != cohort.get("case_ids"):
            p.append(f"cohort {name} 的 case_ids 和逐題資料不符")
        if {k: cohort.get(k) for k in want} != want:
            p.append(f"cohort {name} 的摘要（指標／分母）和逐題重算不同")
    if "all" not in (s.get("cohorts") or {}):
        p.append("摘要缺 cohort all")
    return p


def photo_run_problems(report: dict, root: Path, *, name: str | None = None) -> list[str]:
    """新版圖紙照片 run（drawing-retrieval/2）：真模型、收據鏈，candidates 排名可由逐張資料重算。"""
    p = []
    if report.get("schema_version") != dre.SCHEMA_VERSION:
        p.append(f"schema_version 不對：{report.get('schema_version')!r}")
    if name and not name.startswith(f"{report.get('run_id')}-"):
        p.append(f"run_id {report.get('run_id')!r} 和檔名 {name} 不符")
    if report.get("embed_mode") != "real" or report.get("real") is not True:
        p.append(
            f"不是真模型 run：embed_mode={report.get('embed_mode')!r}，real={report.get('real')!r}"
        )
    info = report.get("index_receipt") or {}
    if info.get("problems"):
        p.append(f"run 的索引收據檢查有問題：{info['problems']}")
    p += _common_provenance(
        report.get("code"),
        report.get("isolation"),
        report.get("settings"),
        info.get("current"),
        None,
        root,
        info.get("build_receipt"),
    )
    if not _sha_ok((report.get("gold") or {}).get("sha256")):
        p.append("缺 gold.json hash")
    if not report.get("identify_params") or not report.get("candidate_ranking_definition"):
        p.append("缺 identify 參數或候選排名定義")
    if "stop" not in report:
        p.append("缺 stop 結果")
    accounts = report.get("accounts") or {}
    p += [f"缺帳號 {a}" for a in REQUIRED_ACCOUNTS if a not in accounts]
    for acc, result in accounts.items():
        p += [f"{acc}：{x}" for x in _photo_account_problems(result)]
    return p


def _photo_account_problems(result: dict) -> list[str]:
    p, scored = [], []
    photos = result.get("photos") or []
    if not photos:
        return ["沒有照片逐張資料"]
    for r in photos:
        ranking = rm.score_photo_case(
            r.get("candidates") or [], r.get("expect"), route_miss=r.get("domain") != "mfg"
        )
        want = {
            k: ranking[k]
            for k in ("in_ranking", "route_miss", "empty_candidates", "rank", "metrics")
        }
        if r.get("candidate_ranking") != want:
            p.append(f"{r.get('name')}：candidate_ranking 和 candidates／expect 重算結果不同")
        scored.append(ranking)
    stored = (result.get("summary") or {}).get("candidate_ranking") or {}
    want_all = rm.aggregate_photo_cases(scored)
    if {k: stored.get(k) for k in want_all} != want_all:
        p.append("summary.candidate_ranking（指標／分母）和逐張重算不同")
    return p


def load_artifacts(runs: Path, root: Path) -> tuple[dict, list[str], set]:
    """掃 eval/runs 的新產物。回傳 (通過驗證的 {相對路徑: {kind, report}}, 問題, 出現過的 kind)。
    舊版（沒有 schema_version）的 *-drawing-retrieval.json 是 legacy，不在這裡驗。"""
    accepted, problems, seen = {}, [], set()
    jobs = [
        ("index", "*-retrieval-index.json", lambda r, n: receipt_problems(r)),
        ("text", "*-retrieval-layer-metrics.json", lambda r, n: text_run_problems(r, root, name=n)),
        ("photo", "*-drawing-retrieval.json", lambda r, n: photo_run_problems(r, root, name=n)),
    ]
    for kind, pattern, check in jobs:
        for f in sorted(runs.glob(pattern)):
            rel = f.relative_to(root).as_posix()
            try:
                report = json.loads(f.read_text(encoding="utf-8"))
            except ValueError as e:
                seen.add(kind)
                problems.append(f"{rel}：不是 JSON：{e}")
                continue
            if kind == "photo" and "schema_version" not in report:
                continue  # legacy 舊版
            seen.add(kind)
            found = check(report, f.name)
            if found:
                problems += [f"{rel}：{x}" for x in found]
            else:
                accepted[rel] = {"kind": kind, "report": report}
    return accepted, problems, seen


# ---------------------------------------------------------------- README 總表


def parse_table(text: str) -> list[dict]:
    """README 的分層檢索指標總表（TABLE_START／TABLE_END 之間）→ 每列一個 dict。"""
    if TABLE_START not in text or TABLE_END not in text:
        raise ValueError("README 沒有分層檢索指標總表的標記")
    body = text.split(TABLE_START, 1)[1].split(TABLE_END, 1)[0]
    rows = []
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("|") or set(line) <= set("|-: "):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if cells[0] == "ID":
            continue
        if len(cells) != len(COLUMNS):
            raise ValueError(f"總表欄數不是 {len(COLUMNS)}：{line[:60]}")
        row = dict(zip(COLUMNS, cells, strict=True))
        for key in ("den_path", "source", "path"):
            m = re.fullmatch(r"`([^`]+)`", row[key])
            row[key] = m.group(1) if m else (None if row[key] == "—" else row[key])
        rows.append(row)
    if not rows:
        raise ValueError("總表沒有任何列")
    return rows


def _entries(cell: str) -> list[str]:
    return [x.strip() for x in cell.split(SEP)]


def _row_values(row: dict, root: Path, data) -> tuple[list, list | None]:
    """依來源欄位取出 JSON／CSV 的值與分母（分母沒有欄位時回 None）。"""
    path, src = row["path"], row["source"]
    if path.startswith("csv:"):
        counts = csv_counts(root / src)
        values = [counts[k] for k in path[4:].split(SEP)]
        den = None
        if row["den_path"] and row["den_path"].startswith("csv:"):
            den = [counts[row["den_path"][4:]]]
        return values, den
    values = [resolve(data, x) for x in expand(path)]
    dp = row["den_path"]
    if dp is None:
        return values, None
    if dp == "字串分母":
        return values, [int(v.split("/")[1]) if isinstance(v, str) else None for v in values]
    return values, [resolve(data, x) for x in expand(dp)]


def row_problems(row: dict, root: Path, accepted: dict) -> list[str]:
    rid, status, src = row["id"], row["status"], row["source"]
    if status not in STATUSES:
        return [f"{rid}：狀態 {status!r} 不是 {STATUSES} 之一"]
    if status == PENDING:
        p = []
        if row["number"] != PENDING or row["denominator"] != "—":
            p.append(
                f"{rid}：待代跑列的數字／分母只能是「{PENDING}」／「—」，不能估數字：{row['number']!r}"
            )
        if not src or "<run_id>" not in src or not row["path"]:
            p.append(f"{rid}：待代跑列要寫將來的來源檔（含 <run_id>）與欄位")
        return p
    if not src or "<" in src:
        return [f"{rid}：缺來源檔"]
    file = root / src
    if not file.is_file():
        return [f"{rid}：來源檔不存在：{src}"]
    if not row["path"]:
        return [f"{rid}：缺來源欄位"]
    if status == REAL:
        if src not in accepted:
            return [
                f"{rid}：{src} 不是通過 provenance 驗證的真模型 run（mock、缺收據或不在 eval/runs）"
            ]
        data = accepted[src]["report"]
    else:
        if src in accepted:
            return [f"{rid}：{status} 不能指向新 run {src}"]
        data = None if src.endswith(".csv") else json.loads(file.read_text(encoding="utf-8"))
        if (status == DERIVED) != row["path"].startswith("csv:"):
            return [f"{rid}：{DERIVED} 只用在 csv: 重算列，其他 legacy 列用 {LEGACY}"]
    try:
        values, dens = _row_values(row, root, data)
    except (KeyError, IndexError, ValueError, OSError) as e:
        return [f"{rid}：來源欄位 {row['path']} 讀不到：{e!r}"]
    p = []
    nums = _entries(row["number"])
    if len(nums) != len(values) or not all(
        same_number(n, v) for n, v in zip(nums, values, strict=False)
    ):
        p.append(f"{rid}：README 數字 {row['number']} ≠ {src} 的 {row['path']} = {values}")
    cells = _entries(row["denominator"])
    if dens is None:
        if cells != ["—"] or status == REAL:
            p.append(f"{rid}：缺分母欄位")
    else:
        dens = dens * len(cells) if len(dens) == 1 else dens
        if len(cells) == 1:
            cells = cells * len(dens)
        ok = len(cells) == len(dens) and all(
            c == "—" if d is None else same_number(c, d) for c, d in zip(cells, dens, strict=False)
        )
        if not ok:
            p.append(f"{rid}：README 分母 {row['denominator']} ≠ {row['den_path']} = {dens}")
    return p


def readme_problems(text: str, root: Path, accepted: dict) -> list[str]:
    """README 總表整體：每列追得到來源；legacy 檔都有被引用；有真 run 就不能還寫待代跑。"""
    try:
        rows = parse_table(text)
    except ValueError as e:
        return [str(e)]
    p = []
    for row in rows:
        p += row_problems(row, root, accepted)
    cited = {r["source"] for r in rows}
    p += [f"總表沒有引用 {s}" for s in LEGACY_SOURCES if s not in cited]
    kinds = {v["kind"] for v in accepted.values()}
    for row in rows:
        if row["status"] != PENDING or not row["source"]:
            continue
        need = "text" if "retrieval-layer-metrics" in row["source"] else "photo"
        if need in kinds:
            p.append(f"{row['id']}：已有通過驗證的 {need} 真 run，README 不能還寫「{PENDING}」")
    return p


REQUIRED_TEXT = (
    "分層檢索指標總表",
    PENDING,
    LEGACY,
    DERIVED,
    "planner",
    "sales_a",
    "10/10",
    "Text-to-SQL",
    "不適用",
    "citation_ok",
    "生成層",
    "Context Precision",
    "Context Recall",
    "LLM",
    "HYBRID_BASE_URL",
    "11434",
    "Chinese-CLIP",
    "bge-m3",
    "collection_global_diagnostic_postfilter",
    "--receipt",
    "--index-receipt",
    "make eval-retrieval-index",
    "make eval-retrieval",
    "make eval-drawing-retrieval",
)


# ---------------------------------------------------------------- 自建假產物


def fake_receipt(**over) -> dict:
    def coll(n, tag):
        return {
            "owner_key": "x",
            "item_ids": [f"{tag}-1"],
            "item_count": 1,
            "chunk_ids": [],
            "chunk_count": n,
            "chunks_by_owner": {},
            "chunks_sha256": f"chunks-{tag}-aaaa",
            "chunk_vecs": {"sha256": f"vecs-{tag}-aaaa"},
        }

    params = {"top_k_chunks": 5, "min_chunk_score": 0.3, "relative_chunk_ratio": 0.6}
    r = rr.assemble_index_receipt(
        manifest={
            "embed_mode": "real",
            "chunk_count": 21,
            "part_chunk_count": 34,
            "kb_version": "v1",
            "kb_hash": "kbhash-aaaa",
        },
        manifest_problems=[],
        collections={"art": coll(21, "art"), "mfg": coll(34, "mfg")},
        settings={"embed_mode": "real", "index_backend": "file"},
        models={
            "text": {"name": "BAAI/bge-m3", "revision": "rev1", "dim": 1024, "dtype": "float32"}
        },
        retrieval_params=params,
        models_yaml_sha256="models-yaml-aaaa",
        kb={
            "version": "v1",
            "hash_now": "kbhash-aaaa",
            "version_manifest": "v1",
            "hash_manifest": "kbhash-aaaa",
        },
        qa={"eval/qa.jsonl": "qa-aaaa-1", "eval/drawing_retrieval/qa.jsonl": "qa-aaaa-2"},
        code={"head": "a" * 40, "dirty": False, "dirty_paths": [], "error": None},
        isolation={"env_file_disabled": True, "strict": True},
        index_build={"invoked": True, "exit_code": 0},
        created_at="2026-10-28T00:00:00Z",
    )
    r["run_id"] = "20261028T000000-idx1"
    return r | over


def _score_row(case_id, gold, returned, owner="x"):
    empty = None if gold else "no_answer"
    return {
        "case_id": case_id,
        "status": "answerable" if gold else "expected_empty",
        "empty_kind": empty,
        "gold_chunk_ids": gold,
        "returned": [{"chunk_id": c, "owner_id": owner} for c in returned],
        "score": rm.score_text_case(returned, gold, empty_kind=empty),
        "error": None,
        "cohorts": ["all"],
        "diagnostic": {"note": "diagnostic"},
    }


def _group(domain, mode, call_kind, account=None):
    spec = rre.group_spec(domain, mode, call_kind, account)
    rows = [
        _score_row("q1", ["d#01-0"], ["d#02-0", "d#01-0"]),
        _score_row("q2", ["d#03-0"], ["d#03-0"]),
        _score_row("q3", [], ["d#04-0"]),
    ]
    scores = [r["score"] for r in rows]
    summary = {
        "n_cases": 3,
        "n_scored": 3,
        "n_errors": 0,
        "cohorts": {"all": {"case_ids": ["q1", "q2", "q3"], **rm.aggregate_text_cases(scores)}},
    }
    return {**spec, "rows": rows, "summary": summary}


def write_fake_run_set(root: Path) -> dict:
    """在 root/eval/runs 寫一組自洽的假 real 產物（收據、文字 run、照片 run），回傳路徑與內容。"""
    runs = root / "eval" / "runs"
    runs.mkdir(parents=True)
    receipt = fake_receipt()
    rpath = runs / f"{receipt['run_id']}-retrieval-index.json"
    rpath.write_text(json.dumps(receipt, ensure_ascii=False), encoding="utf-8")
    info = {
        "path": rpath.relative_to(root).as_posix(),
        "sha256": rr.sha256_file(rpath),
        "run_id": receipt["run_id"],
        "real": True,
        "problems": [],
    }
    groups = [_group("art", rre.PRODUCTION, "specified"), _group("art", rre.PRODUCTION, "global")]
    for a in REQUIRED_ACCOUNTS:
        groups.append(_group("mfg", rre.PRODUCTION, "specified", a))
        groups.append(_group("mfg", rre.DIAGNOSTIC, "global", a))
    text = {
        "schema_version": rre.SCHEMA_VERSION,
        "kind": "retrieval-layer-metrics",
        "run_id": "20261028T010000-txt1",
        "created_at": "2026-10-28T01:00:00Z",
        "finished_at": "2026-10-28T01:05:00Z",
        "status": "ok",
        "exit_code": 0,
        "embed_mode": "real",
        "real": True,
        "official": True,
        "allow_mock": False,
        "problems": [],
        "k": 5,
        "provenance": {
            "code": receipt["code"],
            "isolation": receipt["isolation"],
            "settings": receipt["settings"],
            "inputs": {"a": "hash-aaaa-1", "b": "hash-aaaa-2", "c": "hash-aaaa-3"},
            "retrieval_params": receipt["config"]["retrieval_params"],
            "index_now": copy.deepcopy(receipt),
            "index_receipt": info,
        },
        "corpus": {
            d: {"item_ids": c["item_ids"], "chunk_count": c["chunk_count"]}
            for d, c in receipt["collections"].items()
        },
        "gold": {d: {"case_ids": ["q1", "q2", "q3"]} for d in rr.DOMAINS},
        "accounts": dict.fromkeys(REQUIRED_ACCOUNTS, {}),
        "groups": groups,
        "summary": {g["group_id"]: g["summary"] for g in groups},
    }
    tpath = runs / f"{text['run_id']}-retrieval-layer-metrics.json"
    tpath.write_text(json.dumps(text, ensure_ascii=False), encoding="utf-8")
    photo = _fake_photo_run(receipt, info)
    ppath = runs / f"{photo['run_id']}-drawing-retrieval.json"
    ppath.write_text(json.dumps(photo, ensure_ascii=False), encoding="utf-8")
    return {"root": root, "runs": runs, "receipt": rpath, "text": tpath, "photo": ppath}


def _fake_photo_run(receipt: dict, info: dict) -> dict:
    def result():
        photos = []
        for name, expect, cand in (
            ("a.png", "mfg-001", ["mfg-001", "mfg-002"]),
            ("b.png", "mfg-002", ["mfg-001", "mfg-002", "mfg-003"]),
            ("c.png", "mfg-003", []),
        ):
            rk = rm.score_photo_case(cand, expect)
            photos.append(
                {
                    "name": name,
                    "expect": expect,
                    "domain": "mfg",
                    "candidates": cand,
                    "candidate_ranking": {
                        k: rk[k]
                        for k in ("in_ranking", "route_miss", "empty_candidates", "rank", "metrics")
                    },
                }
            )
        scored = [rm.score_photo_case(p["candidates"], p["expect"]) for p in photos]
        summary = {"candidate_ranking": {"source": "s", **rm.aggregate_photo_cases(scored)}}
        return {"summary": summary, "photos": photos, "texts": [], "stop_reasons": []}

    return {
        "run_id": "20261028T020000-pho1",
        "schema_version": dre.SCHEMA_VERSION,
        "embed_mode": "real",
        "real": True,
        "code": receipt["code"],
        "isolation": receipt["isolation"],
        "settings": receipt["settings"],
        "index_receipt": {
            "build_receipt": info,
            "current": copy.deepcopy(receipt),
            "problems": [],
        },
        "gold": {"path": "eval/drawing_retrieval/gold.json", "sha256": "gold-aaaa-1"},
        "identify_params": {"top_k_search": 5},
        "candidate_ranking_definition": {"source": "s"},
        "stop": {"exit_code": 0, "failed_accounts": []},
        "accounts": {a: result() for a in REQUIRED_ACCOUNTS},
    }


def _rewrite(path: Path, mutate) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    mutate(data)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _rehash_receipt(fake: dict, mutate) -> None:
    """改收據內容後，同步把 run 裡記的 sha256 更新（模擬『收據本身是假的』而不是『被竄改』）。"""
    _rewrite(fake["receipt"], mutate)
    digest = rr.sha256_file(fake["receipt"])
    _rewrite(fake["text"], lambda d: d["provenance"]["index_receipt"].update(sha256=digest))
    _rewrite(fake["photo"], lambda d: d["index_receipt"]["build_receipt"].update(sha256=digest))


@pytest.fixture
def fake(tmp_path):
    return write_fake_run_set(tmp_path)


def _problems(fake: dict) -> dict:
    accepted, problems, seen = load_artifacts(fake["runs"], fake["root"])
    return {"accepted": accepted, "problems": problems, "seen": seen}


# ---------------------------------------------------------------- 驗證器本身（假產物）


def test_validators_accept_a_consistent_real_artifact_set(fake):
    out = _problems(fake)
    assert out["problems"] == []
    assert out["seen"] == {"index", "text", "photo"}
    assert {v["kind"] for v in out["accepted"].values()} == {"index", "text", "photo"}


TEXT_MUTATIONS = {
    "mock embed_mode": (lambda d: d.update(embed_mode="mock", real=False), "不是真模型"),
    "real 旗標被拿掉": (lambda d: d.update(real=False), "不是真模型"),
    "allow_mock": (lambda d: d.update(allow_mock=True), "mock 測試產物"),
    "status 失敗": (lambda d: d.update(status="failed", exit_code=1), "沒有成功完成"),
    "official 不是 true": (lambda d: d.update(official=False), "沒有成功完成"),
    "有問題清單": (lambda d: d.update(problems=["索引不能用"]), "問題清單"),
    "沒有 HEAD": (lambda d: d["provenance"]["code"].update(head=None), "HEAD"),
    "env_file 沒關": (
        lambda d: d["provenance"]["isolation"].update(env_file_disabled=False),
        "env_file",
    ),
    "題庫 hash 缺": (lambda d: d["provenance"]["inputs"].update(a=None), "hash"),
    "缺收據": (lambda d: d["provenance"].update(index_receipt={}), "沒有索引收據路徑"),
    "收據檔不存在": (
        lambda d: d["provenance"]["index_receipt"].update(
            path="eval/runs/none-retrieval-index.json"
        ),
        "索引收據不存在",
    ),
    "收據 sha256 不符": (
        lambda d: d["provenance"]["index_receipt"].update(sha256="0" * 64),
        "sha256",
    ),
    "缺 index_now": (lambda d: d["provenance"].update(index_now=None), "沒有執行當下"),
    "run_id 與檔名不符": (lambda d: d.update(run_id="other"), "和檔名"),
    "缺帳號": (lambda d: d["accounts"].pop("sales"), "缺帳號"),
    "少一組": (lambda d: d["groups"].pop(), "groups 和預期不同"),
    "逐題執行錯誤": (
        lambda d: d["groups"][0]["rows"][0].update(error={"type": "X", "message": "m"}),
        "執行錯誤",
    ),
    "改名次（回傳順序）": (
        lambda d: d["groups"][0]["rows"][0].update(
            returned=[{"chunk_id": "d#01-0"}, {"chunk_id": "d#02-0"}]
        ),
        "逐題指標和回傳",
    ),
    "改分母": (
        lambda d: d["groups"][0]["summary"]["cohorts"]["all"]["answerable"]["metrics"][
            "mrr"
        ].update(denominator=99),
        "和逐題重算不同",
    ),
    "改平均": (
        lambda d: d["groups"][0]["summary"]["cohorts"]["all"]["answerable"]["metrics"][
            "recall@1"
        ].update(mean=1.0),
        "和逐題重算不同",
    ),
    "summary 和 group 不一致": (
        lambda d: d["summary"][d["groups"][0]["group_id"]].update(n_cases=7),
        "summary 和各 group",
    ),
    "診斷被標 production": (
        lambda d: d["groups"][3].update(production=True),
        "診斷被標成 production",
    ),
}


@pytest.mark.parametrize("name", TEXT_MUTATIONS)
def test_text_run_validator_rejects(fake, name):
    mutate, expect = TEXT_MUTATIONS[name]
    _rewrite(fake["text"], mutate)
    out = _problems(fake)
    text_key = fake["text"].relative_to(fake["root"]).as_posix()
    assert text_key not in out["accepted"], "不合格的 run 不能被接受"
    assert any(expect in p for p in out["problems"]), (expect, out["problems"])


RECEIPT_MUTATIONS = {
    "mock 收據": (lambda r: r.update(real=False, embed_mode="mock"), "不是真模型"),
    "沒有重建紀錄": (lambda r: r.update(index_build={"invoked": False}), "沒有 real 建索引紀錄"),
    "建索引失敗": (lambda r: r["index_build"].update(exit_code=1), "建索引失敗"),
    "env_file 沒關": (lambda r: r["isolation"].update(env_file_disabled=False), "env_file"),
    "KB hash 不符": (lambda r: r["kb"].update(hash_now="changed-aaaa", match=False), "KB"),
    "缺模型 revision": (lambda r: r["models"]["text"].update(revision=None), "revision"),
    "缺向量 digest": (lambda r: r["collections"]["art"].update(chunk_vecs={}), "digest"),
    "缺題庫 hash": (lambda r: r["qa"].update({"eval/qa.jsonl": None}), "題庫"),
    "manifest 被改": (lambda r: r["manifest"].update(chunk_count=999), "manifest"),
}


@pytest.mark.parametrize("name", RECEIPT_MUTATIONS)
def test_receipt_validator_rejects_and_blocks_runs_using_it(fake, name):
    mutate, expect = RECEIPT_MUTATIONS[name]
    _rehash_receipt(fake, mutate)
    out = _problems(fake)
    assert any(expect in p for p in out["problems"]), (expect, out["problems"])
    # 用到這份收據的文字與照片 run 一起被擋下
    assert out["accepted"] == {}, "收據不合格時，引用它的 run 也不能被接受"


def test_receipt_mismatch_with_current_index_is_rejected(fake):
    _rewrite(
        fake["text"],
        lambda d: d["provenance"]["index_now"]["collections"]["mfg"]["chunk_vecs"].update(
            sha256="z"
        ),
    )
    out = _problems(fake)
    assert any("索引收據對不上目前索引" in p for p in out["problems"])


PHOTO_MUTATIONS = {
    "mock": (lambda d: d.update(embed_mode="mock", real=False), "不是真模型"),
    "舊版沒有 schema 以外的收據": (
        lambda d: d["index_receipt"].update(build_receipt=None),
        "沒有索引收據路徑",
    ),
    "收據問題未清": (lambda d: d["index_receipt"].update(problems=["x"]), "索引收據檢查有問題"),
    "缺帳號": (lambda d: d["accounts"].pop("manager"), "缺帳號"),
    "改 candidates 名次": (
        lambda d: d["accounts"]["manager"]["photos"][1].update(candidates=["mfg-002", "mfg-001"]),
        "candidate_ranking 和 candidates",
    ),
    "改分母": (
        lambda d: d["accounts"]["sales"]["summary"]["candidate_ranking"]["metrics"]["mrr"].update(
            denominator=1
        ),
        "和逐張重算不同",
    ),
    "缺 stop": (lambda d: d.pop("stop"), "stop"),
    "缺 gold hash": (lambda d: d["gold"].update(sha256=None), "gold"),
}


@pytest.mark.parametrize("name", PHOTO_MUTATIONS)
def test_photo_run_validator_rejects(fake, name):
    mutate, expect = PHOTO_MUTATIONS[name]
    _rewrite(fake["photo"], mutate)
    out = _problems(fake)
    assert fake["photo"].relative_to(fake["root"]).as_posix() not in out["accepted"]
    assert any(expect in p for p in out["problems"]), (expect, out["problems"])


def test_unparseable_artifact_is_a_problem_not_silently_ignored(fake):
    fake["text"].write_text("{not json", encoding="utf-8")
    out = _problems(fake)
    assert any("不是 JSON" in p for p in out["problems"])


def test_legacy_drawing_run_without_schema_is_not_treated_as_new(tmp_path):
    runs = tmp_path / "eval" / "runs"
    runs.mkdir(parents=True)
    (runs / "20261010T140539-f694-drawing-retrieval.json").write_text(
        json.dumps({"run_id": "f694", "accounts": {}}), encoding="utf-8"
    )
    accepted, problems, seen = load_artifacts(runs, tmp_path)
    assert (accepted, problems, seen) == ({}, [], set())


# ------------------------------------------------------------ README 驗證器（假 README＋假 run）


def tr(*cells: str) -> str:
    """總表的一列（9 欄）。"""
    assert len(cells) == len(COLUMNS)
    return "| " + " | ".join(cells) + " |\n"


def real_rows(fake: dict) -> str:
    text = fake["text"].relative_to(fake["root"]).as_posix()
    photo = fake["photo"].relative_to(fake["root"]).as_posix()
    base = "summary.art/production_retrieve/specified.cohorts.all.answerable.metrics"
    rank = "accounts.manager.summary.candidate_ranking.metrics.recall@1"
    return tr(
        "T-1", "文字", "art 指定 R@1／MRR", "0.5／0.75", "2",
        f"`{base}.{{recall@1／mrr}}.denominator`", f"`{text}`",
        f"`{base}.{{recall@1／mrr}}.mean`", REAL,
    ) + tr(
        "P-1", "照片", "manager R@1", "0.3333", "3",
        f"`{rank}.denominator`", f"`{photo}`", f"`{rank}.mean`", REAL,
    )  # fmt: skip


def make_readme(rows: str) -> str:
    head = tr("ID", "層", "指標", "數字", "分母", "分母欄位", "來源檔", "來源欄位", "狀態")
    sep = "|" + "---|" * len(COLUMNS) + "\n"
    return f"{TABLE_START}\n{head}{sep}{rows}{TABLE_END}\n"


def legacy_rows(root: Path) -> str:
    """假 root 裡放四個 legacy 檔，回傳引用它們的列。"""
    runs = root / "eval" / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    met = {"recall": {"c": {"3": {"@1": 0.99, "@3": 1.0}}}, "current": {"c": {"n": 160}}}
    f694 = {"accounts": {"planner": {"summary": {"photo_top1": "56/94", "photo_wrong": 0}}}}
    csv_text = "photo,truth,top1,top5\na,x,True,True\nb,y,True,True\nc,(未收錄),False,False\n"
    (runs / "20261004T165452-a7ba-met-photos.json").write_text(
        json.dumps({"summary": met}), encoding="utf-8"
    )
    (runs / "20260929T075014-aeab.json").write_text(
        json.dumps({"summary": {"image_top1": 1.0, "n_known": 2}}), encoding="utf-8"
    )
    (runs / "20260929T075014-aeab-images.csv").write_text(csv_text, encoding="utf-8")
    (runs / "20261010T140539-f694-drawing-retrieval.json").write_text(
        json.dumps(f694), encoding="utf-8"
    )
    s = "eval/runs/"
    return (
        tr(
            "L-1", "辨識", "CLIP R@1／R@3", "0.99／1.0", "160", "`summary.current.c.n`",
            f"`{s}20261004T165452-a7ba-met-photos.json`", "`summary.recall.c.3.{@1／@3}`", LEGACY,
        )
        + tr(
            "L-2", "辨識", "Top-1", "1.0", "2", "`summary.n_known`",
            f"`{s}20260929T075014-aeab.json`", "`summary.image_top1`", LEGACY,
        )
        + tr(
            "L-3", "辨識", "由 CSV 重算 Top-1／Top-5", "2／2", "2", "`csv:n_known`",
            f"`{s}20260929T075014-aeab-images.csv`", "`csv:top1／top5`", DERIVED,
        )
        + tr(
            "L-4", "辨識", "planner top1／wrong", "56/94／0", "94／—", "`字串分母`",
            f"`{s}20261010T140539-f694-drawing-retrieval.json`",
            "`accounts.planner.summary.{photo_top1／photo_wrong}`", LEGACY,
        )
    )  # fmt: skip


def pending_row(rid: str = "N-1", number: str = PENDING, den: str = "—", src: str | None = None):
    src = src or "`eval/runs/<run_id>-retrieval-layer-metrics.json`"
    path = "`summary.<group_id>.cohorts.all`"
    return tr(rid, "文字", "art 指定", number, den, "—", src, path, PENDING)


def pending_rows() -> str:
    photo = "`eval/runs/<run_id>-drawing-retrieval.json`"
    return pending_row() + tr(
        "N-2", "照片", "manager", PENDING, "—", "—", photo,
        "`accounts.manager.summary.candidate_ranking`", PENDING,
    )  # fmt: skip


def _readme_check(root: Path, rows: str) -> list[str]:
    accepted, _, _ = load_artifacts(root / "eval" / "runs", root)
    return readme_problems(make_readme(rows), root, accepted)


def test_readme_validator_accepts_legacy_and_pending_rows(tmp_path):
    rows = legacy_rows(tmp_path) + pending_rows()
    assert _readme_check(tmp_path, rows) == []


def test_readme_validator_accepts_real_rows_traced_to_validated_runs(fake):
    rows = legacy_rows(fake["root"]) + real_rows(fake)
    assert _readme_check(fake["root"], rows) == []


@pytest.mark.parametrize(
    ("old", "new", "expect"),
    [
        ("0.99／1.0", "0.98／1.0", "README 數字"),
        ("| 160 |", "| 161 |", "README 分母"),
        ("| 1.0 |", "| 0.9 |", "README 數字"),
        ("2／2", "2／1", "README 數字"),
        ("56/94／0", "57/94／0", "README 數字"),
        ("94／—", "93／—", "README 分母"),
        ("`summary.n_known`", "`summary.nope`", "讀不到"),
        (
            f"`eval/runs/20260929T075014-aeab.json` | `summary.image_top1` | {LEGACY}",
            f"`eval/runs/missing.json` | `summary.image_top1` | {LEGACY}",
            "來源檔不存在",
        ),
        (
            f"`eval/runs/20260929T075014-aeab.json` | `summary.image_top1` | {LEGACY}",
            f"— | `summary.image_top1` | {LEGACY}",
            "缺來源檔",
        ),
        (f"`summary.image_top1` | {LEGACY}", f"— | {LEGACY}", "缺來源欄位"),
        (f"`csv:top1／top5` | {DERIVED}", f"`csv:top1／top5` | {LEGACY}", DERIVED),
        (f"`summary.image_top1` | {LEGACY}", "`summary.image_top1` | estimated", "狀態"),
    ],
)
def test_readme_validator_rejects_bad_legacy_rows(tmp_path, old, new, expect):
    rows = legacy_rows(tmp_path)
    assert old in rows, old
    problems = _readme_check(tmp_path, rows.replace(old, new, 1))
    assert any(expect in p for p in problems), (expect, problems)


def test_readme_validator_requires_all_legacy_sources_to_be_cited(tmp_path):
    rows = legacy_rows(tmp_path)
    kept = "".join(line + "\n" for line in rows.splitlines() if "aeab-images.csv" not in line)
    assert any("aeab-images.csv" in p for p in _readme_check(tmp_path, kept))


@pytest.mark.parametrize(
    ("old", "new", "expect"),
    [
        ("0.5／0.75", "0.6／0.75", "README 數字"),
        ("| 2 |", "| 3 |", "README 分母"),
        ("0.3333", "0.5", "README 數字"),
    ],
)
def test_readme_validator_rejects_real_rows_that_do_not_match_the_json(fake, old, new, expect):
    rows = legacy_rows(fake["root"]) + real_rows(fake)
    assert old in rows
    problems = _readme_check(fake["root"], rows.replace(old, new, 1))
    assert any(expect in p for p in problems), problems


def test_readme_validator_rejects_real_row_pointing_to_mock_run(fake):
    _rewrite(fake["text"], lambda d: d.update(embed_mode="mock", real=False))
    rows = legacy_rows(fake["root"]) + real_rows(fake)
    problems = _readme_check(fake["root"], rows)
    assert any("不是通過 provenance 驗證的真模型 run" in p for p in problems)


def test_readme_validator_rejects_real_row_without_any_artifact(tmp_path):
    runs = tmp_path / "eval" / "runs"
    src = "`eval/runs/20261030T000000-x-retrieval-layer-metrics.json`"
    rows = legacy_rows(tmp_path) + tr(
        "T-9", "文字", "art", "0.9", "5", "`a.b`", src, "`summary.x`", REAL
    )
    assert runs.is_dir()
    problems = _readme_check(tmp_path, rows)
    assert any("來源檔不存在" in p for p in problems)


@pytest.mark.parametrize(
    "cells",
    [
        ("0.9", "—"),  # 估計值
        (f"{PENDING} 約 0.9", "—"),
        (PENDING, "5"),
    ],
)
def test_readme_validator_rejects_numbers_in_pending_rows(tmp_path, cells):
    rows = legacy_rows(tmp_path) + pending_row(number=cells[0], den=cells[1])
    assert any("不能估數字" in p for p in _readme_check(tmp_path, rows))


def test_readme_validator_rejects_pending_marker_with_known_run(fake):
    rows = legacy_rows(fake["root"]) + pending_rows()
    problems = _readme_check(fake["root"], rows)
    assert sum("README 不能還寫" in p for p in problems) == 2


def test_readme_validator_rejects_pending_row_without_planned_source(tmp_path):
    rows = legacy_rows(tmp_path) + f"| N-1 | 文字 | art | {PENDING} | — | — | — | — | {PENDING} |\n"
    assert any("將來的來源檔" in p for p in _readme_check(tmp_path, rows))


def test_readme_without_table_is_rejected(tmp_path):
    assert any("沒有分層檢索指標總表" in p for p in readme_problems("# README", tmp_path, {}))


# ---------------------------------------------------------------- 真 README 與真產物


@pytest.fixture(scope="module")
def artifacts():
    accepted, problems, seen = load_artifacts(RUNS, ROOT)
    return {"accepted": accepted, "problems": problems, "seen": seen}


def readme_text() -> str:
    return README.read_text(encoding="utf-8")


def test_readme_has_layered_table_and_required_statements():
    text = readme_text()
    missing = [s for s in REQUIRED_TEXT if s not in text]
    assert missing == [], f"README 缺少：{missing}"
    rows = parse_table(text)
    assert len({r["id"] for r in rows}) == len(rows), "總表 ID 重複"
    by_status = {s: [r for r in rows if r["status"] == s] for s in STATUSES}
    assert by_status[LEGACY] and by_status[DERIVED], "legacy 列與重算列都要有"
    # 新增的文字／照片真模型數字：每個領域／模式／帳號與兩個帳號的照片都要有列（待代跑或真 run）
    new_rows = by_status[PENDING] + by_status[REAL]
    for key in (
        "art/production_retrieve/specified",
        "art/production_retrieve/global",
        "mfg/production_retrieve/specified/manager",
        "mfg/production_retrieve/specified/sales",
        f"mfg/{rre.DIAGNOSTIC}/global/manager",
        f"mfg/{rre.DIAGNOSTIC}/global/sales",
        "accounts.manager",
        "accounts.sales",
    ):
        assert any(key in (r["path"] or "") or key in r["metric"] for r in new_rows), key


def test_documented_legacy_numbers_trace_to_runs():
    """README 引用的 legacy 數字永遠追得到：來源檔、欄位、分母都和保存的檔案一致。"""
    rows = [r for r in parse_table(readme_text()) if r["status"] in (LEGACY, DERIVED)]
    assert rows
    problems = [p for r in rows for p in row_problems(r, ROOT, {})]
    assert problems == []


def test_pending_rows_carry_no_numbers():
    rows = [r for r in parse_table(readme_text()) if r["status"] == PENDING]
    assert [p for r in rows for p in row_problems(r, ROOT, {})] == []


def _wait(reason: str):
    pytest.xfail(f"等待人類證據：{reason}")


def test_real_run_provenance_and_recomputable_metrics(artifacts):
    """有真產物時嚴格驗證（任何一份不合格就失敗）；還沒有時 xfail（等待人類證據，AC-6 未完成）。"""
    assert artifacts["problems"] == [], "\n".join(artifacts["problems"])
    missing = [k for k in ("index", "text", "photo") if k not in artifacts["seen"]]
    if missing:
        files = [KIND_OF.get(m, "*-drawing-retrieval.json（drawing-retrieval/2）") for m in missing]
        _wait(f"人類尚未交回真模型產物：{', '.join(missing)}（{', '.join(files)}）")
    kinds = {v["kind"] for v in artifacts["accepted"].values()}
    assert kinds == {"index", "text", "photo"}


def test_documented_numbers_trace_to_runs(artifacts):
    """README 總表整體：legacy 永遠要對；新增數字要有通過驗證的真 run，否則維持待代跑。"""
    assert artifacts["problems"] == [], "\n".join(artifacts["problems"])
    problems = readme_problems(readme_text(), ROOT, artifacts["accepted"])
    assert problems == [], "\n".join(problems)
    kinds = {v["kind"] for v in artifacts["accepted"].values()}
    if not {"text", "photo"} <= kinds:
        _wait("README 新增的文字／照片檢索數字仍是「待人類代跑真模型」（AC-7 未完成）")
    rows = [r for r in parse_table(readme_text()) if r["status"] == REAL]
    assert rows, "已有真 run，README 總表卻沒有任何 real_run 列"
    cited = {r["source"] for r in rows}
    for rel, art in artifacts["accepted"].items():
        if art["kind"] in ("text", "photo"):
            assert rel in cited, f"README 總表沒有引用真 run {rel}"
