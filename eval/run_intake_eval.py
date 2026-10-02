"""照片建檔評估（docs/adr/013）：在程序內直接執行，不用開後端、不用索引，但要有本地生成端（Ollama）；
結果存 eval/runs/<run_id>-intake.json。

0. 畫作的模糊門檻（不用模型，畫作的資料由人填）：eval/photos 的模擬照與知識庫原圖，
   模糊程度超過 intake.max_blur 就擋下——blur 應該全擋、其他拍法與原圖都不該擋。

圖紙照片：知識庫圖紙原圖（kb/drawings、kb_staging/drawings 共 7 張）＋
eval/drawing_photos/known 的模擬照（同 7 張 × tilt／glare／crop／dim／blur）。
每張照片走和 POST /intake 同一套：
1. 擋下：模糊程度超過 intake.max_blur、找不到紙張四角或標題欄外框 → 請重拍，不送模型
2. 讀標題欄：本地 Qwen3-VL＋JSON schema（shared/prompts/intake_v1.md）
3. 規則：正規化、材料依牌號對照知識庫既有零件校正並補密度、格式／列舉值／範圍驗證
   （不檢查「和知識庫重複」：這些圖紙本來就在知識庫裡）

每個欄位和真值比（kb/parts 的 JSON；外形尺寸執行標準模型算）：
- 正確／空白（null，人會補）／錯了被規則擋下（標紅）／**錯了卻通過驗證**（只能靠人確認抓到，最危險）
- 模型原始輸出與套規則之後各算一次

用法：python eval/run_intake_eval.py [--read-rejected]
  --read-rejected：被擋下的照片也送模型讀一次，量「如果不擋」會怎樣（每張約多 30 秒）
"""

import argparse
import asyncio
import base64
import functools
import json
import statistics
import sys
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from PIL import Image  # noqa: E402

from app.analysis import page  # noqa: E402
from app.cad.sandbox import run_cad  # noqa: E402
from app.core.config import get_models_config  # noqa: E402
from app.rag.kb import validate_parts  # noqa: E402
from app.rag.preprocess import load_image, to_jpeg_bytes  # noqa: E402
from app.rag.providers import ProviderUnavailable, get_provider  # noqa: E402
from app.services import intake_service as svc  # noqa: E402

print = functools.partial(print, flush=True)  # noqa: A001 — 背景執行時也能即時看到進度
PHOTOS = ROOT / "eval" / "drawing_photos" / "known"
DIMS = ("width", "depth", "height")


def load_truth() -> dict[str, dict]:
    parts = {}
    for base in ("kb", "kb_staging"):
        for p in sorted((ROOT / base / "parts").glob("*.json")):
            d = json.loads(p.read_text(encoding="utf-8"))
            parts[d["id"]] = {**d, "_base": base}
    out = {}
    for pid, d in parts.items():
        code = (ROOT / d["_base"] / d["cad"].removeprefix("kb/")).read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            r = asyncio.run(run_cad(code, Path(tmp), trusted=True))
        if not r.ok:
            raise SystemExit(f"{pid} 標準模型執行失敗：{r.error}")
        dims = r.data["dims"]
        out[pid] = {
            "company": d["company"],
            "name": d["name"]["zh"],
            "part_no": d["part_no"],
            "drawing_no": d["drawing_no"],
            "revision": d["revision"],
            "material": d["material"],
            "confidentiality": d["confidentiality"],
            **{k: round(dims[k], 3) for k in DIMS},
            "density_g_cm3": d["density_g_cm3"],
            "_drawing": ROOT / d["_base"] / d["drawing"].removeprefix("kb/"),
        }
    return out


def same(key: str, got, want) -> bool:
    if got is None:
        return False
    if key in DIMS or key == "density_g_cm3":
        return abs(float(got) - float(want)) <= max(0.01, 0.001 * abs(float(want)))
    return svc._normalize(key, got, "text") == svc._normalize(key, want, "text")


