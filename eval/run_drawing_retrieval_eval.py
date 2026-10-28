"""圖紙文字與照片檢索評估（TASK-20261010-001；TASK-20261027-001 補候選排名指標與 provenance）：
在程序內直接執行，不用開後端；要先建好索引並有它的建索引收據
（make eval-retrieval-index：真的 Chinese-CLIP、bge-m3，見 eval/retrieval_runtime.py）。
不讀 .env：啟動時先 retrieval_runtime.bootstrap()（第一次讀設定之前關掉 env_file），
設定只從環境變數與 shared/*.yaml 取得。
結果存 <out-dir>/<run_id>-drawing-retrieval.json（預設 eval/runs；EMBED_MODE 不是 real 時
只能寫到 eval/runs 以外的暫存目錄）。

gold set：eval/drawing_retrieval/gold.json
1. 照片：回報的截圖、知識庫原圖、審查提出的方形小局部、三視圖各自的局部與視圖中央的方形小局部
   （知識庫與未收錄零件的圖紙，切出後放大 1.3 或 2 倍）、模擬實拍（局部、傾斜、昏暗、反光、模糊）、
   未收錄的圖紙。
   和 /search/any、智慧助理同一條路：領域路由 → identify_drawing
   （只在目前身分看得到的圖紙裡辨識）
2. 文字：名稱、料號、圖號、描述各一句，送 search_parts_text
   （/search/parts，只檢索看得到的圖紙）
每個身分各跑一次（預設主管＝看得到全部、業務＝看不到機密）。
看不到的圖紙，正確答案和「不存在」一樣是 null。

照片有兩種數字，分開記：
- 辨識（got）：identify_drawing 三道關都過才算認出，photo_top1 等原本的摘要
- 候選排名（candidate_ranking）：candidates＝identify_drawing 幾何驗證後的混合排名前 5
  （通過驗證的在前，再依 ORB inliers、CLIP 分數），包含沒通過驗證的候選；不是 CLIP 粗篩名單，
  也不等於辨識成功。Recall@1/2/3/5＝正解在前 K 名的照片比例，MRR＝1／正解名次。
  分母是看得到正解的照片：領域路由不是 mfg（route miss）、候選空或沒排進去都留在分母記 0；
  未收錄（unknown）與看不到（hidden）不進排名分母，另外拆開記拒答、誤接受與外洩。
外洩看整份回傳列表（照片的全部 candidates、文字的全部結果），不只第一名。

停止條件（任一成立就以非 0 結束，JSON 照寫）：
- 指錯圖紙（辨識成功但不是答案，或看不到／不存在的圖紙被辨識成其他圖紙）
- 看不到的圖紙出現在任何結果裡
- 回報的截圖（有權限身分）沒有認出 mfg-006；沒有權限的身分認出任何圖紙；gold 裡沒有回報的截圖
- 文字查詢第一名不是答案（看得到時）
低品質照片（例如模糊）辨識不到算「沒認出」，不算指錯；比例照實記錄。

結束碼：0＝通過；1＝停止條件成立（已寫 JSON）；2＝沒有產出成績（設定隔離失敗、索引或收據
和目前設定不符、mock 要寫進 eval/runs）。

用法：python eval/run_drawing_retrieval_eval.py [--accounts manager,sales]
      [--index-receipt latest|PATH] [--gold PATH] [--out-dir DIR] [--run-id ID]
"""

