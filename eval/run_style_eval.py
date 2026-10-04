"""畫作卡推測評估（docs/adr/018）：在程序內直接執行，不用開後端、不用索引（要有 Chinese-CLIP）；
結果存 eval/runs/<run_id>-style.json。正解在 eval/style_truth.json。

1. 畫作（知識庫內外 10 幅原圖＋eval/photos/known 25 張模擬照）：
   - 風格大類第一名對不對（畫面上顯示的是大類）；
   - 細分流派第一名、前三名有沒有正解（只當參考）；
   - 題材、媒材第一名、前三名；
   - 第一名對了卻標「看不太出來」、錯了卻沒標（標示有沒有用）。
2. 「像畫作」把關：畫作全部要過；圖紙照片（eval/drawing_photos）全部要擋下。
3. 延遲：一張照片的推測時間（照片向量另計，和以圖搜圖共用；不含第一次算標籤向量）。

和後端 API 同一套：照片先經過 load_image（上傳時的前處理），標籤與門檻讀 shared/models.yaml。
用法：python eval/run_style_eval.py
"""

import functools
import json
import statistics
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.analysis import style  # noqa: E402
from app.core.config import get_models_config  # noqa: E402
from app.rag.embedders import embed_image  # noqa: E402
from app.rag.preprocess import load_image  # noqa: E402

print = functools.partial(print, flush=True)  # noqa: A001 — 背景執行時也能即時看到進度
TRUTH = json.loads((ROOT / "eval" / "style_truth.json").read_text(encoding="utf-8"))


def painting_cases() -> list[tuple[str, Path, str]]:
    """(案例名稱, 圖檔, 正解 key)：原圖與模擬照。"""
    cases = []
    for key in TRUTH:
        if key.startswith("_"):
            continue
        if key.startswith("unknown-"):
            cases.append((key, ROOT / "eval" / "photos" / "unknown" / f"{key}.jpg", key))
            continue
        for d in ("kb", "kb_staging"):
            p = ROOT / d / "images" / f"{key}.jpg"
            if p.exists():
                cases.append((key, p, key))
    for p in sorted((ROOT / "eval" / "photos" / "known").glob("*.jpg")):
        cases.append((p.stem, p, p.stem.split("__")[0]))
    return cases


def judge(result: dict, truth: dict) -> dict:
    fields = {f["key"]: f for f in result["fields"]}
    out: dict = {"painting_score": result["painting_score"], "is_painting": result["is_painting"]}
    if not result["is_painting"]:
        return out
    s = fields["style"]
    out["style_group"] = {
        "pred": s["name"],
        "prob": s["prob"],
        "uncertain": s["uncertain"],
        "ok": s["name"] in truth["style_group"],
    }
    fine = [c["name"] for c in s["candidates"]]
    out["style"] = {
        "pred": fine,
        "ok1": fine[0] in truth["style"],
        "ok3": any(n in truth["style"] for n in fine),
    }
    for k in ("genre", "media"):
        f = fields[k]
        names = [c["name"] for c in f["candidates"]]
        out[k] = {
            "pred": names,
            "prob": f["prob"],
            "uncertain": f["uncertain"],
            "ok": names[0] in truth[k],
            "ok3": any(n in truth[k] for n in names),
        }
    return out


def rate(rows: list[dict], key: str, sub: str) -> str:
    vals = [r[key][sub] for r in rows if key in r]
    return f"{sum(vals)}/{len(vals)}"


def flags(rows: list[dict], key: str, ok: str = "ok") -> dict:
    """第一名對了卻標「看不太出來」（多餘的保留）、錯了卻沒標（該保留卻沒保留）。"""
    rs = [r[key] for r in rows if key in r]
    return {
        "uncertain": sum(r["uncertain"] for r in rs),
        "right_but_uncertain": sum(r[ok] and r["uncertain"] for r in rs),
        "wrong_not_flagged": sum(not r[ok] and not r["uncertain"] for r in rs),
    }