async def read(img: Image.Image, spec) -> tuple[dict, dict]:
    messages, schema = svc._prompt(spec)
    url = "data:image/jpeg;base64," + base64.b64encode(to_jpeg_bytes(img, 90)).decode()
    messages[-1]["content"] = [
        {"type": "image_url", "image_url": {"url": url}},
        {"type": "text", "text": messages[-1]["content"]},
    ]
    cfg = get_models_config().intake
    for strategy in ("hybrid", "hybrid_fallback"):
        try:
            provider = get_provider(strategy)
            provider.max_tokens, provider.temperature = cfg.max_tokens, 0.0
            provider.response_format = {
                "type": "json_schema",
                "json_schema": {"name": "intake", "schema": schema, "strict": True},
            }
            t0 = time.perf_counter()
            answer = "".join([p async for p in provider.stream(messages)])
            info = {
                "model": provider.model,
                "ms": round((time.perf_counter() - t0) * 1000),
                "tokens": [provider.usage.input_tokens, provider.usage.output_tokens],
            }
            return svc._parse(answer), info
        except ProviderUnavailable as e:
            print(f"    {strategy} 無法使用：{e}")
    raise SystemExit("本地生成端都無法使用：請先啟動 Ollama（見 README）")


def grade(values: dict, sources: dict, truth: dict, spec) -> dict:
    out = {}
    for key, fs in spec.fields.items():
        if key not in truth:
            continue  # 照片上沒有、由人填的欄位（類別、負責單位…）
        v = values.get(key)
        status, _ = svc._check("mfg", key, values, fs, [])
        if same(key, v, truth[key]):
            verdict = "ok"
        elif v is None:
            verdict = "null"
        elif status in ("invalid", "missing"):
            verdict = "caught"
        else:
            verdict = "silent"
        out[key] = {"truth": truth[key], "got": v, "source": sources.get(key), "verdict": verdict}
    return out