import argparse
import functools
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in (ROOT / "backend", ROOT / "eval"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import retrieval_metrics as rm  # noqa: E402
import retrieval_runtime as rr  # noqa: E402
from PIL import Image  # noqa: E402

print = functools.partial(print, flush=True)  # noqa: A001 — 背景執行時也能即時看到進度
GOLD = ROOT / "eval" / "drawing_retrieval" / "gold.json"
SCHEMA_VERSION = "drawing-retrieval/2"
CANDIDATE_SOURCE = (
    "identify_drawing 幾何驗證後的混合排名（含沒通過驗證的候選；不是 CLIP 粗篩、不等於 got）"
)
CANDIDATE_RANKING = {
    "source": "search_service.identify_drawing(...)['results']",
    "description": (
        "幾何驗證後的混合排名前 top_k_search 名：通過驗證的在前，再依 ORB inliers、CLIP 分數排序，"
        "包含沒通過驗證的候選；不是 CLIP 粗篩名單，也不等於辨識成功（got／matched）"
    ),
    "ks": list(rm.PHOTO_KS),
    "metrics": "Recall@K＝正解在前 K 名（每張照片至多一個正解）；MRR＝1／正解名次，沒排進去 0",
    "denominator": (
        "看得到正解的照片；route 不是 mfg、候選空或沒排進去都留在分母記 0；"
        "未收錄（unknown）與看不到（hidden）不進排名分母，另計拒答、誤接受與外洩"
    ),
}
STOP_RULES = {
    "photo_wrong": "指錯圖紙：辨識成功但不是答案，或看不到／不存在的圖紙被辨識成其他圖紙",
    "leak": "看不到的圖紙出現在照片候選或文字結果的任何名次",
    "text_not_ok": "文字查詢：看得到時第一名不是答案；看不到時答案出現在任何名次",
    "reported_not_ok": "回報的截圖：有權限時沒認出 mfg-006，沒有權限時認出了圖紙",
    "reported_missing": "gold 裡沒有回報的截圖（variant=reported）",
}
_DETAIL_KEYS = ("score", "inliers", "overlap", "coverage", "verified")
# 三視圖在圖紙版面（800×970，app/cad/drawing.py；未收錄零件的圖紙也是同一個版面）的位置，
# 前視圖、上視圖連右側與下方的尺寸標註；回報的截圖就是前視圖這樣切
VIEWS = {
    "view_right": (30, 80, 370, 420),
    "view_front": (380, 80, 740, 420),
    "view_top": (380, 430, 740, 770),
}


# 方形的小局部：每個視圖中央 180×180（視圖方框是 300×300，見 verify._DRAWING_VIEWS）
PATCHES = {
    "patch_right": (80, 160, 260, 340),
    "patch_front": (460, 160, 640, 340),
    "patch_top": (460, 510, 640, 690),
}


def view_crop(path: Path, box: tuple[int, int, int, int], factor: float = 1.3) -> Image.Image:
    """切出一塊（版面座標）、放大 factor 倍（預設 1.3，和回報的截圖差不多）。"""
    img = Image.open(path).convert("RGB")
    s = img.width / 800
    crop = img.crop(tuple(round(v * s) for v in box))
    size = (round(crop.width * factor), round(crop.height * factor))
    return crop.resize(size, Image.LANCZOS)


def uploaded(raw: bytes | Image.Image) -> Image.Image:
    """和 POST /images 一樣：EXIF 轉正、縮圖、重新編碼成 JPEG 存檔，辨識時再讀回來。"""
    from app.rag.preprocess import load_image, to_jpeg_bytes

    img = raw if isinstance(raw, Image.Image) else load_image(raw)
    return load_image(to_jpeg_bytes(img))


def photo_cases(gold: dict, part_ids: set[str]) -> list[dict]:
    """展開 gold set 的照片：每張 {name, variant, expect, image}。"""
    out = []
    for spec in gold["photos"]:
        if "path" in spec:
            p = ROOT / spec["path"]
            out.append(
                {
                    "name": p.name,
                    "variant": spec["variant"],
                    "expect": spec["expect"],
                    "image": uploaded(p.read_bytes()),
                }
            )
            continue
        if spec.get("generate") == "views":
            for p in sorted(x for g in spec["from"] for x in ROOT.glob(g)):
                for variant, box in VIEWS.items():
                    out.append(
                        {
                            "name": f"{p.stem}__{variant}",
                            "variant": variant,
                            "expect": p.stem if p.stem in part_ids else None,
                            "image": uploaded(view_crop(p, box)),
                        }
                    )
            continue
        if spec.get("generate") == "patches":
            for p in sorted(x for g in spec["from"] for x in ROOT.glob(g)):
                for variant, box in PATCHES.items():
                    for factor in spec["factors"]:
                        out.append(
                            {
                                "name": f"{p.stem}__{variant}_x{factor:g}",
                                "variant": variant,
                                "expect": p.stem if p.stem in part_ids else None,
                                "image": uploaded(view_crop(p, box, factor)),
                            }
                        )
            continue
        if "crop" in spec:
            p = ROOT / spec["crop"]
            box, factor = tuple(spec["box"]), spec["factor"]
            out.append(
                {
                    "name": f"{p.stem}__crop{list(box)}_x{factor:g}",
                    "variant": spec["variant"],
                    "expect": p.stem if p.stem in part_ids else None,
                    "image": uploaded(view_crop(p, box, factor)),
                }
            )
            continue
        for p in sorted(ROOT.glob(spec["glob"])):
            stem = p.stem.split("__")[0]
            variant = p.stem.split("__")[1] if "__" in p.stem else spec["variant"]
            out.append(
                {
                    "name": p.name,
                    "variant": variant,
                    "expect": stem if stem in part_ids else None,
                    "image": uploaded(p.read_bytes()),
                }
            )
    return out


# ---------------------------------------------------------------- 逐題資料（純函式）


def photo_row(
    c: dict, visible: set[str], domain: str, margin: float, res: dict | None, ms: int
) -> dict:
    """一張照片的逐題資料。res＝identify_drawing 的回傳；領域路由不是 mfg 時沒有跑辨識，傳 None。

    前 14 個欄位和 TASK-20261010-001 相同；之後是新增的：
    candidate_details（每個候選的 CLIP 分數、inliers、overlap、coverage、verified）、
    negative_kind（沒有看得到的正解時：unknown／hidden）、
    candidate_ranking（retrieval_metrics.score_photo_case：in_ranking、route_miss、
    empty_candidates、rank、metrics）、leaked_ids（候選中看不到的圖紙，整份列表）。
    """
    results = res["results"] if res else []
    got = res["best_part_id"] if res and res["matched"] else None
    cand = [x["part"]["id"] for x in results]
    top = results[0] if results else None
    expect = c["expect"] if c["expect"] in visible else None
    ranking = rm.score_photo_case(cand, expect, route_miss=domain != "mfg")
    return {
        "name": c["name"],
        "variant": c["variant"],
        "expect": expect,
        "expect_in_kb": c["expect"],
        "domain": domain,
        "margin": round(margin, 4),
        "got": got,
        "candidates": cand,
        "top_inliers": top["inliers"] if top else None,
        "top_overlap": top["overlap"] if top else None,
        "ok": got == expect,
        "wrong": got is not None and got != expect,
        "leak": bool(set(cand) - visible),
        "ms": ms,
        "candidate_details": [
            {"id": x["part"]["id"], **{k: x.get(k) for k in _DETAIL_KEYS}} for x in results
        ],
        "negative_kind": rm.photo_negative_kind(expect, c["expect"]),
        "candidate_ranking": {
            k: ranking[k]
            for k in ("in_ranking", "route_miss", "empty_candidates", "rank", "metrics")
        },
        "leaked_ids": [x for x in rm.dedupe_ids(cand) if x not in visible],
    }


def text_row(t: dict, visible: set[str], ids: list[str]) -> dict:
    """一句文字查詢的逐題資料；ids＝search_parts_text 回傳的圖紙 ID（照名次）。
    前 7 個欄位和 TASK-20261010-001 相同，leaked_ids 是新增的。"""
    expect = t["expect"] if t["expect"] in visible else None
    top = ids[0] if ids else None
    return {
        "q": t["q"],
        "by": t["by"],
        "expect": expect,
        "top": top,
        "results": ids,
        # 看得到：第一名要是答案；看不到：答案不能出現在任何名次（和不存在一樣）
        "ok": top == expect if expect else t["expect"] not in ids,
        "leak": bool(set(ids) - visible),
        "leaked_ids": [x for x in rm.dedupe_ids(ids) if x not in visible],
    }


def run_photos(cases: list[dict], visible: set[str]) -> list[dict]:
    from app.rag.embedders import embed_image
    from app.rag.router import route
    from app.services import search_service

    rows = []
    for c in cases:
        t0 = time.perf_counter()
        img = c["image"]
        vec = embed_image(img)
        r = route(vec)
        res = None
        if r.domain == "mfg":
            res = search_service.identify_drawing("eval", img=img, vec=vec, part_ids=visible)
        ms = round((time.perf_counter() - t0) * 1000)
        rows.append(photo_row(c, visible, r.domain, r.margin, res, ms))
    return rows


def run_texts(gold: dict, visible: set[str], scope_text: str) -> list[dict]:
    from app.services import search_service

    rows = []
    for t in gold["texts"]:
        res = search_service.search_parts_text(t["q"], None, visible, scope_text)
        rows.append(text_row(t, visible, [x["part"]["id"] for x in res["results"]]))
    return rows


# ---------------------------------------------------------------- 摘要與停止條件（純函式）


def _negative_summary(rows: list[dict]) -> dict:
    """沒有看得到正解的照片：拒答（沒認出）、誤接受（認出任何圖紙）、外洩，各附分母。"""
    n = len(rows)
    return {
        "n": n,
        "rejected": rm.rate(sum(r["got"] is None for r in rows), n),
        "false_accept": rm.rate(sum(r["got"] is not None for r in rows), n),
        "leak_cases": rm.rate(sum(r["leak"] for r in rows), n),
    }


def account_summary(photos: list[dict], texts: list[dict]) -> dict:
    """一個身分的摘要。前 6 個欄位和 TASK-20261010-001 相同（photo_rejected 仍把未收錄與
    看不到合在一起）；之後是新增的：candidate_ranking（retrieval_metrics.aggregate_photo_cases
    ＋source）、candidate_ranking_by_variant、matched_top1（photo_top1 的數字版）、
    photo_negatives（unknown／hidden 拆開）、leak_detail（外洩的段數／題數與 ID）。"""
    known = [r for r in photos if r["expect"]]
    unknown = [r for r in photos if not r["expect"]]
    by_variant: dict[str, list[int]] = {}
    for r in known:
        v = by_variant.setdefault(r["variant"], [0, 0])
        v[0] += r["ok"]
        v[1] += 1
    return {
        "photo_top1": f"{sum(r['ok'] for r in known)}/{len(known)}",
        "photo_by_variant": {k: f"{a}/{b}" for k, (a, b) in sorted(by_variant.items())},
        "photo_rejected": f"{sum(r['ok'] for r in unknown)}/{len(unknown)}",
        "photo_wrong": sum(r["wrong"] for r in photos),
        "text_ok": f"{sum(r['ok'] for r in texts)}/{len(texts)}",
        "leaks": sum(r["leak"] for r in photos + texts),
        "candidate_ranking": {
            "source": CANDIDATE_SOURCE,
            **rm.aggregate_photo_cases([r["candidate_ranking"] for r in photos]),
        },
        "candidate_ranking_by_variant": {
            v: rm.aggregate_photo_cases(
                [r["candidate_ranking"] for r in photos if r["variant"] == v]
            )
            for v in sorted(by_variant)
        },
        "matched_top1": rm.rate(sum(r["ok"] for r in known), len(known)),
        "photo_negatives": {
            kind: _negative_summary([r for r in photos if r["negative_kind"] == kind])
            for kind in ("unknown", "hidden")
        },
        "leak_detail": {
            "photos": rm.summarize_leaks(
                [{"case_id": r["name"], "leaked_ids": r["leaked_ids"]} for r in photos]
            ),
            "texts": rm.summarize_leaks(
                [{"case_id": r["q"], "leaked_ids": r["leaked_ids"]} for r in texts]
            ),
        },
    }


def stop_reasons(summary: dict, photos: list[dict], texts: list[dict]) -> list[str]:
    """成立的停止條件（STOP_RULES 的鍵）；空清單＝通過。候選排名的高低不影響停止。"""
    reasons = []
    if summary["photo_wrong"]:
        reasons.append("photo_wrong")
    if summary["leaks"]:
        reasons.append("leak")
    if not all(r["ok"] for r in texts):
        reasons.append("text_not_ok")
    reported = next((r for r in photos if r["variant"] == "reported"), None)
    if reported is None:
        reasons.append("reported_missing")
    elif not reported["ok"]:
        reasons.append("reported_not_ok")
    return reasons


def account_result(photos: list[dict], texts: list[dict]) -> dict:
    """{"summary", "photos", "texts", "stop_reasons"}；前三個是原本 JSON 每個身分的欄位。"""
    summary = account_summary(photos, texts)
    return {
        "summary": summary,
        "photos": photos,
        "texts": texts,
        "stop_reasons": stop_reasons(summary, photos, texts),
    }


def finalize(report: dict) -> int:
    """寫入 report["stop"]，回傳結束碼（任一身分停止條件成立＝1）。"""
    failed = [a for a, r in report["accounts"].items() if r["stop_reasons"]]
    code = 1 if failed else 0
    report["stop"] = {"exit_code": code, "failed_accounts": failed, "rules": STOP_RULES}
    return code


def write_report(report: dict, out_dir: Path) -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{report['run_id']}-drawing-retrieval.json"
    with out.open("x", encoding="utf-8") as f:  # 排他建立：不覆寫既有結果，FileExistsError 往上丟
        f.write(json.dumps(report, ensure_ascii=False, indent=1))
    return out


# ---------------------------------------------------------------- 索引收據


def find_receipt(value: str, runs_dir: Path = rr.RUNS_DIR) -> Path | None:
    """--index-receipt：latest＝runs_dir 裡最新的 *-retrieval-index.json（檔名開頭是 UTC 時間），
    沒有就 None；其他值當路徑。"""
    if value != "latest":
        return Path(value)
    found = sorted(Path(runs_dir).glob("*-retrieval-index.json"))
    return found[-1] if found else None


def check_index(current: dict, receipt_arg: str, *, real: bool) -> tuple[dict, list[str]]:
    """目前載入的索引（current＝collect_index_receipt）和建索引收據要一致；real 時兩者都要是真模型。

    回傳 (build_receipt 摘要, 問題清單)；有問題就不能產出成績。build_receipt 摘要：
    {"selection": "latest"|"explicit", "path", "sha256", "run_id", "created_at", "real",
     "index_build"}（找不到收據時後五個是 None）
    """
    path = find_receipt(receipt_arg)
    info = {
        "selection": "latest" if receipt_arg == "latest" else "explicit",
        "path": None,
        "sha256": None,
        "run_id": None,
        "created_at": None,
        "real": None,
        "index_build": None,
    }
    problems = rr.verify_index_receipt(current, require_real=real)
    if path is None or not path.is_file():
        hint = "先跑 make eval-retrieval-index，或用 --index-receipt 指定"
        return info, [*problems, f"找不到建索引收據（--index-receipt {receipt_arg}）：{hint}"]
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return info, [*problems, f"建索引收據讀不了：{path}：{e}"]
    info |= {
        "path": rr.repo_relative(path),
        "sha256": rr.sha256_file(path),
        "run_id": receipt.get("run_id"),
        "created_at": receipt.get("created_at"),
        "real": receipt.get("real"),
        "index_build": receipt.get("index_build"),
    }
    problems += rr.verify_index_receipt(
        receipt, require_real=real, require_build=True, current=current
    )
    return info, list(dict.fromkeys(problems))


def _fail(msg: str) -> int:
    print(f"沒有產出成績：{msg}", file=sys.stderr)
    return 2


def _ranking_line(ranking: dict) -> str:
    """候選排名的一行進度輸出：R@K 命中／分母、MRR、route miss 張數。"""
    m = ranking["metrics"]
    hits = "  ".join(
        f"R@{k} {m[f'recall@{k}']['numerator']:g}/{m[f'recall@{k}']['denominator']}"
        for k in rm.PHOTO_KS
    )
    mrr = "—" if m["mrr"]["mean"] is None else f"{m['mrr']['mean']:.3f}"
    return f"{hits}  MRR {mrr}（route miss {ranking['n_route_miss']}）"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--accounts", default=None, help="逗號分隔，預設照 gold.json")
    ap.add_argument(
        "--gold", default=str(GOLD), help="gold set（預設 eval/drawing_retrieval/gold.json）"
    )
    ap.add_argument(
        "--index-receipt",
        default="latest",
        help="建索引收據：latest＝eval/runs 最新的 *-retrieval-index.json（預設），或指定路徑",
    )
    ap.add_argument(
        "--out-dir", default=str(rr.RUNS_DIR), help="輸出目錄（預設 eval/runs；mock 只能寫別處）"
    )
    ap.add_argument("--run-id", default=None)
    args = ap.parse_args(argv)

    try:
        isolation = rr.bootstrap(strict=True)
    except rr.RuntimeIsolationError as e:
        return _fail(str(e))
    from app.agent import guard
    from app.core.config import get_models_config, get_settings, kb_version
    from app.repositories.index_store import IndexMismatch, get_store
    from app.services import identity

    settings = get_settings()
    real = settings.embed_mode == "real"
    out_dir = Path(args.out_dir)
    if not real and rr.is_official_runs_dir(out_dir):
        return _fail(
            f"EMBED_MODE={settings.embed_mode}：mock 成績不能寫進 eval/runs，"
            "請用 --out-dir 指到暫存目錄"
        )
    run_id = args.run_id or rr.new_run_id()
    existing = out_dir / f"{run_id}-drawing-retrieval.json"
    if existing.exists():  # 先擋同 out-dir／同 ID，不開始評估
        return _fail(f"{rr.repo_relative(existing)} 已存在：不覆寫既有結果，請換 --run-id")
    gold_path = Path(args.gold)
    gold = json.loads(gold_path.read_text(encoding="utf-8"))
    store = get_store()
    try:
        store.load()
    except IndexMismatch as e:
        return _fail(f"索引不能用：{e}")
    current = rr.collect_index_receipt(store, isolation=isolation, qa_paths=[gold_path])
    build_receipt, problems = check_index(current, args.index_receipt, real=real)
    if problems:
        return _fail("索引收據檢查失敗\n- " + "\n- ".join(problems))

    part_ids = {p["id"] for p in store.parts}
    cases = photo_cases(gold, part_ids)
    accounts = args.accounts.split(",") if args.accounts else gold["accounts"]
    report = {
        "run_id": run_id,
        "kb_version": kb_version(),
        "accounts": {},
        # 以下是 TASK-20261027-001 新增的 provenance
        "schema_version": SCHEMA_VERSION,
        "created_at": rr.utc_now(),
        "embed_mode": settings.embed_mode,
        "real": real and current["real"],
        "code": current["code"],
        "isolation": current["isolation"],
        "settings": current["settings"],
        "index_receipt": {"build_receipt": build_receipt, "current": current, "problems": problems},
        "gold": {
            "path": rr.repo_relative(gold_path),
            "sha256": rr.sha256_file(gold_path),
            "photo_count": len(cases),
            "text_count": len(gold["texts"]),
        },
        "identify_params": dict(get_models_config().drawing_retrieval),
        "candidate_ranking_definition": CANDIDATE_RANKING,
    }
    for acc_id in accounts:
        acc = identity.get_account(acc_id)
        visible = identity.visible_part_ids(acc, store.parts)
        scope = guard.MetaFilter.of(acc, "mfg")
        print(f"\n== {acc.label}（{acc_id}）看得到 {sorted(visible)}")
        photos = run_photos(cases, visible)
        texts = run_texts(gold, visible, scope.text)
        for r in photos:
            mark = "OK " if r["ok"] else ("錯！" if r["wrong"] else "沒認出")
            print(
                f"  [{mark}] {r['name']:42} 答案 {r['expect']!s:8} 得到 {r['got']!s:8} "
                f"inliers {r['top_inliers']!s:5} overlap {r['top_overlap']!s:5}"
            )
        for r in texts:
            print(
                f"  [{'OK ' if r['ok'] else '錯！'}] 文字 {r['by']} {r['q']!r:34} "
                f"答案 {r['expect']!s:8} 第一名 {r['top']!s:8}"
            )
        result = account_result(photos, texts)
        summary = result["summary"]
        legacy = {k: summary[k] for k in list(summary)[:6]}
        print(f"  摘要：{json.dumps(legacy, ensure_ascii=False)}")
        ranking = _ranking_line(summary["candidate_ranking"])
        print(f"  候選排名（幾何驗證後混合排名，不是 CLIP 粗篩）：{ranking}")
        if result["stop_reasons"]:
            print(f"  停止條件成立：{', '.join(result['stop_reasons'])}")
        report["accounts"][acc_id] = result | {
            "label": acc.label,
            "visible_part_ids": sorted(visible),
            "scope_text": scope.text,
        }
    code = finalize(report)
    try:
        out = write_report(report, out_dir)
    except FileExistsError:
        return _fail(f"{run_id}-drawing-retrieval.json 已存在（評估期間被建立）：不覆寫既有結果")
    print(f"\n結果：{rr.repo_relative(out)}（{'real' if report['real'] else 'mock'}）")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