def main() -> int:
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:4]
    spec = get_models_config().style_guess
    style.guess(
        embed_image(load_image((ROOT / "kb" / "images" / "npm-000001.jpg").read_bytes()))
    )  # 先算標籤向量

    rows, lat = [], []
    for name, path, key in painting_cases():
        vec = embed_image(load_image(path.read_bytes()))
        t0 = time.perf_counter()
        r = style.guess(vec)
        lat.append((time.perf_counter() - t0) * 1000)
        j = judge(r, TRUTH[key]) | {"case": name, "truth": key, "photo": "__" in name}
        rows.append(j)
        if not j["is_painting"]:
            print(f"{name:<22} ✗ 被當成不是畫作（{j['painting_score']:.2f}）")
            continue
        mark = lambda ok: "O" if ok else "X"  # noqa: E731
        g, s, ge, me = j["style_group"], j["style"], j["genre"], j["media"]
        q = lambda f: "?" if f["uncertain"] else ""  # noqa: E731 — 標了「看不太出來」
        print(
            f"{name:<22} 大類 {mark(g['ok'])} {g['pred']}({g['prob']:.2f}{q(g)})"
            f" | 細分 {mark(s['ok1'])} {'、'.join(s['pred'])}"
            f" | 題材 {mark(ge['ok'])} {ge['pred'][0]}({ge['prob']:.2f}{q(ge)})"
            f" | 媒材 {mark(me['ok'])} {me['pred'][0]}({me['prob']:.2f}{q(me)})"
        )

    drawings = sorted((ROOT / "eval" / "drawing_photos").rglob("*.*"))
    drawings = [p for p in drawings if p.suffix.lower() in (".jpg", ".png")]
    gate_scores = {}
    for p in drawings:
        r = style.guess(embed_image(load_image(p.read_bytes())))
        gate_scores[str(p.relative_to(ROOT))] = r["painting_score"]

    def block(rs: list[dict]) -> dict:
        return {
            "n": len(rs),
            "passed_gate": sum(r["is_painting"] for r in rs),
            "painting_score_min": round(min(r["painting_score"] for r in rs), 4),
            "style_group": rate(rs, "style_group", "ok"),
            "style_top1": rate(rs, "style", "ok1"),
            "style_top3": rate(rs, "style", "ok3"),
            "genre_top1": rate(rs, "genre", "ok"),
            "genre_top3": rate(rs, "genre", "ok3"),
            "media_top1": rate(rs, "media", "ok"),
            "media_top3": rate(rs, "media", "ok3"),
            "flags": {k: flags(rs, k) for k in ("style_group", "genre", "media")},
        }

    originals = [r for r in rows if not r["photo"]]
    photos = [r for r in rows if r["photo"]]
    summary = {
        "originals": block(originals),
        "known_photos": block(photos),
        "unknown_only": block([r for r in originals if r["truth"].startswith("unknown-")]),
        "gate_non_paintings": {
            "n": len(gate_scores),
            "blocked": sum(v < spec.painting_min for v in gate_scores.values()),
            "painting_score_max": round(max(gate_scores.values()), 4) if gate_scores else None,
        },
        "latency_ms_p50": round(statistics.median(lat), 1),
        "latency_ms_p95": round(sorted(lat)[int(len(lat) * 0.95) - 1], 1),
    }
    out = {
        "run_id": run_id,
        "created_at": datetime.now(UTC).isoformat(),
        "config": spec.model_dump(mode="json"),
        "summary": summary,
        "rows": rows,
        "non_painting_scores": gate_scores,
    }
    path = ROOT / "eval" / "runs" / f"{run_id}-style.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    print()
    for label, k in (
        ("原圖（知識庫內外 10 幅）", "originals"),
        ("其中知識庫外 5 幅", "unknown_only"),
        ("模擬照 25 張", "known_photos"),
    ):
        b = summary[k]
        print(
            f"{label}：把關通過 {b['passed_gate']}/{b['n']}；風格大類 {b['style_group']}；"
            f"細分 top1 {b['style_top1']}、top3 {b['style_top3']}；"
            f"題材 {b['genre_top1']}（top3 {b['genre_top3']}）；"
            f"媒材 {b['media_top1']}（top3 {b['media_top3']}）"
        )
        for f, m in b["flags"].items():
            print(
                f"    {f}：標看不太出來 {m['uncertain']}、"
                f"對了卻標 {m['right_but_uncertain']}、錯了沒標 {m['wrong_not_flagged']}"
            )
    g = summary["gate_non_paintings"]
    print(f"圖紙照片擋下 {g['blocked']}/{g['n']}（像畫作的程度最高 {g['painting_score_max']}）")
    print(f"延遲 P50 {summary['latency_ms_p50']} ms、P95 {summary['latency_ms_p95']} ms")
    print(f"→ {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