def art_gate(max_blur: float) -> dict:
    """畫作的模糊門檻：eval/photos 的模擬照（known、unknown）與知識庫原圖，各拍法擋下幾張。"""
    paths = sorted((ROOT / "eval" / "photos").glob("*/*.jpg"))
    paths += sorted((ROOT / "kb" / "images").glob("*.jpg"))
    paths += sorted((ROOT / "kb_staging" / "images").glob("*.jpg"))
    out: dict[str, dict] = {}
    for p in paths:
        variant = p.stem.split("__")[1] if "__" in p.stem else "original"
        score = page.blur_score(load_image(p.read_bytes()))
        v = out.setdefault(variant, {"photos": 0, "rejected": 0, "max_blur": 0.0, "min_blur": 1.0})
        v["photos"] += 1
        v["rejected"] += score > max_blur
        v["max_blur"] = round(max(v["max_blur"], score), 3)
        v["min_blur"] = round(min(v["min_blur"], score), 3)
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--read-rejected", action="store_true", help="被擋下的照片也送模型讀")
    args = parser.parse_args()
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:4]
    cfg, t_start = get_models_config().intake, time.time()
    spec = cfg.mfg
    art = art_gate(cfg.max_blur)
    print(f"畫作的模糊門檻（模糊程度 > {cfg.max_blur:.2f} 擋下）：")
    for v, g in sorted(art.items()):
        print(
            f"  {v:8s} {g['photos']} 張、擋下 {g['rejected']}"
            f"（模糊程度 {g['min_blur']:.2f}–{g['max_blur']:.2f}）"
        )
    # 規則用的「知識庫既有零件」直接讀 kb/parts（不用索引）
    kb_parts = validate_parts(check_drawing=False)[0]
    svc._kb_parts = lambda: kb_parts
    print("執行標準模型，算外形尺寸的真值…")
    truth = load_truth()

    photos = [(pid, "original", t["_drawing"]) for pid, t in truth.items()]
    photos += [
        (p.stem.split("__")[0], p.stem.split("__")[1], p)
        for p in sorted(PHOTOS.glob("*.jpg"))
        if p.stem.split("__")[0] in truth
    ]
    rows = []
    for pid, variant, path in photos:
        img = load_image(path.read_bytes())
        blur = page.blur_score(img)
        reason = None
        if blur > cfg.max_blur:
            reason = "blurry"
        else:
            quad = page.find_page(img)
            size = tuple(cfg.page_size)
            target = size[1] / size[0]
            if quad is None or abs(page.aspect(quad) - target) / target > cfg.page_aspect_tol:
                reason = "page"
            else:
                rect = page.clean_drawing(page.rectify(img, quad, size))
                box = page.title_block_box(rect)
                if box is None or page.fit_layout(rect, box) is None:
                    reason = "layout"
        row = {"part_id": pid, "variant": variant, "blur": round(blur, 3), "rejected": reason}
        if reason is None or args.read_rejected:
            data, info = asyncio.run(read(img, spec))
            values = dict.fromkeys(spec.fields)
            sources = dict.fromkeys(spec.fields)
            for key, fs in spec.fields.items():
                if fs.read and (v := svc._normalize(key, data.get(key), fs.kind)) is not None:
                    values[key], sources[key] = v, svc.MODEL
            raw = grade(values, sources, truth[pid], spec)
            d = {"domain": "mfg", "values": values, "sources": sources, "notes": {}}
            svc._apply_rules(d)
            final = grade(d["values"], d["sources"], truth[pid], spec)
            row.update(read=info, raw=raw, final=final)
            n_ok = sum(f["verdict"] == "ok" for f in final.values())
            silent = [k for k, f in final.items() if f["verdict"] == "silent"]
            print(
                f"  {pid} {variant:8s} {'擋下(' + reason + ')' if reason else '      '} "
                f"{n_ok}/{len(final)} 正確，{info['ms'] / 1000:.0f} 秒"
                + (f"，通過驗證但錯：{'、'.join(silent)}" if silent else "")
            )
        else:
            print(f"  {pid} {variant:8s} 擋下（{reason}，模糊程度 {blur:.2f}）")
        rows.append(row)

    def tally(which: str, accepted_only: bool = True) -> dict:
        cells = [
            f
            for r in rows
            if which in r and (r["rejected"] is None or not accepted_only)
            for f in r[which].values()
        ]
        c = {v: sum(f["verdict"] == v for f in cells) for v in ("ok", "null", "caught", "silent")}
        return {**c, "fields": len(cells)}

    by_variant = {}
    for v in sorted({r["variant"] for r in rows}):
        rs = [r for r in rows if r["variant"] == v]
        by_variant[v] = {
            "photos": len(rs),
            "rejected": sum(r["rejected"] is not None for r in rs),
            "final": {
                k: sum(f["verdict"] == k for r in rs if "final" in r for f in r["final"].values())
                for k in ("ok", "null", "caught", "silent")
            },
        }
    per_field = {}
    for key in spec.fields:
        cells = [
            r["final"][key]
            for r in rows
            if "final" in r and r["rejected"] is None and key in r["final"]
        ]
        if cells:
            per_field[key] = {
                v: sum(f["verdict"] == v for f in cells) for v in ("ok", "null", "caught", "silent")
            }
    reads = [r["read"]["ms"] for r in rows if "read" in r]
    out = {
        "run_id": run_id,
        "created_at": datetime.now(UTC).isoformat(),
        "kind": "intake",
        "params": cfg.model_dump(mode="json"),
        "model": next((r["read"]["model"] for r in rows if "read" in r), None),
        "summary": {
            "photos": len(rows),
            "rejected": sum(r["rejected"] is not None for r in rows),
            "raw": tally("raw"),
            "final": tally("final"),
            "rejected_if_read": tally("final", accepted_only=False) if args.read_rejected else None,
            "read_ms_median": statistics.median(reads) if reads else None,
            "elapsed_s": round(time.time() - t_start),
        },
        "by_variant": by_variant,
        "per_field": per_field,
        "art_gate": art,
        "rows": rows,
    }
    path = ROOT / "eval" / "runs" / f"{run_id}-intake.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    s = out["summary"]
    print(f"\n{s['photos']} 張照片，擋下 {s['rejected']} 張（請重拍）；沒擋下的照片每個欄位：")
    for which, label in (("raw", "模型原始輸出"), ("final", "套規則之後")):
        t = s[which]
        print(
            f"  {label}：正確 {t['ok']}/{t['fields']}、空白 {t['null']}、"
            f"錯了被擋下 {t['caught']}、錯了卻通過驗證 {t['silent']}"
        )
    print("\n依拍法（套規則之後）：")
    for v, b in by_variant.items():
        f = b["final"]
        print(
            f"  {v:8s} {b['photos']} 張、擋下 {b['rejected']}：正確 {f['ok']}、空白 {f['null']}、"
            f"擋下 {f['caught']}、通過但錯 {f['silent']}"
        )
    if s["read_ms_median"]:
        print(
            f"\n讀一張中位數 {s['read_ms_median'] / 1000:.1f} 秒（{out['model']}）；"
            f"共 {s['elapsed_s']} 秒 → {path}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
